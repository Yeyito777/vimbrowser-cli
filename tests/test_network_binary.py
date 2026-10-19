import subprocess
import tempfile
import unittest
from pathlib import Path
from test_cli import CLI, OneShotServer


class NetworkBinaryTests(unittest.TestCase):
    def test_preserves_non_utf8_bytes(self):
        data = b"%PDF-1.4\n%\xff\x00\x80\xfe\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            out = directory / "download.pdf"
            with OneShotServer(directory, data) as server:
                result = subprocess.run(
                    [str(CLI), "network", "--socket", str(server.path),
                     "42", "body", "16", "--output", str(out)],
                    capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(server.command, b"network 42 body 16\n")
            self.assertEqual(out.read_bytes(), data)
            self.assertNotIn(data, result.stdout)

    def test_refuses_existing_output_before_connecting(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "existing"
            out.write_bytes(b"original")
            result = subprocess.run(
                [str(CLI), "network", "--socket", "/nonexistent",
                 "42", "body", "16", "--output", str(out)],
                capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(out.read_bytes(), b"original")

    def test_error_response_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            out = directory / "download.pdf"
            with OneShotServer(directory, b"ERR request not found\n") as server:
                result = subprocess.run(
                    [str(CLI), "network", "--socket", str(server.path),
                     "42", "body", "16", "--output", str(out)],
                    capture_output=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(out.exists())
