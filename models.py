"""
models.py - SQLAlchemy 2.0 ORM entity models for CardioSense (v2 FK-normalized schema).
"""
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

import numpy as np
from sqlalchemy import (
    String,
    Integer,
    Float,
    Text,
    Boolean,
    DateTime,
    LargeBinary,
    ForeignKey,
    CheckConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import db


def encode_float32_le(values: Any) -> bytes:
    """Encode a 1D numeric sequence into little-endian float32 bytes."""
    return np.asarray(values, dtype="<f4").tobytes()


def decode_float32_le(blob: Optional[bytes]) -> np.ndarray:
    """Decode little-endian float32 bytes into a 1D numpy float32 array."""
    if not blob:
        return np.empty(0, dtype=np.float32)
    return np.frombuffer(blob, dtype="<f4")


def decimate_minmax(
    samples: np.ndarray,
    max_points: int,
    attention: Optional[np.ndarray] = None,
) -> Tuple[List[float], Optional[List[float]]]:
    """
    Min/max peak-preserving decimation returning at most `max_points` samples.
    Preserves exact global min/max and local peaks; keeps `attention` aligned 1-to-1.
    """
    n = len(samples)
    if n == 0:
        return [], ([] if attention is not None else None)
    if max_points >= n:
        att_list = attention.tolist() if attention is not None else None
        return samples.tolist(), att_list
    if max_points == 1:
        idx = int(np.argmax(samples))
        att_list = [float(attention[idx])] if attention is not None else None
        return [float(samples[idx])], att_list

    n_buckets = max_points // 2
    edges = np.linspace(0, n, n_buckets + 1, dtype=np.int64)
    out_indices: List[int] = []
    for b in range(n_buckets):
        start = int(edges[b])
        end = int(edges[b + 1])
        if start >= end:
            continue
        segment = samples[start:end]
        if end - start == 1:
            out_indices.append(start)
        else:
            rel_min = int(np.argmin(segment))
            rel_max = int(np.argmax(segment))
            if rel_min <= rel_max:
                out_indices.append(start + rel_min)
                out_indices.append(start + rel_max)
            else:
                out_indices.append(start + rel_max)
                out_indices.append(start + rel_min)

    idx_arr = np.asarray(out_indices, dtype=np.int64)
    dec_samples = samples[idx_arr].tolist()
    dec_attention = attention[idx_arr].tolist() if attention is not None else None
    return dec_samples, dec_attention


def utcnow() -> datetime:
    """Return current UTC timestamp as naive datetime for consistent SQLite storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a naive UTC datetime to ISO-8601 string with Z suffix."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_json_loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


class User(db.Model):
    """Core user account table for patients, doctors, and admins."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('patient', 'doctor', 'admin')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    patient_profile: Mapped[Optional["PatientProfile"]] = relationship(
        "PatientProfile",
        foreign_keys="PatientProfile.user_id",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    assigned_patients: Mapped[List["PatientProfile"]] = relationship(
        "PatientProfile",
        foreign_keys="PatientProfile.doctor_id",
        back_populates="doctor",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "full_name": self.full_name,
            "role": self.role,
            "is_active": self.is_active,
            "must_change_password": self.must_change_password,
            "created_at": iso_utc(self.created_at),
            "last_login_at": iso_utc(self.last_login_at),
        }


class PatientProfile(db.Model):
    """Clinical and contact profile for a patient, linked to exactly one doctor."""

    __tablename__ = "patient_profiles"
    __table_args__ = (
        CheckConstraint("age >= 1 AND age <= 130", name="ck_patient_profiles_age"),
        CheckConstraint("gender IN ('M', 'F')", name="ck_patient_profiles_gender"),
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    doctor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    gender: Mapped[str] = mapped_column(String(10), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    emergency_contact_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    emergency_contact_phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    chronic_conditions: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    clinical_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    user: Mapped["User"] = relationship(
        "User", foreign_keys=[user_id], back_populates="patient_profile"
    )
    doctor: Mapped["User"] = relationship(
        "User", foreign_keys=[doctor_id], back_populates="assigned_patients"
    )

    def get_chronic_list(self) -> List[str]:
        val = _safe_json_loads(self.chronic_conditions, [])
        return val if isinstance(val, list) else []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "doctor_id": self.doctor_id,
            "age": self.age,
            "gender": self.gender,
            "phone": self.phone or "",
            "emergency_contact_name": self.emergency_contact_name or "",
            "emergency_contact_phone": self.emergency_contact_phone or "",
            "chronic_conditions": self.get_chronic_list(),
            "clinical_notes": self.clinical_notes or "",
            "updated_at": iso_utc(self.updated_at),
        }


class Case(db.Model):
    """Escalated and archived clinical ECG cases routed to a patient's assigned doctor."""

    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint("status IN ('ESCALATED', 'ARCHIVED')", name="ck_cases_status"),
        CheckConstraint("priority IN ('URGENT', 'HIGH', 'NORMAL')", name="ck_cases_priority"),
        CheckConstraint(
            "doctor_decision IS NULL OR doctor_decision IN ('INFECTED', 'HEALTHY', 'NOISY')",
            name="ck_cases_doctor_decision",
        ),
        Index("ix_cases_doctor_status_priority_created", "doctor_id", "status", "priority", "created_at"),
        Index("ix_cases_doctor_reviewed_at", "doctor_id", "reviewed_at"),
        Index("ix_cases_patient_created", "patient_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    doctor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    age_at_test: Mapped[int] = mapped_column(Integer, nullable=False)
    gender_at_test: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), index=True, default="ESCALATED", nullable=False)
    priority: Mapped[str] = mapped_column(String(16), index=True, default="NORMAL", nullable=False)

    complaint: Mapped[str] = mapped_column(Text, default="", nullable=False)
    chronic: Mapped[str] = mapped_column(Text, default="", nullable=False)
    symptoms: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    history: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    signal: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, index=True, default=0.0, nullable=False)
    raw_prediction: Mapped[str] = mapped_column(String(20), default="NORMAL", nullable=False)
    prediction: Mapped[str] = mapped_column(String(50), default="UNCERTAIN", nullable=False)
    bpm: Mapped[int] = mapped_column(Integer, default=72, nullable=False)
    rate_status: Mapped[str] = mapped_column(String(50), default="NORMAL", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)
    xai_weights: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    ai_attention_note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    doctor_decision: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    doctor_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True, nullable=True)

    patient: Mapped["User"] = relationship("User", foreign_keys=[patient_id])
    doctor: Mapped["User"] = relationship("User", foreign_keys=[doctor_id])
    reviewed_by: Mapped[Optional["User"]] = relationship("User", foreign_keys=[reviewed_by_id])

    def to_summary_dict(self, has_waveform: Optional[bool] = None) -> Dict[str, Any]:
        patient_display = self.patient.full_name if self.patient else f"Patient #{self.patient_id}"
        patient_username = self.patient.username if self.patient else ""
        note_text = self.ai_attention_note or ""
        if has_waveform is None:
            legacy_sig = _safe_json_loads(self.signal, [])
            has_wf = bool(legacy_sig)
        else:
            has_wf = bool(has_waveform)
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "patient_name": patient_display,
            "patient_username": patient_username,
            "doctor_id": self.doctor_id,
            "age": self.age_at_test,
            "gender": self.gender_at_test,
            "status": self.status,
            "priority": self.priority,
            "complaint": self.complaint or "",
            "chronic": self.chronic or "",
            "confidence": self.confidence,
            "raw_prediction": self.raw_prediction,
            "prediction": self.prediction,
            "bpm": self.bpm,
            "rate_status": self.rate_status,
            "created_at": iso_utc(self.created_at),
            "timestamp": iso_utc(self.created_at),
            "reviewed_at": iso_utc(self.reviewed_at),
            "doctor_decision": self.doctor_decision,
            "doctor_note": self.doctor_note,
            "has_attention_note": bool(note_text),
            "ai_attention_note_pending": "جاري" in note_text,
            "has_waveform": has_wf,
        }

    def to_detail_dict(
        self,
        profile: Optional["PatientProfile"] = None,
        recording: Optional["EcgRecording"] = None,
    ) -> Dict[str, Any]:
        if recording is not None:
            sig_list = decode_float32_le(recording.signal).tolist()
            att_list = (
                decode_float32_le(recording.attention).tolist()
                if recording.attention is not None
                else []
            )
            has_wf = len(sig_list) > 0
        else:
            sig_list = _safe_json_loads(self.signal, [])
            att_list = _safe_json_loads(self.xai_weights, [])
            has_wf = len(sig_list) > 0

        d = self.to_summary_dict(has_waveform=has_wf)
        d["signal"] = sig_list
        d["xai_weights"] = att_list
        d["sample_rate_hz"] = recording.sample_rate_hz if recording is not None else 300
        d["symptoms"] = _safe_json_loads(self.symptoms, [])
        d["history"] = _safe_json_loads(self.history, [])
        d["ai_attention_note"] = self.ai_attention_note or ""
        if profile is not None:
            d["patient_profile"] = profile.to_dict()
        return d


