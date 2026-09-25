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

The backend computes `delta_weight = w_before - w_after`. A delta of at least
2.0 grams is recorded as `Taken`; smaller deltas are recorded as `Missed`.
`trigger_buzzer` is `true` for a missed event so the hardware can alert the
patient or caregiver.

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

## AIML adherence risk

Install the additional dependencies from `requirements.txt`. The application
trains a deterministic `RandomForestClassifier` on startup in `model.py`, then
uses each patient's recent 30-day `PillLog` history and active reminder times
to calculate a non-adherence probability. The feature vector is
`[hour_of_day, past_missed_doses, response_delay_minutes]`.

Authorized doctors, caregivers, and patients can request:

`GET /api/aiml/risk/<patient_id>`

The response is `{ "patient_id": 1, "risk_score": 82.5, "level": "HIGH" }`.
Doctor and caregiver dashboards include the same score as a color-coded risk
badge.

## Phase 5: simulation and hardware verification

The Doctor, Caregiver, and Patient portals include an HX711 weight simulator.
Choose a patient and enter `W_before` and `W_after`; the simulator posts to
`POST /api/simulation/log-event`, applies the 2.0g threshold, writes a normal
`PillLog`, creates a missed-dose alert when needed, and returns the recalculated
AIML risk. Doctor and Caregiver views also show the last 50 raw hardware
requests from `GET /api/hardware/traffic`.

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
