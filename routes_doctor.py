"""
routes_doctor.py - Doctor dashboard KPIs, prioritized case queue, case review,
patient list with BPM trends, patient creation, and clinical profile editing.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, g
from sqlalchemy import select, func, case as sa_case, or_

from db import db
from models import (
    User,
    PatientProfile,
    Case,
    PatientHistory,
    PatientDecision,
    MLDatasetRecord,
    EcgRecording,
    utcnow,
    iso_utc,
)
from auth import (
    roles_required,
    verify_doctor_owns_patient,
    normalize_username,
    validate_username,
    validate_password,
    hash_password,
    log_audit,
)

doctor_bp = Blueprint("doctor_bp", __name__)

VALID_DECISIONS = {"INFECTED", "HEALTHY", "NOISY"}
VALID_GENDERS = {"M", "F"}


def _scoped_doctor_id() -> int | None:
    """Return doctor_id filter: current doctor's ID for doctors, or optional query param for admin."""
    if g.current_user.role == "doctor":
        return g.current_user.id
    return request.args.get("doctor_id", type=int)


@doctor_bp.route("/api/doctor/dashboard", methods=["GET"])
@roles_required("doctor", "admin")
def get_doctor_dashboard():
    doc_id = _scoped_doctor_id()
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )

    pending_q = select(func.count(Case.id)).where(Case.status == "ESCALATED")
    urgent_q = select(func.count(Case.id)).where(Case.status == "ESCALATED", Case.priority == "URGENT")
    patients_q = (
        select(func.count(PatientProfile.user_id))
        .join(User, User.id == PatientProfile.user_id)
        .where(User.is_active.is_(True))
    )
    reviewed_q = select(func.count(Case.id)).where(
        Case.status == "ARCHIVED", Case.reviewed_at >= today_start
    )

    if doc_id is not None:
        pending_q = pending_q.where(Case.doctor_id == doc_id)
        urgent_q = urgent_q.where(Case.doctor_id == doc_id)
        patients_q = patients_q.where(PatientProfile.doctor_id == doc_id)
        reviewed_q = reviewed_q.where(Case.doctor_id == doc_id)

    return (
        jsonify(
            {
                "kpis": {
                    "pending_cases": db.session.scalar(pending_q) or 0,
                    "urgent_cases": db.session.scalar(urgent_q) or 0,
                    "my_patients": db.session.scalar(patients_q) or 0,
                    "reviewed_today": db.session.scalar(reviewed_q) or 0,
                }
            }
        ),
        200,
    )


@doctor_bp.route("/api/doctor/cases", methods=["GET"])
@roles_required("doctor", "admin")
def get_doctor_cases():
    doc_id = _scoped_doctor_id()
    search = (request.args.get("search") or "").strip()
    priority = (request.args.get("priority") or "").strip().upper()
    raw_pred = (request.args.get("raw_prediction") or "").strip().upper()
    rate_status = (request.args.get("rate_status") or "").strip().upper()
    sort_by = (request.args.get("sort") or "priority").strip().lower()

    stmt = select(Case).join(User, User.id == Case.patient_id).where(Case.status == "ESCALATED")
    if doc_id is not None:
        stmt = stmt.where(Case.doctor_id == doc_id)
    if priority in ("URGENT", "HIGH", "NORMAL"):
        stmt = stmt.where(Case.priority == priority)
    if raw_pred in ("AFIB", "NORMAL"):
        stmt = stmt.where(Case.raw_prediction == raw_pred)
    if rate_status in ("NORMAL", "BRADYCARDIA", "TACHYCARDIA"):
        stmt = stmt.where(Case.rate_status == rate_status)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(pattern),
                User.username.ilike(pattern),
                Case.complaint.ilike(pattern),
                Case.id.ilike(pattern),
            )
        )

    priority_rank = sa_case(
        (Case.priority == "URGENT", 1),
        (Case.priority == "HIGH", 2),
        else_=3,
    )
    if sort_by == "confidence":
        stmt = stmt.order_by(Case.confidence.asc(), Case.created_at.desc())
    elif sort_by == "newest":
        stmt = stmt.order_by(Case.created_at.desc())
    else:
        stmt = stmt.order_by(priority_rank.asc(), Case.confidence.asc(), Case.created_at.desc())

    cases = db.session.scalars(stmt).all()
    case_ids = [c.id for c in cases]
    rec_case_ids = (
        set(
            db.session.scalars(
                select(EcgRecording.case_id).where(EcgRecording.case_id.in_(case_ids))
            ).all()
        )
        if case_ids
        else set()
    )
    return (
        jsonify(
            [
                c.to_summary_dict(
                    has_waveform=(c.id in rec_case_ids) or bool(c.signal and c.signal != "[]")
                )
                for c in cases
            ]
        ),
        200,
    )


