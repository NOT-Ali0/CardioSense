# CardioSense v2 API Contract

> **Scope:** Authoritative HTTP API reference for the CardioSense Flask backend and vanilla-JS frontend (`templates/index.html` + `static/js/*.js`).

## 1. Security & Session Conventions

- **Authentication:** Signed Flask `HttpOnly`, `SameSite=Lax` session cookie (`Secure` in production).
- **CSRF Protection:** `POST /api/auth/login` and `GET /api/auth/me` return `csrf_token`. Every state-changing request (`POST`, `PUT`, `PATCH`, `DELETE`) on `/api/*` and `/predict` (except `POST /api/auth/login`) MUST include `X-CSRF-Token: <csrf_token>` and `Content-Type: application/json`.
- **First-login Password Change:** While `user.must_change_password === true`, all endpoints except `/api/auth/me`, `/api/auth/change-password`, and `/api/auth/logout` return `403`:
  ```json
  { "error": "Password change required before accessing this resource.", "code": "PASSWORD_CHANGE_REQUIRED" }
  ```
- **Standard Error Shape:** `{ "error": "Human-readable message", "code": "OPTIONAL_CODE" }`.

---

## 2. Authentication Endpoints

### `POST /api/auth/login`
- **Auth:** Public | **Rate Limit:** `5/min; 20/hour` per IP+username
- **Request:** `{ "username": "dr_ahmed", "password": "••••••••••" }`
- **Response `200`:**
  ```json
  {
    "user": { "id": 2, "username": "dr_ahmed", "full_name": "Dr. Ahmed", "role": "doctor", "is_active": true, "must_change_password": false },
    "consent_accepted": true,
    "csrf_token": "<random-token>"
  }
  ```
- **Errors:** `401` (`Invalid username or password`), `429` (`Too many requests`).

### `GET /api/auth/me`
- **Auth:** Any authenticated user (allowed when `must_change_password=true`)
- **Response `200`:** Same shape as login.

### `POST /api/auth/change-password`
- **Auth:** Any authenticated user | **CSRF:** Required | **Rate Limit:** `5/min`
- **Request:** `{ "current_password": "...", "new_password": "..." }` (min 10 chars, != username, != current)
- **Response `200`:** `{ "status": "ok", "user": {...}, "consent_accepted": bool, "csrf_token": "<new-token>" }`

### `POST /api/auth/logout`
- **Auth:** Any authenticated user | **CSRF:** Required
- **Response `200`:** `{ "status": "ok" }`

---

## 3. Patient Endpoints (`role == "patient"`)

### `GET /api/consent` & `POST /api/consent`
- Identity from session (`user_id`).
- `GET 200`: `{ "consent": bool, "policy_version": "1.0", "accepted_at": "2026-11-21T17:00:00Z" | null }`
- `POST 200` (CSRF): `{ "status": "ok", "consent": true, "policy_version": "1.0", "accepted_at": "..." }`

### `GET /api/patient/profile`
- **Response `200`:**
  ```json
  {
    "user": { "id": 4, "username": "pat_ali", "full_name": "Ali Hassan", "role": "patient" },
    "doctor": { "id": 2, "username": "dr_ahmed", "full_name": "Dr. Ahmed" },
    "profile": {
      "user_id": 4, "doctor_id": 2, "age": 45, "gender": "M",
      "phone": "0501111111", "emergency_contact_name": "Hassan", "emergency_contact_phone": "0502222222",
      "chronic_conditions": ["ارتفاع ضغط الدم"], "clinical_notes": "..."
    }
  }
  ```

### `PATCH /api/patient/profile`
- **CSRF:** Required. **Editable fields ONLY:** `phone`, `emergency_contact_name`, `emergency_contact_phone`.
- Attempting to send clinical fields (`age`, `gender`, `chronic_conditions`, `clinical_notes`, `doctor_id`) returns `400`.

