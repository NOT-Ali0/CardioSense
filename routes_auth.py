"""
routes_auth.py - Authentication & session endpoints for CardioSense.
"""
from flask import Blueprint, request, jsonify, session, g

from db import db
from models import User, ConsentLog, utcnow
from auth import (
    limiter,
    login_rate_limit_key,
    login_required,
    normalize_username,
    validate_password,
    hash_password,
    verify_password,
    start_user_session,
    ensure_csrf_token,
    log_audit,
)

auth_bp = Blueprint("auth_bp", __name__)


def _consent_accepted(user: User) -> bool:
    if user.role != "patient":
        return True
    rec = db.session.get(ConsentLog, user.id)
    return bool(rec and rec.consent)


@auth_bp.route("/api/auth/login", methods=["POST"])
@limiter.limit("5 per minute; 20 per hour", key_func=login_rate_limit_key)
def login():
    data = request.get_json(silent=True) or {}
    username = normalize_username(data.get("username"))
    password = data.get("password") or ""

    user = db.session.query(User).filter_by(username=username).first()
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        log_audit(
            "LOGIN_FAIL",
            actor_id=user.id if user else None,
            target_type="user",
            target_id=user.id if user else username,
            detail={"username": username},
            commit=True,
        )
        return jsonify({"error": "Invalid username or password"}), 401

    user.last_login_at = utcnow()
    csrf_token = start_user_session(user)
    log_audit("LOGIN_OK", actor_id=user.id, target_type="user", target_id=user.id, commit=True)

    return jsonify(
        {
            "user": user.to_dict(),
            "consent_accepted": _consent_accepted(user),
            "csrf_token": csrf_token,
        }
    ), 200


@auth_bp.route("/api/auth/logout", methods=["POST"])
@login_required
def logout():
    user = g.current_user
    log_audit("LOGOUT", actor_id=user.id, target_type="user", target_id=user.id, commit=True)
    session.clear()
    return jsonify({"status": "ok"}), 200


@auth_bp.route("/api/auth/me", methods=["GET"])
@login_required
def get_me():
    user = g.current_user
    return jsonify(
        {
            "user": user.to_dict(),
            "consent_accepted": _consent_accepted(user),
            "csrf_token": ensure_csrf_token(),
        }
    ), 200


@auth_bp.route("/api/auth/change-password", methods=["POST"])
@login_required
@limiter.limit("5 per minute")
def change_password():
    user = g.current_user
    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""

    if not verify_password(user.password_hash, current_pw):
        return jsonify({"error": "Current password is incorrect"}), 400

    if new_pw == current_pw:
        return jsonify({"error": "New password must be different from current password"}), 400

    err = validate_password(new_pw, user.username)
    if err:
        return jsonify({"error": err}), 400

    user.password_hash = hash_password(new_pw)
    user.must_change_password = False
    user.session_version += 1
    user.updated_at = utcnow()
    log_audit("PASSWORD_CHANGE", actor_id=user.id, target_type="user", target_id=user.id)
    db.session.commit()

    csrf_token = start_user_session(user)
    return jsonify(
        {
            "status": "ok",
            "user": user.to_dict(),
            "consent_accepted": _consent_accepted(user),
            "csrf_token": csrf_token,
        }
    ), 200
