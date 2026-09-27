const state = { user: null, timer: null, alarmTimer: null, countdownTimer: null, alarmAudio: null, alarm: null, simulationUnlocked: false, alarmKeys: new Set(), schedules: [], csrfToken: '', assistantPatientId: null, assistantContextKey: '', assistantRequestId: 0, assistantController: null };
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
function emptyAiPanel(message) {
  return `<section class="card border-0 shadow-sm p-4 mt-4 ai-lab"><h2 class="h5">AI Insights</h2>${empty(message)}<p class="small text-secondary mb-0">Insights are advisory and are not a substitute for clinical judgment.</p></section>`;
}
function notify(message, type = 'success') { const toast = $('#toast'); toast.className = `alert alert-${type}`; toast.textContent = message; setTimeout(() => toast.classList.add('d-none'), 3500); }
function formData(form) { return Object.fromEntries(new FormData(form)); }
function shell(role, name) {
  $('#dashboardTitle').textContent = `Welcome, ${name}`;
  $('#roleBadge').textContent = role;
  $('#userLabel').textContent = name;
  $('#settingsName').textContent = name;
  $('#settingsRole').textContent = role;
  document.querySelectorAll('[data-role-nav]').forEach((item) => item.classList.toggle('d-none', item.dataset.roleNav !== role));
}
function bindSectionNavigation(role) {
  const view = $(`#${role.toLowerCase()}View`);
  const candidates = [...view.querySelectorAll('h2, h3')];
  const sectionFor = (pattern) => {
    const heading = candidates.find((item) => pattern.test(item.textContent));
    return heading?.closest('section, .card') || heading?.parentElement;
  };
  const targets = {
    dashboardView: $('#dashboardView'),
    medications: sectionFor(/medication records|medication management/i),
    activity: sectionFor(/recent activity|live activity|dose event timeline/i) || sectionFor(/medication schedule/i),
    'ai-insights': view.querySelector('.ai-lab'),
    messages: sectionFor(/care team communication/i),
    settings: $('#settings'),
    patients: sectionFor(/assigned patients/i),
    requests: role === 'Doctor' ? [...view.querySelectorAll('h4')].find((item) => item.textContent.trim() === 'Requests') : null,
    'assigned-patients': role === 'Caregiver' ? view.querySelector('.row') : null,
  };
  document.querySelectorAll('#sectionNavigation [data-section]').forEach((link) => {
    const target = targets[link.dataset.section];
    link.classList.toggle('disabled', !target);
    link.setAttribute('aria-disabled', String(!target));
    link.tabIndex = target ? 0 : -1;
    if (target) {
      target.id = link.dataset.section;
      link.href = `#${target.id}`;
    }
    link.onclick = (event) => {
      if (!target) { event.preventDefault(); notify('This section is not available yet.', 'warning'); return; }
      document.querySelectorAll('#sectionNavigation .nav-link').forEach((item) => item.classList.remove('active'));
      link.classList.add('active');
      const collapse = $('#primaryNav');
      if (collapse.classList.contains('show')) bootstrap.Collapse.getOrCreateInstance(collapse).hide();
    };
  });
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('pillguard-theme', next);
  $('#themeToggle').textContent = next === 'dark' ? '☀' : '☾';
}
function showOnly(id) { ['doctorView','patientView','caregiverView'].forEach((item) => $(`#${item}`).classList.toggle('d-none', item !== id)); }
function isEditingView(id) {
  const view = $(`#${id}`);
  const active = document.activeElement;
  return view.contains(active) && (
    active.matches('input, textarea, select, [contenteditable="true"]') ||
    active.closest('form')
  );
}
function empty(message) { return `<div class="text-secondary text-center py-4">${esc(message)}</div>`; }
function pillBox(schedules, logs = []) {
  return `<div class="card border-0 shadow-sm p-4 mb-4"><h2 class="h5 mb-3">Interactive Pill Box</h2><div class="pill-box">${schedules.slice(0, 8).map((item, index) => {
    const today = localDateKey(new Date(), item.timezone || 'UTC');
    const compartment = item.compartment ?? index + 1;
    const taken = logs.some((log) => {
      const matchesReminder = log.reminder_id != null
        ? String(log.reminder_id) === String(item.id)
        : log.compartment != null && Number(log.compartment) === Number(compartment);
      return matchesReminder && ['Taken', 'Manual Override'].includes(log.status) &&
        localDateKey(new Date(log.timestamp), item.timezone || 'UTC') === today;
    });
    return `<div class="pill-compartment ${taken ? 'taken' : ''}" data-compartment="${compartment}"><div class="pill-icon ${taken ? 'removed' : ''}" data-pill="${compartment}">${taken ? '' : '💊'}</div><div class="small fw-semibold">Compartment ${compartment}</div><div class="small text-secondary">${esc(item.time)} · ${taken ? 'Empty / Taken' : esc(item.med_name)}</div></div>`;
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
function localDateKey(now, timezone) {
  const parts = new Intl.DateTimeFormat('en-US', {timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
function scheduleKey(patientId, reminder, timezone, now) {
  return `${localDateKey(now, timezone)}:${patientId}:${reminder.id}:${reminder.time}`;
}
function checkAlarms() {
  const now = new Date();
  state.schedules.forEach(({patientId, reminder, patientName, timezone}) => {
    const parts = new Intl.DateTimeFormat('en-GB', {timeZone: timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).formatToParts(now);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    const current = `${values.hour}:${values.minute}`;
    const key = scheduleKey(patientId, reminder, timezone, now);
    if (reminder.time === current && !state.alarmKeys.has(key)) { state.alarmKeys.add(key); triggerAlarm(patientId, reminder, patientName); }
  });
}
function configureAlarms(schedules) {
  state.schedules = schedules.flatMap((item) => (item.reminders || item.schedules || []).map((reminder) => ({patientId: item.id, reminder, patientName: item.name || state.user.name, timezone: item.timezone || reminder.timezone || 'UTC'})));
  if (!state.alarmTimer) state.alarmTimer = setInterval(checkAlarms, 1000);
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(() => {});
}
function startNextDoseCountdown(nextDose) {
  if (!$('#nextDoseCountdown')) return;
  if (!nextDose) { $('#nextDoseCountdown').textContent = '--:--:--'; return; }
  const dueAt = new Date(nextDose.scheduled_at).getTime();
  const update = () => {
    const seconds = Math.max(0, Math.floor((dueAt - Date.now()) / 1000));
    $('#nextDoseCountdown').textContent = `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds % 3600 / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  };
  update(); if (state.countdownTimer) clearInterval(state.countdownTimer); state.countdownTimer = setInterval(update, 1000);
}
function simulationPanel(patients, includeTraffic = true) {
  const options = patients.map((patient) => `<option value="${patient.id}">${esc(patient.name)}</option>`).join('') || '<option value="">No assigned patient</option>';
  const initialPatient = patients[0];
  const reminders = initialPatient?.reminders || initialPatient?.schedules || [];
  const reminderOptions = reminders.map((item) => `<option value="${item.id}" data-compartment="${item.compartment ?? ''}" data-weight="${item.tablet_weight ?? 0.5}" data-quantity="${item.expected_quantity ?? 1}" data-tolerance="${item.tolerance ?? 0.2}" data-offset="${item.calibration_offset ?? 0}">${esc(item.time)} · ${esc(item.med_name)}</option>`).join('') || '<option value="">No active dose</option>';
  const traffic = includeTraffic ? `<hr><div class="d-flex justify-content-between align-items-center"><h3 class="h6 mb-0">Live hardware API traffic</h3><button id="refreshTrafficBtn" class="btn btn-sm btn-outline-secondary">Refresh</button></div><div id="trafficLog" class="traffic-log mt-2">${empty('No hardware requests logged yet.')}</div>` : '';
  return `<div class="card border-0 shadow-sm p-4 mt-4 simulator-card"><div class="d-flex justify-content-between align-items-start gap-3"><div><p class="eyebrow mb-1">SOFTWARE SIMULATION</p><h2 class="h5 mb-1">Live pill-box simulator</h2><p class="small text-secondary mb-0">Simulated sensor events help exercise the shared hardware pipeline. Tablet removal never confirms swallowing.</p></div><div class="text-end"><span id="simBuzzer" class="badge text-bg-secondary">Buzzer idle</span><div id="simState" class="badge text-bg-light mt-2">IDLE</div></div></div><div data-simulation-lock class="${state.simulationUnlocked ? 'd-none' : ''} alert alert-warning mt-3 mb-0">Acknowledge the medication reminder before processing a dose simulation.</div><div data-simulation-controls class="${state.simulationUnlocked ? '' : 'd-none'}"><div class="row g-3 mt-1"><div class="col-lg-5"><h3 class="h6">Interactive pill box</h3><div class="pill-box simulator-pill-box">${[1,2,3,4,5,6,7,8].map((slot) => `<button type="button" class="pill-compartment" data-sim-compartment="${slot}" aria-label="Select compartment ${slot}"><span class="pill-icon" aria-hidden="true">💊</span><span class="small fw-semibold d-block">Slot ${slot}</span><span class="small text-secondary d-block">select</span></button>`).join('')}</div></div><div class="col-lg-7"><div class="row g-2 mb-2"><div class="col-md-6"><label class="form-label small" for="scenarioPatient">Patient</label><select id="scenarioPatient" class="form-select form-select-sm">${options}</select></div><div class="col-md-6"><label class="form-label small" for="scenarioReminder">Scheduled dose</label><select id="scenarioReminder" class="form-select form-select-sm">${reminderOptions}</select></div></div><div id="simCalibration" class="small text-secondary mb-2" aria-live="polite"></div><div class="d-flex flex-wrap gap-2 mb-2"><button data-scenario="normal_removal" class="btn btn-sm btn-success">Normal removal</button><button data-scenario="no_removal" class="btn btn-sm btn-outline-secondary">No removal</button><button data-scenario="noise" class="btn btn-sm btn-outline-secondary">Sensor noise</button><button data-scenario="unexpected_increase" class="btn btn-sm btn-outline-danger">Unexpected increase</button><button data-scenario="excessive_removal" class="btn btn-sm btn-outline-warning">Excessive removal</button><button data-scenario="delayed_removal" class="btn btn-sm btn-outline-primary">Delayed removal</button><button data-scenario="no_response" class="btn btn-sm btn-danger">No response</button></div><div class="sensor-graph border rounded-3 p-2"><div class="d-flex flex-wrap justify-content-between small text-secondary mb-1"><span>Simulated HX711 weight readings</span><span><i class="legend-raw"></i> Raw · <i class="legend-filtered"></i> Rolling median</span></div><svg id="sensorGraph" viewBox="0 0 500 120" role="img" aria-labelledby="sensorGraphTitle sensorGraphDescription"><title id="sensorGraphTitle">Simulated sensor weight graph</title><desc id="sensorGraphDescription">Raw and rolling median weight samples; the horizontal scale is sample order.</desc><polyline id="rawSensorLine" points="0,60 500,60" fill="none" stroke="#94a3b8" stroke-width="2"/><polyline id="filteredSensorLine" points="0,60 500,60" fill="none" stroke="#2563eb" stroke-width="3"/></svg><div class="small text-secondary">Detection comparison: threshold rules versus Isolation Forest anomaly flag.</div><div id="sensorAnalysis" class="small mt-2" aria-live="polite">Run a repeatable scenario to view both detectors.</div></div></div></div><div class="row g-3 mt-1"><div class="col-lg-5"><form id="simulationForm" class="row g-2"><input type="hidden" name="reminder_id" id="simulationReminderId"><div class="col-6"><label class="form-label small" for="simWeightBefore">W_before (g)</label><input id="simWeightBefore" name="w_before" type="number" min="0" step="0.1" value="100" class="form-control" required></div><div class="col-6"><label class="form-label small" for="simWeightAfter">W_after (g)</label><input id="simWeightAfter" name="w_after" type="number" min="0" step="0.1" value="99.5" class="form-control" required></div><div class="col-12"><button class="btn btn-primary w-100">Process sensor reading</button></div></form><div id="simulationResult" class="small mt-2"></div></div><div class="col-lg-7"><h3 class="h6 mb-2">Dose event timeline</h3><div id="eventTimeline" class="traffic-log border rounded-3 p-2" aria-live="polite">${empty('No simulated dose events yet.')}</div><button id="confirmEventBtn" class="btn btn-sm btn-outline-success mt-2 d-none">Manually confirm event</button></div></div></div>${traffic}</div>`;
}
async function refreshTraffic() {
  const data = await api('/api/hardware/traffic');
  $('#trafficLog').innerHTML = data.traffic.length ? data.traffic.map((entry) => `<div class="border-bottom py-2"><div><span class="badge text-bg-secondary">${esc(entry.method)}</span> <code>${esc(entry.endpoint)}</code> <span class="small text-secondary">HTTP ${entry.response_status} · ${new Date(entry.created_at).toLocaleString()}</span></div><code>${esc(entry.payload)}</code></div>`).join('') : empty('No hardware requests logged yet.');
}
function bindSimulation(patients) {
  const patientSelect = $('#scenarioPatient');
  const doseSelect = $('#scenarioReminder');
  const simulationGate = document.querySelector('[data-simulation-lock]');
  if (simulationGate && !$('#startSimulationBtn')) {
    simulationGate.textContent = 'Start a software-only scenario; this does not acknowledge a real medication reminder. ';
    const startButton = document.createElement('button');
    startButton.id = 'startSimulationBtn';
    startButton.type = 'button';
    startButton.className = 'btn btn-sm btn-outline-primary ms-2';
    startButton.textContent = 'Start software simulation';
    simulationGate.append(startButton);
  }
  const updateDoseChoices = () => {
    const patient = patients.find((item) => String(item.id) === patientSelect.value);
    const schedules = patient?.reminders || patient?.schedules || [];
    doseSelect.innerHTML = schedules.length
      ? schedules.map((item) => `<option value="${item.id}" data-compartment="${item.compartment ?? ''}" data-weight="${item.tablet_weight ?? 0.5}" data-quantity="${item.expected_quantity ?? 1}" data-tolerance="${item.tolerance ?? 0.2}" data-offset="${item.calibration_offset ?? 0}">${esc(item.time)} · ${esc(item.med_name)}</option>`).join('')
      : '<option value="">No active dose</option>';
    doseSelect.dispatchEvent(new Event('change'));
    document.querySelectorAll('[data-scenario]').forEach((button) => { button.disabled = schedules.length === 0; });
    $('#simulationForm button[type="submit"], #simulationForm button:not([type])').disabled = schedules.length === 0;
    if (patientSelect.value) refreshSimulationEvents(Number(patientSelect.value));
  };
  doseSelect.onchange = () => {
    const option = doseSelect.selectedOptions[0];
    $('#simulationReminderId').value = doseSelect.value;
    const compartment = option?.dataset.compartment || 'not assigned';
    $('#simCalibration').textContent = option
      ? `Compartment ${compartment} · expected ${option.dataset.weight} g × ${option.dataset.quantity} · tolerance ±${option.dataset.tolerance} g · calibration offset ${option.dataset.offset} g`
      : 'No active schedule is available for this patient.';
  };
  patientSelect.onchange = updateDoseChoices;
  updateDoseChoices();
  document.querySelectorAll('[data-sim-compartment]').forEach((button) => {
    button.onclick = () => {
      const matching = [...doseSelect.options].find((option) => option.dataset.compartment === button.dataset.simCompartment);
      if (!matching) return notify(`No scheduled medication is assigned to compartment ${button.dataset.simCompartment}.`, 'warning');
      doseSelect.value = matching.value;
      doseSelect.dispatchEvent(new Event('change'));
      document.querySelectorAll('[data-sim-compartment]').forEach((item) => item.classList.toggle('selected', item === button));
    };
  });
  $('#simulationForm').onsubmit = async (event) => {
    event.preventDefault();
    const values = formData(event.target);
    values.patient_id = Number(patientSelect.value); values.reminder_id = Number(values.reminder_id); values.w_before = Number(values.w_before); values.w_after = Number(values.w_after);
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
        const result = await api('/api/simulation/scenario', {method:'POST', body: JSON.stringify({patient_id: Number(patientSelect.value), reminder_id: Number(doseSelect.value), scenario: button.dataset.scenario})});
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
  const startSimulationButton = $('#startSimulationBtn');
  if (startSimulationButton) startSimulationButton.onclick = unlockSimulation;
  $('#acknowledgeAlarmBtn').onclick = async () => {
    if (!state.alarm) return;
    try {
      await api('/api/alarm/acknowledge', {method:'POST', body: JSON.stringify({patient_id: state.alarm.patientId, reminder_id: state.alarm.reminder.id})});
      stopBuzzer(); window.speechSynthesis?.cancel(); unlockSimulation(); bootstrap.Modal.getOrCreateInstance($('#alarmModal')).hide(); notify('Alarm acknowledged. Awaiting sensor confirmation.');
    } catch (error) { notify(error.message, 'danger'); }
  };
  if (patientSelect.value) refreshSimulationEvents(Number(patientSelect.value));
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
    const allValues = [...analysis.raw_readings, ...analysis.filtered_readings];
    const min = Math.min(...allValues);
    const span = Math.max(0.01, Math.max(...allValues) - min);
    const points = (values) => values.map((value, index) => `${(index / Math.max(1, values.length - 1)) * 500},${110 - ((value - min) / span) * 100}`).join(' ');
    $('#rawSensorLine').setAttribute('points', points(analysis.raw_readings));
    $('#filteredSensorLine').setAttribute('points', points(analysis.filtered_readings));
    const threshold = analysis.threshold_detection;
    const anomaly = analysis.anomaly_detection;
    $('#sensorAnalysis').innerHTML = `<span class="badge ${threshold.removal_detected ? 'text-bg-success' : threshold.anomaly_flag ? 'text-bg-warning' : 'text-bg-secondary'}">Threshold: ${esc(threshold.event)}${threshold.removal_detected ? ' · removal condition met' : ''}</span> <span class="badge ${anomaly.is_anomaly ? 'text-bg-danger' : 'text-bg-primary'}">Isolation Forest: ${anomaly.is_anomaly ? 'anomaly flagged' : 'within learned normal'} · ${esc(anomaly.event)}</span><div class="text-secondary mt-1">Filtered change ${analysis.features.weight_change.toFixed(2)} g · variability ${analysis.features.stability.toFixed(3)} · anomaly score ${anomaly.anomaly_score}</div><div class="text-warning mt-1">${esc(analysis.interpretation)}</div>`;
  }
  const selectedPatientId = Number($('#scenarioPatient').value);
  if (selectedPatientId) refreshSimulationEvents(selectedPatientId);
}
let jitsiApi = null;
function communicationPanel(patients, selectedId = '') {
  const options = patients.map((patient) => `<option value="${patient.id}" ${String(patient.id) === String(selectedId) ? 'selected' : ''}>${esc(patient.name)}</option>`).join('');
  return `<div class="card border-0 shadow-sm p-4 mt-4"><div class="d-flex justify-content-between align-items-center"><h2 class="h5 mb-0">Care team communication</h2><span class="small text-secondary">Secure shared room</span></div><div class="row g-2 mt-2"><div class="col-md-4"><select id="communicationPatient" class="form-select">${options || '<option value="">No patient available</option>'}</select></div><div class="col-md-3"><button id="consultBtn" class="btn btn-primary w-100" ${options ? '' : 'disabled'}>Consult via Call</button></div></div><div class="border rounded-3 p-3 mt-3"><div id="messageList" class="message-list mb-2">${empty('Select a patient to load messages.')}</div><form id="messageForm" class="d-flex gap-2"><input name="message" class="form-control" placeholder="Write a direct message..." maxlength="2000" required><button class="btn btn-outline-primary">Send</button></form></div></div>`;
}
function assistantMessage(text, type = 'bot') {
  const message = document.createElement('div');
  message.className = `assistant-message assistant-message-${type}`;
  message.textContent = text;
  $('#assistantMessages').append(message);
  $('#assistantMessages').scrollTop = $('#assistantMessages').scrollHeight;
  return message;
}
function resetAssistantMessages() {
  $('#assistantMessages').replaceChildren();
  assistantMessage('Hi! I can help with schedules, recorded events, predictions, request status, and finding your way around PillGuard.');
}
function setAssistantContext(patients, selectedId) {
  const widget = $('#assistantWidget');
  if (!widget || !state.user) return;
  const selector = $('#assistantPatient');
  const context = $('#assistantPatientContext');
  const isPatient = state.user.role === 'Patient';
  const options = isPatient ? [] : patients;
  const optionsKey = options.map((patient) => `${patient.id}:${patient.name}`).join('|');
  if (!isPatient && selector.dataset.optionsKey !== optionsKey) {
    selector.replaceChildren();
    if (!options.length) {
      selector.add(new Option('No linked patients', ''));
    } else {
      options.forEach((patient) => selector.add(new Option(patient.name, String(patient.id))));
    }
    selector.dataset.optionsKey = optionsKey;
  }
  context.classList.toggle('d-none', isPatient);
  const previousId = state.assistantPatientId;
  const preferredId = previousId && options.some((patient) => patient.id === previousId)
    ? previousId
    : options.find((patient) => patient.id === Number(selectedId))?.id ?? options[0]?.id ?? null;
  state.assistantPatientId = isPatient ? state.user.id : preferredId;
  if (!isPatient && selector.value !== String(preferredId ?? '')) selector.value = String(preferredId ?? '');
  const contextKey = `${state.user.role}:${state.assistantPatientId ?? 'none'}`;
  if (state.assistantContextKey && state.assistantContextKey !== contextKey) {
    state.assistantRequestId += 1;
    state.assistantController?.abort();
    state.assistantController = null;
    $('#assistantSend').disabled = false;
    $('#assistantQuestion').disabled = false;
    $('#assistantForm').reset();
    resetAssistantMessages();
  }
  state.assistantContextKey = contextKey;
  widget.hidden = false;
}
function setAssistantOpen(open) {
  const drawer = $('#assistantDrawer');
  const toggle = $('#assistantToggle');
  drawer.hidden = !open;
  drawer.setAttribute('aria-hidden', String(!open));
  toggle.setAttribute('aria-expanded', String(open));
  toggle.setAttribute('aria-label', open ? 'Close PillGuard Assistant' : 'Open PillGuard Assistant');
  $('#assistantWidget').classList.toggle('assistant-open', open);
  if (open) $('#assistantQuestion').focus();
  else toggle.focus();
}
function renderAssistantResult(message, result) {
  message.replaceChildren();
  message.className = 'assistant-message assistant-message-bot';
  const answer = document.createElement('p');
  answer.className = 'mb-2';
  answer.textContent = result.answer;
  message.append(answer);
  if (result.sources.length) {
    const heading = document.createElement('div');
    heading.className = 'small fw-semibold';
    heading.textContent = 'Supporting records';
    const list = document.createElement('ul');
    list.className = 'small mb-0 ps-3';
    result.sources.forEach((source) => {
      const item = document.createElement('li');
      const date = source.date ? ` · ${new Date(source.date).toLocaleString()}` : '';
      const record = source.record_id ? ` · record #${source.record_id}` : '';
      item.textContent = `${source.label}${date}${record}`;
      list.append(item);
    });
    message.append(heading, list);
  }
  $('#assistantMessages').scrollTop = $('#assistantMessages').scrollHeight;
}
function bindAssistantWidget() {
  const widget = $('#assistantWidget');
  if (!widget) return;
  $('#assistantToggle').onclick = () => setAssistantOpen($('#assistantDrawer').hidden);
  $('#assistantClose').onclick = () => setAssistantOpen(false);
  $('#assistantPatient').onchange = () => {
    const nextId = Number($('#assistantPatient').value) || null;
    if (nextId === state.assistantPatientId) return;
    state.assistantPatientId = nextId;
    state.assistantContextKey = `${state.user.role}:${nextId ?? 'none'}`;
    state.assistantRequestId += 1;
    state.assistantController?.abort();
    state.assistantController = null;
    $('#assistantSend').disabled = false;
    $('#assistantQuestion').disabled = false;
    $('#assistantForm').reset();
    resetAssistantMessages();
  };
  document.querySelectorAll('[data-assistant-suggestion]').forEach((button) => {
    button.onclick = () => {
      $('#assistantQuestion').value = button.dataset.assistantSuggestion;
      $('#assistantForm').requestSubmit();
    };
  });
  $('#assistantForm').onsubmit = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const question = $('#assistantQuestion').value.trim();
    if (!question) return;
    const requestId = ++state.assistantRequestId;
    state.assistantController?.abort();
    const controller = new AbortController();
    state.assistantController = controller;
    assistantMessage(question, 'user');
    const response = assistantMessage('Checking authorized records…', 'loading');
    const submit = $('#assistantSend');
    submit.disabled = true;
    $('#assistantQuestion').disabled = true;
    const payload = {question};
    if (state.user.role !== 'Patient' && state.assistantPatientId) {
      payload.patient_id = state.assistantPatientId;
    }
    try {
      const result = await api('/api/assistant/chat', {
        method: 'POST',
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      if (requestId === state.assistantRequestId) renderAssistantResult(response, result);
    } catch (error) {
      if (requestId === state.assistantRequestId) {
        response.className = 'assistant-message assistant-message-error';
        response.textContent = error.message;
      }
    } finally {
      if (requestId === state.assistantRequestId) {
        state.assistantController = null;
        submit.disabled = false;
        $('#assistantQuestion').disabled = false;
        form.reset();
        $('#assistantQuestion').focus();
      }
    }
  };
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('#assistantDrawer').hidden) setAssistantOpen(false);
  });
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

