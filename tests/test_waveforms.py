"""
tests/test_waveforms.py - Tests for EcgRecording persistence, /predict validation & atomicity,
ownership/RBAC on GET /api/history/<id>/waveform, min/max decimation, case review persistence,
and ml_dataset recording_id references.
"""
import json
import math
import numpy as np
import pytest
from sqlalchemy import select, func

import app as main_app
from db import db
from models import (
    Case,
    PatientHistory,
    EcgRecording,
    MLDatasetRecord,
    encode_float32_le,
    decode_float32_le,
)
from tests.conftest import login_client


def _synthetic_valid_signal(offset: float = 0.0):
    t = np.linspace(0, 10, 3000)
    return (2000.0 + offset + 400.0 * np.sin(2 * np.pi * 1.2 * t)).tolist()


def _synthetic_noisy_signal():
    # Fails smart_gatekeeper: std < 5.0 (Leads Disconnected) while still 3000 finite numbers
    return [2000.0 + (i % 2) * 0.5 for i in range(3000)]


def test_ecg_recording_float32_round_trip(app, seeded_users):
    rng = np.random.default_rng(42)
    sig = rng.uniform(-1500.0, 3500.0, size=3000).astype(np.float64)
    att = rng.uniform(0.0, 1.0, size=3000).astype(np.float64)

    with app.app_context():
        hist = PatientHistory(
            case_id="rt_case_1",
            patient_id=seeded_users["pat1_id"],
            age="45",
            gender="M",
            status="SUCCESS",
            prediction="NORMAL",
            bpm=72,
            rate_status="NORMAL",
        )
        db.session.add(hist)
        db.session.flush()

        rec = EcgRecording(
            history_id=hist.id,
            case_id=None,
            patient_id=seeded_users["pat1_id"],
            sample_rate_hz=300,
            n_samples=3000,
            signal=encode_float32_le(sig),
            attention=encode_float32_le(att),
        )
        db.session.add(rec)
        db.session.commit()
        rec_id = rec.id

        db.session.expire_all()
        loaded = db.session.get(EcgRecording, rec_id)
        assert loaded is not None
        assert loaded.n_samples == 3000
        assert loaded.sample_rate_hz == 300
        assert np.allclose(decode_float32_le(loaded.signal), sig.astype(np.float32), atol=1e-5)
        assert np.allclose(decode_float32_le(loaded.attention), att.astype(np.float32), atol=1e-6)


