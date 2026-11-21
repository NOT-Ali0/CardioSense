"""
tests/test_auth.py - Tests for login, logout, CSRF, must_change_password,
session revocation, rate limiting, and CLI admin creation.
"""
from db import db
from models import User
from auth import hash_password
from tests.conftest import login_client


def test_login_logout_and_me(client, seeded_users):
    # Unauthenticated /me returns 401
    assert client.get("/api/auth/me").status_code == 401

    # Invalid password returns generic 401
    res = client.post("/api/auth/login", json={"username": "admin1", "password": "wrongpassword"})
    assert res.status_code == 401
    assert res.get_json()["error"] == "Invalid username or password"

    # Nonexistent user returns identical generic 401
    res = client.post("/api/auth/login", json={"username": "nobody", "password": "wrongpassword"})
    assert res.status_code == 401
    assert res.get_json()["error"] == "Invalid username or password"

    # Valid login sets HttpOnly cookie and returns csrf_token
    csrf, data = login_client(client, "admin1", "AdminPass123!")
    assert data["user"]["role"] == "admin"
    assert csrf

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.get_json()["user"]["username"] == "admin1"

    # Logout without CSRF fails with 403
    assert client.post("/api/auth/logout").status_code == 403

    # Logout with CSRF succeeds and invalidates session
    res = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_must_change_password_gate(app, client):
    with app.app_context():
        u = User(
            username="newdoc",
            full_name="New Doctor",
            password_hash=hash_password("TempPass123!"),
            role="doctor",
            is_active=True,
            must_change_password=True,
        )
        db.session.add(u)
        db.session.commit()

    csrf, data = login_client(client, "newdoc", "TempPass123!")
    assert data["user"]["must_change_password"] is True

    # Protected endpoints are blocked with 403 PASSWORD_CHANGE_REQUIRED
    blocked = client.get("/api/doctor/cases")
    assert blocked.status_code == 403
    assert blocked.get_json()["code"] == "PASSWORD_CHANGE_REQUIRED"

    # /api/auth/me is still allowed
    assert client.get("/api/auth/me").status_code == 200

    # Weak password rejected
    weak = client.post(
        "/api/auth/change-password",
        json={"current_password": "TempPass123!", "new_password": "short"},
        headers={"X-CSRF-Token": csrf},
    )
    assert weak.status_code == 400

    # Strong password clears flag and unlocks endpoints
    ok = client.post(
        "/api/auth/change-password",
        json={"current_password": "TempPass123!", "new_password": "StrongNewPass99!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 200
    assert ok.get_json()["user"]["must_change_password"] is False
    assert client.get("/api/doctor/cases").status_code == 200


def test_deactivation_immediately_revokes_active_session(app, seeded_users):
    admin_client = app.test_client()
    doc_client = app.test_client()

    admin_csrf, _ = login_client(admin_client, "admin1", "AdminPass123!")
    login_client(doc_client, "dr_sara", "DoctorPass123!")
    assert doc_client.get("/api/auth/me").status_code == 200

    # Reassign dr_sara's patient first so deactivation succeeds
    res = admin_client.patch(
        f"/api/admin/patients/{seeded_users['pat2_id']}/reassign",
        json={"doctor_id": seeded_users["doc1_id"]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert res.status_code == 200

    res = admin_client.patch(
        f"/api/admin/users/{seeded_users['doc2_id']}/status",
        json={"is_active": False},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert res.status_code == 200

    # Existing doctor cookie is immediately rejected
    assert doc_client.get("/api/auth/me").status_code == 401
    assert doc_client.get("/api/doctor/cases").status_code == 401


def test_login_rate_limiting(app_with_ratelimit):
    c = app_with_ratelimit.test_client()
    statuses = []
    for _ in range(7):
        res = c.post("/api/auth/login", json={"username": "bruteuser", "password": "WrongPassword123"})
        statuses.append(res.status_code)
    assert 429 in statuses


def test_cli_create_admin(app):
    runner = app.test_cli_runner()
    res = runner.invoke(
        args=[
            "create-admin",
            "--username",
            "cliadmin",
            "--full-name",
            "CLI Admin",
            "--password",
            "SuperSecretAdmin123!",
        ]
    )
    assert res.exit_code == 0
    assert "Created admin account 'cliadmin'" in res.output
    with app.app_context():
        u = db.session.query(User).filter_by(username="cliadmin").first()
        assert u is not None
        assert u.role == "admin"
        assert u.must_change_password is False


def test_legacy_v1_schema_auto_upgrades_on_startup(tmp_path):
    """Simulate a Docker volume with v1 consent_logs(user) + v2 users table and verify auto-upgrade."""
    import sqlite3
    from app import create_app

    db_file = tmp_path / "legacy_docker.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE consent_logs (user VARCHAR(100) PRIMARY KEY, consent BOOLEAN, timestamp VARCHAR(50))")
    conn.execute("CREATE TABLE cases (id VARCHAR(36) PRIMARY KEY, patient_name VARCHAR(100))")
    conn.commit()
    conn.close()

    upgraded_app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
            "SESSION_COOKIE_SECURE": False,
            "RATELIMIT_ENABLED": False,
            "SECRET_KEY": "test-secret-key-for-pytest-only-32bytes!",
        }
    )

    with upgraded_app.app_context():
        # Verify consent_logs now has user_id and patient login works cleanly
        pat = User(
            username="pat_legacy_test",
            full_name="Legacy Test",
            password_hash=hash_password("PatientPass123!"),
            role="patient",
            is_active=True,
            must_change_password=False,
        )
        db.session.add(pat)
        db.session.commit()

    c = upgraded_app.test_client()
    res = c.post("/api/auth/login", json={"username": "pat_legacy_test", "password": "PatientPass123!"})
    assert res.status_code == 200
    assert res.get_json()["consent_accepted"] is False