function medicationRecordsPanel(patientId, medications, requests, allowRequests) {
  const records = medications.length ? medications.map((medication) => {
    const schedule = medication.schedule || {};
    return `<article class="border rounded-3 p-3 mb-2"><div class="d-flex justify-content-between align-items-start"><div><strong>${esc(medication.name)}</strong> <span class="badge text-bg-${medication.status === 'Active' ? 'success' : medication.status === 'Discontinued' ? 'warning' : 'secondary'}">${esc(medication.status)}</span><div class="small text-secondary">${schedule.time ? `${esc(schedule.time)} · ${esc(schedule.dosage)}` : 'No active schedule'}</div></div></div></article>`;
  }).join('') : empty('No medication records yet.');
  const requestHistory = requests.length ? requests.map((item) => `<div class="border-top py-2"><div><span class="badge text-bg-${item.status === 'PENDING' ? 'warning' : item.status === 'APPROVED' ? 'success' : 'danger'}">${esc(item.status)}</span> ${esc(item.operation)} · ${esc(item.medication_name || item.requested_data.name || 'Medication')}</div><div class="small text-secondary">${new Date(item.created_at).toLocaleString()}${item.decision_reason ? ` · Doctor: ${esc(item.decision_reason)}` : ''}</div></div>`).join('') : empty('No medication change requests.');
  const form = allowRequests ? `<form data-medication-request="${patientId}" class="border rounded-3 p-3 mt-3"><h4 class="h6">Request a medication change</h4><select name="operation" class="form-select mb-2" required><option value="ADD">Add medication</option><option value="EDIT">Edit medication</option><option value="REMOVE">Remove medication</option></select><select name="medication_id" class="form-select mb-2"><option value="">Choose existing medication</option>${medications.filter((item) => item.status === 'Active').map((item) => `<option value="${item.id}">${esc(item.name)}</option>`).join('')}</select><input name="name" class="form-control mb-2" maxlength="150" placeholder="Medication name (add or rename)"><div class="row g-2 mb-2"><div class="col"><input name="dosage" class="form-control" maxlength="100" placeholder="Dosage"></div><div class="col"><input name="time" type="time" class="form-control"></div></div><button class="btn btn-sm btn-primary">Submit for doctor approval</button></form>` : '';
  return `<section class="card border-0 shadow-sm p-4 mt-4"><h2 class="h5">Medication records</h2><div>${records}</div>${form}<div class="mt-3"><h3 class="h6">Change request history</h3>${requestHistory}</div><div data-medication-audit="${patientId}" class="mt-3"></div></section>`;
}

