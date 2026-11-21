# CardioSense — Authentication, Authorization, Data Model & Dashboard Plan

> **Status:** Draft for approval (read-only planning phase — no application code modified).  
> **Scope:** Replace client-side `localStorage` authentication with real server-side sessions & RBAC/ownership, migrate SQLite from free-text names to foreign keys, add patient profiles, upgrade the vanilla-JS doctor dashboard and admin user management, and harden security.  
> **Frontend constraint:** Vanilla HTML/CSS/JS served from Flask (`send_from_directory`), no Jinja templating, no React/Vite/build tools, Arabic/English + RTL and dark/light themes preserved.

---

## A. Current-State Findings

### 1. Endpoint Inventory & Security Audit

Every route currently registered in `app.py` is **completely unauthenticated and unauthorized**:

| Method & Path | Handler (`file:line`) | Who Can Call It Today | User Identity Mechanism | Key Defects |
| :--- | :--- | :--- | :--- | :--- |
| `GET /` | `serve_frontend` (`app.py:102-106`) | Anyone | None | Serves monolithic `templates/index.html` via `send_from_directory`. |
| `GET /api/static/<path>` | Flask static handler (`app.py:35`) | Anyone | None | Static assets (`logo.png`, `logo2.png`, etc.). |
| `POST /predict` | `run_prediction` (`app.py:136-218`) | Anyone | `data.get("name", "Unknown")` (`app.py:143`); `age` (`app.py:144`); `gender` (`app.py:145`) from JSON body | Spoofable patient identity; `age`/`gender` taken from untrusted form; leaks raw `str(e)` on 500 (`app.py:218`); no rate limit. |
| `POST /api/patient/update_case` | `update_case` (`app.py:220-253`) | Anyone | None (only `case_id` in JSON body, `app.py:223`) | Any caller who guesses an 8-char UUID can overwrite symptoms/history and spawn background LLM threads (`app.py:244-247`); `request.json` unguarded if body is null. |
| `POST /api/doctor/respond` | `doctor_respond` (`app.py:255-313`) | Anyone | None (only `case_id` in JSON body, `app.py:258`) | Any unauthenticated caller can adjudicate escalated cases, poison `ml_dataset` (`app.py:265-273`), and inject arbitrary `doctor_note` text; `decision` enum is not validated (`app.py:284-285`). |
| `GET /api/doctor/cases` | `get_doctor_cases` (`app.py:315-321`) | Anyone | None | Exposes **all** `ESCALATED` cases (names, symptoms, chronic conditions, 3,000-sample ECGs, XAI weights) across all patients. |
| `GET /api/doctor/archive` | `get_doctor_archive` (`app.py:323-329`) | Anyone | None | Exposes **all** `ARCHIVED` cases. Sorted by string `timestamp` (`"%Y-%m-%d %I:%M:%S %p"`, `app.py:148`), which sorts 12-hour AM/PM times incorrectly. |
| `GET /api/patient/status/<name>` | `get_patient_status` (`app.py:331-335`) | Anyone | `<name>` URL path parameter | IDOR: anyone can poll any patient's live diagnosis and doctor notes. |
| `GET /api/patient/history/<name>` | `get_patient_history` (`app.py:381-386`) | Anyone | `<name>` URL path parameter | IDOR: anyone can read any patient's full diagnostic history. |
| `GET /api/doctor/patient_history/<name>` | `get_doctor_patient_history` (`app.py:388-393`) | Anyone | `<name>` URL path parameter | Exact duplicate of `get_patient_history`; no doctor role or ownership check. |
| `GET /api/admin/stats` | `get_admin_stats` (`app.py:337-354`) | Anyone | None | Exposes global clinical telemetry counts to unauthenticated users. |
| `GET /api/admin/export_dataset` | `export_dataset` (`app.py:356-379`) | Anyone | None | Streams the entire `ml_dataset` table as CSV without auth. |
| `POST /api/consent` | `record_consent` (`app.py:395-407`) | Anyone | `d.get("user", "unknown")` (`app.py:398`) | Spoofable consent logging; no `GET` endpoint to check consent status from DB. |

### 2. Platform, Frontend & Configuration Findings

- **Client-only "auth" & plaintext secrets (`templates/index.html`):**
  - `templates/index.html:1696`: Hardcoded fallback admin password: `if(!localStorage.getItem("user_admin")) localStorage.setItem("user_admin", "123");`.
  - `templates/index.html:1190-1224`, `1883-1895`: Public self-registration view (`#registerView`) lets anyone register as `patient` or `doctor` and stores plaintext `{ password, role }` in `localStorage` (`user_<name>`).
  - `templates/index.html:1897-1920`: `processAuthentication()` validates credentials purely against `localStorage` and writes `active_session` to `localStorage` (`1915`), which auto-logs in on page load (`1701-1712`).
  - `templates/index.html:2540, 2551`: Consent state is gated by `localStorage.getItem('scai_consent_' + currentUser)` instead of the database.
  - `templates/index.html:1947, 2004-2008`: Doctor feedback messages (`messages_<user>`) are persisted only in `localStorage`.
- **XSS vectors in frontend (`templates/index.html`):** Unescaped `innerHTML` interpolation of `docNote`, `c.name`, `name`, and history fields at `index.html:1958`, `1998-2001`, `2409`, `2584-2591`, `2655-2675`, `2699-2712`.
- **Hardcoded secret & unused SDK (`app.py:21`, `.env.example:2`):**
  - `app.py:21`: `co = cohere.ClientV2(api_key=os.getenv("COHERE_API_KEY", "rMblBD1Cck5Q7SY6TmBhRfcbIodp2d8mbv0kJv96"))`.
  - `.env.example:2`: The same Cohere API key is committed in `.env.example`.
  - `co` is instantiated at import time and **never referenced** anywhere else in `app.py`.
- **Unsafe CORS (`app.py:37`):** `CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)` — wildcard origin with credentials enabled.
- **Missing Flask `SECRET_KEY`:** `app.config["SECRET_KEY"]` is not configured anywhere in `app.py`.
- **SQLite foreign keys disabled (`db.py:15-21`):** `set_sqlite_pragma` enables `WAL`, `busy_timeout=5000`, and `synchronous=NORMAL`, but omits `PRAGMA foreign_keys=ON`.
- **Schema keyed by free-text names (`models.py`):** `Case.patient_name` (`models.py:22`), `PatientHistory.patient_name` (`models.py:109`), `PatientDecision.patient_name` PK (`models.py:149`), and `ConsentLog.user` PK (`models.py:191`).
- **Missing reference doc:** `docs/api-contract.md` does not currently exist in the repository (`docs/` contains only an empty `samples/` folder). `README.md` is currently a Hugging Face PEFT model-card stub (`README.md:1-207`).

---

## B. Target Schema & Migration Approach

### 1. SQLite Enforcement (`db.py`)
Add `cursor.execute("PRAGMA foreign_keys=ON")` to `set_sqlite_pragma` in `db.py:15-21` so every SQLite connection enforces FK constraints and cascades.

### 2. Target Tables, Columns, Foreign Keys & Indexes

#### `users`
| Column | Type | Constraints / Indexes | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | Internal immutable user ID |
| `username` | `VARCHAR(80)` | `UNIQUE NOT NULL`, `INDEX` | Normalized lowercase (`^[a-z0-9._-]{3,80}$`) |
| `full_name` | `VARCHAR(120)` | `NOT NULL` | Display name (Arabic or English) |
| `password_hash` | `VARCHAR(255)` | `NOT NULL` | `werkzeug.security` `scrypt` hash |
| `role` | `VARCHAR(20)` | `NOT NULL`, `INDEX`, `CHECK (role IN ('patient','doctor','admin'))` | Single role per account |
| `is_active` | `BOOLEAN` | `NOT NULL DEFAULT 1`, `INDEX` | Soft deactivation (preserves clinical audit trail) |
| `must_change_password` | `BOOLEAN` | `NOT NULL DEFAULT 1` | `False` for CLI/env bootstrapped admin; `True` for created doctors/patients |
| `session_version` | `INTEGER` | `NOT NULL DEFAULT 1` | Incremented on password change or deactivation to immediately invalidate active cookies |
| `created_by_id` | `INTEGER` | `FK -> users.id ON DELETE SET NULL`, `NULL` | Nullable for bootstrapped admin |
| `created_at` | `DATETIME` | `NOT NULL` (UTC) | |
| `updated_at` | `DATETIME` | `NOT NULL` (UTC) | |
| `last_login_at` | `DATETIME` | `NULL` (UTC) | |

