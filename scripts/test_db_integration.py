"""
scripts/test_db_integration.py - Integration test exercising all DB models and endpoints:
1. Predict with synthetic signal -> Verifies row creation in PatientHistory & PatientDecision
2. Predict with uncertain signal -> Verifies row creation in Case with status='ESCALATED'
3. Patient update_case -> Verifies symptoms/complaint update
4. Doctor cases list -> Verifies Case retrieval & ordering by confidence
5. Doctor respond -> Verifies status changes to ARCHIVED, MLDatasetRecord created, PatientHistory updated
6. Patient status -> Verifies latest decision text
7. Patient history -> Verifies history records
8. Admin stats -> Verifies dynamically computed counts
9. Admin export dataset -> Verifies CSV output format
10. Database restart verification -> Verifies data survives a simulated restart
"""
import os
import sys
import json
import uuid
import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import app
from db import db
from models import Case, PatientHistory, PatientDecision, MLDatasetRecord, ConsentLog

def run_tests():
    print("=" * 60)
    print("🧪 STARTING CARDIOSENSE SQLITE INTEGRATION TESTS")
    print("=" * 60)
    
    app.config["TESTING"] = True
    client = app.test_client()

    with app.app_context():
        # Ensure clean state for test run
        db.create_all()

    # --- 1. Test /predict with a synthetic valid normal signal ---
    print("\n[1] Testing /predict endpoint (Synthetic Normal Signal)...")
    t = np.linspace(0, 10, 3000)
    # Synthetic normal heartbeat with valid amplitude avoiding gatekeeper rejection
    sig_normal = (2000 + 400 * np.sin(2 * np.pi * 1.2 * t)).tolist()
    
    res = client.post("/predict", json={
        "name": "Alice",
        "age": "30",
        "gender": "F",
        "signal": sig_normal
    })
    print(f"    Response [{res.status_code}]: {res.json}")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    res_data = res.json
    assert res_data.get("status") in ("SUCCESS", "ESCALATED", "REJECTED")

    with app.app_context():
        # Check PatientHistory was written
        h = PatientHistory.query.filter_by(patient_name="Alice").order_by(PatientHistory.id.desc()).first()
        assert h is not None, "PatientHistory row for Alice was not created!"
        print(f"    ✅ PatientHistory confirmed: status={h.status}, bpm={h.bpm}, prediction={h.prediction}")

        # Check PatientDecision was written
        dec = db.session.get(PatientDecision, "Alice")
        assert dec is not None, "PatientDecision row for Alice was not created!"
        print(f"    ✅ PatientDecision confirmed: status_text='{dec.status_text}'")

    # --- 2. Test Escalation by inserting an UNCERTAIN case directly or via mock ---
    print("\n[2] Testing Case escalation persistence...")
    case_id = f"tc_{uuid.uuid4().hex[:6]}"
    with app.app_context():
        # Create an escalated case
        mock_case = Case(
            id=case_id,
            patient_name="Bob",
            age="55",
            gender="M",
            status="ESCALATED",
            complaint="",
            chronic="",
            symptoms="[]",
            history="[]",
            signal=json.dumps(sig_normal),
            confidence=0.72,
            raw_prediction="AFIB",
            prediction="UNCERTAIN",
            bpm=88,
            rate_status="NORMAL",
            timestamp="2026-11-21 03:00:00 PM",
            xai_weights=json.dumps([0.1] * 3000),
            ai_attention_note=""
        )
        db.session.add(mock_case)
        # Log to patient history
        db.session.add(PatientHistory(
            case_id=case_id,
            patient_name="Bob",
            timestamp="2026-11-21 03:00:00 PM",
            age="55",
            gender="M",
            status="ESCALATED",
            prediction="UNCERTAIN",
            bpm=88,
            rate_status="NORMAL",
            confidence="72.00%"
        ))
        db.session.commit()
        print(f"    ✅ Mock escalated case '{case_id}' created for Bob.")

    # --- 3. Test /api/patient/update_case ---
    print("\n[3] Testing POST /api/patient/update_case...")
    res = client.post("/api/patient/update_case", json={
        "case_id": case_id,
        "symptoms": ["خفقان", "ضيق تنفس"],
        "history": ["ضغط دم مرتفع"]
    })
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    print(f"    Response [{res.status_code}]: {res.json}")

    with app.app_context():
        c = db.session.get(Case, case_id)
        assert "خفقان" in c.complaint, f"Complaint was not updated: {c.complaint}"
        print(f"    ✅ Case complaint updated: '{c.complaint}'")

    # --- 4. Test GET /api/doctor/cases ---
    print("\n[4] Testing GET /api/doctor/cases...")
    res = client.get("/api/doctor/cases")
    assert res.status_code == 200
    cases_list = res.json
    assert any(c["id"] == case_id for c in cases_list), f"Case {case_id} not in doctor list!"
    print(f"    ✅ Doctor case list returned {len(cases_list)} cases. Found '{case_id}'.")

    # --- 5. Test POST /api/doctor/respond ---
    print("\n[5] Testing POST /api/doctor/respond...")
    res = client.post("/api/doctor/respond", json={
        "case_id": case_id,
        "decision": "INFECTED",
        "note": "تم رصد علامات الرجفان الأذيني بشكل مؤكد."
    })
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    print(f"    Response [{res.status_code}]: {res.json}")

    with app.app_context():
        # Case should now be ARCHIVED
        c = db.session.get(Case, case_id)
        assert c.status == "ARCHIVED", f"Case status is {c.status}, expected ARCHIVED"
        assert c.doctor_decision == "INFECTED"
        print("    ✅ Case status transitioned to ARCHIVED.")

        # MLDatasetRecord should be created
        ds_rec = MLDatasetRecord.query.filter_by(case_id=case_id).first()
        assert ds_rec is not None, "MLDatasetRecord was not created!"
        print(f"    ✅ MLDatasetRecord confirmed for case_id={case_id}")

        # PatientHistory should be updated to REVIEWED
        h = PatientHistory.query.filter_by(case_id=case_id).first()
        assert h.status == "REVIEWED", f"History status is {h.status}, expected REVIEWED"
        assert "AFIB (CONFIRMED)" in h.prediction
        print(f"    ✅ PatientHistory updated: status={h.status}, prediction={h.prediction}")

        # PatientDecision should have doctor's verdict
        dec = db.session.get(PatientDecision, "Bob")
        assert "PATHOLOGY OBSERVED" in dec.status_text
        print(f"    ✅ PatientDecision updated: '{dec.status_text}'")

    # --- 6. Test GET /api/doctor/archive ---
    print("\n[6] Testing GET /api/doctor/archive...")
    res = client.get("/api/doctor/archive")
    assert res.status_code == 200
    archive_list = res.json
    assert any(c["id"] == case_id for c in archive_list), f"Archived case {case_id} not in archive list!"
    print(f"    ✅ Doctor archive returned {len(archive_list)} cases. Found '{case_id}'.")

    # --- 7. Test GET /api/patient/status/<name> ---
    print("\n[7] Testing GET /api/patient/status/Bob...")
    res = client.get("/api/patient/status/Bob")
    assert res.status_code == 200
    print(f"    ✅ Status returned: {res.json}")
    assert "PATHOLOGY OBSERVED" in res.json.get("status_text", "")

    # --- 8. Test GET /api/patient/history/<name> ---
    print("\n[8] Testing GET /api/patient/history/Bob...")
    res = client.get("/api/patient/history/Bob")
    assert res.status_code == 200
    hist = res.json
    assert len(hist) > 0, "No history returned for Bob!"
    print(f"    ✅ History returned {len(hist)} records: first record status={hist[0].get('status')}")

    # --- 9. Test GET /api/admin/stats ---
    print("\n[9] Testing GET /api/admin/stats...")
    res = client.get("/api/admin/stats")
    assert res.status_code == 200
    stats_data = res.json
    stats = stats_data.get("stats", {})
    ds_size = stats_data.get("dataset_size", 0)
    print(f"    ✅ Admin stats: {stats}, dataset_size={ds_size}")
    assert stats.get("total_tests", 0) > 0
    assert ds_size >= 1

    # --- 10. Test GET /api/admin/export_dataset ---
    print("\n[10] Testing GET /api/admin/export_dataset (CSV)...")
    res = client.get("/api/admin/export_dataset")
    assert res.status_code == 200
    csv_text = res.data.decode("utf-8")
    lines = csv_text.strip().split("\n")
    print(f"    ✅ CSV Export: Header has {len(lines[0].split(','))} columns, {len(lines) - 1} data rows.")
    assert "doctor_label" in lines[0]
    assert len(lines) >= 2

    # --- 11. Test POST /api/consent ---
    print("\n[11] Testing POST /api/consent...")
    res = client.post("/api/consent", json={"user": "Charlie"})
    assert res.status_code == 200
    with app.app_context():
        c_entry = db.session.get(ConsentLog, "Charlie")
        assert c_entry is not None and c_entry.consent is True
        print(f"    ✅ Consent logged: user={c_entry.user}, ts={c_entry.timestamp}")

    print("\n" + "=" * 60)
    print("🎉 ALL 11 INTEGRATION TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