@doctor_bp.route("/api/doctor/archive", methods=["GET"])
@roles_required("doctor", "admin")
def get_doctor_archive():
    doc_id = _scoped_doctor_id()
    search = (request.args.get("search") or "").strip()
    decision = (request.args.get("decision") or "").strip().upper()
    limit = min(max(request.args.get("limit", default=50, type=int), 1), 200)
    offset = max(request.args.get("offset", default=0, type=int), 0)

    stmt = select(Case).join(User, User.id == Case.patient_id).where(Case.status == "ARCHIVED")
    if doc_id is not None:
        stmt = stmt.where(Case.doctor_id == doc_id)
    if decision in VALID_DECISIONS:
        stmt = stmt.where(Case.doctor_decision == decision)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(pattern),
                User.username.ilike(pattern),
                Case.id.ilike(pattern),
            )
        )

    stmt = stmt.order_by(Case.reviewed_at.desc(), Case.created_at.desc()).limit(limit).offset(offset)
    cases = db.session.scalars(stmt).all()
    case_ids = [c.id for c in cases]
    rec_case_ids = (
        set(
            db.session.scalars(
                select(EcgRecording.case_id).where(EcgRecording.case_id.in_(case_ids))
            ).all()
        )
        if case_ids
        else set()
    )
    return (
        jsonify(
            [
                c.to_summary_dict(
                    has_waveform=(c.id in rec_case_ids) or bool(c.signal and c.signal != "[]")
                )
                for c in cases
            ]
        ),
        200,
    )


@doctor_bp.route("/api/doctor/cases/<case_id>", methods=["GET"])
@roles_required("doctor", "admin")
def get_case_detail(case_id: str):
    case_record = db.session.get(Case, case_id)
    if case_record is None:
        return jsonify({"error": "Case not found"}), 404
    if g.current_user.role == "doctor" and case_record.doctor_id != g.current_user.id:
        return jsonify({"error": "Forbidden: case is not assigned to you"}), 403

    profile = db.session.get(PatientProfile, case_record.patient_id)
    recording = db.session.scalar(
        select(EcgRecording).where(EcgRecording.case_id == case_record.id)
    )
    return jsonify(case_record.to_detail_dict(profile=profile, recording=recording)), 200


@doctor_bp.route("/api/doctor/respond", methods=["POST"])
@roles_required("doctor")
def doctor_respond():
    data = request.get_json(silent=True) or {}
    case_id = data.get("case_id")
    decision_type = (data.get("decision") or "").strip().upper()
    note = (data.get("note") or "").strip()

    if not case_id:
        return jsonify({"error": "case_id is required"}), 400
    if decision_type not in VALID_DECISIONS:
        return jsonify({"error": "Invalid decision. Must be INFECTED, HEALTHY, or NOISY."}), 400

    case_record = db.session.get(Case, case_id)
    if case_record is None:
        return jsonify({"error": "Case not found"}), 404
    if case_record.doctor_id != g.current_user.id:
        return jsonify({"error": "Forbidden: case is not assigned to you"}), 403
    if case_record.status != "ESCALATED":
        return jsonify({"error": "Case has already been reviewed"}), 409

    # 1. Append to de-identified ML dataset (reference recording_id instead of copying signal)
    recording = db.session.scalar(
        select(EcgRecording).where(EcgRecording.case_id == case_record.id)
    )
    db.session.add(
        MLDatasetRecord(
            case_id=case_record.id,
            recording_id=recording.id if recording is not None else None,
            age=str(case_record.age_at_test),
            gender=case_record.gender_at_test or "U",
            ai_prediction=case_record.raw_prediction,
            doctor_label=decision_type,
            signal=None if recording is not None else case_record.signal,
            created_at=utcnow(),
        )
    )

    # 2. Update live patient decision
    if decision_type == "INFECTED":
        status_msg = "🔴 DECISION: PATHOLOGY OBSERVED (AFIB)"
        hist_pred = "AFIB (CONFIRMED)"
    elif decision_type == "HEALTHY":
        status_msg = "🟢 DECISION: WAVEFORM REVIEWED GENERAL STABLE (NORMAL)"
        hist_pred = "NORMAL (CONFIRMED)"
    else:
        status_msg = "🟡 DECISION: SIGNAL REJECTED DUE TO ARTIFACT OVERLOAD"
        hist_pred = "REJECTED (NOISE)"

    now = utcnow()
    decision_rec = db.session.get(PatientDecision, case_record.patient_id)
    if decision_rec:
        decision_rec.status_text = status_msg
        decision_rec.doctor_note = note or None
        decision_rec.updated_at = now
    else:
        db.session.add(
            PatientDecision(
                patient_id=case_record.patient_id,
                status_text=status_msg,
                doctor_note=note or None,
                updated_at=now,
            )
        )

    # 3. Update patient history
    hist_entries = db.session.scalars(
        select(PatientHistory).where(
            PatientHistory.patient_id == case_record.patient_id,
            PatientHistory.case_id == case_id,
        )
    ).all()
    for hist in hist_entries:
        hist.status = "REVIEWED"
        hist.prediction = hist_pred
        hist.doctor_note = note or None

    # 4. Archive case
    case_record.status = "ARCHIVED"
    case_record.doctor_decision = decision_type
    case_record.doctor_note = note or None
    case_record.reviewed_by_id = g.current_user.id
    case_record.reviewed_at = now

    log_audit(
        "CASE_REVIEW",
        target_type="case",
        target_id=case_record.id,
        detail={"decision": decision_type, "patient_id": case_record.patient_id},
    )
    db.session.commit()
    return jsonify({"status": "success", "case": case_record.to_summary_dict()}), 200


