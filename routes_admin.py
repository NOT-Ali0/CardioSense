"""
routes_admin.py - Admin telemetry, ML dataset export, user management
(create/deactivate doctors, create/reassign patients), and audit logs.
"""
import csv
import io
import json

from flask import Blueprint, request, jsonify, Response, g, current_app
from sqlalchemy import select, func, or_

from db import db
from models import (
    User,
    PatientProfile,
    Case,
    PatientHistory,
    MLDatasetRecord,
    AuditLog,
    utcnow,
)
from auth import (
    roles_required,
    normalize_username,
    validate_username,
    validate_password,
    hash_password,
    log_audit,
)
from routes_doctor import _create_patient_impl

admin_bp = Blueprint("admin_bp", __name__)


@admin_bp.route("/api/admin/stats", methods=["GET"])
@roles_required("admin")
def get_admin_stats():
    total = db.session.scalar(select(func.count(PatientHistory.id))) or 0
    rejected = (
        db.session.scalar(
            select(func.count(PatientHistory.id)).where(PatientHistory.status == "REJECTED")
        )
        or 0
    )
    escalated = (
        db.session.scalar(
            select(func.count(PatientHistory.id)).where(
                PatientHistory.status.in_(["ESCALATED", "REVIEWED"])
            )
        )
        or 0
    )
    normal = (
        db.session.scalar(
            select(func.count(PatientHistory.id)).where(
                PatientHistory.status == "SUCCESS", PatientHistory.prediction == "NORMAL"
            )
        )
        or 0
    )
    afib = (
        db.session.scalar(
            select(func.count(PatientHistory.id)).where(
                PatientHistory.status == "SUCCESS", PatientHistory.prediction == "AFIB"
            )
        )
        or 0
    )
    ds_size = db.session.scalar(select(func.count(MLDatasetRecord.id))) or 0
    doctors_count = (
        db.session.scalar(
            select(func.count(User.id)).where(User.role == "doctor", User.is_active.is_(True))
        )
        or 0
    )
    patients_count = (
        db.session.scalar(
            select(func.count(User.id)).where(User.role == "patient", User.is_active.is_(True))
        )
        or 0
    )
    pending_cases = (
        db.session.scalar(select(func.count(Case.id)).where(Case.status == "ESCALATED")) or 0
    )

    return (
        jsonify(
            {
                "stats": {
                    "total_tests": total,
                    "rejected_tests": rejected,
                    "escalated_tests": escalated,
                    "normal_tests": normal,
                    "afib_tests": afib,
                },
                "dataset_size": ds_size,
                "users": {
                    "active_doctors": doctors_count,
                    "active_patients": patients_count,
                    "pending_cases": pending_cases,
                },
            }
        ),
        200,
    )


@admin_bp.route("/api/admin/export_dataset", methods=["GET"])
@roles_required("admin")
def export_dataset():
    log_audit("DATASET_EXPORT", target_type="dataset", commit=True)
    app_obj = current_app._get_current_object()

    def generate():
        with app_obj.app_context():
            output = io.StringIO()
            writer = csv.writer(output)
            header = ["id", "age", "gender", "ai_prediction", "doctor_label"] + [
                f"sig_{i}" for i in range(3000)
            ]
            writer.writerow(header)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

            records = db.session.scalars(
                select(MLDatasetRecord).order_by(MLDatasetRecord.id.asc())
            ).yield_per(50)
            for record in records:
                sig = json.loads(record.signal) if record.signal else []
                row = [
                    record.case_id or "",
                    str(record.age),
                    record.gender,
                    record.ai_prediction,
                    record.doctor_label,
                ]
                row.extend([str(s) for s in sig])
                writer.writerow(row)
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=scai_clinical_dataset.csv"},
    )


@admin_bp.route("/api/admin/users", methods=["GET"])
@roles_required("admin")
def list_users():
    role_filter = (request.args.get("role") or "").strip().lower()
    active_filter = request.args.get("is_active")
    search = (request.args.get("search") or "").strip()

    stmt = select(User)
    if role_filter in ("patient", "doctor", "admin"):
        stmt = stmt.where(User.role == role_filter)
    if active_filter in ("true", "false", "1", "0"):
        stmt = stmt.where(User.is_active.is_(active_filter in ("true", "1")))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(User.username.ilike(pattern), User.full_name.ilike(pattern)))
    stmt = stmt.order_by(User.role.asc(), User.full_name.asc())

    users = db.session.scalars(stmt).all()

    # Batch counts of active patients per doctor
    doc_counts = dict(
        db.session.execute(
            select(PatientProfile.doctor_id, func.count(PatientProfile.user_id))
            .join(User, User.id == PatientProfile.user_id)
            .where(User.is_active.is_(True))
            .group_by(PatientProfile.doctor_id)
        ).all()
    )

    # Batch load patient profiles + doctor names
    patient_ids = [u.id for u in users if u.role == "patient"]
    profiles_by_uid = {}
    if patient_ids:
        for p in db.session.scalars(
            select(PatientProfile).where(PatientProfile.user_id.in_(patient_ids))
        ).all():
            profiles_by_uid[p.user_id] = p

    doctor_ids = {p.doctor_id for p in profiles_by_uid.values()}
    doctors_by_id = {}
    if doctor_ids:
        for d in db.session.scalars(select(User).where(User.id.in_(doctor_ids))).all():
            doctors_by_id[d.id] = d

    out = []
    for u in users:
        item = u.to_dict()
        if u.role == "doctor":
            item["active_patients_count"] = doc_counts.get(u.id, 0)
        elif u.role == "patient":
            prof = profiles_by_uid.get(u.id)
            if prof:
                doc = doctors_by_id.get(prof.doctor_id)
                item["doctor_id"] = prof.doctor_id
                item["doctor_name"] = doc.full_name if doc else None
                item["age"] = prof.age
                item["gender"] = prof.gender
        out.append(item)

    return jsonify(out), 200