### `POST /predict`
- **CSRF:** Required | **Rate Limit:** `30/min`
- **Request:** `{ "signal": [float, ...] }` — MUST be a JSON array of exactly `3000` finite numbers (no `NaN`, `Inf`, strings, or booleans; returns `400` otherwise and persists nothing). Ignores any `name`/`age`/`gender`; reads `age`, `gender`, `doctor_id`, `chronic_conditions` from `patient_profiles`.
- **Persistence:** Atomically stores the `PatientHistory` entry, optional `Case` (if escalated), and `EcgRecording` (binary float32 signal + optional float32 attention weights) in a single transaction, including for `REJECTED` tests that passed payload validation.
- **Response `200`:**
  - `SUCCESS`: `{ "status": "SUCCESS", "prediction": "NORMAL"|"AFIB", "confidence": "98.12%", "bpm": 74, "rate_status": "NORMAL" }`
  - `REJECTED`: `{ "status": "REJECTED", "message": "...", "bpm": 72, "rate_status": "CORRUPTED" }`
  - `ESCALATED`: `{ "status": "ESCALATED", "case_id": "...", "prediction": "UNCERTAIN", "confidence": "68.40%", "bpm": 112, "rate_status": "TACHYCARDIA", "priority": "URGENT"|"HIGH"|"NORMAL" }`

### `POST /api/patient/update_case`
- **CSRF:** Required | **Ownership:** `case.patient_id == session.user_id` AND `case.status == "ESCALATED"`
- **Request:** `{ "case_id": "...", "symptoms": ["خفقان/تسارع النبض", "ضيق بالتنفس"] }`
- Server automatically attaches `patient_profiles.chronic_conditions` to `case.chronic` and `case.history`.
- **Canonical Arabic Symptom Strings:**
  - `خفقان/تسارع النبض`
  - `ضيق بالتنفس`
  - `دوار/إرهاق`
  - `ألم بالصدر`

### `GET /api/patient/status`
- **Response `200`:** `{ "status_text": "...", "doctor_note": "..." | null, "updated_at": "..." | null }`

### `GET /api/patient/history`
- Query: `?limit=50&offset=0`
- **Response `200`:** Array of `{ id, ts, created_at, case_id, age, gender, status, prediction, bpm, rate_status, has_waveform, confidence, message, doctor_note }`. Does NOT include raw samples.

### `GET /api/history/<history_id>/waveform` (`patient`, `doctor`, `admin`)
- **Auth & Ownership:**
  - Unauthenticated -> `401`.
  - `patient`: own history records only; `doctor`: assigned patients' history records only; `admin`: all records.
  - Missing history record, unauthorized record, or record without a stored waveform -> `404` (to avoid leaking record existence).
- **Query:** `?max_points=N` (optional positive integer; capped at `n_samples`; returns `400` if invalid). When `max_points < n_samples`, applies peak-preserving min/max decimation returning `<= max_points` points.
- **Response `200`:**
  ```json
  {
    "history_id": 12,
    "sample_rate_hz": 300,
    "n_samples": 3000,
    "samples": [2000.0, ...],
    "attention": [0.12, ...] | null,
    "duration_s": 10.0
  }
  ```
  - `attention` is ALWAYS `null` for `patient` role; populated for `doctor`/`admin` when attention weights were stored.

---

## 4. Doctor Endpoints (`role == "doctor"`, plus read-only `"admin"` where noted)

### `GET /api/doctor/dashboard` (`doctor`, `admin`)
- **Response `200`:** `{ "kpis": { "pending_cases": int, "urgent_cases": int, "my_patients": int, "reviewed_today": int } }`

### `GET /api/doctor/cases` (`doctor`, `admin`)
- Query: `?search=&priority=URGENT|HIGH|NORMAL&raw_prediction=AFIB|NORMAL&rate_status=&sort=priority|confidence|newest`
- Returns lightweight case summaries including `has_waveform` (bool), omitting `signal` and `xai_weights`, strictly scoped to `doctor_id` for doctors.

