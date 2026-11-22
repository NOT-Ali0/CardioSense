"""
routes_patient.py - Patient consent, profile, ECG inference, case escalation,
status polling, and diagnostic history endpoints.
"""
import json
import math
import threading
import uuid

from flask import Blueprint, request, jsonify, g, current_app
from sqlalchemy import select

from db import db
from models import (
    Case,
    PatientProfile,
    PatientHistory,
    PatientDecision,
    ConsentLog,
    EcgRecording,
    encode_float32_le,
    decimate_minmax,
    utcnow,
    iso_utc,
)
from preprocessing import MAX_LEN
from auth import roles_required, limiter, log_audit

SAMPLE_RATE_HZ = 300
EXPECTED_SIGNAL_LEN = MAX_LEN

patient_bp = Blueprint("patient_bp", __name__)

PATIENT_EDITABLE_FIELDS = {"phone", "emergency_contact_name", "emergency_contact_phone"}
FORBIDDEN_PATIENT_FIELDS = {
    "age",
    "date_of_birth",
    "gender",
    "chronic_conditions",
    "clinical_notes",
    "doctor_id",
    "role",
    "username",
}


def _set_patient_decision(patient_id: int, status_text: str, doctor_note: str = None) -> None:
    record = db.session.get(PatientDecision, patient_id)
    if record:
        record.status_text = status_text
        record.doctor_note = doctor_note
        record.updated_at = utcnow()
    else:
        db.session.add(
            PatientDecision(
                patient_id=patient_id,
                status_text=status_text,
                doctor_note=doctor_note,
                updated_at=utcnow(),
            )
        )


def _log_history_entry(
    case_id: str,
    patient_id: int,
    age: int,
    gender: str,
    status: str,
    prediction: str,
    bpm: int,
    rate_status: str,
    confidence: str = None,
    message: str = None,
    ai_attention_note: str = None,
    created_at=None,
) -> PatientHistory:
    entry = PatientHistory(
        case_id=case_id,
        patient_id=patient_id,
        created_at=created_at or utcnow(),
        age=str(age),
        gender=str(gender),
        status=status,
        prediction=prediction,
        bpm=bpm,
        rate_status=rate_status,
        confidence=confidence,
        message=message,
        ai_attention_note=ai_attention_note,
    )
    db.session.add(entry)
    return entry


def _validate_signal_payload(signal):
    """Validate ECG signal payload: list of EXPECTED_SIGNAL_LEN finite numbers."""
    if not isinstance(signal, list):
        return None, f"Signal must be a JSON array of {EXPECTED_SIGNAL_LEN} finite numbers."
    if len(signal) != EXPECTED_SIGNAL_LEN:
        return None, f"Signal must contain exactly {EXPECTED_SIGNAL_LEN} samples (got {len(signal)})."

    signal_floats = []
    for x in signal:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None, "Signal must contain only finite numeric values."
        val = float(x)
        if not math.isfinite(val):
            return None, "Signal must not contain NaN or infinite values."
        signal_floats.append(val)
    return signal_floats, None


def compute_case_priority(raw_prediction: str, bpm: int, confidence: float) -> str:
    if raw_prediction == "AFIB" or bpm < 50 or bpm > 120 or confidence < 0.70:
        return "URGENT"
    if bpm < 60 or bpm > 100 or confidence < 0.85:
        return "HIGH"
    return "NORMAL"


@patient_bp.route("/api/consent", methods=["GET"])
@roles_required("patient")
def get_consent():
    rec = db.session.get(ConsentLog, g.current_user.id)
    if not rec:
        return jsonify({"consent": False, "policy_version": "1.0", "accepted_at": None}), 200
    return jsonify(
        {
            "consent": bool(rec.consent),
            "policy_version": rec.policy_version,
            "accepted_at": iso_utc(rec.accepted_at),
        }
    ), 200


@patient_bp.route("/api/consent", methods=["POST"])
@roles_required("patient")
def record_consent():
    data = request.get_json(silent=True) or {}
    policy_version = str(data.get("policy_version") or "1.0")[:20]
    now = utcnow()
    rec = db.session.get(ConsentLog, g.current_user.id)
    if rec:
        rec.consent = True
        rec.policy_version = policy_version
        rec.accepted_at = now
    else:
        db.session.add(
            ConsentLog(
                user_id=g.current_user.id,
                consent=True,
                policy_version=policy_version,
                accepted_at=now,
            )
        )
    log_audit("CONSENT_ACCEPTED", target_type="user", target_id=g.current_user.id)
    db.session.commit()
    return jsonify(
        {
            "status": "ok",
            "consent": True,
            "policy_version": policy_version,
            "accepted_at": iso_utc(now),
        }
    ), 200


