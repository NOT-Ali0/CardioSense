"""
tests/test_users_and_profiles.py - Tests for user lifecycle, doctor deactivation guard,
patient reassignment (including ESCALATED case transfer), and profile field permissions.
"""
import json
from db import db
from models import Case, PatientProfile
from tests.conftest import login_client


def test_admin_creates_doctor_and_patient_and_reassigns(app, seeded_users):
    admin_client = app.test_client()
    csrf, _ = login_client(admin_client, "admin1", "AdminPass123!")

    # Admin creates new doctor
    res = admin_client.post(
        "/api/admin/doctors",
        json={"username": "dr_tariq", "full_name": "Dr. Tariq", "password": "TempDocPassword1!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201
    new_doc_id = res.get_json()["user"]["id"]
    assert res.get_json()["user"]["must_change_password"] is True

    # Admin creates patient assigned to new doctor
    res = admin_client.post(
        "/api/admin/patients",
        json={
            "username": "pat_zaid",
            "full_name": "Zaid Omar",
            "password": "TempPatPassword1!",
            "doctor_id": new_doc_id,
            "age": 50,
            "gender": "M",
            "phone": "0550001111",
            "chronic_conditions": ["السكري"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201
    new_pat_id = res.get_json()["user"]["id"]

    # Add 1 ESCALATED case and 1 ARCHIVED case for pat_zaid under new_doc_id
    with app.app_context():
        db.session.add_all(
            [
                Case(
                    id="open_case_1",
                    patient_id=new_pat_id,
                    doctor_id=new_doc_id,
                    age_at_test=50,
                    gender_at_test="M",
                    status="ESCALATED",
                    priority="HIGH",
                    signal=json.dumps([2000.0] * 10),
                    confidence=0.80,
                    raw_prediction="NORMAL",
                    prediction="UNCERTAIN",
                    bpm=85,
                    rate_status="NORMAL",
                ),
                Case(
                    id="arch_case_1",
                    patient_id=new_pat_id,
                    doctor_id=new_doc_id,
                    age_at_test=50,
                    gender_at_test="M",
                    status="ARCHIVED",
                    priority="NORMAL",
                    signal=json.dumps([2000.0] * 10),
                    confidence=0.90,
                    raw_prediction="NORMAL",
                    prediction="UNCERTAIN",
                    bpm=75,
                    rate_status="NORMAL",
                    doctor_decision="HEALTHY",
                    reviewed_by_id=new_doc_id,
                ),
            ]
        )
        db.session.commit()

    # Deactivating doctor with active assigned patients is BLOCKED (409)
    res = admin_client.patch(
        f"/api/admin/users/{new_doc_id}/status",
        json={"is_active": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 409
    assert res.get_json()["code"] == "DOCTOR_HAS_ACTIVE_PATIENTS"

    # Reassign patient to dr_ahmed -> transfers open ESCALATED case, leaves ARCHIVED untouched
    res = admin_client.patch(
        f"/api/admin/patients/{new_pat_id}/reassign",
        json={"doctor_id": seeded_users["doc1_id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    assert res.get_json()["transferred_cases"] == 1

    with app.app_context():
        assert db.session.get(PatientProfile, new_pat_id).doctor_id == seeded_users["doc1_id"]
        assert db.session.get(Case, "open_case_1").doctor_id == seeded_users["doc1_id"]
        assert db.session.get(Case, "arch_case_1").doctor_id == new_doc_id
        assert db.session.get(Case, "arch_case_1").reviewed_by_id == new_doc_id

    # Now deactivating new_doc_id succeeds
    res = admin_client.patch(
        f"/api/admin/users/{new_doc_id}/status",
        json={"is_active": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200

    # Admin cannot deactivate self
    res = admin_client.patch(
        f"/api/admin/users/{seeded_users['admin_id']}/status",
        json={"is_active": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 400


def test_doctor_creates_patient_and_profile_field_permissions(app, seeded_users):
    doc_client = app.test_client()
    doc_csrf, _ = login_client(doc_client, "dr_ahmed", "DoctorPass123!")

    # Doctor creates patient (auto-linked to dr_ahmed)
    res = doc_client.post(
        "/api/doctor/patients",
        json={
            "username": "pat_noor",
            "full_name": "Noor Salem",
            "password": "TempNoorPass123!",
            "age": 34,
            "gender": "F",
            "phone": "0551234567",
            "chronic_conditions": ["ارتفاع ضغط الدم"],
        },
        headers={"X-CSRF-Token": doc_csrf},
    )
    assert res.status_code == 201
    pat_id = res.get_json()["user"]["id"]
    assert res.get_json()["profile"]["doctor_id"] == seeded_users["doc1_id"]

    # Doctor edits clinical fields
    res = doc_client.patch(
        f"/api/doctor/patients/{pat_id}",
        json={"age": 35, "chronic_conditions": ["ارتفاع ضغط الدم", "السكري"], "clinical_notes": "Follow-up in 2 weeks"},
        headers={"X-CSRF-Token": doc_csrf},
    )
    assert res.status_code == 200
    assert res.get_json()["profile"]["age"] == 35
    assert "السكري" in res.get_json()["profile"]["chronic_conditions"]

    # Patient logs in, changes temp password, views profile, edits contact only
    pat_client = app.test_client()
    pat_csrf, _ = login_client(pat_client, "pat_noor", "TempNoorPass123!")
    res = pat_client.post(
        "/api/auth/change-password",
        json={"current_password": "TempNoorPass123!", "new_password": "NoorNewSecret99!"},
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert res.status_code == 200
    pat_csrf = res.get_json()["csrf_token"]

    prof_res = pat_client.get("/api/patient/profile")
    assert prof_res.status_code == 200
    assert prof_res.get_json()["doctor"]["full_name"] == "Dr. Ahmed"
    assert prof_res.get_json()["profile"]["age"] == 35

    # Patient attempting to edit clinical field (age/gender/doctor_id) is rejected
    bad_edit = pat_client.patch(
        "/api/patient/profile",
        json={"age": 99, "phone": "0559999999"},
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert bad_edit.status_code == 400

    # Patient editing contact fields succeeds
    good_edit = pat_client.patch(
        "/api/patient/profile",
        json={
            "phone": "0559999999",
            "emergency_contact_name": "Salem",
            "emergency_contact_phone": "0558888888",
        },
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert good_edit.status_code == 200
    assert good_edit.get_json()["profile"]["phone"] == "0559999999"
    assert good_edit.get_json()["profile"]["emergency_contact_name"] == "Salem"
