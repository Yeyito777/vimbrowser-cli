"""Native vimbrowser IPC transport helpers."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import sys


class IpcError(RuntimeError):
    """Raised for transport/protocol errors."""


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

    for path in _candidate_socket_paths():
        if path.exists():
            return path
    return _candidate_socket_paths()[0]


def send(command: str, *, socket_override: str | None = None,
         profile_dir: str | None = None, timeout: float = 10.0) -> str:
    """Send one command line and return the full response text."""
    path = socket_path(socket_override=socket_override, profile_dir=profile_dir)
    path_text = str(path)
    if len(path_text.encode()) >= 108:
        raise IpcError(f"socket path too long for AF_UNIX: {path_text}")

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path_text)
            sock.sendall((command.rstrip("\n") + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(262144)
                if not chunk:
                    break
                chunks.append(chunk)
    except FileNotFoundError as exc:
        raise IpcError(
            f"could not connect to {path_text}: socket not found; is vimbrowser running?"
        ) from exc
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        raise IpcError(f"could not connect to {path_text}: {exc}") from exc

    return b"".join(chunks).decode("utf-8", errors="replace")


def ensure_ok(response: str) -> str:
    """Return response or raise if vimbrowser reported an ERR response."""
    if response.startswith("ERR "):
        raise IpcError(response.rstrip())
    return response


def die(message: str, *, code: int = 1) -> None:
    print(f"vimbrowser-cli: {message}", file=sys.stderr)
    raise SystemExit(code)
