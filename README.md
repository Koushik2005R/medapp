# Smart Pill Box Monitoring Backend

This project provides the Phase 1 Flask backend for medication schedules, pill
weight events, and session-based multi-role authentication. SQLite is used by
default. Physical ESP32/HX711 and camera verification remain future integrations;
the current simulator is software-only.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
$env:SECRET_KEY = "generate-a-long-random-value"
flask --app app run --debug
```

`SECRET_KEY` is required; the application will not start with a fallback secret.
The default database is `instance/database.db` (ignored by Git). Set `DATABASE_URL`
to use a different SQLAlchemy-supported database URL. Additive migrations run at
startup and preserve existing records.

## API

### Register and authenticate

```powershell
curl.exe -X POST http://127.0.0.1:5000/api/register `
  -H "Content-Type: application/json" `
  -d '{\"name\":\"Jane Doe\",\"email\":\"jane@example.com\",\"password\":\"change-me-123\",\"role\":\"Patient\"}'

curl.exe -c cookies.txt -X POST http://127.0.0.1:5000/api/login `
  -H "Content-Type: application/json" `
  -d '{\"email\":\"jane@example.com\",\"password\":\"change-me-123\"}'
```

The login session is stored in an HttpOnly, SameSite cookie. Browser API writes
use the CSRF token returned by `GET /api/csrf-token`. Use `GET /api/logout` to
clear the session. Public doctor registration is disabled unless
`ALLOW_PUBLIC_DOCTOR_REGISTRATION=true` is explicitly set.

### Hardware schedule sync

Hardware APIs require a device key in `X-Device-Key`. A doctor provisions a
patient-bound key with `POST /api/doctor/patients/<patient_id>/devices`; the
returned key is shown only once. `GET /api/hardware/get-schedules?patient_id=1`
returns only active reminders for that device's assigned patient.

### Hardware weight event

```powershell
curl.exe -X POST http://127.0.0.1:5000/api/hardware/log-event `
  -H "Content-Type: application/json" `
  -H "X-Device-Key: DEVICE_KEY" `
  -d '{\"patient_id\":1,\"w_before\":100.0,\"w_after\":95.0}'
```

The backend applies the reminder's calibrated tablet weight, expected quantity,
tolerance, calibration offset, response window and median noise filter. A
weight change produces `REMOVAL_DETECTED` and then
`AWAITING_CONFIRMATION`; it never claims that medication was swallowed.
`MISSED` is created only after the configured response window expires.

## Dashboard

Open `http://127.0.0.1:5000/` after starting Flask. The single-page Bootstrap
dashboard selects the Doctor, Patient, or Caregiver portal from the logged-in
user role and updates data with `fetch()` without full-page navigation. Doctors
can link patients, assign caregivers, and sync schedules. Caregivers receive
automatic activity refreshes, can confirm missed events manually, and can
dispatch patient notifications.

## Free communication

Every portal includes a Jitsi Meet call button. Calls use the public
`meet.jit.si` External API and share rooms named
`SmartPillBox_Room_<patient_id>`. Direct messages are stored in the SQLite
`messages` table and are available through `GET /api/messages?patient_id=...`
and `POST /api/messages`.

Missed hardware events automatically create an in-app alert. To add delivery
outside the dashboard, configure either TextBee:

```powershell
$env:TEXTBEE_API_URL = "https://your-textbee-gateway/messages"
$env:TEXTBEE_API_KEY = "your-api-key"
$env:PATIENT_PHONE = "+15551234567"
```

or SMTP fallback:

```powershell
$env:SMTP_HOST = "smtp.gmail.com"
$env:SMTP_PORT = "465"
$env:SMTP_USER = "alerts@example.com"
$env:SMTP_PASSWORD = "gmail-app-password"
```

`POST /api/notify/alert` accepts `{ "patient_id": 1, "message": "..." }`
from an authorized doctor, patient, or caregiver and reports whether delivery
used TextBee, SMTP, or the in-app fallback.

## Phase 3: explainable adherence ML

The adherence model predicts whether the next scheduled dose will be missed.
Because real labelled histories are not available, `model.py` generates a
deterministic synthetic dataset with varied schedules, patient tendencies,
response delays and missed doses. For each upcoming dose, features use only
earlier observations: schedule hour, prior dose count, prior missed rate,
recent missed count, average response delay and schedule variability. The
training/test split is chronological (80/20), and training reports confusion
matrix, precision, recall, F1 and ROC-AUC alongside a majority-class baseline.

The trained preprocessing pipeline and Random Forest are saved as
`instance/adherence_model.joblib` (or `ADHERENCE_MODEL_PATH`). Model metadata
includes the version, data source, split boundaries, feature names, history
requirement and validation limitations. The model is reproducible with seed
42, but synthetic metrics are not clinical accuracy measurements.

