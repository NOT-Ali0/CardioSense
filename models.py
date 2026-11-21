"""
models.py - SQLAlchemy 2.0 ORM entity models for CardioSense (v2 FK-normalized schema).
"""
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import (
    String,
    Integer,
    Float,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    CheckConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import db


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

    def to_summary_dict(self) -> Dict[str, Any]:
        patient_display = self.patient.full_name if self.patient else f"Patient #{self.patient_id}"
        patient_username = self.patient.username if self.patient else ""
        note_text = self.ai_attention_note or ""
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
        }

    def to_detail_dict(self, profile: Optional["PatientProfile"] = None) -> Dict[str, Any]:
        d = self.to_summary_dict()
        d["signal"] = _safe_json_loads(self.signal, [])
        d["xai_weights"] = _safe_json_loads(self.xai_weights, [])
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

    def to_dict(self) -> Dict[str, Any]:
        ts = iso_utc(self.created_at)
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
    age: Mapped[str] = mapped_column(String(20), default="--", nullable=False)
    gender: Mapped[str] = mapped_column(String(10), default="U", nullable=False)
    ai_prediction: Mapped[str] = mapped_column(String(20), nullable=False)
    doctor_label: Mapped[str] = mapped_column(String(50), nullable=False)
    signal: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.case_id,
            "age": self.age,
            "gender": self.gender,
            "ai_prediction": self.ai_prediction,
            "doctor_label": self.doctor_label,
            "signal": _safe_json_loads(self.signal, []),
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