#### `patient_profiles` (1:1 with `users` where `role='patient'`)
| Column | Type | Constraints / Indexes | Notes |
| :--- | :--- | :--- | :--- |
| `user_id` | `INTEGER` | `PRIMARY KEY`, `FK -> users.id ON DELETE CASCADE` | Patient's user ID |
| `doctor_id` | `INTEGER` | `NOT NULL`, `FK -> users.id ON DELETE RESTRICT`, `INDEX` | Enforces **exactly one doctor per patient**; prevents deleting a doctor with assigned patients |
| `date_of_birth` | `DATE` | `NULL` | Preferred over static `age` (server computes integer `age`); see Open Question #4 |
| `age` | `INTEGER` | `NULL` | Fallback if DOB is unknown (`CHECK (age BETWEEN 1 AND 130)`); `/predict` uses computed DOB age or `age` |
| `gender` | `VARCHAR(10)` | `NOT NULL`, `CHECK (gender IN ('M','F'))` | Clinical field (doctor-editable) |
| `phone` | `VARCHAR(30)` | `NULL` | Contact field (patient-editable) |
| `emergency_contact_name` | `VARCHAR(120)` | `NULL` | Contact field (patient-editable) |
| `emergency_contact_phone` | `VARCHAR(30)` | `NULL` | Contact field (patient-editable) |
| `chronic_conditions` | `TEXT` | `NOT NULL DEFAULT '[]'` | JSON list of chronic conditions (doctor-editable) |
| `clinical_notes` | `TEXT` | `NOT NULL DEFAULT ''` | Doctor's ongoing clinical summary for the patient (doctor-editable) |
| `created_at` | `DATETIME` | `NOT NULL` (UTC) | |
| `updated_at` | `DATETIME` | `NOT NULL` (UTC) | |

#### `cases` (Escalated & Archived ECG reviews)
| Column | Type | Constraints / Indexes | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `VARCHAR(36)` | `PRIMARY KEY` | Full or 8-char UUID (recommend full `uuid4().hex` or 12+ chars to prevent guessing) |
| `patient_id` | `INTEGER` | `NOT NULL`, `FK -> users.id ON DELETE RESTRICT`, `INDEX` | Replaces `patient_name` |
| `doctor_id` | `INTEGER` | `NOT NULL`, `FK -> users.id ON DELETE RESTRICT`, `INDEX` | Routed from `patient_profiles.doctor_id` at creation |
| `age_at_test` | `INTEGER` | `NULL` | Snapshot from profile at `/predict` time |
| `gender_at_test` | `VARCHAR(10)` | `NOT NULL DEFAULT 'U'` | Snapshot from profile at `/predict` time |
| `status` | `VARCHAR(20)` | `NOT NULL DEFAULT 'ESCALATED'`, `CHECK (status IN ('ESCALATED','ARCHIVED'))` | |
| `priority` | `VARCHAR(16)` | `NOT NULL DEFAULT 'NORMAL'`, `CHECK (priority IN ('URGENT','HIGH','NORMAL'))` | Computed at escalation (e.g., `URGENT` if `raw_prediction == 'AFIB'` or `bpm < 50` or `bpm > 120` or `confidence < 0.70`) |
| `complaint` | `TEXT` | `NOT NULL DEFAULT ''` | Arabic joined symptom summary |
| `chronic` | `TEXT` | `NOT NULL DEFAULT ''` | Snapshot from profile chronic conditions + escalation selection |
| `symptoms` | `TEXT` | `NOT NULL DEFAULT '[]'` | JSON array |
| `history` | `TEXT` | `NOT NULL DEFAULT '[]'` | JSON array |
| `signal` | `TEXT` | `NOT NULL` | JSON array of 3,000 floats |
| `confidence` | `FLOAT` | `NOT NULL DEFAULT 0.0`, `INDEX` | |
| `raw_prediction` | `VARCHAR(20)` | `NOT NULL DEFAULT 'NORMAL'` | `'AFIB'` or `'NORMAL'` |
| `prediction` | `VARCHAR(50)` | `NOT NULL DEFAULT 'UNCERTAIN'` | |
| `bpm` | `INTEGER` | `NOT NULL DEFAULT 72` | |
| `rate_status` | `VARCHAR(50)` | `NOT NULL DEFAULT 'NORMAL'` | |
| `created_at` | `DATETIME` | `NOT NULL` (UTC), `INDEX` | Replaces AM/PM string for accurate sorting & date math |
| `xai_weights` | `TEXT` | `NOT NULL DEFAULT '[]'` | JSON array of 3,000 floats |
| `ai_attention_note` | `TEXT` | `NOT NULL DEFAULT ''` | Generated in background thread |
| `doctor_decision` | `VARCHAR(50)` | `NULL`, `CHECK (doctor_decision IN ('INFECTED','HEALTHY','NOISY') OR doctor_decision IS NULL)` | |
| `doctor_note` | `TEXT` | `NULL` | |
| `reviewed_by_id` | `INTEGER` | `NULL`, `FK -> users.id ON DELETE SET NULL` | Doctor who submitted the verdict |
| `reviewed_at` | `DATETIME` | `NULL` (UTC), `INDEX` | Powers "Reviewed Today" KPI |

**Composite indexes on `cases`:**
- `ix_cases_doctor_status_priority_created (doctor_id, status, priority, created_at)` — doctor queue & filters
- `ix_cases_doctor_reviewed_at (doctor_id, reviewed_at)` — "Reviewed Today" KPI
- `ix_cases_patient_created (patient_id, created_at)` — patient case history

#### `patient_history`
| Column | Type | Constraints / Indexes | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | |
| `case_id` | `VARCHAR(36)` | `NOT NULL`, `INDEX` | Correlation ID (not a hard FK because `SUCCESS`/`REJECTED` tests have no `cases` row) |
| `patient_id` | `INTEGER` | `NOT NULL`, `FK -> users.id ON DELETE CASCADE`, `INDEX` | Replaces `patient_name` |
| `created_at` | `DATETIME` | `NOT NULL` (UTC), `INDEX` | Serialized as ISO-8601 UTC (`ts` key preserved for UI display) |
| `age` | `VARCHAR(20)` | `NOT NULL DEFAULT '--'` | Snapshot at test time |
| `gender` | `VARCHAR(10)` | `NOT NULL DEFAULT 'U'` | Snapshot at test time |
| `status` | `VARCHAR(50)` | `NOT NULL` | `SUCCESS`, `REJECTED`, `ESCALATED`, `REVIEWED` |
| `prediction` | `VARCHAR(100)` | `NOT NULL DEFAULT 'N/A'` | |
| `bpm` | `INTEGER` | `NOT NULL DEFAULT 72` | |
| `rate_status` | `VARCHAR(50)` | `NOT NULL DEFAULT 'NORMAL'` | |
| `confidence` | `VARCHAR(20)` | `NULL` | Formatted percentage string |
| `message` | `TEXT` | `NULL` | Gatekeeper rejection reason |
| `ai_attention_note` | `TEXT` | `NULL` | |
| `doctor_note` | `TEXT` | `NULL` | Populated on doctor review; replaces `localStorage` `messages_<user>` |

**Composite index:** `ix_history_patient_created (patient_id, created_at)`.

#### `patient_decisions`
| Column | Type | Constraints / Indexes | Notes |
| :--- | :--- | :--- | :--- |
| `patient_id` | `INTEGER` | `PRIMARY KEY`, `FK -> users.id ON DELETE CASCADE` | Replaces `patient_name` PK |
| `status_text` | `TEXT` | `NOT NULL` | Latest live status banner |
| `doctor_note` | `TEXT` | `NULL` | Structured doctor note (avoids fragile `\|ملاحظة الطبيب:` string splitting) |
| `updated_at` | `DATETIME` | `NOT NULL` (UTC) | |

