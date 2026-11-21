"""
tests/conftest.py - Shared pytest fixtures for CardioSense backend test suite.
"""
import os
import sys
import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Ensure app.py uses an isolated in-memory database when imported
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("FLASK_ENV", "development")
os.environ.setdefault("SESSION_COOKIE_SECURE", "0")

from app import create_app
from db import db
from models import User, PatientProfile
from auth import hash_password


@pytest.fixture()
def app():
    test_app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SESSION_COOKIE_SECURE": False,
            "RATELIMIT_ENABLED": False,
            "SECRET_KEY": "test-secret-key-for-pytest-only-32bytes!",
        }
    )
    with test_app.app_context():
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def app_with_ratelimit():
    test_app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SESSION_COOKIE_SECURE": False,
            "RATELIMIT_ENABLED": True,
            "SECRET_KEY": "test-secret-key-for-pytest-only-32bytes!",
        }
    )
    with test_app.app_context():
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def seeded_users(app):
    """Create 1 admin, 2 doctors, and 2 patients (1 per doctor), all with must_change_password=False."""
    with app.app_context():
        admin = User(
            username="admin1",
            full_name="Admin One",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            is_active=True,
            must_change_password=False,
        )
        doc1 = User(
            username="dr_ahmed",
            full_name="Dr. Ahmed",
            password_hash=hash_password("DoctorPass123!"),
            role="doctor",
            is_active=True,
            must_change_password=False,
        )
        doc2 = User(
            username="dr_sara",
            full_name="Dr. Sara",
            password_hash=hash_password("DoctorPass123!"),
            role="doctor",
            is_active=True,
            must_change_password=False,
        )
        db.session.add_all([admin, doc1, doc2])
        db.session.flush()

        pat1 = User(
            username="pat_ali",
            full_name="Ali Hassan",
            password_hash=hash_password("PatientPass123!"),
            role="patient",
            is_active=True,
            must_change_password=False,
            created_by_id=doc1.id,
        )
        pat2 = User(
            username="pat_mona",
            full_name="Mona Khaled",
            password_hash=hash_password("PatientPass123!"),
            role="patient",
            is_active=True,
            must_change_password=False,
            created_by_id=doc2.id,
        )
        db.session.add_all([pat1, pat2])
        db.session.flush()

        prof1 = PatientProfile(
            user_id=pat1.id,
            doctor_id=doc1.id,
            age=45,
            gender="M",
            phone="0501111111",
            emergency_contact_name="Hassan",
            emergency_contact_phone="0502222222",
            chronic_conditions='["ارتفاع ضغط الدم"]',
            clinical_notes="Mild hypertension",
        )
        prof2 = PatientProfile(
            user_id=pat2.id,
            doctor_id=doc2.id,
            age=62,
            gender="F",
            phone="0503333333",
            chronic_conditions='["السكري"]',
            clinical_notes="Type 2 diabetes",
        )
        db.session.add_all([prof1, prof2])
        db.session.commit()

        return {
            "admin_id": admin.id,
            "doc1_id": doc1.id,
            "doc2_id": doc2.id,
            "pat1_id": pat1.id,
            "pat2_id": pat2.id,
        }


def login_client(test_client, username: str, password: str):
    res = test_client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, f"Login failed for {username}: {res.status_code} {res.get_json()}"
    data = res.get_json()
    return data["csrf_token"], data