def test_predict_persists_recordings_for_success_rejected_and_escalated(app, seeded_users, monkeypatch):
    pat_client = app.test_client()
    pat_csrf, _ = login_client(pat_client, "pat_ali", "PatientPass123!")

    # 1. SUCCESS
    monkeypatch.setattr(
        main_app,
        "predict",
        lambda sig: {
            "prediction": "NORMAL",
            "probability": 0.02,
            "confidence": 0.98,
            "xai_weights": [0.15] * 3000,
        },
    )
    res_ok = pat_client.post(
        "/predict",
        json={"signal": _synthetic_valid_signal(1.0)},
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert res_ok.status_code == 200
    assert res_ok.get_json()["status"] == "SUCCESS"

    # 2. REJECTED by gatekeeper (signal is still valid 3000 finite numbers -> recording stored, no attention)
    res_rej = pat_client.post(
        "/predict",
        json={"signal": _synthetic_noisy_signal()},
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert res_rej.status_code == 200
    assert res_rej.get_json()["status"] == "REJECTED"

    # 3. ESCALATED
    monkeypatch.setattr(
        main_app,
        "predict",
        lambda sig: {
            "prediction": "UNCERTAIN",
            "probability": 0.61,
            "confidence": 0.61,
            "xai_weights": [0.75] * 3000,
        },
    )
    res_esc = pat_client.post(
        "/predict",
        json={"signal": _synthetic_valid_signal(5.0)},
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert res_esc.status_code == 200
    esc_body = res_esc.get_json()
    assert esc_body["status"] == "ESCALATED"
    case_id = esc_body["case_id"]

    with app.app_context():
        histories = db.session.scalars(
            select(PatientHistory).order_by(PatientHistory.id.asc())
        ).all()
        assert [h.status for h in histories] == ["SUCCESS", "REJECTED", "ESCALATED"]
        recs = db.session.scalars(select(EcgRecording).order_by(EcgRecording.id.asc())).all()
        assert len(recs) == 3
        assert recs[0].history_id == histories[0].id and recs[0].attention is not None
        assert recs[1].history_id == histories[1].id and recs[1].attention is None
        assert recs[2].history_id == histories[2].id and recs[2].case_id == case_id
        assert recs[2].attention is not None

        # Case does not duplicate signal JSON; detail reads from EcgRecording
        c = db.session.get(Case, case_id)
        assert c.signal == "[]"
        assert c.xai_weights == "[]"

    # Doctor review & MLDatasetRecord references recording_id without copying signal
    doc_client = app.test_client()
    doc_csrf, _ = login_client(doc_client, "dr_ahmed", "DoctorPass123!")

    cases = doc_client.get("/api/doctor/cases").get_json()
    assert len(cases) == 1
    assert cases[0]["has_waveform"] is True
    assert "signal" not in cases[0]

    detail = doc_client.get(f"/api/doctor/cases/{case_id}").get_json()
    assert detail["has_waveform"] is True
    assert len(detail["signal"]) == 3000
    assert len(detail["xai_weights"]) == 3000
    assert abs(detail["xai_weights"][0] - 0.75) < 1e-5

    resp = doc_client.post(
        "/api/doctor/respond",
        json={"case_id": case_id, "decision": "INFECTED", "note": "Confirmed"},
        headers={"X-CSRF-Token": doc_csrf},
    )
    assert resp.status_code == 200

    with app.app_context():
        ml_rec = db.session.scalar(select(MLDatasetRecord).where(MLDatasetRecord.case_id == case_id))
        assert ml_rec is not None
        assert ml_rec.recording_id is not None
        assert ml_rec.signal is None
        assert len(ml_rec.get_signal_list()) == 3000

    # Admin CSV export still streams 3000 signal samples identically
    admin_client = app.test_client()
    login_client(admin_client, "admin1", "AdminPass123!")
    csv_res = admin_client.get("/api/admin/export_dataset")
    assert csv_res.status_code == 200
    lines = csv_res.data.decode("utf-8").splitlines()
    assert len(lines) == 2
    assert len(lines[1].split(",")) == 5 + 3000


def test_predict_atomic_rollback_on_forced_failure(app, seeded_users, monkeypatch):
    monkeypatch.setattr(
        main_app,
        "predict",
        lambda sig: {
            "prediction": "UNCERTAIN",
            "probability": 0.60,
            "confidence": 0.60,
            "xai_weights": [0.3] * 3000,
        },
    )

    pat_client = app.test_client()
    pat_csrf, _ = login_client(pat_client, "pat_ali", "PatientPass123!")

    # Force commit failure AFTER login so /predict transaction must roll back completely
    def _boom():
        raise RuntimeError("Simulated DB commit failure")

    monkeypatch.setattr(db.session, "commit", _boom)

    res = pat_client.post(
        "/predict",
        json={"signal": _synthetic_valid_signal()},
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert res.status_code == 500

    with app.app_context():
        assert db.session.scalar(select(func.count(PatientHistory.id))) == 0
        assert db.session.scalar(select(func.count(EcgRecording.id))) == 0
        assert db.session.scalar(select(func.count(Case.id))) == 0


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"signal": [0.0] * 100},  # wrong length
        {"signal": [0.0] * 3001},  # wrong length
        {"signal": "not-a-list"},  # wrong type
        {"signal": ["1.0"] * 3000},  # strings
        {"signal": [True] * 3000},  # booleans
        {"signal": [0.0] * 2999 + [float("nan")]},  # NaN
        {"signal": [0.0] * 2999 + [float("inf")]},  # +Inf
        {"signal": [0.0] * 2999 + [float("-inf")]},  # -Inf
    ],
)
def test_predict_invalid_signal_returns_400_and_stores_nothing(app, seeded_users, bad_payload):
    pat_client = app.test_client()
    pat_csrf, _ = login_client(pat_client, "pat_ali", "PatientPass123!")

    # Send raw JSON string because Python json.dumps allows NaN/Inf by default (matching malformed clients)
    raw = json.dumps(bad_payload)
    res = pat_client.post(
        "/predict",
        data=raw,
        content_type="application/json",
        headers={"X-CSRF-Token": pat_csrf},
    )
    assert res.status_code == 400
    assert "error" in res.get_json()

    with app.app_context():
        assert db.session.scalar(select(func.count(PatientHistory.id))) == 0
        assert db.session.scalar(select(func.count(EcgRecording.id))) == 0
        assert db.session.scalar(select(func.count(Case.id))) == 0


def test_waveform_ownership_and_role_attention_gating(app, seeded_users, monkeypatch):
    monkeypatch.setattr(
        main_app,
        "predict",
        lambda sig: {
            "prediction": "NORMAL",
            "probability": 0.05,
            "confidence": 0.95,
            "xai_weights": [0.42] * 3000,
        },
    )

    pat1_client = app.test_client()
    pat1_csrf, _ = login_client(pat1_client, "pat_ali", "PatientPass123!")
    assert (
        pat1_client.post(
            "/predict",
            json={"signal": _synthetic_valid_signal()},
            headers={"X-CSRF-Token": pat1_csrf},
        ).status_code
        == 200
    )

    pat1_hist = pat1_client.get("/api/patient/history").get_json()
    assert len(pat1_hist) == 1
    assert pat1_hist[0]["has_waveform"] is True
    assert "samples" not in pat1_hist[0]
    hist_id = pat1_hist[0]["id"]

    # Unauthenticated -> 401
    anon_client = app.test_client()
    assert anon_client.get(f"/api/history/{hist_id}/waveform").status_code == 401

    # Patient A reads own waveform -> 200, attention MUST be null
    wf_pat1 = pat1_client.get(f"/api/history/{hist_id}/waveform")
    assert wf_pat1.status_code == 200
    body_pat1 = wf_pat1.get_json()
    assert body_pat1["history_id"] == hist_id
    assert body_pat1["sample_rate_hz"] == 300
    assert body_pat1["n_samples"] == 3000
    assert body_pat1["duration_s"] == 10.0
    assert len(body_pat1["samples"]) == 3000
    assert body_pat1["attention"] is None

    # Patient B cannot read Patient A's waveform -> 404 (no existence leak)
    pat2_client = app.test_client()
    login_client(pat2_client, "pat_mona", "PatientPass123!")
    assert pat2_client.get(f"/api/history/{hist_id}/waveform").status_code == 404

    # Doctor 2 (not assigned to pat_ali) cannot read -> 404
    doc2_client = app.test_client()
    login_client(doc2_client, "dr_sara", "DoctorPass123!")
    assert doc2_client.get(f"/api/history/{hist_id}/waveform").status_code == 404

    # Doctor 1 (assigned to pat_ali) CAN read and receives attention
    doc1_client = app.test_client()
    login_client(doc1_client, "dr_ahmed", "DoctorPass123!")
    wf_doc1 = doc1_client.get(f"/api/history/{hist_id}/waveform")
    assert wf_doc1.status_code == 200
    body_doc1 = wf_doc1.get_json()
    assert len(body_doc1["samples"]) == 3000
    assert body_doc1["attention"] is not None
    assert len(body_doc1["attention"]) == 3000
    assert abs(body_doc1["attention"][0] - 0.42) < 1e-5

    # Doctor 1 patient history list includes has_waveform=True without samples
    doc_hist = doc1_client.get(f"/api/doctor/patients/{seeded_users['pat1_id']}/history").get_json()
    assert doc_hist[0]["has_waveform"] is True
    assert "samples" not in doc_hist[0]

    # Admin CAN read any patient's waveform and receives attention
    admin_client = app.test_client()
    login_client(admin_client, "admin1", "AdminPass123!")
    wf_admin = admin_client.get(f"/api/history/{hist_id}/waveform")
    assert wf_admin.status_code == 200
    assert wf_admin.get_json()["attention"] is not None


def test_waveform_max_points_decimation_preserves_min_and_max(app, seeded_users):
    sig = np.zeros(3000, dtype=np.float32)
    sig[123] = -987.5
    sig[1777] = 3456.25
    att = np.linspace(0.0, 1.0, 3000, dtype=np.float32)

    with app.app_context():
        hist = PatientHistory(
            case_id="dec_case",
            patient_id=seeded_users["pat1_id"],
            age="45",
            gender="M",
            status="SUCCESS",
            prediction="NORMAL",
            bpm=70,
            rate_status="NORMAL",
        )
        db.session.add(hist)
        db.session.flush()
        db.session.add(
            EcgRecording(
                history_id=hist.id,
                patient_id=seeded_users["pat1_id"],
                sample_rate_hz=300,
                n_samples=3000,
                signal=encode_float32_le(sig),
                attention=encode_float32_le(att),
            )
        )
        db.session.commit()
        hist_id = hist.id

    doc1_client = app.test_client()
    login_client(doc1_client, "dr_ahmed", "DoctorPass123!")

    # Decimate to 200 points
    res = doc1_client.get(f"/api/history/{hist_id}/waveform?max_points=200")
    assert res.status_code == 200
    body = res.get_json()
    assert len(body["samples"]) <= 200
    assert len(body["attention"]) == len(body["samples"])
    assert math.isclose(min(body["samples"]), -987.5, abs_tol=1e-4)
    assert math.isclose(max(body["samples"]), 3456.25, abs_tol=1e-4)
    assert body["n_samples"] == 3000

    # Cap max_points at n_samples
    res_cap = doc1_client.get(f"/api/history/{hist_id}/waveform?max_points=99999")
    assert res_cap.status_code == 200
    assert len(res_cap.get_json()["samples"]) == 3000

    # Invalid max_points -> 400
    assert doc1_client.get(f"/api/history/{hist_id}/waveform?max_points=0").status_code == 400
    assert doc1_client.get(f"/api/history/{hist_id}/waveform?max_points=-10").status_code == 400
    assert doc1_client.get(f"/api/history/{hist_id}/waveform?max_points=abc").status_code == 400


def test_history_without_recording_returns_has_waveform_false_and_404(app, seeded_users):
    with app.app_context():
        old_hist = PatientHistory(
            case_id="legacy_case",
            patient_id=seeded_users["pat1_id"],
            age="45",
            gender="M",
            status="SUCCESS",
            prediction="NORMAL",
            bpm=74,
            rate_status="NORMAL",
        )
        db.session.add(old_hist)
        db.session.commit()
        old_id = old_hist.id

    pat1_client = app.test_client()
    login_client(pat1_client, "pat_ali", "PatientPass123!")

    hist = pat1_client.get("/api/patient/history").get_json()
    assert len(hist) == 1
    assert hist[0]["id"] == old_id
    assert hist[0]["has_waveform"] is False

    wf_res = pat1_client.get(f"/api/history/{old_id}/waveform")
    assert wf_res.status_code == 404