@patient_bp.route("/api/patient/profile", methods=["GET"])
@roles_required("patient")
def get_patient_profile():
    user = g.current_user
    profile = db.session.get(PatientProfile, user.id)
    if not profile:
        return jsonify({"error": "Patient profile not found"}), 404
    doctor = profile.doctor
    return jsonify(
        {
            "user": user.to_dict(),
            "doctor": {
                "id": doctor.id,
                "username": doctor.username,
                "full_name": doctor.full_name,
            }
            if doctor
            else None,
            "profile": profile.to_dict(),
        }
    ), 200


@patient_bp.route("/api/patient/profile", methods=["PATCH"])
@roles_required("patient")
def update_patient_profile():
    user = g.current_user
    profile = db.session.get(PatientProfile, user.id)
    if not profile:
        return jsonify({"error": "Patient profile not found"}), 404

    data = request.get_json(silent=True) or {}
    attempted_forbidden = set(data.keys()) & FORBIDDEN_PATIENT_FIELDS
    if attempted_forbidden:
        return (
            jsonify(
                {
                    "error": "Patients may only edit contact fields (phone, emergency_contact_name, emergency_contact_phone)."
                }
            ),
            400,
        )

    if "phone" in data:
        profile.phone = (str(data["phone"]).strip() if data["phone"] is not None else "")[:30]
    if "emergency_contact_name" in data:
        profile.emergency_contact_name = (
            str(data["emergency_contact_name"]).strip() if data["emergency_contact_name"] is not None else ""
        )[:120]
    if "emergency_contact_phone" in data:
        profile.emergency_contact_phone = (
            str(data["emergency_contact_phone"]).strip() if data["emergency_contact_phone"] is not None else ""
        )[:30]

    profile.updated_at = utcnow()
    db.session.commit()
    return jsonify({"status": "ok", "profile": profile.to_dict()}), 200


