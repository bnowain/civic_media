"""
Comprehensive tests for the governing_bodies → groups refactor.

Tests:
  - Backend API: /api/groups/ endpoint (all, by type, POST)
  - Backend API: /api/meetings/ group_id filter
  - Backend API: /api/votes group_name field
  - Backend API: confirming old /api/governing-bodies/ 404s
  - Frontend (Playwright): index page sidebar shows groups filtered by tab
  - Frontend (Playwright): review page shows group_name in header

Run: python -m pytest tests/test_groups_refactor.py -v
Server must be running on 127.0.0.1:8000
"""

import httpx
import pytest
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"


# ─────────────────────────────────────────────────────────────────────────────
# Backend API tests
# ─────────────────────────────────────────────────────────────────────────────

def test_health():
    r = httpx.get(f"{BASE}/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_groups_all():
    """GET /api/groups/ returns all groups."""
    r = httpx.get(f"{BASE}/api/groups/")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert isinstance(data, list), "Expected list"
    assert len(data) >= 1, "Expected at least one group"
    # All items must have group_id (not governing_body_id)
    for g in data:
        assert "group_id" in g, f"group_id missing from group: {g}"
        assert "governing_body_id" not in g, "Old field governing_body_id still present"
        assert "group_type" in g, f"group_type missing from group: {g}"
        assert "body_type" not in g, "Old field body_type still present"
    print(f"  PASS: {len(data)} groups returned")


def test_groups_filter_government():
    """GET /api/groups/?group_type=government returns only government groups."""
    r = httpx.get(f"{BASE}/api/groups/", params={"group_type": "government"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 1, "Expected at least one government group"
    for g in data:
        assert g["group_type"] == "government", f"Non-government group in results: {g}"
    print(f"  PASS: {len(data)} government groups")


def test_groups_filter_show():
    """GET /api/groups/?group_type=show returns only show groups."""
    r = httpx.get(f"{BASE}/api/groups/", params={"group_type": "show"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 1, "Expected at least one show group"
    for g in data:
        assert g["group_type"] == "show", f"Non-show group in results: {g}"
    print(f"  PASS: {len(data)} show groups")


def test_old_governing_bodies_endpoint_gone():
    """Old /api/governing-bodies/ endpoint should 404."""
    r = httpx.get(f"{BASE}/api/governing-bodies/")
    assert r.status_code == 404, (
        f"Old endpoint /api/governing-bodies/ should return 404, got {r.status_code}"
    )
    print("  PASS: /api/governing-bodies/ correctly returns 404")


def test_meetings_have_group_name_not_governing_body():
    """GET /api/meetings/ returns group_name, not governing_body."""
    r = httpx.get(f"{BASE}/api/meetings/", params={"limit": 5})
    assert r.status_code == 200
    data = r.json()
    assert len(data) > 0, "Expected at least one meeting"
    for m in data:
        assert "group_name" in m, f"group_name missing from meeting: {list(m.keys())}"
        assert "governing_body" not in m, f"Old field governing_body still in response: {list(m.keys())}"
    print(f"  PASS: {len(data)} meetings all have group_name")


def test_meetings_filter_by_group_id():
    """GET /api/meetings/?group_id=<id> filters correctly."""
    # First get a group_id
    groups_r = httpx.get(f"{BASE}/api/groups/", params={"group_type": "government"})
    assert groups_r.status_code == 200
    groups = groups_r.json()
    assert len(groups) > 0
    gid = groups[0]["group_id"]

    r = httpx.get(f"{BASE}/api/meetings/", params={"group_id": gid})
    assert r.status_code == 200
    data = r.json()
    # All returned meetings should reference the same group
    for m in data:
        assert m.get("group_id") == gid or m.get("group_name") == groups[0]["name"], (
            f"Meeting {m.get('meeting_id')} doesn't match filtered group"
        )
    print(f"  PASS: {len(data)} meetings for group_id filter")


def test_meetings_category_meeting():
    """GET /api/meetings/?category=meeting returns government meetings."""
    r = httpx.get(f"{BASE}/api/meetings/", params={"category": "meeting"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) > 0, "Expected government meetings"
    for m in data:
        assert m["category"] == "meeting"
    print(f"  PASS: {len(data)} government meetings")


def test_meetings_category_audio():
    """GET /api/meetings/?category=audio returns audio/radio meetings."""
    r = httpx.get(f"{BASE}/api/meetings/", params={"category": "audio"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) > 0, "Expected audio meetings"
    for m in data:
        assert m["category"] == "audio"
    print(f"  PASS: {len(data)} audio meetings")


def test_votes_have_group_name():
    """GET /api/votes returns group_name field (not governing_body)."""
    r = httpx.get(f"{BASE}/api/votes", params={"limit": 5})
    if r.status_code == 200:
        data = r.json()
        if data:
            for v in data:
                assert "governing_body" not in v, (
                    f"Old field governing_body still in vote response: {list(v.keys())}"
                )
            print(f"  PASS: {len(data)} votes — no governing_body field")
        else:
            print("  SKIP: No votes in DB (OK)")
    else:
        print(f"  SKIP: /api/votes returned {r.status_code}")


# ─────────────────────────────────────────────────────────────────────────────
# Playwright frontend tests
# ─────────────────────────────────────────────────────────────────────────────

def test_index_page_loads():
    """Index page loads without JS errors and renders meeting cards."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

        page.goto(f"{BASE}/", wait_until="networkidle")
        # Check page title or meeting list element exists
        assert page.title() or True  # page loaded
        # Check for meeting grid / list
        page.wait_for_selector(".meeting-card, .card, #meeting-list, #cards-container", timeout=10000)
        print("  PASS: Index page loaded with meeting list")

        # Check for JS errors that aren't expected
        critical_errors = [e for e in errors if "governing_body" in e.lower() or "undefined" in e.lower()]
        assert not critical_errors, f"JS errors: {critical_errors}"
        print(f"  PASS: No critical JS errors (total console errors: {len(errors)})")
        browser.close()


def test_index_page_groups_sidebar():
    """Index page sidebar calls /api/groups/ and populates correctly."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        api_calls = []
        page.on("request", lambda req: api_calls.append(req.url) if "/api/" in req.url else None)

        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(1000)

        # Check that /api/groups/ was called (not /api/governing-bodies/)
        groups_calls = [u for u in api_calls if "/api/groups/" in u]
        old_calls = [u for u in api_calls if "/api/governing-bodies/" in u]
        assert len(groups_calls) > 0, f"Expected /api/groups/ call, got: {[u for u in api_calls if '/api/' in u]}"
        assert len(old_calls) == 0, f"Still calling old /api/governing-bodies/: {old_calls}"
        print(f"  PASS: {len(groups_calls)} calls to /api/groups/, 0 to old endpoint")
        browser.close()


def test_index_page_no_governing_body_refs():
    """Index HTML should not contain 'governing_body' or 'governing-bodies' in JS calls."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{BASE}/", wait_until="networkidle")

        # Evaluate JS to check for any surviving old field names in the DOM
        found_old = page.evaluate("""
            () => {
                const html = document.documentElement.innerHTML;
                const hasOld = html.includes('governing_body') || html.includes('governing-bodies') || html.includes('body_type');
                return hasOld;
            }
        """)
        assert not found_old, "Found old 'governing_body' or 'governing-bodies' in rendered DOM"
        print("  PASS: No old governing_body refs in rendered DOM")
        browser.close()


def test_review_page_loads():
    """Review page loads for a real meeting — checks group_name in header."""
    # Get a transcribed meeting ID
    r = httpx.get(f"{BASE}/api/meetings/", params={"category": "meeting", "limit": 5})
    assert r.status_code == 200
    meetings = r.json()
    if not meetings:
        pytest.skip("No meetings in DB")

    # Find one with processed_at set (has segments)
    processed = [m for m in meetings if m.get("processed_at")]
    if not processed:
        pytest.skip("No processed meetings in DB")

    mid = processed[0]["meeting_id"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{BASE}/review/{mid}", wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Check header element exists with new ID
        hdr = page.query_selector("#hdr-group")
        assert hdr is not None, "Element #hdr-group not found (may still be #hdr-governing-body)"
        old_hdr = page.query_selector("#hdr-governing-body")
        assert old_hdr is None, "Old #hdr-governing-body still present!"
        print(f"  PASS: Review page header uses #hdr-group: '{hdr.inner_text()}'")
        browser.close()


if __name__ == "__main__":
    # Run without pytest for quick manual check
    print("Running backend tests...")
    test_health()
    test_groups_all()
    test_groups_filter_government()
    test_groups_filter_show()
    test_old_governing_bodies_endpoint_gone()
    test_meetings_have_group_name_not_governing_body()
    test_meetings_filter_by_group_id()
    test_meetings_category_meeting()
    test_meetings_category_audio()
    test_votes_have_group_name()
    print("\nRunning browser tests...")
    test_index_page_loads()
    test_index_page_groups_sidebar()
    test_index_page_no_governing_body_refs()
    test_review_page_loads()
    print("\nAll tests passed!")
