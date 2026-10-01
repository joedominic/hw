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

    # Test 1: Open existing profile 'architect'
    page.evaluate("openEditProfileDrawer('architect')")
    page.wait_for_timeout(300)
    is_checked = page.is_checked('#board-greenhouse')
    print("Architect profile has greenhouse checked:", is_checked)
    assert is_checked, "Greenhouse should be checked for 'architect' profile"

    # Test 2: Open existing profile 'director'
    page.evaluate("openEditProfileDrawer('director')")
    page.wait_for_timeout(300)
    is_checked = page.is_checked('#board-greenhouse')
    print("Director profile has greenhouse checked:", is_checked)
    assert is_checked, "Greenhouse should be checked for 'director' profile"

    # Test 3: Open new profile drawer
    page.evaluate("openNewProfileDrawer()")
    page.wait_for_timeout(300)
    is_checked = page.is_checked('#board-greenhouse')
    print("New profile has greenhouse checked by default:", is_checked)
    assert is_checked, "Greenhouse should be checked by default for new profile"

    browser.close()
    print("ALL DRAWER VERIFICATIONS PASSED!")
