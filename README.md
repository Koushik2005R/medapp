# Smart Pill Box Monitoring Backend

This project provides a Flask application for doctor-managed medications,
patient/caregiver medication requests, medication schedules, pill weight events,
and session-based multi-role authentication. SQLite is used by default. Physical
ESP32/HX711 and camera verification remain future integrations; the current
simulator is software-only.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
$env:SECRET_KEY = "generate-a-long-random-value"
flask --app app db upgrade
flask --app app run --debug
```

`SECRET_KEY` is required; the application will not start with a fallback secret.
The default database is `instance/database.db` (ignored by Git). Set `DATABASE_URL`
to use a different SQLAlchemy-supported database URL. Apply versioned database
migrations before starting the application. Migrations preserve existing users,
schedules, messages, notifications, and medication logs, and backfill legacy
schedule names into normalized medication records. Existing hardware traffic
records remain available, with payloads restricted to known telemetry fields
and visibility limited to the patient's assigned doctor or caregiver.

## Medication management and approvals

Medication records, scheduled doses, and sensor events have separate database
models. Doctors linked to a patient can add and edit medications, discontinue
them, or archive them. Discontinued and archived records remain visible in the
patient's history and are never physically deleted.

Patients and their assigned caregivers can view medication records and submit
add, edit, or remove requests. The assigned doctor reviews requests and can
approve them or reject them with a required reason. Pending duplicate requests
are rejected. The requester can read the request status and the doctor's
decision from the portal or these APIs:

- `GET /api/patients/<patient_id>/medications`
- `POST /api/doctor/patients/<patient_id>/medications`
- `PATCH /api/doctor/medications/<medication_id>` (edit, discontinue, or archive)
- `POST /api/patients/<patient_id>/medication-requests`
- `GET /api/patients/<patient_id>/medication-requests`
- `GET /api/doctor/medication-requests`
- `PATCH /api/doctor/medication-requests/<request_id>` with
  `{ "decision": "APPROVED" }` or
  `{ "decision": "REJECTED", "reason": "..." }`
- `GET /api/patients/<patient_id>/medication-audit`
- `GET /api/notifications`

All medication changes and decisions create append-only audit entries, and
notifications are stored for the patient. Only the patient's linked doctor can
approve or reject a request; caregivers can request changes but cannot modify
records directly.

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

Open `http://127.0.0.1:5000/` for the public landing page. Sign in at `/login`
or create a patient or caregiver account at `/register`; doctor registration
requires an administrator invitation unless public doctor registration is
explicitly enabled. Authenticated users are redirected to `/dashboard/doctor`,
`/dashboard/patient`, or `/dashboard/caregiver` according to their role.

The responsive dashboard provides persistent navigation for Dashboard,
Medications, Activity, AI Insights, Messages and Settings. Doctors also have
Patients and Requests; caregivers have Assigned Patients. Doctors can link
patients, assign caregivers, manage medication records and review change
requests. Patients and caregivers can submit medication changes for doctor
approval. Caregivers receive automatic activity refreshes, can confirm missed
events manually and dispatch patient notifications.

## Free communication

Every portal includes a Jitsi Meet call button. Calls use the public
`meet.jit.si` External API and share rooms named
`SmartPillBox_Room_<patient_id>`. Direct messages are stored in the SQLite
`messages` table and are available through `GET /api/messages?patient_id=...`
and `POST /api/messages`.

## PillGuard AI Assistant (Phase 6)

Each authenticated dashboard includes a floating assistant drawer backed by
`POST /api/assistant/chat`. It uses a deterministic intent router and
authorized SQLite queries for active schedules, recorded events, missed-dose
history, adherence statistics, model risk explanations, medication-change
request status, and role-specific application navigation. Patients can ask
about their own records; doctors and caregivers select an assigned patient.
The endpoint requires login and the browser CSRF token.

Answers include supporting record dates/IDs where available and explicitly
report missing history or unsupported questions. The assistant never
prescribes, changes dosage, treats a weight change as proof of swallowing, or
overrides medication safety rules. No patient data is sent to an external AI
provider. Predictions remain advisory synthetic-data research outputs and are
not clinically validated.

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

## Phase 4: supervised adherence prediction experiment

No consented, clinically labelled medication histories are available. The
experiment therefore uses explicitly synthetic histories: each synthetic
patient has a hidden adherence profile and a persistent routine state that
transitions stochastically. Dose outcomes are sampled from that hidden state,
not assigned from a weighted formula over the model's input features. The
default cohort has 64 synthetic patients with 120 daily dose observations each.
The hidden profiles are sampled as 45% steady, 35% variable and 20% disrupted;
each profile has distinct stochastic missed-event and routine-state transition
rates. Only scheduled hour, weekday/weekend and prior recorded outcomes feed
the model. The generator, seed, cohort size, limitations, and features are
documented in `model.py`; this is not a substitute for real-world or clinical
evaluation.

Train the imputation + Random Forest pipeline and produce a JSON holdout report:

```powershell
python scripts/train_adherence_model.py
python scripts/train_adherence_model.py --seed 42 --patients 64 --doses-per-patient 120
```