#### `consent_logs`
| Column | Type | Constraints / Indexes | Notes |
| :--- | :--- | :--- | :--- |
| `user_id` | `INTEGER` | `PRIMARY KEY`, `FK -> users.id ON DELETE CASCADE` | Replaces `user` string PK |
| `consent` | `BOOLEAN` | `NOT NULL DEFAULT 1` | |
| `policy_version` | `VARCHAR(20)` | `NOT NULL DEFAULT '1.0'` | Allows re-prompting if consent text changes later |
| `accepted_at` | `DATETIME` | `NOT NULL` (UTC) | |

#### `ml_dataset` (De-identified Active Learning Store)
| Column | Type | Constraints / Indexes | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | |
| `case_id` | `VARCHAR(36)` | `NULL`, `FK -> cases.id ON DELETE SET NULL`, `INDEX` | Keeps ML row even if case is purged |
| `age` | `VARCHAR(20)` | `NOT NULL DEFAULT '--'` | De-identified demographic |
| `gender` | `VARCHAR(10)` | `NOT NULL DEFAULT 'U'` | De-identified demographic |
| `ai_prediction` | `VARCHAR(20)` | `NOT NULL` | |
| `doctor_label` | `VARCHAR(50)` | `NOT NULL` | |
| `signal` | `TEXT` | `NOT NULL` | 3,000-float JSON array |
| `created_at` | `DATETIME` | `NOT NULL` (UTC) | |

#### `audit_logs` (Optional — Recommended for Clinical Accountability)
| Column | Type | Constraints / Indexes | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | |
| `actor_id` | `INTEGER` | `NULL`, `FK -> users.id ON DELETE SET NULL`, `INDEX` | |
| `action` | `VARCHAR(64)` | `NOT NULL`, `INDEX` | e.g., `LOGIN_OK`, `LOGIN_FAIL`, `PASSWORD_CHANGE`, `USER_CREATE`, `USER_DEACTIVATE`, `PATIENT_REASSIGN`, `CASE_REVIEW`, `DATASET_EXPORT` |
| `target_type` | `VARCHAR(32)` | `NULL` | `user`, `case`, `patient_profile`, `dataset` |
| `target_id` | `VARCHAR(64)` | `NULL` | |
| `ip_address` | `VARCHAR(45)` | `NULL` | |
| `detail` | `TEXT` | `NULL` | Sanitized JSON metadata (never passwords or raw ECG signals) |
| `created_at` | `DATETIME` | `NOT NULL` (UTC), `INDEX` | |

### 3. Migration Approach for Existing SQLite Data
Because existing rows in `instance/cardiosense.db` are test data keyed by free-text names and may be discarded:
1. **Clean-slate v2 reset script (`scripts/reset_db.py` / `flask init-db --reset`):**
   - Copies existing `instance/cardiosense.db` (if present) to `instance/cardiosense.db.bak.<YYYYMMDD_HHMMSS>`.
   - Drops all legacy tables (`cases`, `patient_history`, `patient_decisions`, `ml_dataset`, `consent_logs`) and recreates the v2 FK schema.
2. **Ongoing schema evolution:** Introduce **Flask-Migrate (Alembic)** initialized from the v2 baseline so all future column/index changes are versioned migrations rather than destructive drops. Keep `db.create_all()` only for pytest in-memory databases, not silent production schema drift.
3. **Retire `scripts/import_json.py`:** Mark as deprecated or remove, since JSON flat-files are no longer supported and lack user IDs.

---

## C. Endpoint Matrix

> **Legend:** `BREAKING` = path, method, auth requirement, or request/response contract changed from current `app.py`.  
> **Global rule:** Unless marked `Public` or `Auth-exempt`, every endpoint requires an active session (`401`), an active user (`is_active=True`, else `401`), and `must_change_password=False` (else `403` with `code: "PASSWORD_CHANGE_REQUIRED"`). All `POST`/`PATCH`/`DELETE` endpoints (except `POST /api/auth/login`) require a valid `X-CSRF-Token` header (`403` if invalid).

### 1. Authentication & Session Endpoints (New)

| Old Path | New Path | Allowed Roles | Ownership Rule | Request / Response Changes | Breaking? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| *(localStorage)* | `POST /api/auth/login` | Public (rate-limited) | N/A | **Req:** `{ "username", "password" }` (no `role` field needed — role comes from DB). **Res 200:** `{ "user": { id, username, full_name, role, must_change_password }, "consent_accepted": bool, "csrf_token": "..." }`. Sets `HttpOnly` session cookie. | **NEW** (replaces client auth) |
| *(localStorage)* | `POST /api/auth/logout` | Any authenticated | Self | Clears server session & cookie. **Res 200:** `{ "status": "ok" }`. | **NEW** |
| *(localStorage)* | `GET /api/auth/me` | Any authenticated (allowed if `must_change_password=True`) | Self | **Res 200:** `{ "user": {...}, "consent_accepted": bool, "csrf_token": "..." }`. Returns `401` if not logged in. | **NEW** |
| *(none)* | `POST /api/auth/change-password` | Any authenticated (allowed if `must_change_password=True`) | Self | **Req:** `{ "current_password", "new_password" }`. Validates policy, updates hash, sets `must_change_password=False`, increments `session_version`, refreshes session. **Res 200:** `{ "status": "ok", "csrf_token": "..." }`. | **NEW** |

### 2. Consent & Patient Profile Endpoints

| Old Path | New Path | Allowed Roles | Ownership Rule | Request / Response Changes | Breaking? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| *(localStorage)* | `GET /api/consent` | `patient` | `user_id == session.user_id` | **Res 200:** `{ "consent": bool, "policy_version": "1.0", "accepted_at": iso \| null }`. | **NEW** |
| `POST /api/consent` | `POST /api/consent` | `patient` | `user_id == session.user_id` | **Req:** `{}` (or `{ "policy_version": "1.0" }`). Ignores `user` in body. **Res 200:** `{ "status": "ok", "accepted_at": iso }`. | **BREAKING** (requires patient session; ignores body `user`) |
| *(none)* | `GET /api/patient/profile` | `patient` | `profile.user_id == session.user_id` | **Res 200:** `{ "user": { id, username, full_name }, "doctor": { id, full_name }, "profile": { date_of_birth, age, gender, phone, emergency_contact_name, emergency_contact_phone, chronic_conditions, clinical_notes } }`. | **NEW** |
| *(none)* | `PATCH /api/patient/profile` | `patient` | `profile.user_id == session.user_id` | **Req:** allowlisted contact fields ONLY (`phone`, `emergency_contact_name`, `emergency_contact_phone`). Rejects/ignores clinical fields (`age`, `date_of_birth`, `gender`, `chronic_conditions`, `doctor_id`). **Res 200:** updated profile. | **NEW** |

### 3. Patient ECG & History Endpoints

| Old Path | New Path | Allowed Roles | Ownership Rule | Request / Response Changes | Breaking? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `POST /predict` | `POST /predict` | `patient` | `patient_id == session.user_id`; `doctor_id` from `patient_profiles` | **Req:** `{ "signal": [float x 3000] }`. Ignores `name`, `age`, `gender` from body; loads them from `patient_profiles`. Rejects with `400` if profile lacks valid `age`/`gender` or `signal` invalid. **Res 200:** same shape (`status`, `prediction`, `confidence`, `bpm`, `rate_status`, optional `case_id`/`message`). | **BREAKING** (requires patient session; `name`/`age`/`gender` ignored) |
| `POST /api/patient/update_case` | `POST /api/patient/update_case` | `patient` | `case.patient_id == session.user_id` AND `case.status == 'ESCALATED'` | **Req:** `{ "case_id", "symptoms": [...], "history": [...] }`. Returns `404` if not found or `403` if owned by another patient. | **BREAKING** (requires patient session + ownership) |
| `GET /api/patient/status/<name>` | `GET /api/patient/status` | `patient` | `decision.patient_id == session.user_id` | Removes `<name>` from URL. **Res 200:** `{ "status_text": "...", "doctor_note": "..." \| null, "updated_at": iso \| null }`. | **BREAKING** (path changed; requires patient session) |
| `GET /api/patient/history/<name>` | `GET /api/patient/history` | `patient` | `history.patient_id == session.user_id` | Removes `<name>` from URL. Supports optional `?limit=50&offset=0`. **Res 200:** array of history objects including `doctor_note`. | **BREAKING** (path changed; requires patient session) |