function activityPanel(logs, includePatient = false) {
  const rows = logs.slice(0, 20).map((log) => `<tr>${includePatient ? `<td>${esc(log.patient_name)}</td>` : ''}<td>${new Date(log.timestamp).toLocaleString()}</td><td>${Number(log.delta_weight || 0).toFixed(1)} g</td><td><span class="badge ${log.status === 'Taken' ? 'text-bg-success' : log.status === 'Missed' ? 'text-bg-danger' : 'text-bg-primary'}">${esc(log.status)}</span></td></tr>`).join('');
  return `<section class="card border-0 shadow-sm p-4 mt-4"><h2 class="h5">Recent activity</h2>${logs.length ? `<div class="table-responsive"><table class="table align-middle mb-0"><thead><tr>${includePatient ? '<th>Patient</th>' : ''}<th>Time</th><th>Weight change</th><th>Outcome</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty('No medication activity has been recorded yet.')}</section>`;
}

function doctorMedicationPanel(patients) {
  if (!patients.length) return `<section class="card border-0 shadow-sm p-4 mt-4"><h2 class="h5">Medication management</h2>${empty('Link a patient to manage their medication records.')}<h4 class="h6 mt-3">Requests</h4>${empty('Medication requests will appear here when a patient is linked.')}</section>`;
  return `<section class="card border-0 shadow-sm p-4 mt-4"><h2 class="h5">Medication management</h2>${patients.length ? patients.map((patient) => `<div class="border-top pt-3 mt-3"><h3 class="h6">${esc(patient.name)}</h3>${(patient.medications || []).length ? patient.medications.map((medication) => {
    const schedule = medication.schedule || {};
    const statusClass = medication.status === 'Active' ? 'success' : medication.status === 'Discontinued' ? 'warning' : 'secondary';
    const controls = medication.status === 'Active'
      ? `<form data-doctor-medication="${medication.id}" class="row g-2 mt-1"><input name="name" class="form-control" value="${esc(medication.name)}" required><div class="col"><input name="dosage" class="form-control" value="${esc(schedule.dosage || '')}" placeholder="Dosage"></div><div class="col"><input name="time" type="time" class="form-control" value="${esc(schedule.time || '')}"></div><div class="col-12 d-flex gap-2"><button class="btn btn-sm btn-outline-primary">Save edits</button><button type="button" data-medication-status="Discontinued" data-medication-id="${medication.id}" class="btn btn-sm btn-outline-warning">Discontinue</button><button type="button" data-medication-status="Archived" data-medication-id="${medication.id}" class="btn btn-sm btn-outline-secondary">Archive</button></div></form>`
      : medication.status === 'Discontinued'
        ? `<button data-medication-status="Archived" data-medication-id="${medication.id}" class="btn btn-sm btn-outline-secondary mt-2">Archive</button>`
        : '';
    return `<div class="border rounded-3 p-3 mb-2"><div class="d-flex justify-content-between"><strong>${esc(medication.name)}</strong><span class="badge text-bg-${statusClass}">${esc(medication.status)}</span></div>${controls}</div>`;
  }).join('') : empty('No medication records.')}<h4 class="h6 mt-3">Requests</h4>${(patient.medication_requests || []).map((item) => `<div class="border-top py-2"><span class="badge text-bg-${item.status === 'PENDING' ? 'warning' : item.status === 'APPROVED' ? 'success' : 'danger'}">${esc(item.status)}</span> ${esc(item.operation)} · ${esc(item.medication_name || item.requested_data.name || 'Medication')} <span class="small text-secondary">requested by ${esc(item.requested_by_name)}</span>${item.status === 'PENDING' ? `<div class="d-flex gap-2 mt-2"><button data-medication-decision="APPROVED" data-request-id="${item.id}" class="btn btn-sm btn-success">Approve</button><button data-medication-decision="REJECTED" data-request-id="${item.id}" class="btn btn-sm btn-outline-danger">Reject</button></div>` : `<div class="small text-secondary">${item.decision_reason ? `Decision: ${esc(item.decision_reason)}` : ''}</div>`}</div>`).join('') || empty('No requests.')}</div>`).join('') : empty('Link a patient to manage medications.')}</section>`;
}

async function bindMedicationPanels(patientId) {
  const auditElement = $(`[data-medication-audit="${patientId}"]`);
  if (auditElement) {
    try {
      const history = await api(`/api/patients/${patientId}/medication-audit`);
      auditElement.innerHTML = `<h3 class="h6">Immutable medication audit</h3>${history.audit.length ? history.audit.map((item) => `<div class="small border-top py-1">${esc(item.action)} · ${new Date(item.created_at).toLocaleString()} · ${esc(item.reason || '')}</div>`).join('') : empty('No changes recorded.')}`;
    } catch (error) { auditElement.innerHTML = `<div class="small text-danger">${esc(error.message)}</div>`; }
  }
  const form = $(`[data-medication-request="${patientId}"]`);
  if (!form) return;
  const operation = form.elements.operation;
  const target = form.elements.medication_id;
  const setInputs = () => {
    const add = operation.value === 'ADD';
    form.elements.name.required = add;
    form.elements.dosage.required = add;
    form.elements.time.required = add;
    target.required = operation.value !== 'ADD';
    form.elements.name.placeholder = operation.value === 'EDIT' ? 'Optional new medication name' : 'Medication name';
  };
  operation.onchange = setInputs;
  setInputs();
  form.onsubmit = async (event) => {
    event.preventDefault();
    const values = formData(form);
    const requested_data = {};
    for (const field of ['name', 'dosage', 'time']) if (values[field].trim()) requested_data[field] = values[field].trim();
    const body = {operation: values.operation, requested_data};
    if (values.operation !== 'ADD') body.medication_id = Number(values.medication_id);
    try {
      await api(`/api/patients/${patientId}/medication-requests`, {method:'POST', body: JSON.stringify(body)});
      notify('Medication change sent to the doctor for approval.');
      await loadDashboard();
    } catch (error) { notify(error.message, 'danger'); }
  };
}

function bindDoctorMedicationPanel() {
  document.querySelectorAll('[data-doctor-medication]').forEach((form) => {
    form.onsubmit = async (event) => {
      event.preventDefault();
      try {
        const values = formData(form);
        await api(`/api/doctor/medications/${form.dataset.doctorMedication}`, {
          method:'PATCH',
          body: JSON.stringify(Object.fromEntries(Object.entries(values).filter(([, value]) => value.trim()))),
        });
        notify('Medication updated.');
        await loadDoctor();
      } catch (error) { notify(error.message, 'danger'); }
    };
  });
  document.querySelectorAll('[data-medication-status]').forEach((button) => {
    button.onclick = async () => {
      if (!window.confirm(`Are you sure you want to ${button.dataset.medicationStatus.toLowerCase()} this medication? The history will be retained.`)) return;
      try {
        await api(`/api/doctor/medications/${button.dataset.medicationId}`, {
          method:'PATCH', body: JSON.stringify({status: button.dataset.medicationStatus}),
        });
        notify(`Medication ${button.dataset.medicationStatus.toLowerCase()}.`);
        await loadDoctor();
      } catch (error) { notify(error.message, 'danger'); }
    };
  });
  document.querySelectorAll('[data-medication-decision]').forEach((button) => {
    button.onclick = async () => {
      const decision = button.dataset.medicationDecision;
      if (decision === 'APPROVED' && !window.confirm('Approve this medication change request?')) return;
      const reason = decision === 'REJECTED' ? window.prompt('Reason for rejecting this medication request:') : '';
      if (decision === 'REJECTED' && !reason?.trim()) return;
      try {
        await api(`/api/doctor/medication-requests/${button.dataset.requestId}`, {
          method:'PATCH', body: JSON.stringify({decision, reason: reason || ''}),
        });
        notify(`Medication request ${decision.toLowerCase()}.`);
        await loadDoctor();
      } catch (error) { notify(error.message, 'danger'); }
    };
  });
}

async function loadDoctor() {
  const data = await api('/api/doctor/patients'); showOnly('doctorView');
  if (isEditingView('doctorView')) return;
  const patients = data.patients;
  setAssistantContext(patients, patients[0]?.id);
  $('#doctorView').innerHTML = `<div class="row g-4">
    <div class="col-lg-4"><div class="card border-0 shadow-sm p-4"><h2 class="h5">Add a patient</h2><p class="small text-secondary">Link an existing patient by email.</p><form id="linkPatientForm" class="d-flex gap-2"><input name="email" type="email" class="form-control" placeholder="patient@email.com" required><button class="btn btn-primary">Add</button></form><div class="border-top mt-4 pt-3"><div class="small fw-semibold mb-2">Patient directory</div><form id="directoryForm" class="d-flex gap-2"><input name="search" class="form-control form-control-sm" placeholder="Search name or email"><button class="btn btn-sm btn-outline-primary">Search</button></form><div id="directoryResults" class="small mt-2"></div></div></div>
      <div class="card border-0 shadow-sm p-4 mt-4"><h2 class="h5">New medication schedule</h2><form id="scheduleForm"><select name="patient_id" class="form-select mb-2" required><option value="">Choose patient</option>${patients.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select><input name="med_name" class="form-control mb-2" placeholder="Medication name" required><div class="row g-2 mb-2"><div class="col"><input name="time" type="time" class="form-control" required></div><div class="col"><input name="compartment" type="number" min="1" max="8" class="form-control" placeholder="Compartment"></div></div><input name="dosage" class="form-control mb-2" placeholder="Dosage" required><div class="row g-2 mb-2"><div class="col"><label class="form-label small">Tablet g</label><input name="tablet_weight" type="number" min=".01" step=".01" value=".5" class="form-control"></div><div class="col"><label class="form-label small">Quantity</label><input name="expected_quantity" type="number" min="1" max="100" value="1" class="form-control"></div><div class="col"><label class="form-label small">Tolerance g</label><input name="tolerance" type="number" min=".01" step=".01" value=".2" class="form-control"></div></div><div class="row g-2 mb-3"><div class="col"><label class="form-label small">Response min</label><input name="response_window_minutes" type="number" min="1" max="1440" value="30" class="form-control"></div><div class="col"><label class="form-label small">Calibration g</label><input name="calibration_offset" type="number" step=".01" value="0" class="form-control"></div><div class="col"><label class="form-label small">Noise g</label><input name="noise_threshold" type="number" min=".01" step=".01" value=".15" class="form-control"></div></div><button class="btn btn-primary w-100">Sync schedule</button></form></div></div>
    <div class="col-lg-8"><div class="card border-0 shadow-sm p-4"><div class="d-flex justify-content-between"><h2 class="h5">Assigned patients</h2><span class="text-secondary small">${patients.length} linked</span></div>${patients.length ? patients.map(patientCard).join('') : empty('No patients linked yet.')}</div>${doctorMedicationPanel(patients)}${activityPanel(patients.flatMap((patient) => (patient.logs || []).map((log) => ({...log, patient_name: patient.name}))).sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp)), true)}${patients[0] ? aiLabPanel(patients[0].id, patients[0].risk, patients[0].logs) : emptyAiPanel('Link a patient to view their care insights.')}${communicationPanel(patients, patients[0]?.id)}${simulationPanel(patients)}</div></div>`;
  $('#linkPatientForm').onsubmit = async (event) => { event.preventDefault(); try { await api('/api/doctor/patients/link', {method:'POST', body: JSON.stringify(formData(event.target))}); notify('Patient linked'); loadDoctor(); } catch (error) { notify(error.message, 'danger'); } };
  $('#scheduleForm').onsubmit = async (event) => { event.preventDefault(); const values = formData(event.target); try { await api(`/api/doctor/patients/${values.patient_id}/schedules`, {method:'POST', body: JSON.stringify(values)}); notify('Schedule synced to patient'); event.target.reset(); loadDoctor(); } catch (error) { notify(error.message, 'danger'); } };
  $('#directoryForm').onsubmit = async (event) => { event.preventDefault(); try { const results = await api(`/api/doctor/directory?search=${encodeURIComponent(formData(event.target).search)}`); $('#directoryResults').innerHTML = results.patients.length ? results.patients.map((p) => `<div class="d-flex justify-content-between align-items-center border-bottom py-2"><span>${esc(p.name)}<br><span class="text-secondary">${esc(p.email)}</span></span>${p.linked_to_doctor ? '<span class="badge text-bg-success">Linked</span>' : `<button data-directory-link="${p.email}" class="btn btn-sm btn-outline-primary">Link</button>`}</div>`).join('') : empty('No patients found.'); document.querySelectorAll('[data-directory-link]').forEach((button) => { button.onclick = async () => { try { await api('/api/doctor/patients/link', {method:'POST', body: JSON.stringify({email: button.dataset.directoryLink})}); notify('Patient linked'); await loadDoctor(); } catch (error) { notify(error.message, 'danger'); } }; }); } catch (error) { notify(error.message, 'danger'); } };
  document.querySelectorAll('[data-link-caregiver]').forEach((button) => { button.onclick = () => { $('#caregiverLinkForm input[name="patient_id"]').value = button.dataset.patientId; bootstrap.Modal.getOrCreateInstance($('#caregiverLinkModal')).show(); }; });
  $('#caregiverLinkForm').onsubmit = async (event) => { event.preventDefault(); const values = formData(event.target); try { await api(`/api/doctor/patients/${values.patient_id}/caregiver`, {method:'POST', body: JSON.stringify({email: values.email})}); bootstrap.Modal.getOrCreateInstance($('#caregiverLinkModal')).hide(); event.target.reset(); notify('Caregiver linked'); await loadDoctor(); } catch (error) { notify(error.message, 'danger'); } };
  bindCommunication(patients, patients[0]?.id);
  bindSimulation(patients);
  bindDoctorMedicationPanel();
  if (patients[0]) bindAiLab(patients[0].id);
  configureAlarms(patients);
  bindSectionNavigation('Doctor');
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
  return `<section class="card border-0 shadow-sm p-4 mt-4 ai-lab"><div class="d-flex justify-content-between align-items-start gap-3"><div><p class="eyebrow mb-1">AI INSIGHTS · RESEARCH EXPERIMENT</p><h2 class="h4 mb-1">Upcoming-dose prediction</h2><p class="small text-secondary mb-0">Synthetic-data experiment only. Not clinically validated and not medical advice.</p></div><span class="badge text-bg-warning">Research only</span></div><div id="upcomingDosePrediction" class="loading-block mt-3" role="status">Loading the pre-dose prediction…</div><div class="row g-3 mt-2"><div class="col-md-4">${statCard('Observed 7-day adherence', adherencePercent(logs, 7), 'Recorded events only')}</div><div class="col-md-4">${statCard('Observed 30-day adherence', adherencePercent(logs, 30), 'Recorded events only')}</div><div class="col-md-4">${statCard('Model history threshold', '7 doses', 'Only earlier recorded events are features')}</div></div><div class="row g-4 mt-1"><div class="col-lg-6"><h3 class="h6">Seven-day recorded outcomes</h3>${adherenceChart(logs, 7)}<h3 class="h6 mt-4">Thirty-day recorded outcomes</h3>${adherenceChart(logs, 30)}</div><div class="col-lg-6"><div id="aiMetrics" class="loading-block">Loading chronological holdout results…</div><div id="featureImportance" class="mt-3"></div><div id="sensorLab" class="mt-4">Loading sensor analysis…</div></div></div><div class="mt-3"><h3 class="h6">Historical factors</h3><div class="small text-secondary">${risk.historical_factors?.map(esc).join(' · ') || 'No explanatory factors available.'}</div><div id="aiLimitations" class="small text-warning mt-2">${esc(risk.validation_limitations || 'Synthetic research output; not clinically validated.')}</div></div></section>`;
}
function adherencePercent(logs, days) { const cutoff = Date.now() - days * 86400000; const recent = logs.filter((log) => new Date(log.timestamp).getTime() >= cutoff); if (!recent.length) return '—'; return `${Math.round(recent.filter((log) => log.status !== 'Missed').length / recent.length * 100)}%`; }
async function bindAiLab(patientId) {
  try {
    const [model, sensor, prediction] = await Promise.all([
      api('/api/aiml/model'),
      api('/api/sensor/demo'),
      api(`/api/aiml/prediction/${patientId}`),
    ]);
    const formatMetric = (value) => value == null ? 'n/a' : Number(value).toFixed(3);
    const metricRow = (label, values) => `<tr><th scope="row">${esc(label)}</th><td>${formatMetric(values.precision)}</td><td>${formatMetric(values.recall)}</td><td>${formatMetric(values.f1)}</td><td>${formatMetric(values.roc_auc)}</td></tr>`;
    $('#aiMetrics').innerHTML = `<h3 class="h6">Chronological holdout evaluation <span class="badge text-bg-secondary">${esc(model.model_version)}</span></h3><p class="small text-secondary">${esc(model.data_source)} · ${Number(model.training_rows)} training rows · ${Number(model.test_rows)} test rows</p><div class="small text-secondary mb-2">Train through ${esc(model.train_last_dose_at)} · test ${esc(model.test_start)} to ${esc(model.test_end)} · seed ${esc(model.random_seed)}</div><div class="table-responsive"><table class="table table-sm align-middle"><caption class="caption-top small">Held-out scores; not clinical performance.</caption><thead><tr><th scope="col">Approach</th><th scope="col">Precision</th><th scope="col">Recall</th><th scope="col">F1</th><th scope="col">ROC-AUC</th></tr></thead><tbody>${metricRow('Random Forest', model.evaluation)}${metricRow('Majority baseline', model.baseline)}</tbody></table></div><h4 class="h6 mt-3">Random Forest confusion matrix</h4><div class="confusion-matrix" role="group" aria-label="Confusion matrix; rows are actual and columns are predicted"><span></span><strong>Predicted on-time</strong><strong>Predicted missed</strong><strong>Actual on-time</strong><span>${model.evaluation.confusion_matrix[0][0]}</span><span>${model.evaluation.confusion_matrix[0][1]}</span><strong>Actual missed</strong><span>${model.evaluation.confusion_matrix[1][0]}</span><span>${model.evaluation.confusion_matrix[1][1]}</span></div>`;
    const upcoming = prediction.upcoming_dose;
    $('#upcomingDosePrediction').className = 'border rounded-3 p-3 mt-3';
    $('#upcomingDosePrediction').innerHTML = !upcoming
      ? `<strong>No prediction available</strong><div class="small text-secondary">${esc(prediction.level === 'NO_UPCOMING_DOSE' ? 'There is no upcoming active scheduled dose.' : 'There is not enough pre-dose history.')}</div>`
      : `<div class="d-flex flex-wrap justify-content-between align-items-start gap-2"><div><div class="eyebrow">NEXT SCHEDULED DOSE</div><strong>${esc(upcoming.medication)}</strong><div class="small text-secondary">${new Date(upcoming.scheduled_at).toLocaleString()} · ${esc(upcoming.time)}</div></div><span class="badge ${prediction.miss_probability == null ? 'text-bg-secondary' : prediction.miss_probability >= 0.5 ? 'text-bg-warning' : 'text-bg-success'}">${prediction.miss_probability == null ? 'Insufficient history' : `${prediction.miss_probability >= 0.5 ? 'Predicted missed' : 'Predicted on-time'} · ${Number(prediction.miss_probability * 100).toFixed(1)}% estimated miss probability`}</span></div><div class="small text-secondary mt-2">History: ${Number(prediction.history_count)} of ${Number(prediction.minimum_history)} required prior events · ${esc(prediction.model_version)} · ${esc(prediction.data_source)}</div>`;
    $('#aiLimitations').textContent = model.validation_limitations;
    $('#featureImportance').innerHTML = `<h3 class="h6">Model feature importance</h3>${Object.entries(model.feature_importance || {}).map(([name, value]) => `<div class="importance-row"><span>${esc(name.replaceAll('_', ' '))}</span><div class="importance-bar"><i style="width:${Math.min(100, value * 100)}%"></i></div><span>${(value * 100).toFixed(1)}%</span></div>`).join('')}`;
    const sensorMetrics = sensor.evaluation;
    const confusionTable = (name, matrix) => `<div class="table-responsive"><table class="table table-sm table-bordered"><caption class="caption-top small">${esc(name)} confusion matrix; rows are actual</caption><thead><tr><th scope="col">Actual / predicted</th><th scope="col">Clear</th><th scope="col">Anomaly</th></tr></thead><tbody><tr><th scope="row">Normal</th><td>${matrix[0][0]}</td><td>${matrix[0][1]}</td></tr><tr><th scope="row">Anomaly</th><td>${matrix[1][0]}</td><td>${matrix[1][1]}</td></tr></tbody></table></div>`;
    $('#sensorLab').innerHTML = `<h3 class="h6">Sensor anomaly experiment <span class="badge text-bg-info">${esc(sensorMetrics.model_version)}</span></h3><p class="small text-secondary">${esc(sensorMetrics.data_source)} · ${Number(sensorMetrics.test_rows)} held-out synthetic windows</p><div class="table-responsive"><table class="table table-sm"><caption class="caption-top small">Isolation Forest compared with threshold-rule anomaly flags.</caption><thead><tr><th scope="col">Detector</th><th scope="col">Precision</th><th scope="col">Recall</th><th scope="col">F1</th><th scope="col">ROC-AUC</th></tr></thead><tbody><tr><th scope="row">Isolation Forest</th><td>${Number(sensorMetrics.isolation_forest.precision).toFixed(3)}</td><td>${Number(sensorMetrics.isolation_forest.recall).toFixed(3)}</td><td>${Number(sensorMetrics.isolation_forest.f1).toFixed(3)}</td><td>${sensorMetrics.isolation_forest.roc_auc == null ? 'n/a' : Number(sensorMetrics.isolation_forest.roc_auc).toFixed(3)}</td></tr><tr><th scope="row">Threshold baseline</th><td>${Number(sensorMetrics.threshold_baseline.precision).toFixed(3)}</td><td>${Number(sensorMetrics.threshold_baseline.recall).toFixed(3)}</td><td>${Number(sensorMetrics.threshold_baseline.f1).toFixed(3)}</td><td>n/a</td></tr></tbody></table></div><div class="row g-2"><div class="col-md-6">${confusionTable('Isolation Forest', sensorMetrics.isolation_forest.confusion_matrix)}</div><div class="col-md-6">${confusionTable('Threshold baseline', sensorMetrics.threshold_baseline.confusion_matrix)}</div></div><div class="small fw-semibold">Held-out scenario flag rates</div>${Object.entries(sensorMetrics.per_scenario).map(([name, item]) => `<div class="sensor-scenario"><strong>${esc(name.replaceAll('_', ' '))}</strong><span class="small">Isolation Forest ${Number(item.isolation_forest_flag_rate * 100).toFixed(0)}%</span><span class="small">Threshold ${Number(item.threshold_flag_rate * 100).toFixed(0)}%</span><small>${Number(item.test_rows)} cases</small></div>`).join('')}<div class="small text-secondary mt-2">${esc(sensorMetrics.validation_limitations)}</div><h4 class="h6 mt-3">Repeatable live examples</h4>${sensor.scenarios.map((item) => `<div class="sensor-scenario"><strong>${esc(item.scenario)}</strong><span class="badge ${item.anomaly_detection.is_anomaly ? 'text-bg-danger' : 'text-bg-success'}">ML ${item.anomaly_detection.is_anomaly ? 'anomaly' : 'normal'}</span><span class="badge text-bg-light">Threshold ${esc(item.threshold_detection.event)}</span><small>score ${item.anomaly_detection.anomaly_score}</small></div>`).join('')}`;
  } catch (error) { if ($('#aiMetrics')) $('#aiMetrics').innerHTML = `<div class="alert alert-warning">AI Insights unavailable: ${esc(error.message)}</div>`; }
}
function patientCard(patient) { const latest = patient.logs?.[0]; return `<article class="border-top py-3"><div class="d-flex justify-content-between align-items-start gap-2"><div><h3 class="h6 mb-1">${esc(patient.name)}</h3><small class="text-secondary">${esc(patient.email)}</small><div class="small text-secondary mt-2">${patient.caregiver ? `Caregiver: ${esc(patient.caregiver.email)}` : 'No caregiver linked'}</div></div><div class="text-end">${riskBadge(patient.risk)}<div class="small text-secondary mt-1">${patient.reminders.length} active schedules</div></div></div>${latest ? `<div class="small mt-2"><span class="badge ${latest.status === 'Taken' ? 'text-bg-success' : latest.status === 'Missed' ? 'text-bg-danger' : 'text-bg-primary'}">${latest.status === 'Taken' ? 'Taken (Weight Verified)' : latest.status === 'Manual Override' ? 'Taken (Caregiver Override)' : 'Missed (Timeout)'}</span> ${new Date(latest.timestamp).toLocaleString()}</div>` : ''}<button data-link-caregiver data-patient-id="${patient.id}" class="btn btn-sm btn-outline-primary mt-2">${patient.caregiver ? 'Change caregiver' : 'Link caregiver'}</button></article>`; }

async function loadPatient() {
  const data = await api('/api/patient/dashboard'); showOnly('patientView');
  if (isEditingView('patientView')) return;
  if (data.logs[0] && ['Taken', 'Manual Override'].includes(data.logs[0].status)) {
    stopBuzzer();
  }
  const doctor = data.doctor;
  const nextReminder = data.next_dose?.reminder;
  $('#patientView').innerHTML = `<div class="provider-banner p-4 mb-4"><p class="small opacity-75 mb-1">PATIENT OVERVIEW <span class="badge text-bg-light ms-2">Connected account</span></p><h2 class="h4 mb-1">${doctor ? `Dr. ${esc(doctor.name)}` : 'No doctor assigned yet'}</h2><div class="small">${doctor ? esc(doctor.email) : 'Connect with your care team to begin.'}${data.caregiver ? ` · Caregiver: ${esc(data.caregiver.name)} (${esc(data.caregiver.email)})` : ''}</div></div><div id="patientConfirmation"></div>${pillBox(data.schedules, data.logs)}${medicationRecordsPanel(state.user.id, data.medications || [], data.medication_requests || [], true)}<div class="row g-4"><div class="col-lg-7"><div class="card border-0 shadow-sm p-4"><h2 class="h5 mb-3">Medication schedule</h2><div class="row g-3">${data.schedules.length ? data.schedules.map(scheduleCard).join('') : empty('Your care team has not added a schedule yet.')}</div></div></div><div class="col-lg-5"><div class="card border-0 shadow-sm p-4"><div class="d-flex justify-content-between"><h2 class="h5">Next dose</h2><span class="status-dot mt-2"></span></div><div id="nextDoseCountdown" class="metric mt-3">${nextReminder ? esc(nextReminder.time) : '--:--'}</div><p class="text-secondary mb-1">${nextReminder ? `${esc(nextReminder.med_name)} · ${esc(nextReminder.dosage)}` : 'Nothing scheduled'}</p><div class="small text-secondary">Patient time zone: ${esc(data.timezone)} <button id="syncTimezoneBtn" class="btn btn-sm btn-link p-0 ms-1">Use this device's time zone</button></div></div></div></div>${activityPanel(data.logs)}${aiLabPanel(state.user.id, data.risk || {level:'INSUFFICIENT_DATA', historical_factors:[], validation_limitations:'No prediction available.'}, data.logs)}${communicationPanel([{id: state.user.id, name: 'My care team'}], state.user.id)}${simulationPanel([{id: state.user.id, name: 'My account'}], false)}`;
  setAssistantContext([], state.user.id);
  bindCommunication([{id: state.user.id, name: 'My care team'}], state.user.id);
  bindMedicationPanels(state.user.id);
  bindSimulation([{id: state.user.id, name: 'My account', schedules: data.schedules}]);
  configureAlarms([{id: state.user.id, timezone: data.timezone, reminders: data.schedules}]);
  startNextDoseCountdown(data.next_dose);
  $('#syncTimezoneBtn').onclick = async () => {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    try {
      await api('/api/patient/timezone', {method:'POST', body: JSON.stringify({timezone: zone})});
      notify(`Time zone updated to ${zone}`);
      await loadPatient();
    } catch (error) { notify(error.message, 'danger'); }
  };
  bindAiLab(state.user.id);
  bindSectionNavigation('Patient');
}
function scheduleCard(item) { return `<div class="col-md-6"><div class="border rounded-3 p-3 h-100"><div class="schedule-time">${esc(item.time)}</div><div class="fw-semibold">${esc(item.med_name)}</div><div class="small text-secondary">${esc(item.dosage)} · Set by ${esc(item.set_by_role)}</div></div></div>`; }