@patient_bp.route("/predict", methods=["POST"])
@roles_required("patient")
@limiter.limit("30 per minute")
def run_prediction():
    import app as main_app

    user = g.current_user
    profile = db.session.get(PatientProfile, user.id)
    if not profile:
        return jsonify({"error": "Patient clinical profile is missing. Contact your doctor."}), 400

    data = request.get_json(silent=True) or {}
    signal_floats, val_err = _validate_signal_payload(data.get("signal"))
    if val_err:
        return jsonify({"error": val_err}), 400

    patient_age = profile.age
    patient_gender = profile.gender
    case_id = uuid.uuid4().hex[:12]
    signal_blob = encode_float32_le(signal_floats)

    try:
        bpm, rate_status = main_app.analyze_heart_rate(signal_floats)
        is_valid, message = main_app.smart_gatekeeper(signal_floats)
        now = utcnow()

        if not is_valid:
            _set_patient_decision(user.id, f"🟡 REJECTED: {message.upper()}")
            hist = _log_history_entry(
                case_id=case_id,
                patient_id=user.id,
                age=patient_age,
                gender=patient_gender,
                status="REJECTED",
                prediction="N/A",
                bpm=bpm,
                rate_status="CORRUPTED",
                message=message,
                created_at=now,
            )
            db.session.flush()
            db.session.add(
                EcgRecording(
                    history_id=hist.id,
                    case_id=None,
                    patient_id=user.id,
                    sample_rate_hz=SAMPLE_RATE_HZ,
                    n_samples=len(signal_floats),
                    signal=signal_blob,
                    attention=None,
                    created_at=now,
                )
            )
            db.session.commit()
            return (
                jsonify({"status": "REJECTED", "message": message, "bpm": bpm, "rate_status": "CORRUPTED"}),
                200,
            )

        result = main_app.predict(signal_floats)
        prediction = result.get("prediction", "UNCERTAIN")
        confidence = float(result.get("confidence", 0.0))
        prob = float(result.get("probability", 0.0))
        xai_weights_list = result.get("xai_weights")
        attention_blob = (
            encode_float32_le(xai_weights_list)
            if isinstance(xai_weights_list, list) and len(xai_weights_list) > 0
            else None
        )
        raw_ai_prediction = "AFIB" if prob >= 0.5 else "NORMAL"

        if prediction == "UNCERTAIN":
            chronic_list = profile.get_chronic_list()
            chronic_str = "، ".join(chronic_list) if chronic_list else "لا يوجد تاريخ مرضي"
            priority = compute_case_priority(raw_ai_prediction, bpm, confidence)

            new_case = Case(
                id=case_id,
                patient_id=user.id,
                doctor_id=profile.doctor_id,
                age_at_test=patient_age,
                gender_at_test=patient_gender,
                status="ESCALATED",
                priority=priority,
                complaint="",
                chronic=chronic_str,
                symptoms="[]",
                history=json.dumps(chronic_list, ensure_ascii=False),
                signal="[]",
                confidence=confidence,
                raw_prediction=raw_ai_prediction,
                prediction="UNCERTAIN",
                bpm=bpm,
                rate_status=rate_status,
                created_at=now,
                xai_weights="[]",
                ai_attention_note="",
            )
            db.session.add(new_case)
            _set_patient_decision(user.id, "⚪ ESCALATED: Awaiting Physician Review.")
            hist = _log_history_entry(
                case_id=case_id,
                patient_id=user.id,
                age=patient_age,
                gender=patient_gender,
                status="ESCALATED",
                prediction="UNCERTAIN",
                bpm=bpm,
                rate_status=rate_status,
                confidence=f"{confidence:.2%}",
                ai_attention_note="",
                created_at=now,
            )
            db.session.flush()
            db.session.add(
                EcgRecording(
                    history_id=hist.id,
                    case_id=case_id,
                    patient_id=user.id,
                    sample_rate_hz=SAMPLE_RATE_HZ,
                    n_samples=len(signal_floats),
                    signal=signal_blob,
                    attention=attention_blob,
                    created_at=now,
                )
            )
            db.session.commit()
            return (
                jsonify(
                    {
                        "status": "ESCALATED",
                        "case_id": case_id,
                        "prediction": "UNCERTAIN",
                        "confidence": f"{confidence:.2%}",
                        "bpm": bpm,
                        "rate_status": rate_status,
                        "priority": priority,
                    }
                ),
                200,
            )

        _set_patient_decision(user.id, f"🟢 DIAGNOSIS: {prediction}")
        hist = _log_history_entry(
            case_id=case_id,
            patient_id=user.id,
            age=patient_age,
            gender=patient_gender,
            status="SUCCESS",
            prediction=prediction,
            bpm=bpm,
            rate_status=rate_status,
            confidence=f"{confidence:.2%}",
            created_at=now,
        )
        db.session.flush()
        db.session.add(
            EcgRecording(
                history_id=hist.id,
                case_id=None,
                patient_id=user.id,
                sample_rate_hz=SAMPLE_RATE_HZ,
                n_samples=len(signal_floats),
                signal=signal_blob,
                attention=attention_blob,
                created_at=now,
            )
        )
        db.session.commit()
        return (
            jsonify(
                {
                    "status": "SUCCESS",
                    "prediction": prediction,
                    "confidence": f"{confidence:.2%}",
                    "bpm": bpm,
                    "rate_status": rate_status,
                }
            ),
            200,
        )
    except Exception:
        current_app.logger.exception("ECG inference failed for patient_id=%s", user.id)
        db.session.rollback()
        return jsonify({"error": "Internal server error during signal analysis"}), 500


@patient_bp.route("/api/patient/update_case", methods=["POST"])
@roles_required("patient")
def update_case():
    import app as main_app

    user = g.current_user
    data = request.get_json(silent=True) or {}
    case_id = data.get("case_id")
    if not case_id:
        return jsonify({"error": "case_id is required"}), 400

    case_record = db.session.get(Case, case_id)
    if case_record is None:
        return jsonify({"error": "Case not found"}), 404
    if case_record.patient_id != user.id:
        return jsonify({"error": "Forbidden: you do not own this case"}), 403
    if case_record.status != "ESCALATED":
        return jsonify({"error": "Case is no longer open for updates"}), 409

    symptoms = data.get("symptoms", [])
    if not isinstance(symptoms, list):
        return jsonify({"error": "symptoms must be a list"}), 400
    symptoms_clean = [str(s).strip() for s in symptoms if str(s).strip()][:20]

    profile = db.session.get(PatientProfile, user.id)
    chronic_list = profile.get_chronic_list() if profile else []

    case_record.symptoms = json.dumps(symptoms_clean, ensure_ascii=False)
    case_record.history = json.dumps(chronic_list, ensure_ascii=False)
    case_record.complaint = "، ".join(symptoms_clean) if symptoms_clean else "لا توجد أعراض"
    case_record.chronic = "، ".join(chronic_list) if chronic_list else "لا يوجد تاريخ مرضي"

    xai_list = []
    recording = db.session.scalar(
        select(EcgRecording).where(EcgRecording.case_id == case_record.id)
    )
    if recording is not None and recording.attention is not None:
        xai_list = recording.get_attention_array().tolist()
    elif case_record.xai_weights:
        try:
            parsed = json.loads(case_record.xai_weights)
            if isinstance(parsed, list):
                xai_list = parsed
        except Exception:
            xai_list = []

    if main_app.LLM_AVAILABLE and xai_list and not case_record.ai_attention_note:
        case_record.ai_attention_note = (
            "جاري توليد التفسير السريري الدقيق في الخلفية... (يُرجى الانتظار بضع ثوانٍ)"
        )
        db.session.commit()

        app_obj = current_app._get_current_object()
        if not app_obj.config.get("TESTING"):
            threading.Thread(
                target=main_app.generate_llm_explanation_bg,
                args=(app_obj, case_id, xai_list),
                daemon=True,
            ).start()
    else:
        db.session.commit()

    return jsonify({"status": "success"}), 200