The reproducible default experiment uses seed 42 and a chronological 80/20
split by scheduled-dose date. It reports precision, recall, F1, confusion
matrix, ROC-AUC when both test classes exist, and the same metrics for a
majority-class baseline. The saved pipeline and metrics are written to
`instance/adherence_experiment/model.joblib` and
`instance/adherence_experiment/model.metrics.json` (or use `--model-path`,
`--report-path`, and `ADHERENCE_MODEL_PATH`). Evaluation figures are the actual
outputs of the synthetic holdout run; they are not clinical accuracy claims.

Reference output from the default seed-42 run: the 1,536-row chronological
test set produced Random Forest precision 0.3925, recall 0.4124, F1 0.4022 and
ROC-AUC 0.7615. Its confusion matrix (actual rows: on-time, missed; predicted
columns: on-time, missed) was `[[1246, 113], [104, 73]]`. The majority baseline
had precision/recall/F1 0.0000 and ROC-AUC 0.5000, with confusion matrix
`[[1359, 0], [177, 0]]`. These are synthetic engineering results only.

`GET /api/aiml/model` returns the saved experiment results and feature
importance. Authorized doctors, caregivers, and patients can request an
upcoming-dose estimate using either `GET /api/aiml/prediction/<patient_id>` or
the compatible `GET /api/aiml/risk/<patient_id>` route. Prediction inputs are
strictly limited to scheduled-dose details and recorded events timestamped
before the upcoming dose. Fewer than seven earlier events return
`INSUFFICIENT_DATA`; patients without an active upcoming schedule return
`NO_UPCOMING_DOSE`. The AI Insights dashboard shows the prediction, provenance,
limitations, confusion matrix, baseline comparison, and feature importance.
Predictions are advisory research outputs and never override medication safety
rules, alarm timeouts, or manual confirmation.
Doctor and caregiver dashboards include the same score as a color-coded risk
badge.

The redesigned dashboards are responsive across patient, doctor, and caregiver
roles. They include actual recorded-event adherence charts where available,
next-dose countdowns, loading/error/empty states, and an AI Lab with model
metrics, confusion matrix, feature influence, and sensor-analysis comparisons.
Synthetic sensor examples are explicitly labelled; no chart presents synthetic
data as a patient measurement.

## Phase 5: sensor ML and simulator

`sensor_analysis.py` is shared by software simulation and authenticated
`POST /api/hardware/log-event`. It applies the scheduled dose's configurable
tablet weight, expected quantity, tolerance, and calibration offset, then
applies a trailing rolling median to load-cell readings. Calibration values are
stored on each scheduled dose and exposed through the existing hardware
schedule endpoint. Threshold rules and the Isolation Forest independently
classify the same filtered sample window.

Train and save the model plus its evaluation report with:

```powershell
python scripts/train_sensor_model.py
```

The versioned Isolation Forest is trained only on seeded synthetic no-removal
and normal-removal windows (training seed 2025). Evaluation uses separate
held-out synthetic windows (test seed 90210): 60 examples each of no removal,
normal removal, sensor noise, unexpected increase, and excessive removal.
`GET /api/sensor/model` reports actual Isolation Forest and threshold-baseline
precision, recall, F1, confusion matrices, Isolation Forest ROC-AUC, and
per-scenario flag rates. `GET /api/sensor/demo` additionally returns repeatable
examples of all five scenarios. The saved artifact and JSON metrics report are
in `instance/sensor_experiment/model.joblib` and
`instance/sensor_experiment/model.metrics.json`; `SENSOR_MODEL_PATH` and CLI
arguments can override the paths.

The default held-out experiment (sensor-iforest-heldout-v2) produced Isolation
Forest precision 0.8398, recall 0.9611, F1 0.8964, ROC-AUC 0.9529 and confusion
matrix `[[87, 33], [7, 173]]`. Threshold anomaly flags produced precision
0.9890, recall 0.9944 and F1 0.9917, with confusion matrix
`[[118, 2], [1, 179]]`. Threshold rules are included as a simple
comparison, not as a clinical detector. All measurements are on synthetic
signals and do not establish physical-device or clinical performance.

The responsive simulator has an interactive eight-slot pill box, patient and
scheduled-dose selection, visible tablet/calibration settings, manual sample
inputs, a raw-versus-filtered weight graph, buzzer and dose-state indicators,
an event timeline, and detector comparison. Repeatable scenarios include
normal removal, no removal, sensor noise, unexpected increase, excessive
removal, delayed removal, and no response. Every reading is labelled simulated.
The state machine is:

`IDLE -> REMINDER_DUE -> ALERTING -> AWAITING_CONFIRMATION ->
REMOVAL_DETECTED -> MANUALLY_CONFIRMED` (or `MISSED` after timeout).

Medication schedules store tablet weight, expected quantity, tolerance,
calibration offset, response window, and noise threshold. Both
`POST /api/simulation/log-event` and the authenticated hardware endpoint use the
same sensor processing interface. `GET /api/simulation/events?patient_id=1`
returns the event timeline, and `POST /api/simulation/events/<event_id>/confirm`
records an explicit manual confirmation. Repeated readings reuse the same
patient/reminder/date event and caregiver notifications are sent at most once.

