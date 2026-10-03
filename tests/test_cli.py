from __future__ import annotations

import base64
import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
CLI = PROJECT_DIR / "bin" / "vimbrowser-cli"


class OneShotServer:
    """Minimal vimbrowser-ipc/1 server for non-mutating CLI tests."""

    def __init__(self, directory: Path, response: bytes) -> None:
        self.path = directory / "ipc.sock"
        self.response = response
        self.command = b""
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.path))
        self._listener.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> OneShotServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._listener.close()
        self._thread.join(timeout=1)
        if exc_type is None:
            if self._thread.is_alive():
                raise AssertionError("fake IPC server did not finish")
            if self.error is not None:
                raise self.error

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
            with connection:
                while not self.command.endswith(b"\n"):
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    self.command += chunk
                connection.sendall(self.response)
        except BaseException as exc:  # Surface background failures in the test.
            self.error = exc


class CliExitTests(unittest.TestCase):
    def test_help_exits(self) -> None:
        result = subprocess.run(
            [str(CLI), "-h"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: vimbrowser-cli", result.stdout)

    def test_protocol_response_is_printed_and_process_exits(self) -> None:
        response = b'{"protocol":"vimbrowser-ipc","version":1}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-test-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                started = time.monotonic()
                result = subprocess.run(
                    [
                        str(CLI),
                        "protocol",
                        "--socket",
                        str(server.path),
                        "--timeout",
                        "1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                elapsed = time.monotonic() - started

            self.assertEqual(server.command, b"protocol\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, response.decode())
            self.assertLess(elapsed, 1)

    def test_open_uses_background_tab_ipc(self) -> None:
        response = b'{"active_tabid":1,"tabs":[]}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-open-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = subprocess.run(
                    [
                        str(CLI), "open", "--socket", str(server.path),
                        "--timeout", "1", "https://example.com/path?q=1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            server.command,
            b"open-background-tab https://example.com/path?q=1\n",
        )

    def test_open_context_uses_background_context_ipc(self) -> None:
        response = b'{"active_tabid":1,"tabs":[]}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-context-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = subprocess.run(
                    [
                        str(CLI), "open-context", "--socket", str(server.path),
                        "--timeout", "1", "work", "https://example.com",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            server.command,
            b"open-background-context-tab work https://example.com\n",
        )


class StdinPayloadTests(unittest.TestCase):
    def run_cli(self, *args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *args], input=input_text, capture_output=True, text=True,
            timeout=2, check=False,
        )

    def test_js_preserves_exact_multiline_utf8_via_base64(self) -> None:
        source = "  const value = `$HOME  ${USER}`;\\n\nvalue + ' 👁️  ';  \n"
        response = b'{"ok":true,"value":"ok"}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-js-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "js", "7", "--socket", str(server.path), "--timeout", "1",
                    input_text=source,
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        command, tab, encoded = server.command.decode("utf-8").strip().split()
        self.assertEqual((command, tab), ("js-base64", "7"))
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), source)

    def test_frame_js_preserves_exact_payload_via_base64(self) -> None:
        source = "\n(() => 'two  spaces')()\n"
        response = b'{"ok":true,"value":"two  spaces"}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-frame-js-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "frame-js", "7", "frame-A", "--socket", str(server.path),
                    "--timeout", "1", input_text=source,
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        command, tab, frame, encoded = server.command.decode("utf-8").strip().split()
        self.assertEqual((command, tab, frame), ("frame-js-base64", "7", "frame-A"))
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), source)

    def test_raw_sends_one_exact_command_line(self) -> None:
        command = "network 7 detail request-$HOME"
        response = b'{"ok":true}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-raw-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "raw", "--socket", str(server.path), "--timeout", "1",
                    input_text=command,
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.command, command.encode("utf-8") + b"\n")

    def test_network_execute_preserves_sensitive_json_via_base64(self) -> None:
        payload = {
            "version": 1,
            "templateRequestId": 73,
            "url": "https://mail.example.test/sync?opaque=$TOKEN",
            "method": "POST",
            "bodyUtf8": "line one\nline two $BODY",
            "headerOverrides": {"X-Framework-Xsrf-Token": "$SECRET"},
        }
        source = json.dumps(payload, ensure_ascii=False, indent=2)
        response = b'{"ok":true,"status":200}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-network-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "network-execute", "--socket", str(server.path),
                    "--timeout", "1", "7", input_text=source,
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        command, tab, encoded = server.command.rstrip(b"\n").split(b" ", 2)
        self.assertEqual((command, tab), (b"network-execute-base64", b"7"))
        self.assertEqual(base64.b64decode(encoded), source.encode("utf-8"))
        self.assertNotIn(b"$SECRET", server.command)
        self.assertEqual(result.stdout, response.decode())

    def test_network_execute_rejects_inline_payload(self) -> None:
        result = self.run_cli(
            "network-execute", "7", '{"version":1,"templateRequestId":1}',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be provided via stdin", result.stderr)

    def test_inline_and_missing_payloads_fail_before_ipc(self) -> None:
        cases = (
            (("js", "7", "document.title"), "JavaScript must be provided via stdin"),
            (("frame-js", "7", "frame-A", "document.title"), "JavaScript must be provided via stdin"),
            (("raw", "status"), "raw command must be provided via stdin"),
            (("network-execute", "7"), "network execute JSON payload is required on stdin"),
            (("js", "7"), "JavaScript is required on stdin"),
            (("frame-js", "7", "frame-A"), "JavaScript is required on stdin"),
            (("raw",), "raw command is required on stdin"),
        )
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

    def test_raw_rejects_multiple_lines_without_trimming(self) -> None:
        result = self.run_cli("raw", input_text="status\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly one command line without CR, LF, or NUL", result.stderr)


class UploadFileTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )

    def test_help_lists_upload_file(self) -> None:
        result = self.run_cli("-h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("upload-file", result.stdout)
        self.assertIn("activate-control", result.stdout)

    def test_activate_control_sends_exact_handle(self) -> None:
        handle = "eh1_exact-control-capability"
        response = (
            b'{"ok":true,"tabid":7,"target":{"kind":"handle"},'
            b'"activation":{"dispatched":true,"user_activation":true}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-activate-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "activate-control", "7", handle,
                    "--socket", str(server.path), "--timeout", "1",
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            server.command.decode("utf-8").strip(),
            f"activate-control 7 {handle}",
        )
        self.assertTrue(json.loads(result.stdout)["activation"]["user_activation"])

    def test_activate_control_rejects_malformed_handle_before_ipc(self) -> None:
        result = self.run_cli("activate-control", "7", "not-a-handle")
        self.assertEqual(result.returncode, 2)
        self.assertIn("handle is malformed", result.stderr)

    def test_css_target_and_paths_are_encoded_in_versioned_payload(self) -> None:
        response = (
            b'{"ok":true,"tabid":7,"file_count":2,'
            b'"target":{"kind":"css","match_count":1},'
            b'"input":{"multiple":true,"accept":""}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-upload-") as tmp:
            directory = Path(tmp)
            first = directory / "one file.txt"
            second = directory / "two.txt"
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            with OneShotServer(directory, response) as server:
                result = self.run_cli(
                    "upload-file",
                    "7",
                    "css:#attachments",
                    str(first),
                    str(second),
                    "--socket",
                    str(server.path),
                    "--timeout",
                    "1",
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["file_count"], 2)
            words = server.command.decode("utf-8").strip().split()
            self.assertEqual(words[:2], ["upload-file", "7"])
            self.assertEqual(len(words), 3)
            self.assertNotIn(str(first), server.command.decode("utf-8"))
            payload = json.loads(base64.b64decode(words[2], validate=True))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["target"], {"kind": "css", "value": "#attachments"})
            self.assertEqual(payload["paths"], [str(first.resolve()), str(second.resolve())])

    def test_explicit_input_index_target(self) -> None:
        response = b'{"ok":true,"tabid":2,"file_count":1}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-upload-") as tmp:
            directory = Path(tmp)
            upload = directory / "upload.bin"
            upload.write_bytes(b"payload")
            with OneShotServer(directory, response) as server:
                result = self.run_cli(
                    "upload-file", "2", "index:1", str(upload),
                    "--socket", str(server.path), "--timeout", "1",
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            token = server.command.decode("utf-8").strip().split()[2]
            payload = json.loads(base64.b64decode(token, validate=True))
            self.assertEqual(payload["target"], {"kind": "index", "value": 1})

    def test_chooser_target_arms_versioned_payload_without_selector(self) -> None:
        response = (
            b'{"ok":true,"tabid":2,"target":{"kind":"chooser"},'
            b'"chooser":{"state":"armed","file_count":1,'
            b'"expires_in_ms":60000,"dialog_mode":"none"}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-upload-") as tmp:
            directory = Path(tmp)
            upload = directory / "resume.pdf"
            upload.write_bytes(b"pdf fixture")
            with OneShotServer(directory, response) as server:
                result = self.run_cli(
                    "upload-file", "2", "chooser", str(upload),
                    "--socket", str(server.path), "--timeout", "1",
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            token = server.command.decode("utf-8").strip().split()[2]
            payload = json.loads(base64.b64decode(token, validate=True))
            self.assertEqual(payload["target"], {"kind": "chooser"})

    def test_activate_target_encodes_atomic_native_activation_selector(self) -> None:
        response = (
            b'{"ok":true,"tabid":2,"file_count":1,'
            b'"target":{"kind":"activate","match_count":1},'
            b'"chooser":{"state":"consumed","dialog_mode":"open"}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-upload-") as tmp:
            directory = Path(tmp)
            upload = directory / "resume.pdf"
            upload.write_bytes(b"pdf fixture")
            with OneShotServer(directory, response) as server:
                result = self.run_cli(
                    "upload-file", "2", "activate:#browse-resume", str(upload),
                    "--socket", str(server.path), "--timeout", "1",
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            token = server.command.decode("utf-8").strip().split()[2]
            payload = json.loads(base64.b64decode(token, validate=True))
            self.assertEqual(
                payload["target"],
                {"kind": "activate", "value": "#browse-resume"},
            )

    def test_exact_handle_target_is_encoded_without_frame_or_selector(self) -> None:
        response = (
            b'{"ok":true,"tabid":2,"file_count":1,'
            b'"target":{"kind":"handle","match_count":1},'
            b'"chooser":{"state":"consumed","dialog_mode":"open"}}\n'
        )
        handle = "eh1_0123456789ABCDEF0123456789ABCDEF"
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-upload-") as tmp:
            directory = Path(tmp)
            upload = directory / "resume.pdf"
            upload.write_bytes(b"pdf fixture")
            with OneShotServer(directory, response) as server:
                result = self.run_cli(
                    "upload-file", "2", f"handle:{handle}", str(upload),
                    "--socket", str(server.path), "--timeout", "1",
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        token = server.command.decode("utf-8").strip().split()[2]
        payload = json.loads(base64.b64decode(token, validate=True))
        self.assertEqual(payload["target"], {"kind": "handle", "value": handle})

    def test_chooser_status_and_cancel_have_cli_surfaces(self) -> None:
        response = (
            b'{"ok":true,"tabid":2,"target":{"kind":"chooser"},'
            b'"chooser":{"state":"armed","file_count":1}}\n'
        )
        for cli_name, ipc_name in (
            ("upload-file-status", "upload-file-status"),
            ("upload-file-cancel", "upload-file-cancel"),
        ):
            with self.subTest(command=cli_name):
                with tempfile.TemporaryDirectory(
                    prefix="vimbrowser-cli-upload-"
                ) as tmp:
                    with OneShotServer(Path(tmp), response) as server:
                        result = self.run_cli(
                            cli_name, "2", "--socket", str(server.path),
                            "--timeout", "1",
                        )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        server.command.decode("utf-8").strip(), f"{ipc_name} 2"
                    )

    def test_relative_path_is_rejected_before_ipc(self) -> None:
        result = self.run_cli("upload-file", "1", "#upload", "relative.txt")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_path")
        self.assertIn("must be absolute", payload["error"]["message"])

    def test_nonexistent_path_is_rejected_without_echoing_path(self) -> None:
        missing = "/tmp/vimbrowser-cli-definitely-missing-upload-77a36f"
        result = self.run_cli("upload-file", "1", "#upload", missing)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "invalid_path")
        self.assertNotIn(missing, result.stdout + result.stderr)

    def test_directory_is_rejected_as_non_regular(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-upload-") as tmp:
            result = self.run_cli("upload-file", "1", "#upload", tmp)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a regular file", json.loads(result.stdout)["error"]["message"])

    def test_invalid_index_target_is_structured_error(self) -> None:
        with tempfile.NamedTemporaryFile() as upload:
            result = self.run_cli("upload-file", "1", "index:-1", upload.name)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_target")

    def test_browser_structured_error_is_printed_and_exits_nonzero(self) -> None:
        response = (
            b'{"ok":false,"error":{"code":"target_not_file_input",'
            b'"message":"target is not an input of type file"}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-upload-") as tmp:
            directory = Path(tmp)
            upload = directory / "upload.txt"
            upload.write_text("not logged", encoding="utf-8")
            with OneShotServer(directory, response) as server:
                result = self.run_cli(
                    "upload-file", "1", "#ordinary-text-input", str(upload),
                    "--socket", str(server.path), "--timeout", "1",
                )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "target_not_file_input")
        self.assertNotIn(str(upload), result.stdout + result.stderr)


class FrameInspectionTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *args], capture_output=True, text=True, timeout=2,
            check=False,
        )

    def test_frame_tree_command(self) -> None:
        response = b'{"ok":true,"tabid":7,"main_frame_id":"main","frames":[]}\n'
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-frame-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "frame-tree", "7", "--socket", str(server.path),
                    "--timeout", "1",
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.command, b"frame-tree 7\n")

    def test_inspect_controls_encodes_exact_frame_and_filters(self) -> None:
        response = (
            b'{"ok":true,"tabid":7,"frame":{"id":"frame-A"},'
            b'"inspection":{"match_count":1,"controls":[{'
            b'"handle":"eh1_token","role":"button","name":"Browse"}]}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-frame-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "inspect-controls", "7", "--frame", "frame-A",
                    "--role", "button", "--name-exact", "Browse",
                    "--context-contains", "Upload files", "--require-one",
                    "--socket", str(server.path), "--timeout", "1",
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        words = server.command.decode("utf-8").strip().split()
        self.assertEqual(words[:2], ["inspect-controls", "7"])
        query = json.loads(base64.b64decode(words[2], validate=True))
        self.assertEqual(query["frame_id"], "frame-A")
        self.assertEqual(query["filter"]["role"], "button")
        self.assertEqual(query["filter"]["exact_name"], "Browse")
        self.assertEqual(query["filter"]["context_contains"], "Upload files")

    def test_require_one_rejects_ambiguous_results_without_hiding_candidates(self) -> None:
        response = (
            b'{"ok":true,"tabid":7,"frame":{"id":"frame-A"},'
            b'"inspection":{"match_count":2,"controls":['
            b'{"handle":"eh1_one"},{"handle":"eh1_two"}]}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-frame-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "inspect-controls", "7", "--frame", "frame-A",
                    "--name-exact", "Browse", "--require-one",
                    "--socket", str(server.path), "--timeout", "1",
                )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "ambiguous_target")
        self.assertEqual(len(payload["inspection"]["controls"]), 2)

    def test_require_one_rejects_truncated_single_return(self) -> None:
        response = (
            b'{"ok":true,"tabid":7,"frame":{"id":"frame-A"},'
            b'"inspection":{"match_count":2,"returned_count":1,'
            b'"truncated":true,"controls":[{"handle":"eh1_one"}]}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-frame-") as tmp:
            with OneShotServer(Path(tmp), response) as server:
                result = self.run_cli(
                    "inspect-controls", "7", "--frame", "frame-A",
                    "--name-exact", "Browse", "--limit", "1", "--require-one",
                    "--socket", str(server.path), "--timeout", "1",
                )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "ambiguous_target")
        self.assertTrue(payload["error"]["truncated"])
        self.assertEqual(len(payload["inspection"]["controls"]), 1)

    def test_inspect_controls_preserves_browser_error(self) -> None:
        response = (
            b'{"ok":false,"error":{"code":"stale_document",'
            b'"message":"frame document changed"}}\n'
        )
        with tempfile.TemporaryDirectory(prefix="vimbrowser-cli-frame-") as tmp:
            with OneShotServer(Path(tmp), response):
                result = self.run_cli(
                    "inspect-controls", "7", "--frame", "frame-A",
                    "--require-one", "--socket", str(Path(tmp) / "ipc.sock"),
                    "--timeout", "1",
                )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "stale_document")
        self.assertNotIn("inspection", payload)


if __name__ == "__main__":
    unittest.main()
