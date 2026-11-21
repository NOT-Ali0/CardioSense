# SCAI CardioSense — Clinical ECG Triage & Cardiology Portal

CardioSense is a full-stack clinical ECG screening and arrhythmia (AFib) triage platform built with **Flask**, **SQLite + SQLAlchemy 2.0 (Flask-Migrate/Alembic)**, **PyTorch (SE-ResNet1D)**, and a **zero-build vanilla HTML/CSS/JS** frontend with Web Bluetooth, Arabic/English + RTL, and dark/light themes.

---

## Key Features

- **Role-Based Access Control & Object Ownership:** `admin`, `doctor`, and `patient` roles with strict server-side authorization. No public self-registration: admins provision doctors/patients, doctors provision patients linked to themselves, and admins can reassign patients between doctors.
- **Hardened Authentication:** `scrypt` password hashing, `HttpOnly` / `SameSite=Lax` / `Secure` Flask session cookies, instant session revocation via `session_version`, mandatory first-login password change (`must_change_password`), CSRF protection (`X-CSRF-Token`), login & inference rate limiting (`Flask-Limiter`), restricted CORS, and audit logging.
- **Patient Portal & Profile:** Live Web Bluetooth ECG acquisition (3,000 samples @ 300 Hz), AI triage (`/predict` reads `age` and `gender` directly from `patient_profiles`), DB-backed privacy consent, editable contact details, read-only clinical profile, DB-backed physician feedback, and printable PDF reports.
- **Professional Doctor Dashboard:** KPI strip (pending, urgent, my patients, reviewed today), prioritized searchable/filterable case queue, zoomable ECG waveform with XAI overlay & AI attention notes, patient profile side panel, clinical decision panel, patient list with BPM trend sparklines, and "+ Add Patient" dialog.
- **Admin Console:** Real-time inference telemetry, de-identified active-learning CSV export, user management (doctors & patients), doctor deactivation guard, patient reassignment (transfers open `ESCALATED` cases), and security audit log.

---

## Local Development Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Generate a strong SECRET_KEY and set SESSION_COOKIE_SECURE=0 for local HTTP

# 3. Initialize or upgrade the database
FLASK_APP=app.py flask db upgrade

# 4. Create the first admin account (never hardcoded!)
FLASK_APP=app.py flask create-admin

# 5. Start the server
FLASK_ENV=development SESSION_COOKIE_SECURE=0 python3 app.py --port 5001
```

Open `http://localhost:5001` in Chromium/Chrome (Web Bluetooth requires `localhost` or HTTPS).

### Migrating from Legacy (v1) SQLite Data
If upgrading an existing local v1 database keyed by free-text names:
```bash
python3 scripts/reset_db.py
FLASK_APP=app.py flask db stamp head
FLASK_APP=app.py flask create-admin
```
This backs up `instance/cardiosense.db` and recreates the v2 FK-normalized schema.

---

## Docker Deployment

```bash
# 1. Configure .env (set a 64-char hex SECRET_KEY; set SESSION_COOKIE_SECURE=1 & TRUST_PROXY=1 behind HTTPS)
cp .env.example .env

# 2. Build and start
docker compose up --build -d

# 3. Create the initial admin user inside the container
docker compose exec web flask create-admin
```

---

## Running Automated Tests

```bash
pytest -v
```

---

## Documentation
- API Reference: [docs/api-contract.md](docs/api-contract.md)
- Architecture & Security Plan: [docs/plans/auth-and-dashboard-plan.md](docs/plans/auth-and-dashboard-plan.md)
