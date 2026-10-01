import os
import sys

sys.path.insert(0, os.path.abspath('.'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
import django
django.setup()

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.conf import settings
from playwright.sync_api import sync_playwright

User = get_user_model()
user = User.objects.filter(username='migration_bootstrap').first()
if not user:
    user = User.objects.first()

session = SessionStore()
session['_auth_user_id'] = str(user.pk)
session['_auth_user_backend'] = 'django.contrib.auth.backends.ModelBackend'
session['_auth_user_hash'] = user.get_session_auth_hash()
session.save()

session_cookie_name = settings.SESSION_COOKIE_NAME

output_dir = r"C:\Users\joedo\.gemini\antigravity-cli\brain\77867640-d726-4613-ae6f-61a8757b6c04"
cockpit_screenshot = os.path.join(output_dir, "cockpit_with_compact_drawer.png")
cockpit_closed_screenshot = os.path.join(output_dir, "cockpit_sidebar_drawer_triggers.png")

with sync_playwright() as p:
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    browser = p.chromium.launch(executable_path=edge_path, headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 900})
    context.add_cookies([{
        'name': session_cookie_name,
        'value': session.session_key,
        'domain': '127.0.0.1',
        'path': '/',
    }])

    page = context.new_page()
    page.goto('http://127.0.0.1:8080/jobs/cockpit/')
    page.wait_for_load_state('domcontentloaded')
    page.wait_for_timeout(2000)

    # Capture initial cockpit with updated sidebar triggers
    page.screenshot(path=cockpit_closed_screenshot)
    print(f"Captured cockpit view: {cockpit_closed_screenshot}")

    # Open edit profile drawer
    page.evaluate("openEditProfileDrawer('general')")
    page.wait_for_timeout(500)

    # Check drawer panel dimensions
    panel_box = page.locator('#profile-drawer-panel').bounding_box()
    print(f"Panel Bounding Box: {panel_box}")
    form_box = page.locator('#profile-drawer-form').bounding_box()
    print(f"Form Bounding Box: {form_box}")

    # Capture open drawer screenshot
    page.screenshot(path=cockpit_screenshot)
    print(f"Captured drawer view: {cockpit_screenshot}")

    browser.close()
print("All done!")