@doctor_bp.route("/api/doctor/patients", methods=["GET"])
@roles_required("doctor", "admin")
def list_doctor_patients():
    doc_id = _scoped_doctor_id()
    search = (request.args.get("search") or "").strip()

    stmt = (
        select(User, PatientProfile)
        .join(PatientProfile, PatientProfile.user_id == User.id)
        .where(User.role == "patient")
    )
    if doc_id is not None:
        stmt = stmt.where(PatientProfile.doctor_id == doc_id)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(User.full_name.ilike(pattern), User.username.ilike(pattern)))
    stmt = stmt.order_by(User.full_name.asc())

    rows = db.session.execute(stmt).all()
    patient_ids = [u.id for u, _ in rows]

    # Batch-query history & pending cases to avoid N+1
    history_by_patient = defaultdict(list)
    pending_by_patient = defaultdict(int)
    if patient_ids:
        hist_rows = db.session.scalars(
            select(PatientHistory)
            .where(PatientHistory.patient_id.in_(patient_ids))
            .order_by(PatientHistory.created_at.desc(), PatientHistory.id.desc())
        ).all()
        for h in hist_rows:
            if len(history_by_patient[h.patient_id]) < 8:
                history_by_patient[h.patient_id].append(h)

        pending_counts = db.session.execute(
            select(Case.patient_id, func.count(Case.id))
            .where(Case.patient_id.in_(patient_ids), Case.status == "ESCALATED")
            .group_by(Case.patient_id)
        ).all()
        for pid, cnt in pending_counts:
            pending_by_patient[pid] = cnt

    result = []
    for u, prof in rows:
        hist = history_by_patient[u.id]
        latest = hist[0] if hist else None
        bpm_trend = [
            {"ts": iso_utc(h.created_at), "bpm": h.bpm, "status": h.status}
            for h in reversed(hist)
        ]
        result.append(
            {
                "id": u.id,
                "username": u.username,
                "full_name": u.full_name,
                "is_active": u.is_active,
                "doctor_id": prof.doctor_id,
                "age": prof.age,
                "gender": prof.gender,
                "phone": prof.phone or "",
                "chronic_conditions": prof.get_chronic_list(),
                "last_test_at": iso_utc(latest.created_at) if latest else None,
                "last_status": latest.status if latest else None,
                "last_prediction": latest.prediction if latest else None,
                "last_bpm": latest.bpm if latest else None,
                "bpm_trend": bpm_trend,
                "pending_cases_count": pending_by_patient[u.id],
            }
        )

    return jsonify(result), 200


@doctor_bp.route("/api/doctor/patients", methods=["POST"])
@roles_required("doctor")
def create_patient_by_doctor():
    return _create_patient_impl(doctor_id=g.current_user.id, creator_id=g.current_user.id)