@admin_bp.route("/api/admin/doctors", methods=["POST"])
@roles_required("admin")
def create_doctor():
    data = request.get_json(silent=True) or {}
    username = normalize_username(data.get("username"))
    full_name = (data.get("full_name") or "").strip()
    password = data.get("password") or ""

    err = validate_username(username)
    if err:
        return jsonify({"error": err}), 400
    if not full_name or len(full_name) > 120:
        return jsonify({"error": "Full name is required (1-120 characters)."}), 400
    pw_err = validate_password(password, username)
    if pw_err:
        return jsonify({"error": pw_err}), 400

    if db.session.query(User.id).filter_by(username=username).first() is not None:
        return jsonify({"error": "Username already exists."}), 409

    doctor = User(
        username=username,
        full_name=full_name,
        password_hash=hash_password(password),
        role="doctor",
        is_active=True,
        must_change_password=True,
        created_by_id=g.current_user.id,
    )
    db.session.add(doctor)
    db.session.flush()
    log_audit(
        "USER_CREATE",
        target_type="user",
        target_id=doctor.id,
        detail={"role": "doctor"},
    )
    db.session.commit()
    return jsonify({"user": doctor.to_dict()}), 201


@admin_bp.route("/api/admin/patients", methods=["POST"])
@roles_required("admin")
def create_patient_by_admin():
    data = request.get_json(silent=True) or {}
    try:
        doctor_id = int(data.get("doctor_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid doctor_id is required."}), 400
    return _create_patient_impl(doctor_id=doctor_id, creator_id=g.current_user.id)


@admin_bp.route("/api/admin/users/<int:user_id>/status", methods=["PATCH"])
@roles_required("admin")
def set_user_status(user_id: int):
    target = db.session.get(User, user_id)
    if target is None:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("is_active"), bool):
        return jsonify({"error": "Boolean field 'is_active' is required."}), 400

    new_active = data["is_active"]
    if target.id == g.current_user.id and not new_active:
        return jsonify({"error": "You cannot deactivate your own account."}), 400

    if target.role == "doctor" and not new_active:
        active_patients = (
            db.session.scalar(
                select(func.count(PatientProfile.user_id))
                .join(User, User.id == PatientProfile.user_id)
                .where(PatientProfile.doctor_id == target.id, User.is_active.is_(True))
            )
            or 0
        )
        if active_patients > 0:
            return (
                jsonify(
                    {
                        "error": f"Cannot deactivate doctor with {active_patients} active assigned patient(s). Reassign them first.",
                        "code": "DOCTOR_HAS_ACTIVE_PATIENTS",
                        "active_patients_count": active_patients,
                    }
                ),
                409,
            )

    target.is_active = new_active
    if not new_active:
        target.session_version += 1
    target.updated_at = utcnow()

    log_audit(
        "USER_STATUS_CHANGE",
        target_type="user",
        target_id=target.id,
        detail={"is_active": new_active, "role": target.role},
    )
    db.session.commit()
    return jsonify({"status": "ok", "user": target.to_dict()}), 200


@admin_bp.route("/api/admin/patients/<int:patient_id>/reassign", methods=["PATCH"])
@roles_required("admin")
def reassign_patient(patient_id: int):
    profile = db.session.get(PatientProfile, patient_id)
    if profile is None:
        return jsonify({"error": "Patient not found"}), 404

    data = request.get_json(silent=True) or {}
    try:
        new_doctor_id = int(data.get("doctor_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid doctor_id is required."}), 400

    new_doctor = db.session.get(User, new_doctor_id)
    if new_doctor is None or new_doctor.role != "doctor" or not new_doctor.is_active:
        return jsonify({"error": "Target doctor must be an active doctor account."}), 400

    old_doctor_id = profile.doctor_id
    profile.doctor_id = new_doctor_id
    profile.updated_at = utcnow()

    # Transfer open ESCALATED cases to the new doctor; keep ARCHIVED cases with original reviewer
    open_cases = db.session.scalars(
        select(Case).where(Case.patient_id == patient_id, Case.status == "ESCALATED")
    ).all()
    for c in open_cases:
        c.doctor_id = new_doctor_id

    log_audit(
        "PATIENT_REASSIGN",
        target_type="patient_profile",
        target_id=patient_id,
        detail={
            "old_doctor_id": old_doctor_id,
            "new_doctor_id": new_doctor_id,
            "transferred_cases": len(open_cases),
        },
    )
    db.session.commit()
    return (
        jsonify(
            {
                "status": "ok",
                "patient_id": patient_id,
                "doctor_id": new_doctor_id,
                "doctor_name": new_doctor.full_name,
                "transferred_cases": len(open_cases),
            }
        ),
        200,
    )


@admin_bp.route("/api/admin/audit-logs", methods=["GET"])
@roles_required("admin")
def get_audit_logs():
    action = (request.args.get("action") or "").strip().upper()
    limit = min(max(request.args.get("limit", default=100, type=int), 1), 500)
    offset = max(request.args.get("offset", default=0, type=int), 0)

    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
    rows = db.session.scalars(stmt).all()
    return jsonify([r.to_dict() for r in rows]), 200