Doctor and Caregiver views also show the last 50 sanitized hardware requests from
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
python -m pip install -r requirements.txt; $env:SECRET_KEY = "replace-with-a-long-random-value"; python -m flask --app app db upgrade; python -m flask --app app run
```

## Phase 7: final demonstration package

### Architecture and feature boundaries

```text
Browser (Bootstrap + vanilla JS)
        |
        v
Flask routes and role authorization
        |
        +--> SQLite + additive migrations
        +--> shared dose/sensor processing
        +--> adherence Random Forest (synthetic research data)
        +--> sensor Isolation Forest (synthetic signal data)
        `--> deterministic, authorized PillGuard assistant
```

**Implemented:** registration/login, CSRF-protected sessions, doctor/patient/
caregiver authorization, medication schedules, notifications, software dose
simulation, persistent dose events, explainable adherence ML, load-cell
anomaly analysis, dashboards, messaging, and the database-grounded assistant.

**Simulated:** HX711/load-cell readings, sensor noise and calibration, buzzer
states, medication-removal scenarios, synthetic ML training/evaluation data,
and all demo records created by `scripts/demo_setup.py`.

**Future integrations:** physical ESP32/HX711 hardware, production calibration,
camera verification, clinical validation, and external AI providers with an
explicit privacy review. Camera verification is not implemented.

### One-command synthetic demo setup

Run from the repository root. This uses `instance/demo.db`, leaving the normal
`instance/database.db` untouched:

```powershell
$env:SECRET_KEY = "demo-only-change-me"; python scripts/demo_setup.py --reset
```

The script is idempotent without `--reset` and prints separate Doctor, Patient,
and Caregiver accounts. The password for the printed demo accounts is
`DemoPass123!`; the printed device key is for the simulated ESP32 client only.
Start the demo with:

```powershell
$env:SECRET_KEY = "demo-only-change-me"; $env:DATABASE_URL = "sqlite:///$((Resolve-Path .\instance\demo.db).Path.Replace('\','/'))"; python -m flask --app app run --debug
```

### Repeatable demonstration script

1. Open `http://127.0.0.1:5000/` and log in as the Doctor. Show the linked
   patient, schedule configuration, AI Lab, and the synthetic-data badges.
2. Log in as the Patient. Show the next-dose countdown, schedule, assistant,
   and the distinction between recorded weight events and proof of swallowing.
3. Log in as the Caregiver. Show the event timeline, notification, caregiver
   confirmation control, and assistant source dates.
4. In AI Insights, compare the sensor Isolation Forest to the threshold baseline
   on held-out synthetic scenarios. In the simulator, run normal removal, no
   removal, sensor noise, unexpected increase, excessive removal, delayed
   removal, and no response. Explain that removal and anomaly flags are not
   confirmation of swallowing.
5. From a separate terminal, verify the future hardware boundary:

```powershell
python scripts/hardware_test.py --base-url http://127.0.0.1:5000 --patient-id <printed-patient-id> --device-key <printed-device-key>
```

This exercises the authenticated schedule and weight-event contract using
software HTTP requests; it does not claim that physical hardware was tested.

### Automated validation

Run the complete unit and integration suite with the required secret:

```powershell
$env:SECRET_KEY = "test-only-secret"; python -m pytest -q
```

The suite covers authentication, registration, CSRF, role authorization,
scheduling, simulation state transitions, duplicate notifications, hardware
access, assistant permissions, adherence-risk APIs, sensor-analysis APIs, and
dashboard availability. Run `python -m py_compile app.py model.py
sensor_analysis.py assistant.py scripts/demo_setup.py` for a syntax check.

### Screenshots and review evidence

The application provides the major review views directly in the dashboards:
Doctor overview, Patient schedule, Caregiver activity, AI Lab, simulator,
assistant, and the responsive mobile layout. Screenshots should be captured
from the running demo database after completing the demonstration script; no
static or synthetic screenshot is committed as evidence. Suggested filenames
are `doctor-dashboard.png`, `patient-dashboard.png`, `caregiver-dashboard.png`,
`ai-lab.png`, `simulator-normal-removal.png`, `assistant-sources.png`, and
`mobile-dashboard.png`.

### Likely viva questions

- Why is the ML data synthetic? Real labelled patient histories are not
  available; the generator is deterministic and documented, so metrics are
  reproducible but not clinical evidence.
- How is target leakage avoided? Upcoming-dose features use only observations
  earlier than the target event, with a chronological split.
- Does a weight drop prove swallowing? No. It is an intake/removal signal only.
- How are users and devices authorized? Session roles and patient links govern
  records; device keys are hashed and bound to one patient.
- What happens when history is insufficient? The API returns
  `INSUFFICIENT_DATA` instead of inventing a risk score.
- What is the camera status? Camera verification is a documented future
  integration and is not implemented.
- Why are anomaly detection and threshold detection separate? Thresholds
  represent configured removal logic; the Isolation Forest flags unusual
  windows and cannot confirm consumption.
