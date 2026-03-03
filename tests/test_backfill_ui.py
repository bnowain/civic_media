"""
Playwright smoke tests for the Backfill Manager UI.

Requirements:
  - Server running at localhost:8000
  - pip install playwright pytest pytest-asyncio
  - playwright install chromium

Run:
  pytest tests/test_backfill_ui.py -v
  pytest tests/test_backfill_ui.py -v -k headless   # headless only
  pytest tests/test_backfill_ui.py -v -k headed      # headed (visible browser)
"""

import time
import pytest
from playwright.sync_api import sync_playwright, Page, Browser

BASE_URL = "http://localhost:8000"


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser: Browser):
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    yield pg
    pg.close()


def wait_for_queue(page: Page, timeout: int = 10):
    """Wait for the queue table to finish loading."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = page.inner_text("#queue-title")
        if "Loading" not in title:
            return title
        time.sleep(0.5)
    raise TimeoutError("Queue never finished loading")


# ── Page load ──────────────────────────────────────────────────────────────

def test_page_loads(page: Page):
    """Backfill page loads and shows expected elements."""
    page.goto(f"{BASE_URL}/backfill")
    page.wait_for_load_state("load")
    time.sleep(3)

    assert "Backfill" in page.title()
    assert page.is_visible("#active-panel")
    assert page.is_visible(".controls")
    assert page.is_visible("#queue-body")


def test_status_pills_populated(page: Page):
    """Status pills show numeric counts after page load."""
    page.goto(f"{BASE_URL}/backfill")
    page.wait_for_load_state("load")
    time.sleep(3)

    for pill_id in ["cnt-download", "cnt-transcode", "cnt-process", "cnt-stuck"]:
        text = page.inner_text(f"#{pill_id}")
        assert text != "—", f"#{pill_id} was not populated (still shows '—')"
        assert text.isdigit(), f"#{pill_id} is not a number: {text!r}"


def test_queue_loads(page: Page):
    """Queue table populates with meeting rows."""
    page.goto(f"{BASE_URL}/backfill")
    page.wait_for_load_state("load")
    title = wait_for_queue(page, timeout=15)

    assert "pending" in title.lower(), f"Unexpected queue title: {title!r}"
    rows = page.query_selector_all("#queue-body tr")
    assert len(rows) > 0, "No rows in queue table"


def test_queue_row_structure(page: Page):
    """Each queue row has date, body, title, status badge, and action buttons."""
    page.goto(f"{BASE_URL}/backfill")
    page.wait_for_load_state("load")
    wait_for_queue(page, timeout=15)

    rows = page.query_selector_all("#queue-body tr")
    assert rows, "No rows to inspect"

    first = rows[0]
    cells = first.query_selector_all("td")
    assert len(cells) >= 6, f"Expected 6 columns, got {len(cells)}"

    # Date cell (index 1) should look like YYYY-MM-DD
    date_text = cells[1].inner_text().strip()
    assert len(date_text) == 10 and date_text[4] == "-", f"Unexpected date: {date_text!r}"

    # Should have Skip and ▶ buttons
    btns = first.query_selector_all("button")
    btn_texts = [b.inner_text().strip() for b in btns]
    assert any("Skip" in t or "Unskip" in t for t in btn_texts), "No Skip button"
    assert any("▶" in t for t in btn_texts), "No process (▶) button"


def test_search_filter(page: Page):
    """Search box filters the displayed rows."""
    page.goto(f"{BASE_URL}/backfill")
    page.wait_for_load_state("load")
    wait_for_queue(page, timeout=15)

    rows_before = len(page.query_selector_all("#queue-body tr"))
    page.fill("#search", "Board of Supervisors")
    time.sleep(0.3)

    rows_after = page.query_selector_all("#queue-body tr")
    # Should have filtered to only BOS rows (or show "no match" row)
    assert len(rows_after) <= rows_before, "Search filter did not reduce rows"


def test_checkbox_selection(page: Page):
    """Checking a row enables the Process Selected button."""
    page.goto(f"{BASE_URL}/backfill")
    page.wait_for_load_state("load")
    wait_for_queue(page, timeout=15)

    # Initially disabled
    btn = page.query_selector("#btn-selected")
    assert btn.is_disabled(), "Process Selected should start disabled"

    # Check first row
    first_checkbox = page.query_selector("#queue-body tr input[type=checkbox]")
    assert first_checkbox, "No checkbox in first row"
    first_checkbox.check()
    time.sleep(0.2)

    count_text = page.inner_text("#sel-count")
    assert count_text == "1", f"Expected sel-count=1, got {count_text!r}"
    assert not btn.is_disabled(), "Process Selected should be enabled after checking a row"


def test_active_panel_state(page: Page):
    """Active panel shows IDLE or ACTIVE badge correctly."""
    page.goto(f"{BASE_URL}/backfill")
    page.wait_for_load_state("load")
    time.sleep(3)

    badge = page.inner_text("#active-badge")
    assert badge in ("IDLE", "ACTIVE"), f"Unexpected badge: {badge!r}"


# ── API endpoints ───────────────────────────────────────────────────────────

def test_api_status():
    """GET /api/backfill/status returns expected keys."""
    import urllib.request, json
    with urllib.request.urlopen(f"{BASE_URL}/api/backfill/status") as r:
        d = json.loads(r.read())
    assert "download_pending"  in d
    assert "transcode_pending" in d
    assert "process_pending"   in d
    assert "stuck"             in d


def test_api_worker_idle():
    """GET /api/backfill/worker-idle returns idle bool."""
    import urllib.request, json
    with urllib.request.urlopen(f"{BASE_URL}/api/backfill/worker-idle") as r:
        d = json.loads(r.read())
    assert "idle" in d
    assert isinstance(d["idle"], bool)


def test_api_active():
    """GET /api/backfill/active returns active or None."""
    import urllib.request, json
    with urllib.request.urlopen(f"{BASE_URL}/api/backfill/active") as r:
        d = json.loads(r.read())
    assert "active" in d
    if d["active"] is not None:
        assert "meeting_id" in d["active"]
        assert "stage"      in d["active"]
        assert "pct"        in d["active"]


def test_api_queue_structure():
    """GET /api/backfill/queue?limit=5 returns expected shape."""
    import urllib.request, json
    with urllib.request.urlopen(f"{BASE_URL}/api/backfill/queue?limit=5") as r:
        d = json.loads(r.read())
    assert "items"  in d
    assert "total"  in d
    assert "offset" in d
    assert "limit"  in d
    if d["items"]:
        item = d["items"][0]
        for key in ("meeting_id", "meeting_date", "governing_body", "title",
                    "skipped", "active", "file_exists"):
            assert key in item, f"Missing key: {key}"


def test_screenshot(page: Page, tmp_path):
    """Take a full-page screenshot for visual inspection."""
    page.goto(f"{BASE_URL}/backfill")
    page.wait_for_load_state("load")
    wait_for_queue(page, timeout=15)

    screenshot = tmp_path / "backfill_ui.png"
    page.screenshot(path=str(screenshot), full_page=True)
    assert screenshot.exists()
    print(f"\nScreenshot saved: {screenshot}")
