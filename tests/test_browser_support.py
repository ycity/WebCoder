"""Regression checks: UI tests never fall back to the Windows desktop browser."""
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from tests.browser_support import INSTALL_HINT, launch_test_browser


def test_only_the_playwright_headless_shell_is_requested():
    browser = object()
    launch = Mock(return_value=browser)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    assert launch_test_browser(playwright) is browser
    launch.assert_called_once_with(headless=True)


def test_a_missing_shell_skips_without_trying_edge_or_chrome():
    launch = Mock(side_effect=RuntimeError("BrowserType.launch: Executable doesn't exist at /missing/headless_shell"))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    with pytest.raises(pytest.skip.Exception) as skipped:
        launch_test_browser(playwright)
    assert INSTALL_HINT in str(skipped.value)
    launch.assert_called_once_with(headless=True)


def test_other_launch_errors_are_not_hidden_or_retried():
    error = RuntimeError("browser crashed during startup")
    launch = Mock(side_effect=error)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    with pytest.raises(RuntimeError) as failed:
        launch_test_browser(playwright)
    assert failed.value is error
    launch.assert_called_once_with(headless=True)


def test_hub_port_skips_ports_a_browser_refuses(monkeypatch):
    from tests import conftest

    sockets = []
    for port in (80, 6000, 6667, 50000):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = ("127.0.0.1", port)
        sockets.append(sock)
    monkeypatch.setattr(conftest.socket, "socket", Mock(side_effect=sockets))
    assert conftest.free_port() == 50000
    for sock in sockets:
        sock.bind.assert_called_once_with(("127.0.0.1", 0))
        sock.__exit__.assert_called_once()
