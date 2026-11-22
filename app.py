"""
app.py - CardioSense Flask application factory, security configuration,
clinical signal processing helpers, CLI commands, and blueprint registration.
"""
import argparse
import os
import secrets
import shutil
from datetime import datetime, timedelta, timezone

import click
import numpy as np
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from scipy.signal import find_peaks
from sqlalchemy import inspect as sa_inspect
from werkzeug.middleware.proxy_fix import ProxyFix

from db import db, migrate
from models import Case, User
from auth import (
    limiter,
    register_security_hooks,
    bootstrap_admin_from_env,
    normalize_username,
    validate_username,
    validate_password,
    hash_password,
)
from predictor import predict

# Optional Cohere client — only initialized when COHERE_API_KEY env var is explicitly provided
co = None
_cohere_key = os.getenv("COHERE_API_KEY", "").strip()
if _cohere_key:
    try:
        import cohere

        co = cohere.ClientV2(api_key=_cohere_key)
    except ImportError:
        co = None

# Optional local fine-tuned LLM explainer
try:
    from llm_explainer import explain_attention

    LLM_AVAILABLE = True
    print("✅ Fine-tuned LLM explainer loaded.")
except ImportError:
    LLM_AVAILABLE = False
    print("⚠️ llm_explainer not found — local attention note explainer disabled.")


def analyze_heart_rate(raw_signal, fs=300):
    signal = np.array(raw_signal, dtype=np.float32)
    signal = signal - np.mean(signal)
    peaks, _ = find_peaks(signal, distance=75, height=np.max(signal) * 0.30)
    if len(peaks) < 2:
        return 72, "NORMAL"
    rr_intervals = np.diff(peaks) / fs
    bpm = int(60.0 / np.mean(rr_intervals))
    if bpm < 60:
        return bpm, "BRADYCARDIA"
    if bpm > 100:
        return bpm, "TACHYCARDIA"
    return bpm, "NORMAL"


def smart_gatekeeper(raw_signal):
    raw_signal = np.array(raw_signal, dtype=np.float32)
    std_val = np.std(raw_signal)
    max_abs_val = np.max(np.abs(raw_signal))

    if std_val < 2.0 and max_abs_val < 10.0:
        return True, "Dataset Signal: Pre-validated"
    if std_val < 5.0:
        return False, "Leads Disconnected (Open Circuit)"
    if np.sum((raw_signal >= 4092) | (raw_signal <= 2)) > 300:
        return False, "Signal Clipping (Saturation Overflow)"

    mean_diff = np.mean(np.abs(np.diff(raw_signal)))
    if mean_diff > 180.0:
        return False, "Extreme Noise (Muscle Artifacts)"

    peak_to_peak = np.max(raw_signal) - np.min(raw_signal)
    if peak_to_peak > 3800.0:
        return False, "Severe Motion Artifact (Unstable Baseline)"

    return True, "Valid Signal Profile"


def generate_llm_explanation_bg(app_instance, case_id: str, xai_weights: list):
    """Runs in a background thread to generate the clinical LLM explanation note."""
    with app_instance.app_context():
        try:
            note = explain_attention(xai_weights)
        except Exception as exc:
            app_instance.logger.warning("Background LLM Error: %s", exc)
            note = "تعذر توليد التفسير الدقيق بسبب خطأ في النموذج المحلي."

        case_record = db.session.get(Case, case_id)
        if case_record:
            case_record.ai_attention_note = note
            db.session.commit()


def _upgrade_legacy_v1_schema_if_needed(application: Flask) -> None:
    """
    Detect a leftover v1 name-keyed SQLite database (e.g., in an existing Docker volume
    where consent_logs has 'user' instead of 'user_id' or cases has 'patient_name'),
    back it up, drop only the legacy v1 tables (preserving any v2 users/profiles),
    and let db.create_all() recreate them with the v2 FK-normalized schema.
    """
    from sqlalchemy import text

    inspector = sa_inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    is_legacy = False

    if "consent_logs" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("consent_logs")}
        if "user_id" not in cols:
            is_legacy = True
    if not is_legacy and "cases" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("cases")}
        if "patient_id" not in cols:
            is_legacy = True
    if not is_legacy and "patient_history" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("patient_history")}
        if "patient_id" not in cols:
            is_legacy = True
    if not is_legacy and "patient_decisions" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("patient_decisions")}
        if "patient_id" not in cols:
            is_legacy = True

    if not is_legacy:
        return

    uri = application.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite:///") and uri != "sqlite:///:memory:":
        db_path = uri.replace("sqlite:///", "", 1)
        if os.path.exists(db_path):
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = f"{db_path}.v1.bak.{ts}"
            try:
                shutil.copy2(db_path, backup_path)
                application.logger.warning(
                    "Legacy v1 SQLite schema detected. Backed up to %s and rebuilding v2 tables.",
                    backup_path,
                )
            except OSError as exc:
                application.logger.warning("Could not back up legacy DB (%s); rebuilding v2 tables.", exc)

    legacy_tables = [
        "ml_dataset",
        "cases",
        "patient_history",
        "patient_decisions",
        "consent_logs",
    ]
    with db.engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for tbl in legacy_tables:
            if tbl in existing_tables:
                conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
        conn.execute(text("PRAGMA foreign_keys=ON"))


