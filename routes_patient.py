"""
routes_patient.py - Patient consent, profile, ECG inference, case escalation,
status polling, and diagnostic history endpoints.
"""
import json
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
    utcnow,
    iso_utc,
)
from auth import roles_required, limiter, log_audit

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
) -> None:
    db.session.add(
        PatientHistory(
            case_id=case_id,
            patient_id=patient_id,
            created_at=utcnow(),
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
    )


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
    signal = data.get("signal")
    if not isinstance(signal, list) or len(signal) == 0 or len(signal) > 20000:
        return jsonify({"error": "Valid ECG signal array is required"}), 400

    try:
        signal_floats = [float(x) for x in signal]
    except (TypeError, ValueError):
        return jsonify({"error": "Signal must contain numeric values"}), 400

    patient_age = profile.age
    patient_gender = profile.gender
    case_id = uuid.uuid4().hex[:12]

    try:
        bpm, rate_status = main_app.analyze_heart_rate(signal_floats)
        is_valid, message = main_app.smart_gatekeeper(signal_floats)

        if not is_valid:
            _set_patient_decision(user.id, f"🟡 REJECTED: {message.upper()}")
            _log_history_entry(
                case_id=case_id,
                patient_id=user.id,
                age=patient_age,
                gender=patient_gender,
                status="REJECTED",
                prediction="N/A",
                bpm=bpm,
                rate_status="CORRUPTED",
                message=message,
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
        xai_weights_list = result.get("xai_weights", [0.0] * 3000)
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
                signal=json.dumps(signal_floats),
                confidence=confidence,
                raw_prediction=raw_ai_prediction,
                prediction="UNCERTAIN",
                bpm=bpm,
                rate_status=rate_status,
                created_at=utcnow(),
                xai_weights=json.dumps(xai_weights_list),
                ai_attention_note="",
            )
            db.session.add(new_case)
            _set_patient_decision(user.id, "⚪ ESCALATED: Awaiting Physician Review.")
            _log_history_entry(
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
        _log_history_entry(
            case_id=case_id,
            patient_id=user.id,
            age=patient_age,
            gender=patient_gender,
            status="SUCCESS",
            prediction=prediction,
            bpm=bpm,
            rate_status=rate_status,
            confidence=f"{confidence:.2%}",
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

    if main_app.LLM_AVAILABLE and case_record.xai_weights and not case_record.ai_attention_note:
        case_record.ai_attention_note = (
            "جاري توليد التفسير السريري الدقيق في الخلفية... (يُرجى الانتظار بضع ثوانٍ)"
        )
        db.session.commit()
        try:
            xai_list = json.loads(case_record.xai_weights)
        except Exception:
            xai_list = []

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
    return jsonify([h.to_dict() for h in rows]), 200