### `GET /api/doctor/archive` (`doctor`, `admin`)
- Query: `?search=&decision=INFECTED|HEALTHY|NOISY&limit=50&offset=0`
- Returns lightweight archived case summaries including `has_waveform` (bool), omitting `signal` and `xai_weights`.

### `GET /api/doctor/cases/<case_id>` (`doctor`, `admin`)
- Ownership: `case.doctor_id == session.user_id` for doctors (`403` otherwise).
- Returns full case detail including `has_waveform`, `sample_rate_hz`, `signal` (loaded from `EcgRecording`), `xai_weights` (loaded from `EcgRecording`), `ai_attention_note`, and nested `patient_profile`.

### `POST /api/doctor/respond` (`doctor` ONLY — admin is read-only)
- **CSRF:** Required | **Ownership:** `case.doctor_id == session.user_id` AND `case.status == "ESCALATED"`
- **Request:** `{ "case_id": "...", "decision": "INFECTED"|"HEALTHY"|"NOISY", "note": "..." }`
- **Decision Matrix:**
  - `INFECTED` -> History: `REVIEWED / AFIB (CONFIRMED)`, Decision banner: `🔴 DECISION: PATHOLOGY OBSERVED (AFIB)`
  - `HEALTHY` -> History: `REVIEWED / NORMAL (CONFIRMED)`, Decision banner: `🟢 DECISION: WAVEFORM REVIEWED GENERAL STABLE (NORMAL)`
  - `NOISY` -> History: `REVIEWED / REJECTED (NOISE)`, Decision banner: `🟡 DECISION: SIGNAL REJECTED DUE TO ARTIFACT OVERLOAD`

### `GET /api/doctor/patients` (`doctor`, `admin`)
- Query: `?search=`
- Returns assigned patients with `{ id, username, full_name, age, gender, phone, chronic_conditions, last_test_at, last_status, last_prediction, last_bpm, bpm_trend: [{ ts, bpm, status }], pending_cases_count }`.

### `POST /api/doctor/patients` (`doctor`)
- **CSRF:** Required. Auto-links new patient to `doctor_id = session.user_id` with `must_change_password=True`.
- **Request:** `{ username, full_name, password, age (int 1..130), gender ("M"|"F"), phone?, emergency_contact_name?, emergency_contact_phone?, chronic_conditions?, clinical_notes? }`

### `GET /api/doctor/patients/<patient_id>` & `PATCH /api/doctor/patients/<patient_id>` (`doctor`, `admin`)
- Ownership verified for doctors. `PATCH` updates clinical fields (`full_name`, `age`, `gender`, `chronic_conditions`, `clinical_notes`).

### `GET /api/doctor/patients/<patient_id>/history` (`doctor`, `admin`)
- Ownership verified for doctors. Returns patient history array with `has_waveform` (bool) on each item (no raw samples).

---

## 5. Admin Endpoints (`role == "admin"`)

- `GET /api/admin/stats`: Global inference telemetry + `dataset_size` + `users` summary.
- `GET /api/admin/export_dataset`: Streams de-identified `scai_clinical_dataset.csv` and logs `DATASET_EXPORT`.
- `GET /api/admin/users`: `?role=&is_active=&search=` — lists users with doctor patient counts and patient doctor assignments.
- `POST /api/admin/doctors`: Creates doctor (`must_change_password=True`) with admin-supplied `password`.
- `POST /api/admin/patients`: Creates patient assigned to required active `doctor_id`.
- `PATCH /api/admin/users/<user_id>/status`: `{ "is_active": bool }` — blocks self-deactivation (`400`) and blocks deactivating a doctor with active assigned patients (`409 DOCTOR_HAS_ACTIVE_PATIENTS`). Immediately revokes active sessions.
- `PATCH /api/admin/patients/<patient_id>/reassign`: `{ "doctor_id": int }` — updates `patient_profiles.doctor_id` and transfers open `ESCALATED` cases to the new doctor (leaves `ARCHIVED` cases attributed to the original reviewer).
- `GET /api/admin/audit-logs`: `?action=&limit=100&offset=0`.
