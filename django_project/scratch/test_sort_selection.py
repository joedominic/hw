import os, sys
sys.path.insert(0, os.path.abspath('django_project'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
import django; django.setup()
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.conf import settings
from playwright.sync_api import sync_playwright

User = get_user_model()
user = User.objects.filter(username='migration_bootstrap').first() or User.objects.first()
session = SessionStore()
session['_auth_user_id'] = str(user.pk)
session['_auth_user_backend'] = 'django.contrib.auth.backends.ModelBackend'
session['_auth_user_hash'] = user.get_session_auth_hash()
session.save()

with sync_playwright() as p:
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    browser = p.chromium.launch(executable_path=edge_path, headless=True)

    ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
    ctx.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': session.session_key, 'domain': '127.0.0.1', 'path': '/'}])
    page = ctx.new_page()
    page.goto('http://127.0.0.1:8080/jobs/cockpit/')
    page.wait_for_load_state('domcontentloaded')
    page.wait_for_timeout(1000)

    # Verify initial row count
    rows = page.query_selector_all('.opportunity-row')
    print(f"Found {len(rows)} opportunity rows.")

    if len(rows) > 1:
        # Test 1: Sort by oldest
        print("\n--- Test 1: Sorting by 'oldest' ---")
        page.select_option('#cockpit-sort-select', 'oldest')
        page.wait_for_timeout(500)

        # Get rows in current DOM order
        sorted_rows = page.query_selector_all('.opportunity-row')
        print(f"Total rows after sort: {len(sorted_rows)}")

        # Click row 1 (the 2nd item)
        target_row = sorted_rows[1]
        target_id = target_row.get_attribute('data-job-id')
        target_title = target_row.get_attribute('data-job-title')
        print(f"Clicking 2nd row: ID={target_id}, Title={target_title}")
        target_row.click()
        page.wait_for_timeout(300)

        inspector_id = page.inner_text('#inspect-job-id')
        inspector_title = page.inner_text('#inspect-job-title')
        print(f"Inspector displayed: ID='{inspector_id}', Title='{inspector_title}'")
        assert f"#{target_id}" in inspector_id, f"Expected ID #{target_id} in {inspector_id}"
        assert target_title in inspector_title, f"Expected Title '{target_title}' in {inspector_title}"
        print("PASS: Clicked row matches inspector after sorting by 'oldest'")

        # Click the last row
        last_row = sorted_rows[-1]
        last_id = last_row.get_attribute('data-job-id')
        last_title = last_row.get_attribute('data-job-title')
        print(f"\nClicking last row: ID={last_id}, Title={last_title}")
        last_row.click()
        page.wait_for_timeout(300)

        inspector_id = page.inner_text('#inspect-job-id')
        inspector_title = page.inner_text('#inspect-job-title')
        print(f"Inspector displayed: ID='{inspector_id}', Title='{inspector_title}'")
        assert f"#{last_id}" in inspector_id, f"Expected ID #{last_id} in {inspector_id}"
        assert last_title in inspector_title, f"Expected Title '{last_title}' in {inspector_title}"
        print("PASS: Clicked last row matches inspector")

        # Test 2: Sort by match
        print("\n--- Test 2: Sorting by 'match' ---")
        page.select_option('#cockpit-sort-select', 'match')
        page.wait_for_timeout(500)

        sorted_rows_match = page.query_selector_all('.opportunity-row')
        # Click 3rd row (index 2)
        idx = min(2, len(sorted_rows_match) - 1)
        row_3 = sorted_rows_match[idx]
        id_3 = row_3.get_attribute('data-job-id')
        title_3 = row_3.get_attribute('data-job-title')
        print(f"Clicking row {idx}: ID={id_3}, Title={title_3}")
        row_3.click()
        page.wait_for_timeout(300)

        inspector_id = page.inner_text('#inspect-job-id')
        inspector_title = page.inner_text('#inspect-job-title')
        print(f"Inspector displayed: ID='{inspector_id}', Title='{inspector_title}'")
        assert f"#{id_3}" in inspector_id, f"Expected ID #{id_3} in {inspector_id}"
        assert title_3 in inspector_title, f"Expected Title '{title_3}' in {inspector_title}"
        print("PASS: Clicked row matches inspector after sorting by 'match'")

        # Test 3: Keyboard navigation with 'j'
        print("\n--- Test 3: Keyboard navigation 'j' (move down) ---")
        page.keyboard.press('j')
        page.wait_for_timeout(300)

        expected_next_row = sorted_rows_match[min(idx + 1, len(sorted_rows_match) - 1)]
        expected_next_id = expected_next_row.get_attribute('data-job-id')
        expected_next_title = expected_next_row.get_attribute('data-job-title')

        inspector_id = page.inner_text('#inspect-job-id')
        inspector_title = page.inner_text('#inspect-job-title')
        print(f"After 'j', inspector displayed: ID='{inspector_id}', Title='{inspector_title}'")
        assert f"#{expected_next_id}" in inspector_id, f"Expected ID #{expected_next_id} in {inspector_id}"
        assert expected_next_title in inspector_title, f"Expected Title '{expected_next_title}' in {inspector_title}"
        print("PASS: Keyboard navigation 'j' correctly advanced to next sorted row")

    print("\nALL VERIFICATIONS PASSED SUCCESSFULLY!")
    browser.close()