def _upgrade_ml_dataset_schema_if_needed(application: Flask) -> None:
    """
    Ensure existing SQLite databases (e.g., Docker volumes started without `flask db upgrade`)
    have `ml_dataset.recording_id` and nullable `ml_dataset.signal` while preserving all rows.
    """
    from sqlalchemy import text

    inspector = sa_inspect(db.engine)
    if "ml_dataset" not in set(inspector.get_table_names()):
        return

    cols = {c["name"]: c for c in inspector.get_columns("ml_dataset")}
    has_recording_id = "recording_id" in cols
    signal_nullable = bool(cols.get("signal", {}).get("nullable", True))
    if has_recording_id and signal_nullable:
        return

    application.logger.info("Upgrading ml_dataset schema to add recording_id and nullable signal.")
    with db.engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                """
                CREATE TABLE _ml_dataset_new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    case_id VARCHAR(36),
                    recording_id INTEGER,
                    age VARCHAR(20) NOT NULL,
                    gender VARCHAR(10) NOT NULL,
                    ai_prediction VARCHAR(20) NOT NULL,
                    doctor_label VARCHAR(50) NOT NULL,
                    signal TEXT,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES cases (id) ON DELETE SET NULL,
                    FOREIGN KEY(recording_id) REFERENCES ecg_recordings (id) ON DELETE SET NULL
                )
                """
            )
        )
        if has_recording_id:
            conn.execute(
                text(
                    "INSERT INTO _ml_dataset_new (id, case_id, recording_id, age, gender, ai_prediction, doctor_label, signal, created_at) "
                    "SELECT id, case_id, recording_id, age, gender, ai_prediction, doctor_label, signal, created_at FROM ml_dataset"
                )
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO _ml_dataset_new (id, case_id, recording_id, age, gender, ai_prediction, doctor_label, signal, created_at) "
                    "SELECT id, case_id, NULL, age, gender, ai_prediction, doctor_label, signal, created_at FROM ml_dataset"
                )
            )
        conn.execute(text("DROP TABLE ml_dataset"))
        conn.execute(text("ALTER TABLE _ml_dataset_new RENAME TO ml_dataset"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ml_dataset_case_id ON ml_dataset (case_id)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_ml_dataset_recording_id ON ml_dataset (recording_id)")
        )
        conn.execute(text("PRAGMA foreign_keys=ON"))


def create_app(test_config: dict | None = None) -> Flask:
    application = Flask(__name__, static_folder="static", static_url_path="/api/static")

    # Secret key handling: require strong env var in production, allow ephemeral key in dev/testing
    env_mode = os.getenv("FLASK_ENV", "production").lower()
    is_testing = bool(test_config and test_config.get("TESTING"))
    secret_key = os.getenv("SECRET_KEY", "").strip()
    if not secret_key:
        if is_testing or env_mode == "development" or os.getenv("FLASK_DEBUG") == "1":
            secret_key = secrets.token_hex(32)
        else:
            # Generate ephemeral key with a loud warning rather than using a hardcoded string
            secret_key = secrets.token_hex(32)
            application.logger.warning(
                "SECRET_KEY environment variable is not set; using an ephemeral key. "
                "Set a persistent 32+ byte SECRET_KEY in production!"
            )

    secure_cookie_env = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower()
    if secure_cookie_env in ("0", "false", "no"):
        cookie_secure = False
    elif secure_cookie_env in ("1", "true", "yes"):
        cookie_secure = True
    else:
        cookie_secure = not (is_testing or env_mode == "development")

    application.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "DATABASE_URL",
            f"sqlite:///{os.path.join(application.instance_path, 'cardiosense.db')}",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,  # 2 MB max payload
    )

    if test_config:
        application.config.update(test_config)

    if os.getenv("TRUST_PROXY", "").strip() in ("1", "true", "yes"):
        application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Restricted CORS: disabled by default (same-origin); allow explicit origin list via CORS_ORIGINS
    cors_origins_raw = os.getenv("CORS_ORIGINS", "").strip()
    if cors_origins_raw:
        allowed_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip() and o.strip() != "*"]
        if allowed_origins:
            CORS(
                application,
                resources={r"/api/*": {"origins": allowed_origins}, r"/predict": {"origins": allowed_origins}},
                supports_credentials=True,
            )

    os.makedirs(application.instance_path, exist_ok=True)
    db.init_app(application)
    migrate.init_app(application, db)
    limiter.init_app(application)
    register_security_hooks(application)

    # Register modular blueprints
    from routes_auth import auth_bp
    from routes_patient import patient_bp
    from routes_doctor import doctor_bp
    from routes_admin import admin_bp

    application.register_blueprint(auth_bp)
    application.register_blueprint(patient_bp)
    application.register_blueprint(doctor_bp)
    application.register_blueprint(admin_bp)

    @application.route("/")
    def serve_frontend():
        base_dir = os.path.dirname(os.path.abspath(__file__))
        templates_dir = os.path.join(base_dir, "templates")
        return send_from_directory(templates_dir, "index.html")

    # Safe JSON error handlers (never expose internal stack traces)
    @application.errorhandler(400)
    def handle_400(e):
        return jsonify({"error": "Bad request"}), 400

    @application.errorhandler(404)
    def handle_404(e):
        return jsonify({"error": "Not found"}), 404

    @application.errorhandler(405)
    def handle_405(e):
        return jsonify({"error": "Method not allowed"}), 405

    @application.errorhandler(413)
    def handle_413(e):
        return jsonify({"error": "Request payload too large"}), 413

    @application.errorhandler(429)
    def handle_429(e):
        return jsonify({"error": "Too many requests. Please try again later."}), 429

    @application.errorhandler(500)
    def handle_500(e):
        application.logger.exception("Unhandled server error")
        return jsonify({"error": "Internal server error"}), 500

    # CLI commands
    @application.cli.command("create-admin")
    @click.option("--username", prompt=True, help="Admin username (lowercase).")
    @click.option("--full-name", prompt=True, default="System Administrator", help="Admin display name.")
    @click.option(
        "--password",
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
        help="Admin password (min 10 chars).",
    )
    def create_admin_cmd(username: str, full_name: str, password: str):
        """Create an initial or additional admin account."""
        uname = normalize_username(username)
        err = validate_username(uname)
        if err:
            raise click.ClickException(err)
        pw_err = validate_password(password, uname)
        if pw_err:
            raise click.ClickException(pw_err)
        if db.session.query(User.id).filter_by(username=uname).first() is not None:
            raise click.ClickException(f"User '{uname}' already exists.")
        admin = User(
            username=uname,
            full_name=full_name.strip() or "System Administrator",
            password_hash=hash_password(password),
            role="admin",
            is_active=True,
            must_change_password=False,
        )
        db.session.add(admin)
        db.session.commit()
        click.echo(f"Created admin account '{uname}' (id={admin.id}).")

    @application.cli.command("reset-db")
    @click.option("--no-backup", is_flag=True, help="Skip backing up existing SQLite file.")
    def reset_db_cmd(no_backup: bool):
        """Drop legacy tables and initialize the v2 FK-normalized schema."""
        from scripts.reset_db import reset_database

        reset_database(application, backup=not no_backup)

    with application.app_context():
        if os.getenv("SKIP_CREATE_ALL") != "1":
            _upgrade_legacy_v1_schema_if_needed(application)
            db.create_all()
            _upgrade_ml_dataset_schema_if_needed(application)
        if not application.config.get("TESTING") and os.getenv("SKIP_CREATE_ALL") != "1":
            bootstrap_admin_from_env(application)

    return application


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SCAI Unified Clinical Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host IP to bind to")
    parser.add_argument("--port", type=int, default=5001, help="Port to listen on")
    parser.add_argument("--public", action="store_true", help="Expose server to the internet via Ngrok")
    args = parser.parse_args()

    print(f"🚀 Starting Unified SCAI Server on http://{args.host}:{args.port}")

    if LLM_AVAILABLE:
        print("🔄 Warming up local fine-tuned LLM explainer...")
        try:
            explain_attention([0.0] * 3000)
            print("✅ LLM warm-up complete.")
        except Exception as exc:
            print(f"⚠️ LLM warm-up failed: {exc}")

    if args.public:
        try:
            from pyngrok import ngrok

            public_url = ngrok.connect(args.port).public_url
            print(f"\n=======================================================")
            print(f"🌍 PUBLIC INTERNET URL: {public_url}")
            print(f"=======================================================\n")
        except ImportError:
            print("⚠️ Warning: 'pyngrok' is not installed. Run 'pip install pyngrok' to use --public.")

    app.run(host=args.host, port=args.port, debug=False)