@patient_bp.route("/api/patient/status", methods=["GET"])
@roles_required("patient")
def get_patient_status():
    decision = db.session.get(PatientDecision, g.current_user.id)
    if not decision:
        return jsonify({"status_text": "IDLE", "doctor_note": None, "updated_at": None}), 200
    return (
        jsonify(
            {
                "status_text": decision.status_text,
                "doctor_note": decision.doctor_note,
                "updated_at": iso_utc(decision.updated_at),
            }
        ),
        200,
    )


@patient_bp.route("/api/patient/history", methods=["GET"])
@roles_required("patient")
def get_patient_history():
    limit = min(max(request.args.get("limit", default=50, type=int), 1), 200)
    offset = max(request.args.get("offset", default=0, type=int), 0)
    rows = db.session.scalars(
        select(PatientHistory)
        .where(PatientHistory.patient_id == g.current_user.id)
        .order_by(PatientHistory.created_at.desc(), PatientHistory.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    hist_ids = [h.id for h in rows]
    rec_ids = (
        set(
            db.session.scalars(
                select(EcgRecording.history_id).where(EcgRecording.history_id.in_(hist_ids))
            ).all()
        )
        if hist_ids
        else set()
    )
    return jsonify([h.to_dict(has_waveform=(h.id in rec_ids)) for h in rows]), 200


@patient_bp.route("/api/history/<int:history_id>/waveform", methods=["GET"])
@roles_required("patient", "doctor", "admin")
def get_history_waveform(history_id: int):
    hist = db.session.get(PatientHistory, history_id)
    if hist is None:
        return jsonify({"error": "Waveform not found"}), 404

    user = g.current_user
    if user.role == "patient":
        if hist.patient_id != user.id:
            return jsonify({"error": "Waveform not found"}), 404
    elif user.role == "doctor":
        profile = db.session.get(PatientProfile, hist.patient_id)
        if profile is None or profile.doctor_id != user.id:
            return jsonify({"error": "Waveform not found"}), 404

    rec = db.session.scalar(
        select(EcgRecording).where(EcgRecording.history_id == history_id)
    )
    if rec is None:
        return jsonify({"error": "Waveform not found"}), 404

    max_points_raw = request.args.get("max_points")
    max_points = None
    if max_points_raw is not None and max_points_raw != "":
        try:
            max_points = int(max_points_raw)
        except ValueError:
            return jsonify({"error": "max_points must be a positive integer"}), 400
        if max_points <= 0:
            return jsonify({"error": "max_points must be a positive integer"}), 400
        max_points = min(max_points, rec.n_samples)

    signal_arr = rec.get_signal_array()
    include_attention = user.role in ("doctor", "admin") and rec.attention is not None
    att_arr = rec.get_attention_array() if include_attention else None
    if att_arr is not None and len(att_arr) != len(signal_arr):
        att_arr = None

    if max_points is not None and max_points < len(signal_arr):
        samples_out, attention_out = decimate_minmax(signal_arr, max_points, att_arr)
    else:
        samples_out = signal_arr.tolist()
        attention_out = att_arr.tolist() if att_arr is not None else None

    duration_s = round(rec.n_samples / rec.sample_rate_hz, 4) if rec.sample_rate_hz > 0 else 0.0
    return (
        jsonify(
            {
                "history_id": hist.id,
                "sample_rate_hz": rec.sample_rate_hz,
                "n_samples": rec.n_samples,
                "samples": samples_out,
                "attention": attention_out,
                "duration_s": duration_s,
            }
        ),
        200,
    )
