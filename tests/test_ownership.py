"""
tests/test_ownership.py - Comprehensive RBAC and object-level ownership tests.
"""
import json
from db import db
from models import Case
from tests.conftest import login_client


def _seed_case(app, case_id: str, patient_id: int, doctor_id: int):
    with app.app_context():
        c = Case(
            id=case_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            age_at_test=45,
            gender_at_test="M",
            status="ESCALATED",
            priority="URGENT",
            signal=json.dumps([2000.0] * 100),
            confidence=0.65,
            raw_prediction="AFIB",
            prediction="UNCERTAIN",
            bpm=110,
            rate_status="TACHYCARDIA",
            xai_weights=json.dumps([0.1] * 100),
        )
        db.session.add(c)
        db.session.commit()


def test_all_protected_endpoints_reject_anonymous(client):
    endpoints = [
        ("GET", "/api/auth/me"),
        ("GET", "/api/consent"),
        ("POST", "/api/consent"),
        ("GET", "/api/patient/profile"),
        ("PATCH", "/api/patient/profile"),
        ("POST", "/predict"),
        ("POST", "/api/patient/update_case"),
        ("GET", "/api/patient/status"),
        ("GET", "/api/patient/history"),
        ("GET", "/api/history/1/waveform"),
        ("GET", "/api/doctor/dashboard"),
        ("GET", "/api/doctor/cases"),
        ("GET", "/api/doctor/archive"),
        ("GET", "/api/doctor/cases/abc"),
        ("POST", "/api/doctor/respond"),
        ("GET", "/api/doctor/patients"),
        ("POST", "/api/doctor/patients"),
        ("GET", "/api/doctor/patients/1"),
        ("PATCH", "/api/doctor/patients/1"),
        ("GET", "/api/doctor/patients/1/history"),
        ("GET", "/api/admin/stats"),
        ("GET", "/api/admin/export_dataset"),
        ("GET", "/api/admin/users"),
        ("POST", "/api/admin/doctors"),
        ("POST", "/api/admin/patients"),
        ("PATCH", "/api/admin/users/1/status"),
        ("PATCH", "/api/admin/patients/1/reassign"),
        ("GET", "/api/admin/audit-logs"),
    ]
    for method, path in endpoints:
        res = client.open(path, method=method, json={})
        assert res.status_code == 401, f"{method} {path} returned {res.status_code}, expected 401"


def test_doctor_object_ownership_isolation(app, seeded_users):
    _seed_case(app, "case_doc1", seeded_users["pat1_id"], seeded_users["doc1_id"])
    _seed_case(app, "case_doc2", seeded_users["pat2_id"], seeded_users["doc2_id"])

    doc1_client = app.test_client()
    doc1_csrf, _ = login_client(doc1_client, "dr_ahmed", "DoctorPass123!")

    # Doctor 1 only sees case_doc1 in queue
    cases = doc1_client.get("/api/doctor/cases").get_json()
    assert [c["id"] for c in cases] == ["case_doc1"]

    # Doctor 1 cannot view or respond to Doctor 2's case
    assert doc1_client.get("/api/doctor/cases/case_doc2").status_code == 403
    res = doc1_client.post(
        "/api/doctor/respond",
        json={"case_id": "case_doc2", "decision": "HEALTHY"},
        headers={"X-CSRF-Token": doc1_csrf},
    )
    assert res.status_code == 403

    # Doctor 1 only sees pat1 in patient list
    patients = doc1_client.get("/api/doctor/patients").get_json()
    assert [p["id"] for p in patients] == [seeded_users["pat1_id"]]

    # Doctor 1 cannot view/edit/read history of pat2
    assert doc1_client.get(f"/api/doctor/patients/{seeded_users['pat2_id']}").status_code == 403
    assert (
        doc1_client.patch(
            f"/api/doctor/patients/{seeded_users['pat2_id']}",
            json={"age": 70},
            headers={"X-CSRF-Token": doc1_csrf},
        ).status_code
        == 403
    )
    assert doc1_client.get(f"/api/doctor/patients/{seeded_users['pat2_id']}/history").status_code == 403

    # Doctor 1 cannot access admin or patient-only endpoints
    assert doc1_client.get("/api/admin/stats").status_code == 403
    assert doc1_client.get("/api/patient/profile").status_code == 403


def test_patient_cannot_access_other_patients_or_doctor_admin(app, seeded_users):
    _seed_case(app, "case_pat1", seeded_users["pat1_id"], seeded_users["doc1_id"])
    _seed_case(app, "case_pat2", seeded_users["pat2_id"], seeded_users["doc2_id"])

    pat1_client = app.test_client()
    pat1_csrf, _ = login_client(pat1_client, "pat_ali", "PatientPass123!")

    # Cannot update another patient's case
    res = pat1_client.post(
        "/api/patient/update_case",
        json={"case_id": "case_pat2", "symptoms": ["خفقان"]},
        headers={"X-CSRF-Token": pat1_csrf},
    )
    assert res.status_code == 403

    # Cannot access doctor or admin endpoints
    assert pat1_client.get("/api/doctor/cases").status_code == 403
    assert pat1_client.get("/api/admin/users").status_code == 403


def test_admin_is_readonly_on_doctor_respond(app, seeded_users):
    _seed_case(app, "case_doc1", seeded_users["pat1_id"], seeded_users["doc1_id"])

    admin_client = app.test_client()
    admin_csrf, _ = login_client(admin_client, "admin1", "AdminPass123!")

    # Admin can view cases and case detail
    assert admin_client.get("/api/doctor/cases").status_code == 200
    assert admin_client.get("/api/doctor/cases/case_doc1").status_code == 200

    # Admin cannot submit clinical verdicts (doctor-only)
    res = admin_client.post(
        "/api/doctor/respond",
        json={"case_id": "case_doc1", "decision": "HEALTHY"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert res.status_code == 403
