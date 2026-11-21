"""
scripts/import_json.py - One-time, idempotent migration script to import existing
patient_history.json and consent_log.json into the SQLite database.
"""
import os
import sys
import json

# Add project root to sys.path so modules can be imported directly
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import app
from db import db
from models import PatientHistory, ConsentLog
from sqlalchemy import select

def import_history(session):
    history_file = os.path.join(BASE_DIR, "patient_history.json")
    if not os.path.exists(history_file):
        print(f"ℹ️ No {history_file} found to import.")
        return 0

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to parse {history_file}: {e}")
        return 0

    imported_count = 0
    # Structure of patient_history.json: { "patient_name": [ { entry... }, ... ], ... }
    for patient_name, entries in data.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            case_id = str(entry.get("case_id", ""))
            ts = str(entry.get("ts", ""))
            
            # Check for existing record to guarantee idempotency
            existing = session.scalar(
                select(PatientHistory).where(
                    PatientHistory.patient_name == patient_name,
                    PatientHistory.case_id == case_id,
                    PatientHistory.timestamp == ts
                )
            )
            if existing:
                continue

            rec = PatientHistory(
                case_id=case_id,
                patient_name=patient_name,
                timestamp=ts,
                age=str(entry.get("age", "--")),
                gender=str(entry.get("gender", "U")),
                status=str(entry.get("status", "SUCCESS")),
                prediction=str(entry.get("prediction", "N/A")),
                bpm=int(entry.get("bpm", 72)),
                rate_status=str(entry.get("rate_status", "NORMAL")),
                confidence=entry.get("confidence"),
                message=entry.get("message"),
                ai_attention_note=entry.get("ai_attention_note")
            )
            session.add(rec)
            imported_count += 1

    session.commit()
    print(f"✅ Imported {imported_count} records from patient_history.json.")
    return imported_count

def import_consent(session):
    consent_file = os.path.join(BASE_DIR, "consent_log.json")
    if not os.path.exists(consent_file):
        print(f"ℹ️ No {consent_file} found to import.")
        return 0

    try:
        with open(consent_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to parse {consent_file}: {e}")
        return 0

    imported_count = 0
    # Structure: { "username": { "consent": true, "ts": "2026-..." }, ... }
    for user, val in data.items():
        existing = session.get(ConsentLog, user)
        if existing:
            continue

        consent_val = True
        ts_val = ""
        if isinstance(val, dict):
            consent_val = bool(val.get("consent", True))
            ts_val = str(val.get("ts", ""))
        
        rec = ConsentLog(user=user, consent=consent_val, timestamp=ts_val)
        session.add(rec)
        imported_count += 1

    session.commit()
    print(f"✅ Imported {imported_count} records from consent_log.json.")
    return imported_count

def main():
    print("🚀 Running JSON -> SQLite Idempotent Importer...")
    with app.app_context():
        db.create_all()
        h_count = import_history(db.session)
        c_count = import_consent(db.session)
        print(f"🎉 Import finished. Total new: {h_count} history entries, {c_count} consent entries.")

if __name__ == "__main__":
    main()
