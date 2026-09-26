"""Use Playwright's dedicated headless shell for UI tests, never a desktop browser.

System Edge / Chrome can probe the Windows account's password when a password form is submitted. Repeated
launches with fresh profiles can therefore generate failed Windows logons and lock the account. Playwright's
headless shell has no desktop password manager; headless=True without a channel selects that shell.
"""
from __future__ import annotations

import pytest

INSTALL_HINT = "Install the UI test browser with: python -m playwright install chromium-headless-shell"


def launch_test_browser(playwright):
    """Only the Playwright headless shell. A missing install skips UI tests; any other launch failure is an error.
    No channel / executable_path and no retry with a system browser, even if Edge or Chrome is installed."""
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip(INSTALL_HINT)
        raise