Authorized doctors, caregivers, and patients can request:

`GET /api/aiml/risk/<patient_id>`

The response includes `risk_score`, `level`, `historical_factors`,
`model_version`, `data_source`, `history_count`, and
`validation_limitations`. With fewer than three historical dose logs it
returns `INSUFFICIENT_DATA` rather than inventing a prediction.

`GET /api/aiml/model` returns the training/evaluation metadata. These
predictions are advisory research outputs only; they never override dose
safety rules, alarm timeouts, or manual confirmation.
Doctor and caregiver dashboards include the same score as a color-coded risk
badge.

The redesigned dashboards are responsive across patient, doctor, and caregiver
roles. They include actual recorded-event adherence charts where available,
next-dose countdowns, loading/error/empty states, and an AI Lab with model
metrics, confusion matrix, feature influence, and sensor-analysis comparisons.
Synthetic sensor examples are explicitly labelled; no chart presents synthetic
data as a patient measurement.

## Phase 4: intelligent weight-sensor analysis

`sensor_analysis.py` is the shared analysis layer for simulated and future
physical load-cell readings. It provides a configurable Gaussian noise/drift
generator, rolling median filtering, calibration offsets, and window features:
weight change, variance, stability, duration, and change rate.

An Isolation Forest is trained and evaluated on deterministic synthetic
normal, removal, unexpected-increase, excessive-removal, and unstable-signal
scenarios. `GET /api/sensor/model` reports precision, recall, F1, data source,
and validation limitations. The simulator response includes raw and filtered
series, threshold classification, anomaly score, and side-by-side threshold
versus ML results. The UI plots both raw and filtered readings.

Threshold detection answers whether a calibrated medication-removal condition
was met. The Isolation Forest flags unusual signal windows independently.
An anomaly is not a confirmed removal, and a detected removal is not proof of
consumption. Both are advisory synthetic-sensor analyses and do not override
alarm, response-window, or manual-confirmation safety rules.

## Phase 2: medication monitoring simulator

The Doctor, Caregiver, and Patient portals include a live software simulator
with a pill-box diagram, simulated HX711 graph, buzzer indicator, event
timeline, and repeatable scenarios for normal removal, delayed removal, no
response, sensor noise, and unexpected weight changes. Every reading is
explicitly labelled simulated. The state machine is:

`IDLE -> REMINDER_DUE -> ALERTING -> AWAITING_CONFIRMATION ->
REMOVAL_DETECTED -> MANUALLY_CONFIRMED` (or `MISSED` after timeout).

Medication schedules store tablet weight, expected quantity, tolerance,
calibration offset, response window, and noise threshold. Both
`POST /api/simulation/log-event` and the authenticated hardware endpoint use the
same sensor processing interface. `GET /api/simulation/events?patient_id=1`
returns the event timeline, and `POST /api/simulation/events/<event_id>/confirm`
records an explicit manual confirmation. Repeated readings reuse the same
patient/reminder/date event and caregiver notifications are sent at most once.

Doctor and Caregiver views also show the last 50 raw hardware requests from
`GET /api/hardware/traffic`. Weight-based removal is an intake signal only; it
does not prove swallowing.

Useful ESP32/local Wi-Fi checks:

```powershell
curl.exe "http://127.0.0.1:5000/api/hardware/get-schedules?patient_id=1" -H "X-Device-Key: DEVICE_KEY"
curl.exe -X POST "http://127.0.0.1:5000/api/hardware/log-event" `
  -H "Content-Type: application/json" `
  -H "X-Device-Key: DEVICE_KEY" -d '{"patient_id":1,"w_before":100.0,"w_after":95.0}'
curl.exe -X POST "http://127.0.0.1:5000/api/hardware/log-event" `
  -H "Content-Type: application/json" `
  -H "X-Device-Key: DEVICE_KEY" -d '{"patient_id":1,"w_before":100.0,"w_after":99.5}'
```

The equivalent Python client is `scripts/hardware_test.py`:

```powershell
python scripts/hardware_test.py --patient-id 1 --device-key DEVICE_KEY
```

Unified structure:

```text
medapp/
|-- app.py
|-- model.py
|-- instance/database.db       # created automatically; ignored by Git
|-- requirements.txt
|-- README.md
|-- templates/index.html
|-- static/app.js
|-- static/styles.css
`-- scripts/hardware_test.py
```

Single-command startup after Python is installed:

```powershell
python -m pip install -r requirements.txt; $env:SECRET_KEY = "replace-with-a-long-random-value"; python -m flask --app app run
```