def _create_patient_impl(doctor_id: int, creator_id: int):
    data = request.get_json(silent=True) or {}
    username = normalize_username(data.get("username"))
    full_name = (data.get("full_name") or "").strip()
    password = data.get("password") or ""
    gender = (data.get("gender") or "").strip().upper()

    err = validate_username(username)
    if err:
        return jsonify({"error": err}), 400
    if not full_name or len(full_name) > 120:
        return jsonify({"error": "Full name is required (1-120 characters)."}), 400
    pw_err = validate_password(password, username)
    if pw_err:
        return jsonify({"error": pw_err}), 400

    try:
        age = int(data.get("age"))
    except (TypeError, ValueError):
        return jsonify({"error": "Integer age (1-130) is required."}), 400
    if age < 1 or age > 130:
        return jsonify({"error": "Age must be between 1 and 130."}), 400
    if gender not in VALID_GENDERS:
        return jsonify({"error": "Gender must be 'M' or 'F'."}), 400

    if db.session.query(User.id).filter_by(username=username).first() is not None:
        return jsonify({"error": "Username already exists."}), 409

    doctor = db.session.get(User, doctor_id)
    if doctor is None or doctor.role != "doctor" or not doctor.is_active:
        return jsonify({"error": "Assigned doctor must be an active doctor account."}), 400

    chronic = data.get("chronic_conditions", [])
    if not isinstance(chronic, list):
        return jsonify({"error": "chronic_conditions must be a list."}), 400
    chronic_clean = [str(c).strip() for c in chronic if str(c).strip()][:20]

    new_user = User(
        username=username,
        full_name=full_name,
        password_hash=hash_password(password),
        role="patient",
        is_active=True,
        must_change_password=True,
        created_by_id=creator_id,
    )
    db.session.add(new_user)
    db.session.flush()

    profile = PatientProfile(
        user_id=new_user.id,
        doctor_id=doctor_id,
        age=age,
        gender=gender,
        phone=(str(data.get("phone") or "").strip())[:30] or None,
        emergency_contact_name=(str(data.get("emergency_contact_name") or "").strip())[:120] or None,
        emergency_contact_phone=(str(data.get("emergency_contact_phone") or "").strip())[:30] or None,
        chronic_conditions=json.dumps(chronic_clean, ensure_ascii=False),
        clinical_notes=(str(data.get("clinical_notes") or "").strip())[:2000],
    )
    db.session.add(profile)
    log_audit(
        "USER_CREATE",
        actor_id=creator_id,
        target_type="user",
        target_id=new_user.id,
        detail={"role": "patient", "doctor_id": doctor_id},
    )
    db.session.commit()

    return jsonify({"user": new_user.to_dict(), "profile": profile.to_dict()}), 201


@doctor_bp.route("/api/doctor/patients/<int:patient_id>", methods=["GET"])
@roles_required("doctor", "admin")
def get_doctor_patient_detail(patient_id: int):
    profile, err_resp = verify_doctor_owns_patient(g.current_user, patient_id)
    if err_resp:
        return err_resp
    user = profile.user
    doctor = profile.doctor
    return (
        jsonify(
            {
                "user": user.to_dict(),
                "doctor": {"id": doctor.id, "username": doctor.username, "full_name": doctor.full_name}
                if doctor
                else None,
                "profile": profile.to_dict(),
            }
        ),
        200,
    )


@doctor_bp.route("/api/doctor/patients/<int:patient_id>", methods=["PATCH"])
@roles_required("doctor", "admin")
def update_doctor_patient_clinical(patient_id: int):
    profile, err_resp = verify_doctor_owns_patient(g.current_user, patient_id)
    if err_resp:
        return err_resp

    data = request.get_json(silent=True) or {}
    if "age" in data:
        try:
            age = int(data["age"])
        except (TypeError, ValueError):
            return jsonify({"error": "Age must be an integer between 1 and 130."}), 400
        if age < 1 or age > 130:
            return jsonify({"error": "Age must be between 1 and 130."}), 400
        profile.age = age

    if "gender" in data:
        gender = str(data["gender"]).strip().upper()
        if gender not in VALID_GENDERS:
            return jsonify({"error": "Gender must be 'M' or 'F'."}), 400
        profile.gender = gender

    if "chronic_conditions" in data:
        if not isinstance(data["chronic_conditions"], list):
            return jsonify({"error": "chronic_conditions must be a list."}), 400
        chronic_clean = [str(c).strip() for c in data["chronic_conditions"] if str(c).strip()][:20]
        profile.chronic_conditions = json.dumps(chronic_clean, ensure_ascii=False)

    if "clinical_notes" in data:
        profile.clinical_notes = str(data["clinical_notes"] or "").strip()[:2000]

    if "full_name" in data:
        fn = str(data["full_name"] or "").strip()
        if not fn or len(fn) > 120:
            return jsonify({"error": "Full name must be 1-120 characters."}), 400
        profile.user.full_name = fn

    profile.updated_at = utcnow()
    log_audit("PATIENT_PROFILE_UPDATE", target_type="patient_profile", target_id=patient_id)
    db.session.commit()
    return jsonify({"status": "ok", "user": profile.user.to_dict(), "profile": profile.to_dict()}), 200


@doctor_bp.route("/api/doctor/patients/<int:patient_id>/history", methods=["GET"])
@roles_required("doctor", "admin")
def get_doctor_patient_history(patient_id: int):
    profile, err_resp = verify_doctor_owns_patient(g.current_user, patient_id)
    if err_resp:
        return err_resp
    limit = min(max(request.args.get("limit", default=50, type=int), 1), 200)
    offset = max(request.args.get("offset", default=0, type=int), 0)
    rows = db.session.scalars(
        select(PatientHistory)
        .where(PatientHistory.patient_id == profile.user_id)
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
