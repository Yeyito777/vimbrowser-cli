from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_cli import CLI, OneShotServer

COOKIES = {
    "cookies": [
        {"name": "auth_token", "value": "s3cr3t-value", "domain": ".x.com", "path": "/"},
        {"name": "ct0", "value": "csrf-value", "domain": ".x.com", "path": "/"},
    ]
}


def run_cli(*args: str, input_text: str = "", env: dict[str, str] | None = None, cwd: str | None = None):
    environment = {k: v for k, v in os.environ.items() if k != "VIMBROWSER_CLI_COOKIE_VALUES"}
    environment.update(env or {})
    return subprocess.run([str(CLI), *args], input=input_text, capture_output=True, text=True,
                          timeout=5, check=False, env=environment, cwd=cwd)


class ExplicitTargetTests(unittest.TestCase):
    def test_close_tab_requires_an_explicit_tab(self) -> None:
        # No IPC is attempted: the socket path does not exist.
        result = run_cli("close-tab", "--socket", "/nonexistent/vimbrowser.sock")
        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit tab", result.stderr)

    def test_close_tab_sends_the_exact_tab(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vb-close-") as tmp:
            with OneShotServer(Path(tmp), b'{"ok":true}\n') as server:
                result = run_cli("close-tab", "12", "--socket", str(server.path), "--timeout", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.command, b"tab-close 12\n")

    def test_js_requires_an_explicit_tab(self) -> None:
        result = run_cli("js", "--socket", "/nonexistent/vimbrowser.sock", input_text="1 + 1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit tab", result.stderr)

    def test_cookies_requires_a_tab_or_url(self) -> None:
        result = run_cli("cookies", "--socket", "/nonexistent/vimbrowser.sock")
        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit tab", result.stderr)

    def cookies(self, *extra: str, env: dict[str, str] | None = None):
        with tempfile.TemporaryDirectory(prefix="vb-cookies-") as tmp:
            with OneShotServer(Path(tmp), json.dumps(COOKIES).encode() + b"\n") as server:
                result = run_cli("cookies", "--url", "https://x.com", *extra,
                                 "--socket", str(server.path), "--timeout", "1", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.command, b"cookies-url https://x.com\n")
        return result

    def test_cookie_values_are_hidden_by_default(self) -> None:
        result = self.cookies()
        self.assertNotIn("s3cr3t-value", result.stdout)
        self.assertNotIn("csrf-value", result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual([c["name"] for c in payload["cookies"]], ["auth_token", "ct0"])
        self.assertTrue(all("value" not in c for c in payload["cookies"]))
        self.assertTrue(payload["values_hidden"])

    def test_cookie_values_on_explicit_request(self) -> None:
        for extra, env in ((("--values",), None), ((), {"VIMBROWSER_CLI_COOKIE_VALUES": "1"})):
            result = self.cookies(*extra, env=env)
            self.assertEqual(json.loads(result.stdout), COOKIES)


class LauncherTests(unittest.TestCase):
    def test_caller_working_directory_cannot_shadow_cli_modules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            marker = root / "imported"
            (root / "src" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
            result = run_cli("status", "--help", cwd=directory)
            self.assertFalse(marker.exists(), "caller cwd src package was imported")
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