async function loadCaregiver() {
  const data = await api('/api/caregiver/dashboard'); showOnly('caregiverView');
  if (isEditingView('caregiverView')) return;
  const selectedPatient = data.patients[0];
  setAssistantContext(data.patients, data.patients[0]?.id);
  $('#caregiverView').innerHTML = `<div class="row g-3 mb-4">${data.patients.length ? data.patients.map(patient => `<div class="col-md-6 col-xl-4"><div class="card border-0 shadow-sm p-3"><div class="small text-secondary">PATIENT</div><div class="fw-semibold mb-2">${esc(patient.name)}</div>${riskBadge(patient.risk)}<div class="small text-secondary mt-2">${patient.reminders.length} active schedules</div></div></div>`).join('') : empty('No patients are assigned to you yet.')}</div>${data.patients.map((patient) => medicationRecordsPanel(patient.id, patient.medications || [], patient.medication_requests || [], true)).join('')}<div class="card border-0 shadow-sm p-4"><div class="d-flex flex-wrap justify-content-between gap-2 mb-3"><div><h2 class="h5 mb-1">Live activity</h2><p class="small text-secondary mb-0">Refreshing automatically every 10 seconds</p></div><button id="notifyBtn" class="btn btn-primary btn-sm">Dispatch instant notification</button></div>${data.logs.length ? `<div class="table-responsive"><table class="table align-middle mb-0"><thead><tr><th>Patient</th><th>Timestamp</th><th>Weight delta</th><th>Status</th><th></th></tr></thead><tbody>${data.logs.map(logRow).join('')}</tbody></table></div>` : empty('No weight activity recorded yet.')}</div>${selectedPatient ? aiLabPanel(selectedPatient.id, selectedPatient.risk, data.logs.filter((log) => log.patient_id === selectedPatient.id)) : emptyAiPanel('AI Insights will be available when a patient is assigned.')}${communicationPanel(data.patients, data.patients[0]?.id)}${simulationPanel(data.patients)}`;
  document.querySelectorAll('[data-override]').forEach((button) => button.onclick = async () => { const reason = window.prompt('Why are you confirming this dose?'); if (!reason) return; try { await api(`/api/caregiver/logs/${button.dataset.override}/override`, {method:'POST', body: JSON.stringify({reason})}); stopBuzzer(); notify('Medication marked as taken'); loadCaregiver(); } catch (error) { notify(error.message, 'danger'); } });
  $('#notifyBtn').onclick = () => dispatchNotification(data.patients);
  bindCommunication(data.patients, data.patients[0]?.id);
  data.patients.forEach((patient) => bindMedicationPanels(patient.id));
  bindSimulation(data.patients);
  configureAlarms(data.patients);
  if (selectedPatient) bindAiLab(selectedPatient.id);
  bindSectionNavigation('Caregiver');
}
function logRow(log) { const canOverride = log.status === 'Missed'; const label = log.status === 'Taken' ? 'Taken (Weight Verified)' : log.status === 'Manual Override' ? 'Taken (Caregiver Override)' : 'Missed (Timeout)'; return `<tr><td class="fw-semibold">${esc(log.patient_name)}</td><td class="small">${new Date(log.timestamp).toLocaleString()}</td><td>${Number(log.delta_weight).toFixed(1)}g</td><td><span class="badge ${log.status === 'Taken' ? 'text-bg-success' : log.status === 'Missed' ? 'text-bg-danger' : 'text-bg-primary'}">${label}</span></td><td>${canOverride ? `<button data-override="${log.id}" class="btn btn-sm btn-outline-success">Mark taken</button>` : ''}</td></tr>`; }
async function dispatchNotification(patients) { if (!patients.length) return notify('No assigned patients', 'warning'); const patient = prompt(`Patient ID (${patients.map(p => `${p.id}: ${p.name}`).join(', ')}):`); const message = prompt('Notification message:'); if (!patient || !message) return; try { await api('/api/caregiver/notify', {method:'POST', body: JSON.stringify({patient_id: Number(patient), message})}); notify('Notification dispatched'); } catch (error) { notify(error.message, 'danger'); } }

