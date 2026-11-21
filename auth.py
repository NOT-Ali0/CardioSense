"""
auth.py - Authentication, password hashing/policy, CSRF, rate limiting,
RBAC/ownership decorators, and audit logging for CardioSense.
"""
import hmac
import json
import os
import re
import secrets
from functools import wraps
from typing import Optional, Tuple, Any

from flask import request, session, jsonify, g, current_app
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

from db import db
from models import User, PatientProfile, AuditLog, utcnow

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,80}$")
MIN_PASSWORD_LEN = 10
MAX_PASSWORD_LEN = 128

# Routes exempted from must_change_password gate
PASSWORD_CHANGE_ALLOWED_PATHS = {
    "/api/auth/me",
    "/api/auth/change-password",
    "/api/auth/logout",
}

# Mutating routes exempted from CSRF check (unauthenticated login)
CSRF_EXEMPT_PATHS = {
    "/api/auth/login",
}

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    default_limits=[],
)


def normalize_username(raw: Optional[str]) -> str:
    return (raw or "").strip().lower()


def validate_username(username: str) -> Optional[str]:
    if not USERNAME_RE.match(username):
        return "Username must be 3-80 characters using lowercase letters, digits, '.', '_' or '-'."
    return None


def validate_password(password: str, username: str = "") -> Optional[str]:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters long."
    if len(password) > MAX_PASSWORD_LEN:
        return f"Password must be at most {MAX_PASSWORD_LEN} characters long."
    if username and password.strip().lower() == username.strip().lower():
        return "Password must not be identical to the username."
    return None


def hash_password(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash or not isinstance(password, str):
        return False
    return check_password_hash(password_hash, password)


def ensure_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def start_user_session(user: User) -> str:
    """Clear any prior session (anti-fixation) and establish authenticated session."""
    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    session["sv"] = user.session_version
    return ensure_csrf_token()


def get_client_ip() -> str:
    return request.remote_addr or "unknown"


def log_audit(
    action: str,
    actor_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[Any] = None,
    detail: Optional[dict] = None,
    commit: bool = False,
) -> None:
    if actor_id is None and hasattr(g, "current_user") and g.current_user is not None:
        actor_id = g.current_user.id
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        ip_address=get_client_ip(),
        detail=json.dumps(detail, ensure_ascii=False) if detail is not None else None,
        created_at=utcnow(),
    )
    db.session.add(entry)
    if commit:
        db.session.commit()


def load_session_user() -> Optional[User]:
    """Load and validate the currently authenticated user from the signed session cookie."""
    uid = session.get("user_id")
    sv = session.get("sv")
    if uid is None or sv is None:
        return None
    user = db.session.get(User, uid)
    if user is None or not user.is_active or user.session_version != sv:
        session.clear()
        return None
    return user


def register_security_hooks(app) -> None:
    """Register before_request hooks for user loading, CSRF, and must_change_password enforcement."""

    @app.before_request
    def _security_before_request():
        g.current_user = load_session_user()

        path = request.path
        is_api = path.startswith("/api/") or path == "/predict"
        if not is_api:
            return None

        # 1. CSRF + JSON content-type enforcement on state-changing API requests
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and path not in CSRF_EXEMPT_PATHS:
            if g.current_user is not None:
                expected = session.get("csrf_token") or ""
                provided = request.headers.get("X-CSRF-Token", "")
                if not expected or not provided or not hmac.compare_digest(expected, provided):
                    return jsonify({"error": "Invalid or missing CSRF token", "code": "CSRF_INVALID"}), 403
                if request.content_length and not request.is_json:
                    return jsonify({"error": "Content-Type must be application/json"}), 415

        # 2. Enforce must_change_password across all protected endpoints
        if (
            g.current_user is not None
            and g.current_user.must_change_password
            and path not in PASSWORD_CHANGE_ALLOWED_PATHS
        ):
            return (
                jsonify(
                    {
                        "error": "Password change required before accessing this resource.",
                        "code": "PASSWORD_CHANGE_REQUIRED",
                    }
                ),
                403,
            )

        return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if getattr(g, "current_user", None) is None:
            return jsonify({"error": "Authentication required"}), 401
        return fn(*args, **kwargs)

    return wrapper


def roles_required(*roles: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = getattr(g, "current_user", None)
            if user is None:
                return jsonify({"error": "Authentication required"}), 401
            if user.role not in roles:
                return jsonify({"error": "Forbidden: insufficient permissions"}), 403
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def verify_doctor_owns_patient(doctor_or_admin: User, patient_id: int) -> Tuple[Optional[PatientProfile], Optional[Tuple[Any, int]]]:
    """Return (PatientProfile, None) if accessible, else (None, error_response)."""
    profile = db.session.get(PatientProfile, patient_id)
    if profile is None:
        return None, (jsonify({"error": "Patient not found"}), 404)
    if doctor_or_admin.role == "doctor" and profile.doctor_id != doctor_or_admin.id:
        return None, (jsonify({"error": "Forbidden: patient is not assigned to you"}), 403)
    return profile, None


def login_rate_limit_key() -> str:
    """Rate-limit login by client IP + normalized username."""
    ip = get_remote_address()
    data = request.get_json(silent=True) or {}
    username = normalize_username(data.get("username"))
    return f"{ip}:{username}" if username else ip


def bootstrap_admin_from_env(app_instance) -> None:
    """
    Optional idempotent first-admin creation from environment variables.
    Runs ONLY when zero admin users exist in the database.
    """
    username = normalize_username(os.getenv("BOOTSTRAP_ADMIN_USERNAME"))
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    full_name = (os.getenv("BOOTSTRAP_ADMIN_NAME") or "System Administrator").strip()
    if not username or not password:
        return

    with app_instance.app_context():
        existing_admin = db.session.query(User.id).filter_by(role="admin").first()
        if existing_admin is not None:
            return
        if validate_username(username):
            app_instance.logger.warning("BOOTSTRAP_ADMIN_USERNAME is invalid; skipping bootstrap.")
            return
        err = validate_password(password, username)
        if err:
            app_instance.logger.warning("BOOTSTRAP_ADMIN_PASSWORD rejected (%s); skipping bootstrap.", err)
            return
        admin = User(
            username=username,
            full_name=full_name,
            password_hash=hash_password(password),
            role="admin",
            is_active=True,
            must_change_password=False,
        )
        db.session.add(admin)
        db.session.commit()
        app_instance.logger.warning(
            "Bootstrapped initial admin '%s' from environment. Remove BOOTSTRAP_ADMIN_PASSWORD after startup.",
            username,
        )
