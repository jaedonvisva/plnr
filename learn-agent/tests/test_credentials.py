"""Real-browser test for learn.credentials.maybe_autofill_login against a
two-step ADFS-style login form (username-only screen, advancing reveals the
password screen) -- the shape confirmed live against the real WatIAM page
at adfs.uwaterloo.ca. Two things were confirmed live and are deliberately
reproduced exactly here, since both were the actual root causes of two
separate live bugs:

1. The step-1 advance control and the step-2 submit control are TWO
   DIFFERENT elements (#nextButton, visible immediately; #submitButton,
   `display:none` until #nextButton is clicked) -- NOT the same element
   reused across steps, which was an earlier, incorrect assumption that
   shipped a fix that silently did nothing (clicking a `display:none`
   element is a no-op/hang, not a click).
2. #passwordInput exists in the DOM from page load, just not visible --
   so detecting "am I on step 1" must check visibility, not presence.
"""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from learn import credentials

TWO_STEP_FORM_HTML = """
<!doctype html>
<html>
<body>
  <input id="userNameInput" type="text" />
  <input id="passwordInput" type="password" style="display:none" />
  <span id="nextButton" role="button" onclick="showStep2()">Next</span>
  <span id="submitButton" role="button" style="display:none" onclick="submitLogin()">Sign in</span>
  <div id="result"></div>
  <script>
    function showStep2() {
      document.getElementById('passwordInput').style.display = 'block';
      document.getElementById('nextButton').style.display = 'none';
      document.getElementById('submitButton').style.display = 'inline-block';
    }
    function submitLogin() {
      document.getElementById('result').textContent =
        'submitted:' + document.getElementById('passwordInput').value;
    }
  </script>
</body>
</html>
"""


@pytest.fixture
def two_step_login_page(tmp_path, monkeypatch):
    monkeypatch.setenv("WATIAM_USERNAME", "testuser")
    monkeypatch.setenv("WATIAM_PASSWORD", "testpass")
    # Prevent the real .env file (if present) from being loaded over the
    # monkeypatched env vars.
    monkeypatch.setattr(credentials, "ENV_FILE", tmp_path / "nonexistent.env")

    html_path = tmp_path / "login.html"
    html_path.write_text(TWO_STEP_FORM_HTML)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        yield page
        browser.close()


def test_autofill_completes_two_step_form_without_manual_enter(two_step_login_page):
    page = two_step_login_page

    filled = credentials.maybe_autofill_login(page, timeout_ms=5000)

    assert filled is True
    # Root-cause regression check: password must actually get submitted,
    # not left stuck on the username-only first screen.
    page.wait_for_selector("#result:has-text('submitted:testpass')", timeout=5000)


SINGLE_STEP_FORM_HTML = """
<!doctype html>
<html>
<body>
  <input id="userNameInput" type="text" />
  <input id="passwordInput" type="password" />
  <button id="submitButton" onclick="submitLogin()">Sign in</button>
  <div id="result"></div>
  <script>
    function submitLogin() {
      document.getElementById('result').textContent =
        'submitted:' + document.getElementById('passwordInput').value;
    }
  </script>
</body>
</html>
"""


@pytest.fixture
def single_step_login_page(tmp_path, monkeypatch):
    monkeypatch.setenv("WATIAM_USERNAME", "testuser")
    monkeypatch.setenv("WATIAM_PASSWORD", "testpass")
    monkeypatch.setattr(credentials, "ENV_FILE", tmp_path / "nonexistent.env")

    html_path = tmp_path / "login.html"
    html_path.write_text(SINGLE_STEP_FORM_HTML)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        yield page
        browser.close()


def test_autofill_still_works_on_single_step_form(single_step_login_page):
    page = single_step_login_page

    filled = credentials.maybe_autofill_login(page, timeout_ms=5000)

    assert filled is True
    page.wait_for_selector("#result:has-text('submitted:testpass')", timeout=5000)
