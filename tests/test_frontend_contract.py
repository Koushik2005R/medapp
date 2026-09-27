import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_navigation_link_has_a_runtime_target():
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    section_links = set(re.findall(r'data-section="([^"]+)"', template))
    target_block = re.search(r"const targets = \{(.*?)\n  \};", javascript, re.DOTALL)
    assert target_block
    target_names = set(re.findall(r"^\s*['\"]?([a-zA-Z][a-zA-Z-]+)['\"]?:", target_block.group(1), re.MULTILINE))
    assert section_links <= target_names
    assert "aria-disabled" in javascript
    assert "link.href = `#${target.id}`" in javascript
    assert "notify('This section is not available yet.'" in javascript


def test_role_navigation_and_responsive_dashboard_contracts():
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
    role_sections = {
        "Doctor": {"patients", "requests"},
        "Patient": set(),
        "Caregiver": {"assigned-patients"},
    }
    for role, sections in role_sections.items():
        role_links = set(re.findall(
            rf'data-role-nav="{role}".*?data-section="([^"]+)"',
            template,
        ))
        assert role_links == sections

    assert "@media (max-width: 991.98px)" in styles
    assert "@media (max-width: 575.98px)" in styles
    assert ".app-nav .navbar-collapse" in styles
    assert ".assistant-drawer" in styles
    assert "width: calc(100vw - 1.5rem)" in styles
    assert "table-responsive" in (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "Start a software-only scenario" in javascript
    assert "startSimulationButton.onclick = unlockSimulation" in javascript


def test_dashboard_polls_preserve_active_form_inputs_and_pill_state_is_associated():
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "function isEditingView(id)" in javascript
    for role in ("doctorView", "patientView", "caregiverView"):
        assert f"if (isEditingView('{role}')) return;" in javascript
    assert "String(log.reminder_id) === String(item.id)" in javascript
    assert "Number(log.compartment) === Number(compartment)" in javascript


def test_alarm_delivery_uses_server_sent_events_instead_of_browser_clock_matching():
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "new EventSource('/api/stream')" in javascript
    assert "eventType !== 'alarm'" in javascript
    assert "function checkAlarms()" in javascript
    assert "eventType === 'schedule_updated' || eventType === 'log_updated'" in javascript
    assert "handleDataEvent(eventType, payload)" in javascript
    assert "state.eventSource.onerror = () => setStreamStatus(true)" in javascript
    assert "Live updates reconnecting…" in javascript
    assert "state.eventSource.close()" in javascript
