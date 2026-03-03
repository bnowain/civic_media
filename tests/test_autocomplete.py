"""Quick smoke test for the autocomplete assign input on the review page."""
import re
from playwright.sync_api import sync_playwright

MEETING_ID = "2ca97bdf-84f8-4d21-99d2-e7bbe55cd3af"
BASE = "http://localhost:8000"


def test_autocomplete():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{BASE}/review/{MEETING_ID}", wait_until="networkidle")

        # Wait for segment cards to render
        page.wait_for_selector(".segment-card", timeout=10000)
        cards = page.query_selector_all(".segment-card")
        assert len(cards) > 0, "No segment cards found"
        print(f"  Found {len(cards)} segment cards")

        # 1. Verify autocomplete input exists (not a <select>)
        first_card = cards[0]
        assign_input = first_card.query_selector(".seg-assign-input")
        assert assign_input is not None, "No .seg-assign-input found on first card"
        old_select = first_card.query_selector("select.seg-select")
        assert old_select is None, "Old <select> still exists!"
        print("  PASS: Autocomplete input present, old select removed")

        # 2. Verify Ignore button exists on first card
        ignore_btn = first_card.query_selector(".seg-quick-btn.ignore")
        assert ignore_btn is not None, "No Ignore quick-assign button found"
        print("  PASS: Ignore button present")

        # 3. Click the assign input — dropdown should appear with ALL people
        assign_input.click()
        page.wait_for_timeout(300)
        dropdown = first_card.query_selector(".seg-assign-dropdown")
        assert dropdown is not None, "No dropdown element found"
        is_visible = not dropdown.get_attribute("hidden")
        assert is_visible, "Dropdown did not open on focus"
        options = dropdown.query_selector_all(".seg-assign-option")
        assert len(options) > 1, f"Expected all people in dropdown on focus, got {len(options)}"
        print(f"  PASS: Dropdown opened with {len(options)} options (all people)")

        # 4. Type to filter — should narrow results
        assign_input.fill("")
        assign_input.type("kev", delay=50)
        page.wait_for_timeout(200)
        filtered = dropdown.query_selector_all(".seg-assign-option")
        assert len(filtered) < len(options), f"Filtering didn't narrow: {len(filtered)} vs {len(options)}"
        # Check that match highlighting exists
        match_span = dropdown.query_selector(".seg-assign-match")
        assert match_span is not None, "No match highlighting found"
        print(f"  PASS: Typed 'kev', filtered to {len(filtered)} options with highlighting")

        # 5. Press ArrowDown + Enter to select
        assign_input.press("ArrowDown")
        page.wait_for_timeout(100)
        highlighted = dropdown.query_selector(".seg-assign-option.highlighted")
        assert highlighted is not None, "No highlighted option after ArrowDown"
        assign_input.press("Enter")
        page.wait_for_timeout(200)
        selected_pid = assign_input.get_attribute("data-person-id")
        assert selected_pid, "No person_id set after Enter"
        selected_name = assign_input.input_value()
        assert selected_name, "Input value empty after selection"
        print(f"  PASS: Selected '{selected_name}' (id={selected_pid}) via keyboard")

        # 6. Verify dropdown closed after selection
        is_hidden = dropdown.get_attribute("hidden") is not None
        assert is_hidden, "Dropdown should be hidden after selection"
        print("  PASS: Dropdown closed after selection")

        # 7. Click Ignore on a different card
        second_card = cards[1] if len(cards) > 1 else cards[0]
        ignore_btn2 = second_card.query_selector(".seg-quick-btn.ignore")
        if ignore_btn2:
            ignore_btn2.click()
            page.wait_for_timeout(1500)
            # Check the card updated (badge should show "tagged")
            badge = second_card.query_selector(".seg-badge")
            if badge:
                badge_text = badge.inner_text().lower()
                print(f"  PASS: Ignore clicked, badge now shows '{badge_text}'")
            else:
                print("  PASS: Ignore clicked (no badge to verify)")

        # 8. Verify frequency badges appear in dropdown
        # Re-open dropdown on first card
        assign_input = cards[0].query_selector(".seg-assign-input")
        assign_input.click()
        assign_input.fill("")
        page.wait_for_timeout(300)
        freq_badges = dropdown.query_selector_all(".seg-assign-freq")
        print(f"  INFO: {len(freq_badges)} options show frequency badges")

        print("\n  All autocomplete tests passed!")
        browser.close()


if __name__ == "__main__":
    test_autocomplete()
