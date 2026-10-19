"""Native vimbrowser IPC transport helpers."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import time
import math


class IpcError(RuntimeError):
    """Raised for transport/protocol errors, without echoing command payloads."""

    def __init__(self, message: str, *, phase="protocol", outcome="unknown", received_bytes=0):
        super().__init__(message)
        self.phase = phase
        self.outcome = outcome
        self.received_bytes = received_bytes


def _candidate_socket_paths() -> list[Path]:
    paths: list[Path] = []

    if value := os.environ.get("VIMBROWSER_IPC"):
        paths.append(Path(value).expanduser())
    if value := os.environ.get("VIMBROWSER_PROFILE_DIR"):
        paths.append(Path(value).expanduser() / "ipc.sock")

    # Installed vimbrowser wrapper profile used by this machine.
    paths.append(Path.home() / ".runtime" / "vimbrowser-yeyito" / "ipc.sock")

    if value := os.environ.get("XDG_STATE_HOME"):
        paths.append(Path(value).expanduser() / "vimbrowser" / "ipc.sock")
    paths.append(Path.home() / ".local" / "state" / "vimbrowser" / "ipc.sock")
    paths.append(Path("/tmp/vimbrowser/ipc.sock"))

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def socket_path(*, socket_override: str | None = None,
                profile_dir: str | None = None) -> Path:
    """Resolve the IPC socket path without requiring the process to be live."""
    if socket_override:
        return Path(socket_override).expanduser()
    if profile_dir:
        return Path(profile_dir).expanduser() / "ipc.sock"

    # An explicit environment target must NEVER silently fall through to the
    # owner's live socket when a fixture profile has not started yet.
    if value := os.environ.get("VIMBROWSER_IPC"):
        return Path(value).expanduser()
    if value := os.environ.get("VIMBROWSER_PROFILE_DIR"):
        return Path(value).expanduser() / "ipc.sock"
    for path in _candidate_socket_paths():
        if path.exists():
            return path
    return _candidate_socket_paths()[0]


def send_bytes(command: str, *, socket_override: str | None = None,
               profile_dir: str | None = None, timeout: float = 10.0) -> bytes:
    """Send one command line and preserve the full response bytes."""
    path = socket_path(socket_override=socket_override, profile_dir=profile_dir)
    path_text = str(path)
    if len(path_text.encode()) >= 108:
        raise IpcError(f"socket path too long for AF_UNIX: {path_text}")

    if not math.isfinite(timeout) or timeout <= 0:
        raise IpcError("timeout must be a finite positive number", phase="validation", outcome="not_sent")
    phase = "connect"
    received = 0
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path_text)
            phase = "send"
            sock.settimeout(max(.001, deadline - time.monotonic()))
            sock.sendall((command.rstrip("\n") + "\n").encode("utf-8"))
            phase = "receive"
            chunks: list[bytes] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("total response deadline exceeded")
                sock.settimeout(remaining)
                chunk = sock.recv(262144)
                if not chunk:
                    break
                received += len(chunk)
                chunks.append(chunk)
    except OSError as exc:
        outcome = "not_sent" if phase == "connect" else "unknown"
        note = "request not sent" if outcome == "not_sent" else (
            "operation may still be queued/running or completed; disconnect is not rollback; "
            "do not blindly retry mutations; use diagnose")
        raise IpcError(f"IPC {phase} failed: {exc}; {note}",
                       phase=phase, outcome=outcome, received_bytes=received) from exc

    return b"".join(chunks)


def send(command: str, *, socket_override: str | None = None,
         profile_dir: str | None = None, timeout: float = 10.0) -> str:
    """Send one command line and return the full response text."""
    return send_bytes(command, socket_override=socket_override,
                      profile_dir=profile_dir, timeout=timeout).decode(
                          "utf-8", errors="replace")


def ensure_ok(response: str) -> str:
    """Return response or raise if vimbrowser reported an ERR response."""
    if response.startswith("ERR "):
        raise IpcError(response.rstrip())
    return response


def die(message: str, *, code: int = 1) -> None:
    print(f"vimbrowser-cli: {message}", file=sys.stderr)
    raise SystemExit(code)