class PatientHistory(db.Model):
    """Historical log of every ECG analysis (SUCCESS, REJECTED, ESCALATED, REVIEWED)."""

    __tablename__ = "patient_history"
    __table_args__ = (
        Index("ix_history_patient_created", "patient_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)
    age: Mapped[str] = mapped_column(String(20), default="--", nullable=False)
    gender: Mapped[str] = mapped_column(String(10), default="U", nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    prediction: Mapped[str] = mapped_column(String(100), default="N/A", nullable=False)
    bpm: Mapped[int] = mapped_column(Integer, default=72, nullable=False)
    rate_status: Mapped[str] = mapped_column(String(50), default="NORMAL", nullable=False)
    confidence: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_attention_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    doctor_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    recording: Mapped[Optional["EcgRecording"]] = relationship(
        "EcgRecording",
        back_populates="history",
        uselist=False,
        passive_deletes=True,
    )

    def to_dict(self, has_waveform: Optional[bool] = None) -> Dict[str, Any]:
        ts = iso_utc(self.created_at)
        has_wf = bool(has_waveform) if has_waveform is not None else (self.recording is not None)
        d: Dict[str, Any] = {
            "id": self.id,
            "ts": ts,
            "created_at": ts,
            "case_id": self.case_id,
            "age": self.age,
            "gender": self.gender,
            "status": self.status,
            "prediction": self.prediction,
            "bpm": self.bpm,
            "rate_status": self.rate_status,
            "has_waveform": has_wf,
        }
        if self.confidence is not None:
            d["confidence"] = self.confidence
        if self.message is not None:
            d["message"] = self.message
        if self.ai_attention_note is not None:
            d["ai_attention_note"] = self.ai_attention_note
        if self.doctor_note is not None:
            d["doctor_note"] = self.doctor_note
        return d


class EcgRecording(db.Model):
    """Binary-packed float32 ECG waveform and optional AI attention weights per history entry."""

    __tablename__ = "ecg_recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    history_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("patient_history.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    case_id: Mapped[Optional[str]] = mapped_column(String(36), index=True, nullable=True)
    patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sample_rate_hz: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    signal: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    attention: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)

    history: Mapped["PatientHistory"] = relationship("PatientHistory", back_populates="recording")

    def get_signal_array(self) -> np.ndarray:
        return decode_float32_le(self.signal)

    def get_attention_array(self) -> Optional[np.ndarray]:
        if self.attention is None:
            return None
        return decode_float32_le(self.attention)


class PatientDecision(db.Model):
    """Stores latest live decision message for each patient."""

    __tablename__ = "patient_decisions"

    patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    status_text: Mapped[str] = mapped_column(Text, nullable=False)
    doctor_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class ConsentLog(db.Model):
    """Records privacy and data usage consent per authenticated user."""

    __tablename__ = "consent_logs"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    consent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(20), default="1.0", nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class MLDatasetRecord(db.Model):
    """De-identified reviewed ECG records with physician ground-truth labels for active learning."""

    __tablename__ = "ml_dataset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="SET NULL"), index=True, nullable=True
    )
    recording_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("ecg_recordings.id", ondelete="SET NULL"), index=True, nullable=True
    )
    age: Mapped[str] = mapped_column(String(20), default="--", nullable=False)
    gender: Mapped[str] = mapped_column(String(10), default="U", nullable=False)
    ai_prediction: Mapped[str] = mapped_column(String(20), nullable=False)
    doctor_label: Mapped[str] = mapped_column(String(50), nullable=False)
    signal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    recording: Mapped[Optional["EcgRecording"]] = relationship(
        "EcgRecording", foreign_keys=[recording_id]
    )

    def get_signal_list(self) -> List[float]:
        if self.recording is not None:
            return decode_float32_le(self.recording.signal).tolist()
        return _safe_json_loads(self.signal, [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.case_id,
            "recording_id": self.recording_id,
            "age": self.age,
            "gender": self.gender,
            "ai_prediction": self.ai_prediction,
            "doctor_label": self.doctor_label,
            "signal": self.get_signal_list(),
        }


class AuditLog(db.Model):
    """Security and clinical management audit log."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)

    actor: Mapped[Optional["User"]] = relationship("User", foreign_keys=[actor_id])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "actor_username": self.actor.username if self.actor else None,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "ip_address": self.ip_address,
            "detail": _safe_json_loads(self.detail, self.detail),
            "created_at": iso_utc(self.created_at),
        }
