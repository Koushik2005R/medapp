const state = { user: null, timer: null, alarmTimer: null, countdownTimer: null, alarmAudio: null, alarm: null, simulationUnlocked: false, alarmKeys: new Set(), schedules: [], csrfToken: '' };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char]));
const theme = localStorage.getItem('pillguard-theme') || 'light';
document.documentElement.dataset.theme = theme;

async function api(url, options = {}) {
  const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  if (state.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) headers['X-CSRF-Token'] = state.csrfToken;
  const response = await fetch(url, { credentials: 'same-origin', headers, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
function notify(message, type = 'success') { const toast = $('#toast'); toast.className = `alert alert-${type}`; toast.textContent = message; setTimeout(() => toast.classList.add('d-none'), 3500); }
function formData(form) { return Object.fromEntries(new FormData(form)); }
function shell(role, name) { $('#authView').classList.add('d-none'); $('#dashboardView').classList.remove('d-none'); $('#dashboardTitle').textContent = `Welcome, ${name}`; $('#roleBadge').textContent = role; $('#userLabel').textContent = name; $('#logoutBtn').classList.remove('d-none'); }
function showOnly(id) { ['doctorView','patientView','caregiverView'].forEach((item) => $(`#${item}`).classList.toggle('d-none', item !== id)); }
function empty(message) { return `<div class="text-secondary text-center py-4">${esc(message)}</div>`; }
function pillBox(schedules, logs = []) {
  return `<div class="card border-0 shadow-sm p-4 mb-4"><h2 class="h5 mb-3">Interactive Pill Box</h2><div class="pill-box">${schedules.slice(0, 8).map((item, index) => {
    const taken = logs.some((log) => log.status === 'Taken' && log.timestamp.slice(0, 10) === new Date().toISOString().slice(0, 10));
    return `<div class="pill-compartment ${taken ? 'taken' : ''}" data-compartment="${item.compartment || index + 1}"><div class="pill-icon ${taken ? 'removed' : ''}" data-pill="${item.compartment || index + 1}">${taken ? '' : '💊'}</div><div class="small fw-semibold">Compartment ${item.compartment || index + 1}</div><div class="small text-secondary">${esc(item.time)} · ${taken ? 'Empty / Taken' : esc(item.med_name)}</div></div>`;
  }).join('') || empty('No active compartments scheduled.')}</div></div>`;
}
function startBuzzer() {
  if (state.alarmAudio) return;
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) return;
  const context = new AudioContext();
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = 'square'; oscillator.frequency.value = 880; gain.gain.value = .04;
  oscillator.connect(gain); gain.connect(context.destination); oscillator.start();
  state.alarmAudio = { context, oscillator, gain };
  state.alarmAudio.interval = setInterval(() => { oscillator.frequency.value = oscillator.frequency.value === 880 ? 660 : 880; }, 500);
}
function stopBuzzer() {
  if (!state.alarmAudio) return;
  clearInterval(state.alarmAudio.interval); state.alarmAudio.gain.gain.exponentialRampToValueAtTime(.001, state.alarmAudio.context.currentTime + .1); state.alarmAudio.oscillator.stop(state.alarmAudio.context.currentTime + .12); state.alarmAudio.context.close(); state.alarmAudio = null;
}
function unlockSimulation() {
  state.simulationUnlocked = true;
  document.querySelectorAll('[data-simulation-controls]').forEach((element) => element.classList.remove('d-none'));
  document.querySelectorAll('[data-simulation-lock]').forEach((element) => element.classList.add('d-none'));
}
function triggerAlarm(patientId, reminder, patientName = state.user.name) {
  state.alarm = { patientId, reminder };
  startBuzzer();
  if ('speechSynthesis' in window) {
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(`Attention ${patientName}, it is time for your scheduled medication: ${reminder.med_name}. Please access your Pill Box now.`));
  }
  $('#alarmMedication').textContent = `${reminder.med_name} · ${reminder.dosage} at ${reminder.time}`;
  bootstrap.Modal.getOrCreateInstance($('#alarmModal')).show();
  if ('Notification' in window && Notification.permission === 'granted') new Notification('PillGuard medication reminder', {body: 'MEDICATION TIME! Please access your Pill Box now.'});
}
function scheduleKey(patientId, reminder) {
  const now = new Date();
  return `${now.toISOString().slice(0, 10)}:${patientId}:${reminder.id}:${reminder.time}`;
}
function checkAlarms() {
  const now = new Date(); const current = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  state.schedules.forEach(({patientId, reminder, patientName}) => { const key = scheduleKey(patientId, reminder); if (reminder.time === current && !state.alarmKeys.has(key)) { state.alarmKeys.add(key); triggerAlarm(patientId, reminder, patientName); } });
}
function configureAlarms(schedules) {
  state.schedules = schedules.flatMap((item) => (item.reminders || item.schedules || []).map((reminder) => ({patientId: item.id, reminder, patientName: item.name || state.user.name})));
  if (!state.alarmTimer) state.alarmTimer = setInterval(checkAlarms, 1000);
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(() => {});
}
function startNextDoseCountdown(schedules) {
  if (!$('#nextDoseCountdown') || !schedules.length) return;
  const update = () => {
    const now = new Date(); const [hour, minute] = schedules[0].time.split(':').map(Number);
    const next = new Date(now); next.setHours(hour, minute, 0, 0); if (next <= now) next.setDate(next.getDate() + 1);
    const seconds = Math.floor((next - now) / 1000);
    $('#nextDoseCountdown').textContent = `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds % 3600 / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  };
  update(); if (state.countdownTimer) clearInterval(state.countdownTimer); state.countdownTimer = setInterval(update, 1000);
}
function simulationPanel(patients, includeTraffic = true) {
  const options = patients.map((patient) => `<option value="${patient.id}">${esc(patient.name)}</option>`).join('');
  const traffic = includeTraffic ? `<hr><div class="d-flex justify-content-between align-items-center"><h3 class="h6 mb-0">Live hardware API traffic</h3><button id="refreshTrafficBtn" class="btn btn-sm btn-outline-secondary">Refresh</button></div><div id="trafficLog" class="traffic-log mt-2">${empty('No hardware requests logged yet.')}</div>` : '';
  return `<div class="card border-0 shadow-sm p-4 mt-4 simulator-card"><div class="d-flex justify-content-between align-items-start"><div><p class="eyebrow mb-1">SOFTWARE SIMULATION</p><h2 class="h5 mb-1">Live pill-box simulator</h2><p class="small text-secondary mb-0">All readings and events are simulated; weight change does not prove swallowing.</p></div><div class="text-end"><span id="simBuzzer" class="badge text-bg-secondary">Buzzer idle</span><div id="simState" class="badge text-bg-light mt-2">IDLE</div></div></div><div data-simulation-lock class="${state.simulationUnlocked ? 'd-none' : ''} alert alert-warning mt-3 mb-0">Acknowledge the medication alarm to begin a dose simulation.</div><div data-simulation-controls class="${state.simulationUnlocked ? '' : 'd-none'}"><div class="row g-3 mt-1"><div class="col-lg-5"><div class="pill-box simulator-pill-box">${[1,2,3,4,5,6,7,8].map((slot) => `<div class="pill-compartment" data-sim-compartment="${slot}"><div class="pill-icon">💊</div><div class="small fw-semibold">Slot ${slot}</div><div class="small text-secondary">ready</div></div>`).join('')}</div></div><div class="col-lg-7"><div class="d-flex flex-wrap gap-2 mb-2"><select id="scenarioPatient" class="form-select form-select-sm w-auto">${options}</select><button data-scenario="normal_removal" class="btn btn-sm btn-success">Normal removal</button><button data-scenario="delayed_removal" class="btn btn-sm btn-warning">Delayed removal</button><button data-scenario="no_response" class="btn btn-sm btn-danger">No response</button><button data-scenario="noise" class="btn btn-sm btn-outline-secondary">Noise</button><button data-scenario="unexpected_weight" class="btn btn-sm btn-outline-danger">Unexpected weight</button></div><div class="sensor-graph border rounded-3 p-2"><div class="small text-secondary mb-1">Raw vs filtered HX711 stream (simulated grams)</div><svg id="sensorGraph" viewBox="0 0 500 120" role="img" aria-label="Simulated sensor graph"><polyline id="rawSensorLine" points="0,90 50,88 100,91 150,87 200,90 250,88 300,90 350,89 400,90 450,88 500,90" fill="none" stroke="#94a3b8" stroke-width="2"/><polyline id="filteredSensorLine" points="0,90 50,88 100,91 150,87 200,90 250,88 300,90 350,89 400,90 450,88 500,90" fill="none" stroke="#2563eb" stroke-width="3"/></svg><div id="sensorAnalysis" class="small mt-2"></div></div></div></div><div class="row g-3 mt-1"><div class="col-lg-5"><form id="simulationForm" class="row g-2"><div class="col-6"><label class="form-label small">W_before (g)</label><input name="w_before" type="number" min="0" step="0.1" value="100" class="form-control" required></div><div class="col-6"><label class="form-label small">W_after (g)</label><input name="w_after" type="number" min="0" step="0.1" value="99.5" class="form-control" required></div><div class="col-12"><button class="btn btn-primary w-100">Process sensor reading</button></div></form><div id="simulationResult" class="small mt-2"></div></div><div class="col-lg-7"><h3 class="h6 mb-2">Dose event timeline</h3><div id="eventTimeline" class="traffic-log border rounded-3 p-2">${empty('No simulated dose events yet.')}</div><button id="confirmEventBtn" class="btn btn-sm btn-outline-success mt-2 d-none">Manually confirm removal</button></div></div></div>${traffic}</div>`;
}
async function refreshTraffic() {
  const data = await api('/api/hardware/traffic');
  $('#trafficLog').innerHTML = data.traffic.length ? data.traffic.map((entry) => `<div class="border-bottom py-2"><div><span class="badge text-bg-secondary">${esc(entry.method)}</span> <code>${esc(entry.endpoint)}</code> <span class="small text-secondary">HTTP ${entry.response_status} · ${new Date(entry.created_at).toLocaleString()}</span></div><code>${esc(entry.payload)}</code></div>`).join('') : empty('No hardware requests logged yet.');
}
function bindSimulation(patients) {
  $('#simulationForm').onsubmit = async (event) => {
    event.preventDefault();
    const values = formData(event.target);
    values.patient_id = Number($('#scenarioPatient').value); values.w_before = Number(values.w_before); values.w_after = Number(values.w_after);
    try {
      const result = await api('/api/simulation/log-event', {method:'POST', body: JSON.stringify(values)});
      renderSimulationEvent(result.event, result.sensor_analysis);
      notify(`Sensor processed: ${result.state}`);
      if ($('#trafficLog')) await refreshTraffic();
      if (state.user.role === 'Caregiver') await loadCaregiver();
    } catch (error) { notify(error.message, 'danger'); }
  };
  document.querySelectorAll('[data-scenario]').forEach((button) => {
    button.onclick = async () => {
      try {
        const result = await api('/api/simulation/scenario', {method:'POST', body: JSON.stringify({patient_id: Number($('#scenarioPatient').value), scenario: button.dataset.scenario})});
        renderSimulationEvent(result.event, result.sensor_analysis);
        notify(`${button.textContent}: ${result.event.state}`);
      } catch (error) { notify(error.message, 'danger'); }
    };
  });
  $('#confirmEventBtn').onclick = async () => {
    const eventId = $('#confirmEventBtn').dataset.eventId;
    try {
      const result = await api(`/api/simulation/events/${eventId}/confirm`, {method:'POST', body: '{}'});
      renderSimulationEvent(result.event); notify('Removal manually confirmed.');
    } catch (error) { notify(error.message, 'danger'); }
  };
  if ($('#refreshTrafficBtn')) {
    $('#refreshTrafficBtn').onclick = () => refreshTraffic().catch((error) => notify(error.message, 'danger'));
    refreshTraffic().catch((error) => notify(error.message, 'danger'));
  }
  $('#acknowledgeAlarmBtn').onclick = async () => {
    if (!state.alarm) return;
    try {
      await api('/api/alarm/acknowledge', {method:'POST', body: JSON.stringify({patient_id: state.alarm.patientId, reminder_id: state.alarm.reminder.id})});
      stopBuzzer(); window.speechSynthesis?.cancel(); unlockSimulation(); bootstrap.Modal.getOrCreateInstance($('#alarmModal')).hide(); notify('Alarm acknowledged. Awaiting sensor confirmation.');
    } catch (error) { notify(error.message, 'danger'); }
  };
  refreshSimulationEvents(Number($('#scenarioPatient').value));
}
async function refreshSimulationEvents(patientId) {
  if (!$('#eventTimeline')) return;
  try {
    const data = await api(`/api/simulation/events?patient_id=${patientId}`);
    $('#eventTimeline').innerHTML = data.events.length ? data.events.map((event) => `<div class="border-bottom py-2"><span class="badge text-bg-${event.state === 'MISSED' ? 'danger' : event.state === 'MANUALLY_CONFIRMED' ? 'success' : 'primary'}">${esc(event.state)}</span> <span class="small">${new Date(event.scheduled_at).toLocaleString()}</span><br><span class="small text-secondary">${esc(event.source)} · ${esc(event.verification_method || 'awaiting verification')} · ${event.filtered_delta == null ? '--' : Number(event.filtered_delta).toFixed(2) + 'g'}</span></div>`).join('') : empty('No simulated dose events yet.');
  } catch (error) { notify(error.message, 'danger'); }
}
function renderSimulationEvent(event, analysis = {}) {
  if (!event) return;
  $('#simState').textContent = event.state;
  $('#simState').className = `badge text-bg-${event.state === 'MISSED' ? 'danger' : event.state === 'MANUALLY_CONFIRMED' ? 'success' : 'primary'} mt-2`;
  $('#simBuzzer').textContent = event.state === 'ALERTING' ? 'Buzzer active' : 'Buzzer idle';
  $('#simBuzzer').className = `badge ${event.state === 'ALERTING' ? 'text-bg-danger' : 'text-bg-secondary'}`;
  const confirm = $('#confirmEventBtn');
  if (confirm) { confirm.dataset.eventId = event.id; confirm.classList.toggle('d-none', !['AWAITING_CONFIRMATION', 'REMOVAL_DETECTED'].includes(event.state)); }
  if (event.compartment) document.querySelector(`[data-sim-compartment="${event.compartment}"]`)?.classList.toggle('taken', event.state === 'MANUALLY_CONFIRMED');
  if (analysis.raw_readings && $('#sensorGraph')) {
    const points = (values) => values.map((value, index) => `${(index / Math.max(1, values.length - 1)) * 500},${Math.max(8, Math.min(112, 100 - value))}`).join(' ');
    $('#rawSensorLine').setAttribute('points', points(analysis.raw_readings));
    $('#filteredSensorLine').setAttribute('points', points(analysis.filtered_readings));
    const threshold = analysis.threshold_detection;
    const anomaly = analysis.anomaly_detection;
    $('#sensorAnalysis').innerHTML = `<span class="badge ${threshold.removal_detected ? 'text-bg-success' : 'text-bg-secondary'}">Threshold: ${esc(threshold.event)}</span> <span class="badge ${anomaly.is_anomaly ? 'text-bg-danger' : 'text-bg-primary'}">ML: ${esc(anomaly.event)}</span><div class="text-secondary mt-1">Change ${analysis.features.weight_change.toFixed(2)}g · variance ${analysis.features.variance.toFixed(3)} · anomaly score ${anomaly.anomaly_score}</div><div class="text-warning mt-1">${esc(analysis.interpretation)}</div>`;
  }
  refreshSimulationEvents(Number($('#scenarioPatient').value));
}
let jitsiApi = null;
function communicationPanel(patients, selectedId = '') {
  const options = patients.map((patient) => `<option value="${patient.id}" ${String(patient.id) === String(selectedId) ? 'selected' : ''}>${esc(patient.name)}</option>`).join('');
  return `<div class="card border-0 shadow-sm p-4 mt-4"><div class="d-flex justify-content-between align-items-center"><h2 class="h5 mb-0">Care team communication</h2><span class="small text-secondary">Secure shared room</span></div><div class="row g-2 mt-2"><div class="col-md-4"><select id="communicationPatient" class="form-select">${options || '<option value="">No patient available</option>'}</select></div><div class="col-md-3"><button id="consultBtn" class="btn btn-primary w-100" ${options ? '' : 'disabled'}>Consult via Call</button></div></div><div class="border rounded-3 p-3 mt-3"><div id="messageList" class="message-list mb-2">${empty('Select a patient to load messages.')}</div><form id="messageForm" class="d-flex gap-2"><input name="message" class="form-control" placeholder="Write a direct message..." maxlength="2000" required><button class="btn btn-outline-primary">Send</button></form></div></div>`;
}
function bindCommunication(patients, selectedId = '') {
  const selector = $('#communicationPatient');
  if (!selector) return;
  const loadMessages = async () => {
    if (!selector.value) return;
    try {
      const data = await api(`/api/messages?patient_id=${selector.value}`);
      $('#messageList').innerHTML = data.messages.length ? data.messages.map((message) => `<div class="mb-2"><div class="message-bubble bg-light"><strong>${esc(message.sender_name)}</strong><br>${esc(message.body)}</div><small class="text-secondary">${new Date(message.created_at).toLocaleString()}</small></div>`).join('') : empty('No messages yet.');
      $('#messageList').scrollTop = $('#messageList').scrollHeight;
    } catch (error) { notify(error.message, 'danger'); }
  };
  selector.onchange = loadMessages;
  if (selectedId) loadMessages();
  $('#messageForm').onsubmit = async (event) => {
    event.preventDefault();
    try {
      await api('/api/messages', {method:'POST', body: JSON.stringify({patient_id: Number(selector.value), message: formData(event.target).message})});
      event.target.reset(); await loadMessages();
    } catch (error) { notify(error.message, 'danger'); }
  };
  $('#consultBtn').onclick = () => {
    if (!selector.value) return;
    const roomName = `SmartPillBox_Room_${selector.value}`;
    $('#jitsiFrame').replaceChildren();
    jitsiApi = new JitsiMeetExternalAPI('meet.jit.si', {roomName, parentNode: $('#jitsiFrame'), width: '100%', height: 620});
    bootstrap.Modal.getOrCreateInstance($('#consultModal')).show();
  };
}

async function loadDoctor() {
  const data = await api('/api/doctor/patients'); showOnly('doctorView');
  const patients = data.patients;
  $('#doctorView').innerHTML = `<div class="row g-4">
    <div class="col-lg-4"><div class="card border-0 shadow-sm p-4"><h2 class="h5">Add a patient</h2><p class="small text-secondary">Link an existing patient by email.</p><form id="linkPatientForm" class="d-flex gap-2"><input name="email" type="email" class="form-control" placeholder="patient@email.com" required><button class="btn btn-primary">Add</button></form><div class="border-top mt-4 pt-3"><div class="small fw-semibold mb-2">Patient directory</div><form id="directoryForm" class="d-flex gap-2"><input name="search" class="form-control form-control-sm" placeholder="Search name or email"><button class="btn btn-sm btn-outline-primary">Search</button></form><div id="directoryResults" class="small mt-2"></div></div></div>
      <div class="card border-0 shadow-sm p-4 mt-4"><h2 class="h5">New medication schedule</h2><form id="scheduleForm"><select name="patient_id" class="form-select mb-2" required><option value="">Choose patient</option>${patients.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select><input name="med_name" class="form-control mb-2" placeholder="Medication name" required><div class="row g-2 mb-2"><div class="col"><input name="time" type="time" class="form-control" required></div><div class="col"><input name="compartment" type="number" min="1" max="8" class="form-control" placeholder="Compartment"></div></div><input name="dosage" class="form-control mb-2" placeholder="Dosage" required><div class="row g-2 mb-2"><div class="col"><label class="form-label small">Tablet g</label><input name="tablet_weight" type="number" min=".01" step=".01" value=".5" class="form-control"></div><div class="col"><label class="form-label small">Quantity</label><input name="expected_quantity" type="number" min="1" max="100" value="1" class="form-control"></div><div class="col"><label class="form-label small">Tolerance g</label><input name="tolerance" type="number" min=".01" step=".01" value=".2" class="form-control"></div></div><div class="row g-2 mb-3"><div class="col"><label class="form-label small">Response min</label><input name="response_window_minutes" type="number" min="1" max="1440" value="30" class="form-control"></div><div class="col"><label class="form-label small">Calibration g</label><input name="calibration_offset" type="number" step=".01" value="0" class="form-control"></div><div class="col"><label class="form-label small">Noise g</label><input name="noise_threshold" type="number" min=".01" step=".01" value=".15" class="form-control"></div></div><button class="btn btn-primary w-100">Sync schedule</button></form></div></div>
    <div class="col-lg-8"><div class="card border-0 shadow-sm p-4"><div class="d-flex justify-content-between"><h2 class="h5">Assigned patients</h2><span class="text-secondary small">${patients.length} linked</span></div>${patients.length ? patients.map(patientCard).join('') : empty('No patients linked yet.')}</div>${patients[0] ? aiLabPanel(patients[0].id, patients[0].risk, patients[0].logs) : empty('Link a patient to open the AI Lab.')}${communicationPanel(patients, patients[0]?.id)}${simulationPanel(patients)}</div></div>`;
  $('#linkPatientForm').onsubmit = async (event) => { event.preventDefault(); try { await api('/api/doctor/patients/link', {method:'POST', body: JSON.stringify(formData(event.target))}); notify('Patient linked'); loadDoctor(); } catch (error) { notify(error.message, 'danger'); } };
  $('#scheduleForm').onsubmit = async (event) => { event.preventDefault(); const values = formData(event.target); try { await api(`/api/doctor/patients/${values.patient_id}/schedules`, {method:'POST', body: JSON.stringify(values)}); notify('Schedule synced to patient'); event.target.reset(); loadDoctor(); } catch (error) { notify(error.message, 'danger'); } };
  $('#directoryForm').onsubmit = async (event) => { event.preventDefault(); try { const results = await api(`/api/doctor/directory?search=${encodeURIComponent(formData(event.target).search)}`); $('#directoryResults').innerHTML = results.patients.length ? results.patients.map((p) => `<div class="d-flex justify-content-between align-items-center border-bottom py-2"><span>${esc(p.name)}<br><span class="text-secondary">${esc(p.email)}</span></span>${p.linked_to_doctor ? '<span class="badge text-bg-success">Linked</span>' : `<button data-directory-link="${p.email}" class="btn btn-sm btn-outline-primary">Link</button>`}</div>`).join('') : empty('No patients found.'); document.querySelectorAll('[data-directory-link]').forEach((button) => { button.onclick = async () => { try { await api('/api/doctor/patients/link', {method:'POST', body: JSON.stringify({email: button.dataset.directoryLink})}); notify('Patient linked'); await loadDoctor(); } catch (error) { notify(error.message, 'danger'); } }; }); } catch (error) { notify(error.message, 'danger'); } };
  document.querySelectorAll('[data-link-caregiver]').forEach((button) => { button.onclick = () => { $('#caregiverLinkForm input[name="patient_id"]').value = button.dataset.patientId; bootstrap.Modal.getOrCreateInstance($('#caregiverLinkModal')).show(); }; });
  $('#caregiverLinkForm').onsubmit = async (event) => { event.preventDefault(); const values = formData(event.target); try { await api(`/api/doctor/patients/${values.patient_id}/caregiver`, {method:'POST', body: JSON.stringify({email: values.email})}); bootstrap.Modal.getOrCreateInstance($('#caregiverLinkModal')).hide(); event.target.reset(); notify('Caregiver linked'); await loadDoctor(); } catch (error) { notify(error.message, 'danger'); } };
  bindCommunication(patients, patients[0]?.id);
  bindSimulation(patients);
  if (patients[0]) bindAiLab();
  configureAlarms(patients);
}
function riskBadge(risk) { const tone = risk.level === 'HIGH' ? 'danger' : risk.level === 'MEDIUM' ? 'warning' : risk.level === 'INSUFFICIENT_DATA' ? 'secondary' : 'success'; return `<span class="badge text-bg-${tone}">${risk.level === 'INSUFFICIENT_DATA' ? 'Awaiting history' : `Adherence risk: ${Number(risk.risk_score).toFixed(1)}% ${esc(risk.level)}`}</span>`; }
function statCard(label, value, detail = '') { return `<div class="metric-card"><div class="eyebrow">${esc(label)}</div><div class="metric">${esc(value)}</div><div class="small text-secondary">${esc(detail)}</div></div>`; }
function adherenceChart(logs = [], days = 7) {
  const now = new Date(); const buckets = Array.from({length: days}, (_, index) => { const date = new Date(now); date.setDate(now.getDate() - (days - index - 1)); return {date: date.toISOString().slice(0, 10), taken: 0, missed: 0}; });
  logs.forEach((log) => { const bucket = buckets.find((item) => item.date === log.timestamp.slice(0, 10)); if (bucket) log.status === 'Missed' ? bucket.missed++ : bucket.taken++; });
  const max = Math.max(1, ...buckets.map((item) => item.taken + item.missed));
  return `<div class="chart-bars" role="img" aria-label="Adherence for the last ${days} days">${buckets.map((item) => { const total = item.taken + item.missed; const height = Math.round((total / max) * 100); const takenHeight = total ? Math.round((item.taken / total) * height) : 0; return `<div class="chart-column"><div class="chart-value">${total ? `${Math.round(item.taken / total * 100)}%` : '—'}</div><div class="chart-track" style="height:${Math.max(8, height)}%"><span class="chart-taken" style="height:${takenHeight}%"></span><span class="chart-missed" style="height:${Math.max(0, height - takenHeight)}%"></span></div><div class="chart-label">${item.date.slice(5)}</div></div>`; }).join('')}</div>`;
}
function aiLabPanel(patientId, risk, logs = []) {
  return `<section class="card border-0 shadow-sm p-4 mt-4 ai-lab"><div class="d-flex justify-content-between align-items-start gap-3"><div><p class="eyebrow mb-1">AI LAB · RESEARCH PREVIEW</p><h2 class="h4 mb-1">Explainable medication intelligence</h2><p class="small text-secondary mb-0">Advisory analysis only. Predictions never override medication safety rules.</p></div><span class="badge text-bg-light">Synthetic validation clearly labelled</span></div><div class="row g-3 mt-2"><div class="col-md-4">${statCard('Next-dose risk', risk.level === 'INSUFFICIENT_DATA' ? 'Insufficient data' : `${Number(risk.risk_score).toFixed(1)}%`, risk.level === 'INSUFFICIENT_DATA' ? 'Need more history' : risk.level)}</div><div class="col-md-4">${statCard('7-day adherence', adherencePercent(logs, 7), 'Actual recorded events')}</div><div class="col-md-4">${statCard('30-day adherence', adherencePercent(logs, 30), 'Actual recorded events')}</div></div><div class="row g-4 mt-1"><div class="col-lg-6"><h3 class="h6">Seven-day adherence</h3>${adherenceChart(logs, 7)}<h3 class="h6 mt-4">Thirty-day adherence</h3>${adherenceChart(logs, 30)}</div><div class="col-lg-6"><div id="aiMetrics" class="loading-block">Loading model evaluation…</div><div id="featureImportance" class="mt-3"></div><div id="sensorLab" class="mt-4">Loading sensor analysis…</div></div></div><div class="mt-3"><h3 class="h6">Historical factors</h3><div class="small text-secondary">${risk.historical_factors?.map(esc).join(' · ') || 'No explanatory factors available.'}</div><div class="small text-warning mt-2">${esc(risk.validation_limitations || 'Synthetic research output; not clinically validated.')}</div></div></section>`;
}
function adherencePercent(logs, days) { const cutoff = Date.now() - days * 86400000; const recent = logs.filter((log) => new Date(log.timestamp).getTime() >= cutoff); if (!recent.length) return '—'; return `${Math.round(recent.filter((log) => log.status !== 'Missed').length / recent.length * 100)}%`; }
async function bindAiLab() {
  try {
    const [model, sensor] = await Promise.all([api('/api/aiml/model'), api('/api/sensor/demo')]);
    $('#aiMetrics').innerHTML = `<h3 class="h6">Model evaluation <span class="badge text-bg-secondary">synthetic chronological holdout</span></h3><div class="metric-grid">${statCard('Precision', model.evaluation.precision)}${statCard('Recall', model.evaluation.recall)}${statCard('F1', model.evaluation.f1)}${statCard('ROC-AUC', model.evaluation.roc_auc ?? 'n/a')}</div><div class="confusion-matrix mt-3" aria-label="Confusion matrix"><span></span><strong>Predicted no</strong><strong>Predicted missed</strong><strong>Actual no</strong><span>${model.evaluation.confusion_matrix[0][0]}</span><span>${model.evaluation.confusion_matrix[0][1]}</span><strong>Actual missed</strong><span>${model.evaluation.confusion_matrix[1][0]}</span><span>${model.evaluation.confusion_matrix[1][1]}</span></div>`;
    $('#featureImportance').innerHTML = `<h3 class="h6">Feature influence</h3>${Object.entries(model.feature_importance || {}).map(([name, value]) => `<div class="importance-row"><span>${esc(name.replaceAll('_', ' '))}</span><div class="importance-bar"><i style="width:${Math.min(100, value * 100)}%"></i></div><span>${(value * 100).toFixed(1)}%</span></div>`).join('')}`;
    $('#sensorLab').innerHTML = `<h3 class="h6">Sensor anomaly lab <span class="badge text-bg-info">synthetic demo</span></h3>${sensor.scenarios.map((item) => `<div class="sensor-scenario"><strong>${esc(item.scenario)}</strong><span class="badge ${item.anomaly_detection.is_anomaly ? 'text-bg-danger' : 'text-bg-success'}">ML ${esc(item.anomaly_detection.event)}</span><span class="badge text-bg-light">Threshold ${esc(item.threshold_detection.event)}</span><small>score ${item.anomaly_detection.anomaly_score}</small></div>`).join('')}<div class="small text-secondary mt-2">${esc(sensor.validation_limitations)}</div>`;
  } catch (error) { if ($('#aiMetrics')) $('#aiMetrics').innerHTML = `<div class="alert alert-warning">AI Lab unavailable: ${esc(error.message)}</div>`; }
}
function patientCard(patient) { const latest = patient.logs?.[0]; return `<article class="border-top py-3"><div class="d-flex justify-content-between align-items-start gap-2"><div><h3 class="h6 mb-1">${esc(patient.name)}</h3><small class="text-secondary">${esc(patient.email)}</small><div class="small text-secondary mt-2">${patient.caregiver ? `Caregiver: ${esc(patient.caregiver.email)}` : 'No caregiver linked'}</div></div><div class="text-end">${riskBadge(patient.risk)}<div class="small text-secondary mt-1">${patient.reminders.length} active schedules</div></div></div>${latest ? `<div class="small mt-2"><span class="badge ${latest.status === 'Taken' ? 'text-bg-success' : latest.status === 'Missed' ? 'text-bg-danger' : 'text-bg-primary'}">${latest.status === 'Taken' ? 'Taken (Weight Verified)' : latest.status === 'Manual Override' ? 'Taken (Caregiver Override)' : 'Missed (Timeout)'}</span> ${new Date(latest.timestamp).toLocaleString()}</div>` : ''}<button data-link-caregiver data-patient-id="${patient.id}" class="btn btn-sm btn-outline-primary mt-2">${patient.caregiver ? 'Change caregiver' : 'Link caregiver'}</button></article>`; }

async function loadPatient() {
  const data = await api('/api/patient/dashboard'); showOnly('patientView');
  if (data.logs[0] && ['Taken', 'Manual Override'].includes(data.logs[0].status)) {
    stopBuzzer();
  }
  const doctor = data.doctor;
  const team = [doctor, data.caregiver].filter(Boolean);
  $('#patientView').innerHTML = `<div class="provider-banner p-4 mb-4"><p class="small opacity-75 mb-1">PATIENT OVERVIEW <span class="badge text-bg-light ms-2">Connected account</span></p><h2 class="h4 mb-1">${doctor ? `Dr. ${esc(doctor.name)}` : 'No doctor assigned yet'}</h2><div class="small">${doctor ? esc(doctor.email) : 'Connect with your care team to begin.'}${data.caregiver ? ` · Caregiver: ${esc(data.caregiver.name)} (${esc(data.caregiver.email)})` : ''}</div></div><div id="patientConfirmation"></div>${pillBox(data.schedules, data.logs)}<div class="row g-4"><div class="col-lg-7"><div class="card border-0 shadow-sm p-4"><h2 class="h5 mb-3">Medication schedule</h2><div class="row g-3">${data.schedules.length ? data.schedules.map(scheduleCard).join('') : empty('Your care team has not added a schedule yet.')}</div></div></div><div class="col-lg-5"><div class="card border-0 shadow-sm p-4"><div class="d-flex justify-content-between"><h2 class="h5">Next dose</h2><span class="status-dot mt-2"></span></div><div id="nextDoseCountdown" class="metric mt-3">${data.schedules[0] ? esc(data.schedules[0].time) : '--:--'}</div><p class="text-secondary mb-0">${data.schedules[0] ? `${esc(data.schedules[0].med_name)} · ${esc(data.schedules[0].dosage)}` : 'Nothing scheduled'}</p></div></div></div>${aiLabPanel(state.user.id, data.risk || {level:'INSUFFICIENT_DATA', historical_factors:[], validation_limitations:'No prediction available.'}, data.logs)}${communicationPanel([{id: state.user.id, name: 'My care team'}], state.user.id)}${simulationPanel([{id: state.user.id, name: 'My account'}], false)}`;
  bindCommunication([{id: state.user.id, name: 'My care team'}], state.user.id);
  bindSimulation([{id: state.user.id, name: 'My account'}]);
  configureAlarms([{id: state.user.id, reminders: data.schedules}]);
  startNextDoseCountdown(data.schedules);
  bindAiLab();
}
function scheduleCard(item) { return `<div class="col-md-6"><div class="border rounded-3 p-3 h-100"><div class="schedule-time">${esc(item.time)}</div><div class="fw-semibold">${esc(item.med_name)}</div><div class="small text-secondary">${esc(item.dosage)} · Set by ${esc(item.set_by_role)}</div></div></div>`; }

async function loadCaregiver() {
  const data = await api('/api/caregiver/dashboard'); showOnly('caregiverView');
  const selectedPatient = data.patients[0];
  $('#caregiverView').innerHTML = `<div class="row g-3 mb-4">${data.patients.length ? data.patients.map(patient => `<div class="col-md-6 col-xl-4"><div class="card border-0 shadow-sm p-3"><div class="small text-secondary">PATIENT</div><div class="fw-semibold mb-2">${esc(patient.name)}</div>${riskBadge(patient.risk)}<div class="small text-secondary mt-2">${patient.reminders.length} active schedules</div></div></div>`).join('') : empty('No patients are assigned to you yet.')}</div><div class="card border-0 shadow-sm p-4"><div class="d-flex flex-wrap justify-content-between gap-2 mb-3"><div><h2 class="h5 mb-1">Live activity</h2><p class="small text-secondary mb-0">Refreshing automatically every 10 seconds</p></div><button id="notifyBtn" class="btn btn-primary btn-sm">Dispatch instant notification</button></div>${data.logs.length ? `<div class="table-responsive"><table class="table align-middle mb-0"><thead><tr><th>Patient</th><th>Timestamp</th><th>Weight delta</th><th>Status</th><th></th></tr></thead><tbody>${data.logs.map(logRow).join('')}</tbody></table></div>` : empty('No weight activity recorded yet.')}</div>${selectedPatient ? aiLabPanel(selectedPatient.id, selectedPatient.risk, data.logs.filter((log) => log.patient_id === selectedPatient.id)) : ''}${communicationPanel(data.patients, data.patients[0]?.id)}${simulationPanel(data.patients)}`;
  document.querySelectorAll('[data-override]').forEach((button) => button.onclick = async () => { const reason = window.prompt('Why are you confirming this dose?'); if (!reason) return; try { await api(`/api/caregiver/logs/${button.dataset.override}/override`, {method:'POST', body: JSON.stringify({reason})}); stopBuzzer(); notify('Medication marked as taken'); loadCaregiver(); } catch (error) { notify(error.message, 'danger'); } });
  $('#notifyBtn').onclick = () => dispatchNotification(data.patients);
  bindCommunication(data.patients, data.patients[0]?.id);
  bindSimulation(data.patients);
  configureAlarms(data.patients);
  if (selectedPatient) bindAiLab();
}
function logRow(log) { const canOverride = log.status === 'Missed'; const label = log.status === 'Taken' ? 'Taken (Weight Verified)' : log.status === 'Manual Override' ? 'Taken (Caregiver Override)' : 'Missed (Timeout)'; return `<tr><td class="fw-semibold">${esc(log.patient_name)}</td><td class="small">${new Date(log.timestamp).toLocaleString()}</td><td>${Number(log.delta_weight).toFixed(1)}g</td><td><span class="badge ${log.status === 'Taken' ? 'text-bg-success' : log.status === 'Missed' ? 'text-bg-danger' : 'text-bg-primary'}">${label}</span></td><td>${canOverride ? `<button data-override="${log.id}" class="btn btn-sm btn-outline-success">Mark taken</button>` : ''}</td></tr>`; }
async function dispatchNotification(patients) { if (!patients.length) return notify('No assigned patients', 'warning'); const patient = prompt(`Patient ID (${patients.map(p => `${p.id}: ${p.name}`).join(', ')}):`); const message = prompt('Notification message:'); if (!patient || !message) return; try { await api('/api/caregiver/notify', {method:'POST', body: JSON.stringify({patient_id: Number(patient), message})}); notify('Notification dispatched'); } catch (error) { notify(error.message, 'danger'); } }

async function loadDashboard() { if (state.user.role === 'Doctor') await loadDoctor(); else if (state.user.role === 'Patient') await loadPatient(); else await loadCaregiver(); }
$('#loginForm').onsubmit = async (event) => { event.preventDefault(); try { const data = await api('/api/login', {method:'POST', body: JSON.stringify(formData(event.target))}); state.user = data.user; state.csrfToken = (await api('/api/csrf-token')).csrf_token; shell(state.user.role, state.user.name); await loadDashboard(); state.timer = setInterval(loadDashboard, 10000); } catch (error) { notify(error.message, 'danger'); } };
$('#showSignupBtn').onclick = () => { $('#loginForm').classList.add('d-none'); $('#signupForm').classList.remove('d-none'); $('#showSignupBtn').classList.add('d-none'); };
$('#showLoginBtn').onclick = () => { $('#signupForm').classList.add('d-none'); $('#loginForm').classList.remove('d-none'); $('#showSignupBtn').classList.remove('d-none'); };
$('#signupForm').onsubmit = async (event) => {
  event.preventDefault();
  try {
    const data = await api('/api/register', {method: 'POST', body: JSON.stringify(formData(event.target))});
    event.target.reset();
    $('#signupForm').classList.add('d-none');
    $('#loginForm').classList.remove('d-none');
    $('#showSignupBtn').classList.remove('d-none');
    $('#loginForm input[name="email"]').value = data.user.email;
    notify('Account created. Sign in to continue.');
  } catch (error) { notify(error.message, 'danger'); }
};
$('#logoutBtn').onclick = async () => { await api('/api/logout'); clearInterval(state.timer); clearInterval(state.alarmTimer); clearInterval(state.countdownTimer); stopBuzzer(); state.user = null; state.csrfToken = ''; $('#dashboardView').classList.add('d-none'); $('#authView').classList.remove('d-none'); $('#logoutBtn').classList.add('d-none'); $('#userLabel').textContent = ''; };
$('#themeToggle').onclick = () => { const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = next; localStorage.setItem('pillguard-theme', next); $('#themeToggle').textContent = next === 'dark' ? '☀' : '☾'; };
$('#themeToggle').textContent = theme === 'dark' ? '☀' : '☾';
$('#aboutBtn').onclick = () => bootstrap.Modal.getOrCreateInstance($('#aboutModal')).show();