async function loadDashboard() { if (state.user.role === 'Doctor') await loadDoctor(); else if (state.user.role === 'Patient') await loadPatient(); else await loadCaregiver(); }
const loginForm = $('#loginForm');
if (loginForm && new URLSearchParams(window.location.search).get('registered') === '1') {
  notify('Your account is ready. Sign in to continue.');
}
if (loginForm) loginForm.onsubmit = async (event) => {
  event.preventDefault();
  const button = loginForm.querySelector('button');
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    await api('/api/login', {method:'POST', body: JSON.stringify(formData(loginForm))});
    window.location.assign('/dashboard');
  } catch (error) {
    notify(error.message, 'danger');
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
};
const signupForm = $('#signupForm');
if (signupForm) signupForm.onsubmit = async (event) => {
  event.preventDefault();
  const button = signupForm.querySelector('button');
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    await api('/api/register', {method:'POST', body: JSON.stringify(formData(signupForm))});
    window.location.assign('/login?registered=1');
  } catch (error) {
    notify(error.message, 'danger');
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
};
const logoutButton = $('#logoutBtn');
if (logoutButton) logoutButton.onclick = async () => {
  try { await api('/api/logout'); window.location.assign('/'); }
  catch (error) { notify(error.message, 'danger'); }
};
const themeButton = $('#themeToggle');
if (themeButton) themeButton.onclick = toggleTheme;
const settingsThemeButton = $('#settingsThemeToggle');
if (settingsThemeButton) settingsThemeButton.onclick = toggleTheme;
if (themeButton) themeButton.textContent = theme === 'dark' ? '☀' : '☾';
const aboutButton = $('#aboutBtn');
if (aboutButton) aboutButton.onclick = () => bootstrap.Modal.getOrCreateInstance($('#aboutModal')).show();
bindAssistantWidget();
if ($('#dashboardView')) {
  api('/api/me').then(async (data) => {
    state.user = data.user;
    state.csrfToken = (await api('/api/csrf-token')).csrf_token;
    shell(state.user.role, state.user.name);
    $('#dashboardLoading').classList.add('d-none');
    try {
      await loadDashboard();
      state.timer = setInterval(() => loadDashboard().catch((error) => notify(`Dashboard refresh failed: ${error.message}`, 'danger')), 10000);
    } catch (error) {
      $('#dashboardLoading').classList.remove('d-none');
      $('#dashboardLoading').innerHTML = `We could not load your dashboard. ${esc(error.message)} <button id="retryDashboard" class="btn btn-sm btn-primary ms-2">Try again</button>`;
      $('#retryDashboard').onclick = () => window.location.reload();
    }
  }).catch(() => window.location.replace('/login'));
}
