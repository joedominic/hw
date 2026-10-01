import os, sys
sys.path.insert(0, os.path.abspath('.'))
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

output_dir = r"C:\Users\joedo\.gemini\antigravity-cli\brain\77867640-d726-4613-ae6f-61a8757b6c04"
with sync_playwright() as p:
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    browser = p.chromium.launch(executable_path=edge_path, headless=True)
    ctx = browser.new_context(viewport={'width': 1366, 'height': 768})
    ctx.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': session.session_key, 'domain': '127.0.0.1', 'path': '/'}])
    page = ctx.new_page()
    page.goto('http://127.0.0.1:8080/jobs/cockpit/')
    page.wait_for_load_state('domcontentloaded')
    page.wait_for_timeout(1000)

    # Open drawer
    page.evaluate("openEditProfileDrawer('general')")
    page.wait_for_timeout(500)

    # Change location
    page.fill('#drawer-location', 'Dallas, TX (Hybrid)')
    page.click('button[type="submit"]:has-text("Save Profile")')
    page.wait_for_load_state('domcontentloaded')
    page.wait_for_timeout(1500)

    page.screenshot(path=os.path.join(output_dir, 'after_save_profile.png'))
    print("Saved successfully and captured after_save_profile.png!")
    browser.close()
