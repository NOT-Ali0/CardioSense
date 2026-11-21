"""
tests/test_clinical_flow.py - End-to-end clinical ECG workflow tests:
consent, /predict using profile age/gender, escalation routing, doctor KPIs/filters,
review verdict, ML dataset export, and admin stats.
"""
import numpy as np
import app as main_app
from db import db
from models import Case, PatientHistory, MLDatasetRecord
from tests.conftest import login_client


def _synthetic_valid_signal():
    t = np.linspace(0, 10, 3000)
    return (2000.0 + 400.0 * np.sin(2 * np.pi * 1.2 * t)).tolist()


def test_consent_db_persistence(app, seeded_users):
    pat_client = app.test_client()
    csrf, data = login_client(pat_client, "pat_ali", "PatientPass123!")
    assert data["consent_accepted"] is False

    res = pat_client.get("/api/consent")
    assert res.status_code == 200
    assert res.get_json()["consent"] is False

    res = pat_client.post("/api/consent", json={}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    assert res.get_json()["consent"] is True

    me = pat_client.get("/api/auth/me").get_json()
    assert me["consent_accepted"] is True


def test_predict_uses_profile_and_routes_to_linked_doctor(app, seeded_users, monkeypatch):
    # Force an UNCERTAIN prediction with AFIB leaning so it escalates as URGENT
    monkeypatch.setattr(
        main_app,
        "predict",
        lambda sig: {
            "prediction": "UNCERTAIN",
            "probability": 0.62,
            "confidence": 0.62,
            "xai_weights": [0.2] * 3000,
        },
    )

    pat_client = app.test_client()
    pat_csrf, _ = login_client(pat_client, "pat_ali", "PatientPass123!")

    # Send spoofed name/age/gender in body — backend MUST ignore them and use pat_ali's profile (45, M)
    res = pat_client.post(
        "/predict",
        json={
            "name": "FakePerson",
            "age": 99,
            "gender": "F",
            "signal": _synthetic_valid_signal(),
        },
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "ESCALATED"
    case_id = body["case_id"]

    with app.app_context():
        c = db.session.get(Case, case_id)
        assert c is not None
        assert c.patient_id == seeded_users["pat1_id"]
        assert c.doctor_id == seeded_users["doc1_id"]
        assert c.age_at_test == 45
        assert c.gender_at_test == "M"
        assert c.priority == "URGENT"

    # Patient submits symptoms only; backend attaches profile chronic_conditions
    upd = pat_client.post(
        "/api/patient/update_case",
        json={"case_id": case_id, "symptoms": ["خفقان/تسارع النبض", "دوار/إرهاق"]},
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert upd.status_code == 200
    with app.app_context():
        c = db.session.get(Case, case_id)
        assert "خفقان" in c.complaint
        assert "ارتفاع ضغط الدم" in c.chronic

    # Doctor 1 checks KPIs, queue summary (no signal array), detail (has signal + profile), and responds
    doc_client = app.test_client()
    doc_csrf, _ = login_client(doc_client, "dr_ahmed", "DoctorPass123!")

    kpi = doc_client.get("/api/doctor/dashboard").get_json()["kpis"]
    assert kpi["pending_cases"] == 1
    assert kpi["urgent_cases"] == 1
    assert kpi["my_patients"] == 1
    assert kpi["reviewed_today"] == 0

    queue = doc_client.get("/api/doctor/cases?priority=URGENT&search=Ali").get_json()
    assert len(queue) == 1
    assert queue[0]["id"] == case_id
    assert "signal" not in queue[0]

    detail = doc_client.get(f"/api/doctor/cases/{case_id}").get_json()
    assert len(detail["signal"]) == 3000
    assert detail["patient_profile"]["age"] == 45

    # Invalid decision rejected
    bad_dec = doc_client.post(
        "/api/doctor/respond",
        json={"case_id": case_id, "decision": "UNKNOWN_LABEL"},
        headers={"X-CSRF-Token": doc_csrf},
    )
    assert bad_dec.status_code == 400

    # Valid decision archives case, updates history & decision, writes MLDatasetRecord
    resp = doc_client.post(
        "/api/doctor/respond",
        json={"case_id": case_id, "decision": "INFECTED", "note": "يرجى مراجعة العيادة"},
        headers={"X-CSRF-Token": doc_csrf},
    )
    assert resp.status_code == 200

    kpi_after = doc_client.get("/api/doctor/dashboard").get_json()["kpis"]
    assert kpi_after["pending_cases"] == 0
    assert kpi_after["reviewed_today"] == 1

    # Patient sees structured doctor note via /api/patient/status & /api/patient/history
    st = pat_client.get("/api/patient/status").get_json()
    assert "AFIB" in st["status_text"]
    assert st["doctor_note"] == "يرجى مراجعة العيادة"

    hist = pat_client.get("/api/patient/history").get_json()
    assert hist[0]["status"] == "REVIEWED"
    assert hist[0]["prediction"] == "AFIB (CONFIRMED)"
    assert hist[0]["doctor_note"] == "يرجى مراجعة العيادة"

    # Doctor patient list includes BPM trend
    plist = doc_client.get("/api/doctor/patients").get_json()
    assert len(plist) == 1
    assert len(plist[0]["bpm_trend"]) == 1

    # Admin checks stats & exports CSV
    admin_client = app.test_client()
    login_client(admin_client, "admin1", "AdminPass123!")
    stats = admin_client.get("/api/admin/stats").get_json()
    assert stats["dataset_size"] == 1
    assert stats["stats"]["escalated_tests"] == 1

    csv_res = admin_client.get("/api/admin/export_dataset")
    assert csv_res.status_code == 200
    assert "doctor_label" in csv_res.data.decode("utf-8").splitlines()[0]