### 4. Doctor Dashboard, Case Review & Patient Management Endpoints

| Old Path | New Path | Allowed Roles | Ownership Rule | Request / Response Changes | Breaking? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| *(none)* | `GET /api/doctor/dashboard` | `doctor`, `admin` | Doctor: `doctor_id == session.user_id`; Admin: all (or `?doctor_id=`) | **Res 200:** `{ "kpis": { "pending_cases": int, "urgent_cases": int, "my_patients": int, "reviewed_today": int } }`. | **NEW** |
| `GET /api/doctor/cases` | `GET /api/doctor/cases` | `doctor`, `admin` | Doctor: `case.doctor_id == session.user_id`; Admin: all (or `?doctor_id=`) | Supports `?search=&priority=URGENT\|HIGH\|NORMAL&raw_prediction=AFIB\|NORMAL&rate_status=&sort=priority\|confidence\|newest`. Returns **lightweight case summaries** (omits 3,000-float `signal` & `xai_weights` for fast queue loading; includes `patient_id`, `patient_name`, `priority`, `created_at`). | **BREAKING** (auth + ownership + summary payload without `signal`/`xai_weights`) |
| `GET /api/doctor/archive` | `GET /api/doctor/archive` | `doctor`, `admin` | Doctor: `case.doctor_id == session.user_id`; Admin: all (or `?doctor_id=`) | Supports `?search=&decision=INFECTED\|HEALTHY\|NOISY&limit=50&offset=0`. Sorted by `reviewed_at DESC`. Returns lightweight summaries. | **BREAKING** (auth + ownership + summary payload) |
| *(none)* | `GET /api/doctor/cases/<case_id>` | `doctor`, `admin` | Doctor: `case.doctor_id == session.user_id`; Admin: all | **Res 200:** full case detail including `signal` (3,000 floats), `xai_weights` (3,000 floats), `ai_attention_note`, and nested `patient_profile` snapshot (age, gender, phone, emergency contact, chronic conditions, clinical notes). | **NEW** |
| `POST /api/doctor/respond` | `POST /api/doctor/respond` | `doctor` *(see Open Question #6 on admin)* | `case.doctor_id == session.user_id` AND `case.status == 'ESCALATED'` | Validates `decision` in `('INFECTED','HEALTHY','NOISY')` (`400` otherwise). Sets `reviewed_at`, `reviewed_by_id`, updates `patient_decisions` & `patient_history` by `patient_id`. | **BREAKING** (auth + ownership + strict enum validation) |
| *(none)* | `GET /api/doctor/patients` | `doctor`, `admin` | Doctor: `profile.doctor_id == session.user_id`; Admin: all (or `?doctor_id=`) | Supports `?search=&status=`. **Res 200:** list of patients with `{ id, username, full_name, age, gender, phone, last_test_at, last_status, last_prediction, last_bpm, bpm_trend: [{ ts, bpm }] (last 8 tests), pending_cases_count }`. | **NEW** |
| *(none)* | `POST /api/doctor/patients` | `doctor` | Auto-sets `doctor_id = session.user_id` | **Req:** `{ "username", "full_name", "temp_password" (optional), "date_of_birth" \| "age", "gender", "phone", "emergency_contact_name", "emergency_contact_phone", "chronic_conditions": [...], "clinical_notes" }`. **Res 201:** `{ "user": {...}, "profile": {...}, "temporary_password": "..." }` (shown once). | **NEW** |
| *(none)* | `GET /api/doctor/patients/<int:patient_id>` | `doctor`, `admin` | Doctor: `profile.doctor_id == session.user_id`; Admin: all | **Res 200:** full patient user + profile + assigned doctor + recent summary stats. | **NEW** |
| *(none)* | `PATCH /api/doctor/patients/<int:patient_id>` | `doctor`, `admin` | Doctor: `profile.doctor_id == session.user_id`; Admin: all | **Req:** clinical fields (`date_of_birth`, `age`, `gender`, `chronic_conditions`, `clinical_notes`, `full_name`). **Res 200:** updated profile. | **NEW** |
| `GET /api/doctor/patient_history/<name>` | `GET /api/doctor/patients/<int:patient_id>/history` | `doctor`, `admin` | Doctor: `profile.doctor_id == session.user_id`; Admin: all | Replaces `<name>` with `<patient_id>`. Supports `?limit=50&offset=0`. **Res 200:** array of history records. | **BREAKING** (path changed; requires doctor/admin + ownership) |

### 5. Admin Telemetry & User Management Endpoints

| Old Path | New Path | Allowed Roles | Ownership Rule | Request / Response Changes | Breaking? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `GET /api/admin/stats` | `GET /api/admin/stats` | `admin` | Global | Same response shape + optional user counts (`doctors_count`, `patients_count`, `pending_cases`). | **BREAKING** (requires `admin` session) |
| `GET /api/admin/export_dataset` | `GET /api/admin/export_dataset` | `admin` | Global | Streams CSV; writes `DATASET_EXPORT` audit log. | **BREAKING** (requires `admin` session) |
| *(none)* | `GET /api/admin/users` | `admin` | Global | Query params: `?role=doctor\|patient\|admin&is_active=true\|false&search=`. Returns users with assigned doctor (for patients) or patient count (for doctors). | **NEW** |
| *(none)* | `POST /api/admin/doctors` | `admin` | Global | **Req:** `{ "username", "full_name", "temp_password" (optional) }`. Creates `role='doctor'`, `must_change_password=True`. **Res 201:** `{ "user": {...}, "temporary_password": "..." }`. | **NEW** |
| *(none)* | `POST /api/admin/patients` | `admin` | Global | **Req:** same as `POST /api/doctor/patients` + required `"doctor_id"` (must be an active doctor). **Res 201:** `{ "user": {...}, "profile": {...}, "temporary_password": "..." }`. | **NEW** |
| *(none)* | `PATCH /api/admin/users/<int:user_id>/status` | `admin` | Global (cannot deactivate self) | **Req:** `{ "is_active": bool }`. Increments `session_version` on deactivation. Guards against deactivating a doctor who still has active patients (returns `409 Conflict` unless reassigned first). | **NEW** |
| *(none)* | `PATCH /api/admin/patients/<int:patient_id>/reassign` | `admin` | Global | **Req:** `{ "doctor_id": int }` (must be active doctor). Updates `patient_profiles.doctor_id` and transfers open `ESCALATED` cases to the new doctor (leaves `ARCHIVED` cases attributed to original reviewer). | **NEW** |
| *(none)* | `GET /api/admin/audit-logs` | `admin` | Global | Optional `?action=&limit=100&offset=0`. Returns recent security/admin audit entries. | **NEW** |

---

## D. Security Design

1. **Sessions & Cookie Security**
   - Flask signed cookie sessions backed by `SECRET_KEY` (loaded from env, minimum 32 random bytes; fail fast on startup if missing in production).
   - Cookie attributes:
     - `SESSION_COOKIE_HTTPONLY = True`
     - `SESSION_COOKIE_SAMESITE = "Lax"`
     - `SESSION_COOKIE_SECURE = True` in production (configurable via `SESSION_COOKIE_SECURE=1`, default `True` unless `FLASK_ENV=development` or `TESTING=True`)
     - `PERMANENT_SESSION_LIFETIME = timedelta(hours=8)`
   - **Immediate revocation without Redis:** Store `user_id` and `session_version` in the Flask session cookie. On every authenticated request, load `User` and verify `user.is_active` and `user.session_version == session["sv"]`. Changing password or deactivating a user bumps `session_version`, instantly invalidating all cookies.
   - Call `session.clear()` on login (prevents session fixation) and on logout.
   - Add `werkzeug.middleware.proxy_fix.ProxyFix` (controlled by `TRUST_PROXY=1`) so `Secure` cookies and client IP extraction for rate limiting work behind reverse proxies.

2. **Password Hashing & Policy**
   - Use `werkzeug.security.generate_password_hash(pw, method="scrypt")` and `check_password_hash` (built into Flask/Werkzeug — strong memory-hard KDF, zero extra native build dependencies in `python:3.11-slim`).
   - Password policy: minimum 10 characters, maximum 128 characters, must differ from `username` and (on change) from current password.
   - `must_change_password`: enforced server-side in a `before_request` hook / `@login_required` decorator — blocks every endpoint except `/api/auth/me`, `/api/auth/change-password`, `/api/auth/logout` with `403 {"error": "Password change required", "code": "PASSWORD_CHANGE_REQUIRED"}`.

3. **First Admin Bootstrap (No Hardcoded Passwords)**
   - Delete `localStorage.setItem("user_admin", "123")` from `templates/index.html:1696`.
   - Provide a Flask CLI command: `flask create-admin --username <user> --full-name "<name>"` (prompts securely for password via `click.prompt(..., hide_input=True, confirmation_prompt=True)`, or reads `--password` for automation).
   - Optional idempotent env bootstrap on startup (`BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_PASSWORD`, `BOOTSTRAP_ADMIN_NAME`): runs **only** if `users` has zero `admin` rows, validates password length >= 10, sets `must_change_password=False`, and logs a warning to remove `BOOTSTRAP_ADMIN_PASSWORD` from `.env` after first boot.

4. **Rate Limiting**
   - Add `Flask-Limiter` to `requirements.txt`.
   - Default storage: `memory://` (appropriate for the current single-worker Gunicorn deployment `--workers 1 --threads 4` in `Dockerfile:32`; configurable via `RATELIMIT_STORAGE_URI` for Redis if scaled to multiple workers).
   - Limits:
     - `POST /api/auth/login`: `5 per minute; 20 per hour` (keyed by IP + lowercase username to deter both credential stuffing and targeted brute-force).
     - `POST /api/auth/change-password`: `5 per minute` per user.
     - `POST /predict`: `30 per minute` per user (protects CPU-bound PyTorch inference).
   - Return `429 {"error": "Too many requests. Please try again later."}`.

5. **CSRF Protection (Same-Origin JSON API)**
   - On login and `GET /api/auth/me`, generate/return a per-session random `csrf_token` (`secrets.token_urlsafe(32)`) stored in `session["csrf_token"]`.
   - In `app.before_request`, for all state-changing methods (`POST`, `PUT`, `PATCH`, `DELETE`) on `/api/*` and `/predict` (excluding `POST /api/auth/login`):
     - Require `X-CSRF-Token` header matching `session.get("csrf_token")` via `hmac.compare_digest`.
     - Require `request.is_json` (`Content-Type: application/json`) to prevent simple form-encoded cross-site POSTs.
   - Frontend `apiFetch()` helper automatically attaches `X-CSRF-Token` and `credentials: "same-origin"` on every mutating request.

6. **CORS Hardening**
   - Remove `CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)` (`app.py:37`).
   - Because `templates/index.html` is served same-origin from `/`, cross-origin requests are unnecessary by default.
   - Only enable `Flask-CORS` when `CORS_ORIGINS` env var is explicitly set to a comma-separated allowlist of origins (never `*` with `supports_credentials=True`).

7. **Secrets Handling (Including Exposed Cohere Key)**
   - Remove hardcoded `"rMblBD1Cck5Q7SY6TmBhRfcbIodp2d8mbv0kJv96"` from `app.py:21` and `.env.example:2`.
   - Rotate/revoke that Cohere key immediately (it is already in git history).
   - Remove the unused `co = cohere.ClientV2(...)` import-time initialization in `app.py:21` (or lazy-initialize only when `COHERE_API_KEY` is non-empty and actually needed).
   - Update `.env.example` with placeholder values (`SECRET_KEY=replace-with-64-char-hex`, `COHERE_API_KEY=`, `SESSION_COOKIE_SECURE=0`, `CORS_ORIGINS=`).

8. **Safe Error Messages, Audit Logging & XSS Prevention**
   - Replace `return jsonify({"error": str(e)}), 500` (`app.py:218`) with `app.logger.exception(...)` and a generic `{"error": "Internal server error"}`.
   - Register global Flask error handlers for `400`, `401`, `403`, `404`, `405`, `429`, `500` returning consistent JSON `{"error": "..."}`.
   - Login errors always return `401 {"error": "Invalid username or password"}` regardless of whether the username exists or is inactive (no account enumeration).
   - Write `audit_logs` records for auth events, user lifecycle changes, reassignments, case reviews, and CSV exports.
   - Frontend: introduce `escapeHtml()` and prefer `textContent` over `innerHTML` for all user/clinical strings (especially `doctor_note`, `full_name`, `complaint`).

---

## E. Step-by-Step Implementation Plan

Each step is independently implementable, testable, and committable. The suggested order (1–13) is sound and preserved below, with a small note on Step 12 (creating `docs/api-contract.md` since it does not exist yet).

### Step 1: Schema & SQLite Migration
- **Goal:** Define v2 SQLAlchemy 2.0 models with FKs and indexes, enable `PRAGMA foreign_keys=ON`, and provide a clean-slate reset/init script + Alembic baseline.
- **Files touched:** `db.py`, `models.py`, `scripts/reset_db.py` (new), `requirements.txt` (add `Flask-Migrate` if approved).
- **Acceptance criteria:** All 7 core tables (+ optional `audit_logs`) created with FKs, `CHECK` constraints, and composite indexes; `PRAGMA foreign_keys` returns `1`; `scripts/reset_db.py` backs up existing `cardiosense.db` and recreates cleanly.
- **How to test:** Run `python scripts/reset_db.py`; inspect schema with `sqlite3 instance/cardiosense.db ".schema"`; verify FK violation raises `IntegrityError` in a quick Python check.
- **Risk:** Breaking `app.py` temporarily if `app.py` still queries old column names (`patient_name`). *Mitigation:* Keep Steps 1–3 on a feature branch or update legacy query references in Step 3 before merging to `main`.

### Step 2: Auth Core, Security Config & Admin Bootstrap
- **Goal:** Add `auth.py` (password hashing/policy, session helpers, CSRF middleware, rate limiter, `must_change_password` guard), hardened CORS/secrets config, `/api/auth/*` endpoints, and `flask create-admin` CLI + optional env bootstrap.
- **Files touched:** `auth.py` (new), `app.py`, `requirements.txt` (`Flask-Limiter`), `.env.example`.
- **Acceptance criteria:** Hardcoded Cohere key removed; `SECRET_KEY` validated; `flask create-admin` creates an admin user; `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/change-password`, `POST /api/auth/logout` work with `HttpOnly`/`SameSite=Lax` cookies, CSRF header validation, and rate limiting (`429` after 5 failed attempts/min).
- **How to test:** `flask create-admin`, then `curl -c cookies.txt -b cookies.txt` testing login, `/api/auth/me`, CSRF rejection without `X-CSRF-Token`, password change clearing `must_change_password`, logout, and rate-limit `429`.
- **Risk:** `SESSION_COOKIE_SECURE=True` breaking local HTTP dev. *Mitigation:* Default `SESSION_COOKIE_SECURE` to `True` in production and allow `SESSION_COOKIE_SECURE=0` in `.env` for local HTTP development.

### Step 3: Role & Ownership Decorators on All Existing Endpoints
- **Goal:** Protect every existing endpoint with `@login_required`, `@roles_required(...)`, and object-level ownership checks; migrate existing routes from `patient_name` strings to `patient_id`/`doctor_id` FKs; add `GET /api/consent` and DB-backed `POST /api/consent`.
- **Files touched:** `auth.py`, `app.py`.
- **Acceptance criteria:** Unauthenticated requests to any `/api/*` or `/predict` return `401`; wrong role returns `403`; cross-patient or cross-doctor access returns `403`; `GET /api/patient/status` and `GET /api/patient/history` derive identity strictly from session; `decision` in `/api/doctor/respond` validated against `('INFECTED','HEALTHY','NOISY')`; `app.py:218` no longer leaks `str(e)`.
- **How to test:** Manual `curl` or quick test script verifying `401`/`403` for anon, wrong role, and cross-tenant access on every route.
- **Risk:** Background LLM thread (`app.py:84-99`) running outside request context. *Mitigation:* Thread already uses `app_instance.app_context()` and updates `Case` by `case_id` — verify it continues working with new `Case` schema and writes no session state.

### Step 4: User Management Endpoints (Admin & Doctor)
- **Goal:** Implement admin endpoints (`GET /api/admin/users`, `POST /api/admin/doctors`, `POST /api/admin/patients`, `PATCH /api/admin/users/<id>/status`, `PATCH /api/admin/patients/<id>/reassign`, optional `GET /api/admin/audit-logs`) and doctor patient-creation/listing endpoints (`GET /api/doctor/patients`, `POST /api/doctor/patients`).
- **Files touched:** `routes_admin.py` (new Blueprint) or `app.py`, `routes_doctor.py` (new Blueprint) or `app.py`, `models.py`.
- **Acceptance criteria:** Admin creates doctors (`must_change_password=True`), creates patients assigned to an active doctor, deactivates/reactivates users (blocking self-deactivation and blocking doctor deactivation when active patients are assigned), and reassigns patients; doctor creates patients automatically linked to `doctor_id = current_user.id`; temporary passwords returned once on creation.
- **How to test:** End-to-end API calls creating a doctor as admin, logging in as doctor (forced password change), creating a patient as doctor, reassigning patient as admin, and verifying deactivated user's existing cookie immediately returns `401`.
- **Risk:** Race condition or orphaned patients when deactivating a doctor. *Mitigation:* DB `ON DELETE RESTRICT` + explicit transaction check returning `409 Conflict` if doctor has active patients.

### Step 5: Patient Profile Endpoints & `/predict` Profile Integration
- **Goal:** Add `GET/PATCH /api/patient/profile` (patient edits contact fields only) and `GET/PATCH /api/doctor/patients/<id>` (doctor edits clinical fields only); update `POST /predict` to read `age` and `gender` from `patient_profiles` instead of the request body.
- **Files touched:** `app.py` (or Blueprints), `models.py`.
- **Acceptance criteria:** Patient `PATCH /api/patient/profile` updates phone/emergency contact and rejects/ignores `age`/`date_of_birth`/`gender`/`chronic_conditions`/`doctor_id`; doctor `PATCH /api/doctor/patients/<id>` updates clinical fields only for assigned patients; `/predict` ignores body `name`/`age`/`gender`, snapshots profile `age`/`gender` into `Case` and `PatientHistory`.
- **How to test:** Attempt to `PATCH` `gender` or `doctor_id` as patient (assert unchanged/rejected); run `POST /predict` with spoofed `name`/`age`/`gender` in body and verify DB rows use session patient's profile values.
- **Risk:** Patient created with missing `age` and `date_of_birth`. *Mitigation:* Require either `date_of_birth` or `age` + `gender` at patient creation time (`400` if omitted).

### Step 6: Doctor Dashboard KPIs, Filtered Queue, Case Detail & Linked-Doctor Routing
- **Goal:** Route escalated cases to `patient_profiles.doctor_id`, compute case `priority`, add `GET /api/doctor/dashboard` (KPIs), `GET /api/doctor/cases/<id>` (full waveform + XAI + patient profile side panel), search/filter query params on `GET /api/doctor/cases` and `GET /api/doctor/archive`, and BPM trend data on `GET /api/doctor/patients`.
- **Files touched:** `app.py` (or `routes_doctor.py`), `models.py`.
- **Acceptance criteria:** Escalated case appears **only** in the linked doctor's queue; KPI counts (`pending_cases`, `urgent_cases`, `my_patients`, `reviewed_today`) match DB ground truth; queue endpoint omits heavy `signal`/`xai_weights` arrays while `GET /api/doctor/cases/<id>` returns them along with the patient's profile; `GET /api/doctor/patients` includes last test, status, and last 8 BPM readings.
- **How to test:** Create two doctors with separate patients, escalate cases for each, verify strict isolation, verify search/priority/rate_status filters, and verify `reviewed_today` increments after `POST /api/doctor/respond`.
- **Risk:** N+1 queries on `GET /api/doctor/patients` when computing last test + BPM trend. *Mitigation:* Batch-query recent `PatientHistory` rows for the doctor's patient IDs in one SQL query and group in Python.

### Step 7: Backend Automated Test Suite (`pytest`)
- **Goal:** Build a comprehensive `pytest` suite covering auth, CSRF, rate limiting, `must_change_password`, RBAC, object-level ownership, user management, profile field restrictions, `/predict` profile sourcing, case routing, and dashboard KPIs.
- **Files touched:** `tests/conftest.py`, `tests/test_auth.py`, `tests/test_ownership.py`, `tests/test_users_and_profiles.py`, `tests/test_clinical_flow.py`, `requirements.txt` (or `requirements-dev.txt` with `pytest`). Also update/retire `scripts/test_db_integration.py`.
- **Acceptance criteria:** `pytest` runs cleanly against an isolated temp/in-memory SQLite DB with mocked `predictor.predict` (fast, deterministic) plus 1 real-model smoke test; 100% pass rate covering every endpoint's happy path + `401`/`403`/`404`/`429` paths.
- **How to test:** `pytest -v`.
- **Risk:** `app.py` currently initializes `db.create_all()` and loads `stanford_ecg_model.pth` at module import time. *Mitigation:* Keep tests fast by mocking `app.predict` in unit/API tests and configuring `DATABASE_URL=sqlite:///:memory:` before app fixture setup (or introducing a light `create_app(config)` factory).

### Step 8: Frontend Foundation — Auth, Session, Consent & Modularization
- **Goal:** Split `templates/index.html` CSS/JS into clean static vanilla files (see Section F), remove `#registerView` and all `localStorage` credential/session/consent/message logic, implement `apiFetch()` with CSRF + `401` handling, login form (username + password, no role dropdown), forced password-change modal, logout via API, and DB-backed consent modal.
- **Files touched:** `templates/index.html`, `static/css/main.css`, `static/js/api.js`, `static/js/i18n.js`, `static/js/auth.js`, `static/js/ble.js`, `static/js/patient.js`, `static/js/doctor.js`, `static/js/admin.js`.
- **Acceptance criteria:** Zero references to `user_admin`, `"123"`, `active_session`, `user_*`, `scai_consent_*`, or `messages_*` in frontend code; refreshing the page restores session via `GET /api/auth/me`; first-time login forces password change before entering portal; patient consent is fetched from `GET /api/consent` and saved via `POST /api/consent`; Arabic/English + RTL and dark/light themes work identically.
- **How to test:** Browser manual walkthrough across all 3 roles + inspect `Application -> Local Storage` (only `scai_theme` and `scai_lang` remain) + inspect `Cookies` (`HttpOnly`, `SameSite=Lax`).
- **Risk:** Regressing Web Bluetooth or Chart.js oscilloscope rendering during JS split. *Mitigation:* Extract `ble.js` verbatim first and test Chart.js initialization in both themes before adding new UI views.

### Step 9: Frontend — Patient Profile & Updated Patient Portal
- **Goal:** Add a Patient Profile section/tab showing read-only clinical info (age/DOB, gender, chronic conditions, assigned doctor) and an editable contact form (phone, emergency contact name/phone); remove editable age/gender inputs from the ECG acquisition card; load doctor notes from `GET /api/patient/history` instead of `localStorage`; update `POST /predict`, `GET /api/patient/status`, and `GET /api/patient/history` calls; fix `innerHTML` XSS.
- **Files touched:** `templates/index.html`, `static/js/patient.js`, `static/js/i18n.js`, `static/css/main.css`.
- **Acceptance criteria:** Patient can view assigned doctor, clinical profile, and history; patient can save contact fields via `PATCH /api/patient/profile` and see toast confirmation; `/predict` sends only `{ signal }`; PDF export uses profile data from API; all dynamic strings escaped.
- **How to test:** Log in as patient, edit phone & emergency contact, reload page to verify persistence, run a test ECG (or mock signal in dev console), verify history and PDF report.
- **Risk:** Escalation form currently asks for chronic conditions (`#hist_htn`, etc.) — see Open Question #7.

### Step 10: Frontend — Professional Doctor Dashboard
- **Goal:** Upgrade `#doctorView` with:
  1. **KPI strip:** Pending, Urgent, My Patients, Reviewed Today.
  2. **Tab/Mode switcher:** Case Queue (Active / Archive) and My Patients list.
  3. **Prioritized Case Queue:** Search input, priority/AI-prediction/rate-status filters, urgency badges, click to fetch `GET /api/doctor/cases/<id>`.
  4. **Case Review Workspace:** Zoomable/pannable ECG waveform with XAI red overlay + reset-zoom button, AI attention note (with polling if still generating), **Patient Profile side panel** (demographics, contact, emergency contact, chronic conditions, editable clinical notes, past history), and Decision panel (`NOISY`, `HEALTHY`, `INFECTED` + clinical note).
  5. **My Patients List:** Table/cards showing patient name, last test timestamp, latest status/prediction badge, latest BPM + inline Chart.js sparkline/trend, "View/Edit Clinical Profile" drawer, and **"+ Add Patient" dialog** (displays generated temporary password once with copy button).
- **Files touched:** `templates/index.html`, `static/js/doctor.js`, `static/js/i18n.js`, `static/css/main.css`.
- **Acceptance criteria:** All 5 dashboard features work in Arabic (RTL) and English (LTR) and both dark/light themes; doctor can create a patient, view only their own patients/cases, edit clinical profile fields, and submit decisions that immediately refresh KPIs and queue.
- **How to test:** Log in as Doctor A and Doctor B; create patients under each; verify isolated queues/patient lists, search/filter behavior, waveform zoom/reset, profile editing, and decision submission.
- **Risk:** Layout crowding on smaller viewports. *Mitigation:* Responsive CSS grid (collapsible side panel below `1100px`) reusing existing `--scai-*` tokens.

### Step 11: Frontend — Admin User Management
- **Goal:** Extend `#adminView` with a tabbed layout (`Telemetry & ML Dataset` + `User Management`) plus settings gear (for theme/lang/logout):
  - **Doctors tab/table:** List doctors with status and patient count, "+ Add Doctor" modal (shows temporary password once), Activate/Deactivate button.
  - **Patients tab/table:** List all patients with assigned doctor, "+ Add Patient" modal (with doctor selector), "Reassign Doctor" modal, Activate/Deactivate button.
  - **Audit Log tab (optional):** Filterable recent security events.
- **Files touched:** `templates/index.html`, `static/js/admin.js`, `static/js/i18n.js`, `static/css/main.css`.
- **Acceptance criteria:** Admin can create doctors and patients, reassign patients between active doctors, deactivate/reactivate accounts, and sees clear error toasts if attempting to deactivate a doctor with assigned patients.
- **How to test:** Full admin CRUD walkthrough in browser (AR + EN), verifying created credentials work for login and trigger forced password change.
- **Risk:** Admin accidentally locking themselves out. *Mitigation:* Backend forbids self-deactivation (`400`) and UI hides/disables the deactivate button on the current admin row.

### Step 12: Create/Update `docs/api-contract.md`
- **Goal:** Write an accurate, vanilla-JS-aligned `docs/api-contract.md` documenting all v2 endpoints, auth/session/CSRF headers, error codes, patient state machine, doctor decision matrix, and Arabic symptom strings.
- **Files touched:** `docs/api-contract.md` (new — currently missing from repo).
- **Acceptance criteria:** Every endpoint in `app.py` / Blueprints matches `docs/api-contract.md` 1:1 with request/response examples and status codes.
- **How to test:** Cross-check against `flask routes` output and `pytest` assertions.
- **Risk:** Low.

### Step 13: Docker, Compose & README Updates
- **Goal:** Update `Dockerfile`, `docker-compose.yml`, `.env.example`, and `README.md` for secure production deployment (required `SECRET_KEY`, `SESSION_COOKIE_SECURE`, `TRUST_PROXY`, `flask create-admin` instructions, clean DB init, healthcheck, and removing the leaked Cohere key).
- **Files touched:** `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`.
- **Acceptance criteria:** `docker compose up --build` starts cleanly with `.env`, persists SQLite in `cardiosense_data` volume, `docker compose exec web flask create-admin` works, and `README.md` documents local dev, Docker deployment, admin bootstrap, and running `pytest`.
- **How to test:** Build container and run full login -> patient -> doctor -> admin smoke test against `http://localhost:10000`.
- **Risk:** Container failing to start if `SECRET_KEY` is missing in `.env`. *Mitigation:* Document clearly in `.env.example` and `README.md` with a clear startup error message.

---

## F. Frontend Plan (`templates/index.html`)

### 1. Current Structure of `templates/index.html` (2,757 lines)
- **Head (`1-12`):** Chart.js, Hammer.js, `chartjs-plugin-zoom` via jsDelivr CDN.
- **Inline CSS (`13-1125`, ~1,112 lines):** `oklch` design tokens, dark/light themes, RTL rules, responsive breakpoints (`900px`, `640px`, `600px`), print styles, splash/toast animations.
- **HTML Views (`1127-1545`, `2730-2755`):**
  - `#welcomeScreen` (`1133-1141`)
  - `#loginView` (`1153-1188`) — has role dropdown + register link
  - `#registerView` (`1190-1224`) — **to be deleted**
  - `#settingsView` (`1226-1284`) — theme, lang, logout, patient messages & history
  - `#patientView` (`1286-1403`) — BLE controls, diagnostic banner, comfort card, editable demographics (`#p_age`, `#p_gender`), escalation checkboxes (`#pnlComplaint`), BPM cards, `#ecgChart`
  - `#doctorView` (`1405-1505`) — name-search history lookup, active/archive queue, `#doctorEcgChart`, attention note, decision buttons
  - `#adminView` (`1507-1543`) — 6 telemetry KPI cards + doughnut chart
  - `#consentModal` (`2730-2750`)
- **Inline JS (`1547-2728`, ~1,181 lines):**
  - Translations (`translations.ar` / `translations.en`, `1580-1687`) & `toggleLanguage()` (`1734-1839`)
  - Theme (`toggleTheme()`, `1716-1732`)
  - Broken localStorage auth (`processRegistration`, `processAuthentication`, `logout`, `1883-1920`, `2333-2348`)
  - BLE + live Chart.js (`connectBluetooth`, `disconnectBluetooth`, `processDataChunk`, `startAcquisitionTimer`, `2018-2124`, `2277-2331`)
  - Patient flow (`triggerAIDiagnosis`, `submitCaseToDoctor`, `startDoctorDecisionAutoPolling`, `loadPatientHistory`, `generatePDF`, `requireConsentThen`, `acceptConsent`)
  - Doctor flow (`fetchDoctorCases`, `loadCaseIntoViewer`, `submitDoctorDecision`, `loadDoctorPatientHistory`, `showAttentionNote`)
  - Admin flow (`fetchAdminStats`, `downloadMLDataset`)

### 2. Recommendation: Split into Vanilla Static Files (No Build Step)
**Recommendation: Split CSS and JS out of `templates/index.html` into `static/css/` and `static/js/`** (served by Flask's existing `static_folder='static', static_url_path='/api/static'`), loaded via `<link rel="stylesheet" href="/api/static/css/main.css">` and `<script type="module" src="/api/static/js/app.js"></script>` (or ordered `<script defer>` tags).

**Why split (vs. keeping one monolithic HTML file):**
1. `templates/index.html` is already **2,757 lines**. Adding forced password change, patient profile, full doctor dashboard (KPIs, filtered queue, patient list + BPM trends, case review side panel, add-patient dialog), and admin user management will push it past **4,500+ lines**.
2. **Zero build step preserved:** Native ES modules (`<script type="module">`) work in all modern browsers (and Web Bluetooth already requires Chromium/modern browsers) with no Node, npm, Vite, or Jinja.
3. **Cleaner diffs & safer security review:** Isolating `api.js` (CSRF + `401` handling + `escapeHtml`) and `auth.js` ensures every view uses the same secure fetch wrapper.
4. **Targeted updates:** Steps 8–11 each touch 1–2 small JS files instead of risking merge conflicts inside a 4,500-line file.

**Proposed Static Layout:**
```text
templates/
└── index.html              # Semantic HTML shell for all views & modals (~450-600 lines)
static/
├── css/
│   └── main.css            # Existing theme/RTL/responsive styles + dashboard/table/modal styles
└── js/
    ├── api.js              # apiFetch() (CSRF, JSON, 401 redirect), escapeHtml(), showToast()
    ├── i18n.js             # translations (ar/en), toggleLanguage(), data-i18n helper
    ├── auth.js             # boot session check (/api/auth/me), login, logout, change-password, consent
    ├── ble.js              # Web Bluetooth GATT + live oscilloscope Chart.js
    ├── patient.js          # ECG predict/escalate/poll, patient profile view/edit, history & PDF
    ├── doctor.js           # KPIs, queue filters, case review + profile side panel, patients list + BPM trend, add-patient modal
    ├── admin.js            # Telemetry stats/chart + user management (doctors, patients, reassign, deactivate)
    └── main.js             # App bootstrap, theme init, view router
```
*(Note: migrating from hardcoded `document.getElementById(...).innerText` in `toggleLanguage()` to `data-i18n="key"` attributes on HTML elements will cut `i18n.js` boilerplate in half and prevent missing translations on new screens.)*

### 3. Screen-by-Screen API Needs
| Screen / Modal | Endpoints Consumed | Key UI Elements |
| :--- | :--- | :--- |
| **Login View** | `POST /api/auth/login` | Username + password only (remove role `<select>` and register link); generic error toast on `401`/`429`. |
| **Force Password Change Modal** | `POST /api/auth/change-password`, `POST /api/auth/logout` | Blocking modal shown whenever `user.must_change_password === true`; current + new + confirm password fields. |
| **Consent Modal (Patients)** | `GET /api/consent` (or from `/api/auth/me`), `POST /api/consent` | Shown only when `role === 'patient'` and `!consent_accepted`; waits for `200 OK` before entering portal. |
| **Patient Portal & Profile** | `GET /api/patient/profile`, `PATCH /api/patient/profile`, `POST /predict`, `POST /api/patient/update_case`, `GET /api/patient/status`, `GET /api/patient/history` | Read-only demographics & assigned doctor card; editable contact form (`phone`, `emergency_contact_*`); live BLE + ECG inference; history & doctor notes loaded from DB; PDF print report. |
| **Doctor Dashboard** | `GET /api/doctor/dashboard`, `GET /api/doctor/cases`, `GET /api/doctor/archive`, `GET /api/doctor/cases/<id>`, `POST /api/doctor/respond`, `GET /api/doctor/patients`, `POST /api/doctor/patients`, `GET/PATCH /api/doctor/patients/<id>`, `GET /api/doctor/patients/<id>/history` | 4-card KPI strip; prioritized queue with search + priority/AI/rate filters; zoomable ECG + XAI + AI attention note + patient profile side panel + decision buttons; My Patients list with last test, status, BPM sparkline/trend; "+ Add Patient" modal (shows temp password once). |
| **Admin Portal** | `GET /api/admin/stats`, `GET /api/admin/export_dataset`, `GET /api/admin/users`, `POST /api/admin/doctors`, `POST /api/admin/patients`, `PATCH /api/admin/users/<id>/status`, `PATCH /api/admin/patients/<id>/reassign`, `GET /api/admin/audit-logs` | Existing telemetry KPIs + doughnut chart + CSV export; Doctors table + "+ Add Doctor" + deactivate/activate; Patients table + "+ Add Patient" + "Reassign Doctor" + deactivate/activate; settings gear for theme/lang/logout. |

---

## G. Risks, Open Questions & Clarifications

### Key Technical Risks
1. **Exposed Cohere API key in git history (`app.py:21`, `.env.example:2`):** Removing it from code does not revoke it; the key must be rotated/revoked in the Cohere dashboard.
2. **In-memory rate limiting vs. multi-worker Gunicorn:** `Dockerfile:32` uses `--workers 1 --threads 4`, where `Flask-Limiter` `memory://` works accurately. If workers are ever increased (`--workers > 1`), rate-limit counters will split per process unless backed by Redis.
3. **Web Bluetooth & `Secure` cookies over plain HTTP:** Both Web Bluetooth (`navigator.bluetooth`) and `SESSION_COOKIE_SECURE=True` require HTTPS outside `localhost`. Production deployments must terminate TLS (reverse proxy) with `TRUST_PROXY=1`.
4. **LLM background thread + SQLite concurrency:** Already mitigated by WAL mode + `busy_timeout=5000` (`db.py:18-19`), but `app.py:244-247` spawns unbounded daemon-less `threading.Thread`s. Adding a simple `ThreadPoolExecutor(max_workers=2)` (or keeping single-threaded queue) prevents CPU exhaustion under load.
5. **Doctor queue payload size:** Currently `GET /api/doctor/cases` returns 3,000-float `signal` and 3,000-float `xai_weights` for *every* case in the queue (~60 KB JSON per case). Splitting into list summary + `GET /api/doctor/cases/<id>` detail avoids sluggishness as the queue grows.

### Open Questions (Please Confirm Before Implementation)
1. **Missing `docs/api-contract.md`:** `docs/api-contract.md` is not present in this repository (`docs/` only has an empty `samples/` directory). Should Step 12 create `docs/api-contract.md` from scratch based on the new v2 endpoints and the existing Arabic symptom strings in `templates/index.html:1364-1373, 2225-2233`?
2. **Pending cases on patient reassignment:** When an admin reassigns a patient from Doctor A to Doctor B, should open `ESCALATED` cases automatically transfer to Doctor B (`cases.doctor_id = new_doctor_id`), while already-`ARCHIVED` cases remain attributed to Doctor A (`reviewed_by_id`)?
3. **Deactivating a doctor with assigned patients:** Should deactivation be **blocked with `409 Conflict`** until the admin reassigns all of that doctor's active patients (recommended), or should the API accept an optional `reassign_to_doctor_id` to bulk-reassign in the same call?
4. **Age vs. Date of Birth:** Should `patient_profiles` store `date_of_birth` (preferred so age stays accurate over time) with an optional `age` fallback, or only integer `age`?
5. **Temporary password generation:** When an admin creates a doctor or a doctor/admin creates a patient, should the backend **auto-generate** a strong random temporary password and display it once in a copyable modal, or should the creator type the temporary password (or support both: optional field, auto-generated if left blank)?
6. **Admin access to `POST /api/doctor/respond`:** Should `admin` have **read-only** visibility into clinical cases and patient histories (with only the assigned `doctor` able to submit clinical verdicts), or should `admin` also be able to submit verdicts?
7. **Chronic conditions during escalation (`#pnlComplaint`):** Currently `templates/index.html:1369-1374` lets the patient check chronic conditions (Hypertension, Diabetes, Cardiac History) when escalating a case. Since chronic conditions are now clinical fields owned by the doctor on `patient_profiles`, should the escalation form ask the patient **only for current symptoms** and automatically attach `patient_profiles.chronic_conditions` on the server?
8. **Cohere dependency (`app.py:21`):** Since `co = cohere.ClientV2(...)` is never called in `app.py`, should we remove `cohere` from `app.py` and `requirements.txt`, or keep it as an optional env-only dependency?
9. **Database migrations (`Flask-Migrate`/Alembic):** Are you okay adding `Flask-Migrate` (Alembic) after the clean-slate v2 reset so future schema changes are tracked, or do you prefer keeping `db.create_all()` + explicit scripts?
10. **Patient "Doctor Messages" (`#pnlMessagesBox`):** Currently stored in `localStorage` (`messages_<user>`). Confirm replacing it with server-side `patient_history` records that have a `doctor_note` / `REVIEWED` status so messages persist across browsers and devices.
