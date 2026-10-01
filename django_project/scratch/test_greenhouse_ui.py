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
    
    # 1. Test Discover / Jobs Search page
    page = ctx.new_page()
    page.goto('http://127.0.0.1:8080/jobs/search/')
    page.wait_for_load_state('domcontentloaded')
    page.wait_for_timeout(1000)

    # Check for Greenhouse checkbox in search hero
    gh_cb = page.query_selector('input[name="site_name"][value="greenhouse"]')
    assert gh_cb is not None, "Greenhouse checkbox missing on /jobs/search/"
    print("Found Greenhouse checkbox on /jobs/search/!")

    # 2. Test Cockpit Search Profile Drawer
    page2 = ctx.new_page()
    page2.goto('http://127.0.0.1:8080/jobs/cockpit/')
    page2.wait_for_load_state('domcontentloaded')
    page2.wait_for_timeout(1000)

    # Open drawer
    page2.evaluate("openNewProfileDrawer()")
    page2.wait_for_timeout(500)
    gh_drawer_cb = page2.query_selector('#board-greenhouse')
    assert gh_drawer_cb is not None, "Greenhouse checkbox missing in cockpit Search Profile drawer"
    print("Found Greenhouse checkbox in Cockpit Search Profile drawer!")

    browser.close()
    print("ALL UI CHECKS PASSED!")
