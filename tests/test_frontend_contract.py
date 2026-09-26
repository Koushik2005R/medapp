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
