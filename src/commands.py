"""Command implementations for vimbrowser-cli."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from src import ipc

PROG = "vimbrowser-cli"
UPLOAD_PAYLOAD_VERSION = 1
MAX_UPLOAD_FILES = 32
MAX_UPLOAD_SELECTOR_BYTES = 4096
MAX_UPLOAD_PATH_BYTES = 4096
MAX_UPLOAD_PAYLOAD_BYTES = 256 * 1024
MAX_HANDLE_BYTES = 128
MAX_JS_BYTES = 1024 * 1024


def _parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--socket", default=None,
                   help="IPC socket path (overrides discovery and VIMBROWSER_IPC)")
    p.add_argument("--profile-dir", default=None,
                   help="Profile directory; uses DIR/ipc.sock")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="IPC timeout in seconds (default: 10)")
    return p


def _send(args, command: str) -> str:
    try:
        return ipc.ensure_ok(ipc.send(
            command,
            socket_override=getattr(args, "socket", None),
            profile_dir=getattr(args, "profile_dir", None),
            timeout=getattr(args, "timeout", 10.0),
        ))
    except ipc.IpcError as exc:
        ipc.die(str(exc))


def _json_response(args, command: str, *, label: str | None = None) -> dict:
    response = _send(args, command)
    response_label = label or command
    try:
        value = json.loads(response)
    except json.JSONDecodeError as exc:
        ipc.die(f"invalid JSON response for {response_label!r}: {exc}\n{response[:4096]}")
    if not isinstance(value, dict):
        ipc.die(f"unexpected non-object JSON response for {response_label!r}")
    return value


def _print_json(value, *, pretty: bool = False) -> None:
    if pretty:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def _status(args) -> dict:
    return _json_response(args, "status")


def _tabs(args) -> dict:
    return _json_response(args, "tabs")


def _looks_like_tab_spec(value: str) -> bool:
    return value.isdigit() or value in {"@active", "@first", "@last"}


def _resolve_tab(args, spec: str | None) -> str:
    if not spec or spec == "@active":
        tabid = _status(args).get("active_tabid")
        if not tabid:
            ipc.die("no active tab")
        return str(tabid)

    tabs = None
    if spec == "@first":
        tabs = _tabs(args).get("tabs", [])
        if not tabs:
            ipc.die("no tabs")
        return str(tabs[0].get("id"))
    if spec == "@last":
        tabs = _tabs(args).get("tabs", [])
        if not tabs:
            ipc.die("no tabs")
        return str(tabs[-1].get("id"))
    if spec.isdigit():
        return spec
    ipc.die(f"invalid tab spec {spec!r}; use a stable tab ID, @active, @first, or @last", code=2)


def _format_tab_line(tab: dict, active_tabid) -> str:
    prefix = "* " if tab.get("id") == active_tabid else "  "
    tab_no = tab.get("tab", "?")
    tabid = tab.get("id", "?")
    audible = " [audio]" if tab.get("audible") else ""
    loading = " loading" if tab.get("loading") else ""
    title = tab.get("title") or ""
    url = tab.get("url") or ""
    return f"{prefix}{tab_no:>3}  id={tabid:<5} {title}  {url}{audible}{loading}"


def _joined_tail(parts: list[str], what: str, parser: argparse.ArgumentParser) -> str:
    if not parts:
        parser.error(f"missing {what}")
    return " ".join(parts)


def _stdin_payload(parser: argparse.ArgumentParser, what: str,
                   *, one_line: bool = False) -> str:
    data = sys.stdin.buffer.read()
    if not data:
        parser.error(f"{what} is required on stdin")
    try:
        value = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        parser.error(f"{what} on stdin must be valid UTF-8")
    if one_line and ("\n" in value or "\r" in value or "\0" in value):
        parser.error(f"{what} on stdin must be exactly one command line without CR, LF, or NUL")
    return value


def _encoded_javascript(parser: argparse.ArgumentParser) -> str:
    source = _stdin_payload(parser, "JavaScript")
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_JS_BYTES:
        parser.error(f"JavaScript on stdin exceeds the {MAX_JS_BYTES}-byte limit")
    return base64.b64encode(encoded).decode("ascii")


def _reject_inline_payload(parser: argparse.ArgumentParser, extras: list[str],
                           what: str) -> None:
    if extras:
        parser.error(f"{what} must be provided via stdin; inline payload is not accepted")


def _upload_error(code: str, message: str, *, exit_code: int = 2,
                  **details) -> None:
    """Print a structured upload error without echoing any local path."""
    error = {"code": code, "message": message}
    error.update(details)
    _print_json({"ok": False, "error": error})
    raise SystemExit(exit_code)


def _parse_upload_target(value: str) -> dict:
    """Return the versioned IPC target object for a CLI target expression."""
    if value == "chooser":
        return {"kind": "chooser"}
    if value.startswith("handle:"):
        handle = value.removeprefix("handle:")
        if not handle.startswith("eh1_") or len(handle.encode("utf-8")) > MAX_HANDLE_BYTES:
            raise ValueError("inspected element handle is malformed")
        return {"kind": "handle", "value": handle}
    if value.startswith("activate:"):
        selector = value.removeprefix("activate:")
        if not selector:
            raise ValueError("activation CSS selector target must not be empty")
        if len(selector.encode("utf-8")) > MAX_UPLOAD_SELECTOR_BYTES:
            raise ValueError("activation CSS selector target is too long")
        return {"kind": "activate", "value": selector}
    if value.startswith("index:"):
        index_text = value.removeprefix("index:")
        if not index_text.isdecimal():
            raise ValueError("index target must be a non-negative decimal integer")
        index = int(index_text)
        if index > 10000:
            raise ValueError("index target exceeds the supported limit")
        return {"kind": "index", "value": index}

    selector = value.removeprefix("css:") if value.startswith("css:") else value
    if not selector:
        raise ValueError("CSS selector target must not be empty")
    if len(selector.encode("utf-8")) > MAX_UPLOAD_SELECTOR_BYTES:
        raise ValueError("CSS selector target is too long")
    return {"kind": "css", "value": selector}


def _validated_upload_paths(values: list[str]) -> list[str]:
    """Validate and canonicalize explicit local upload paths.

    The browser repeats these checks in its own process. Keeping the CLI check
    makes obvious caller mistakes fail before any IPC mutation is attempted.
    """
    if not values:
        raise ValueError("at least one file path is required")
    if len(values) > MAX_UPLOAD_FILES:
        raise ValueError(f"at most {MAX_UPLOAD_FILES} files may be assigned at once")

    paths: list[str] = []
    for index, value in enumerate(values):
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"file path {index + 1} must be absolute")
        if len(os.fsencode(value)) > MAX_UPLOAD_PATH_BYTES:
            raise ValueError(f"file path {index + 1} is too long")
        try:
            info = path.stat()
            canonical = path.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            raise ValueError(f"file path {index + 1} does not exist") from None
        except OSError as exc:
            raise ValueError(
                f"file path {index + 1} could not be inspected: {exc.strerror or 'OS error'}"
            ) from None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"file path {index + 1} is not a regular file")
        if not os.access(canonical, os.R_OK):
            raise ValueError(f"file path {index + 1} is not readable")
        canonical_text = str(canonical)
        if len(os.fsencode(canonical_text)) > MAX_UPLOAD_PATH_BYTES:
            raise ValueError(f"canonical file path {index + 1} is too long")
        paths.append(canonical_text)
    return paths


def _upload_ipc_command(tabid: str, target: dict, paths: list[str]) -> str:
    """Encode a whitespace-safe v1 payload for the stable raw IPC command."""
    payload = json.dumps(
        {"version": UPLOAD_PAYLOAD_VERSION, "target": target, "paths": paths},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(payload) > MAX_UPLOAD_PAYLOAD_BYTES:
        raise ValueError("encoded upload request is too large")
    token = base64.b64encode(payload).decode("ascii")
    return f"upload-file {tabid} {token}"


# ─── State and tabs ─────────────────────────────────────────────


def cmd_protocol(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} protocol", parents=[_parent()],
                                description="Show IPC protocol metadata")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args(argv)
    _print_json(_json_response(args, "protocol"), pretty=args.pretty)


def cmd_version(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} version", parents=[_parent()],
                                description="Alias for 'protocol'")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args(argv)
    _print_json(_json_response(args, "version"), pretty=args.pretty)


def cmd_commands(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} commands", parents=[_parent()],
                                description="Show machine-readable IPC command metadata")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args(argv)
    _print_json(_json_response(args, "commands"), pretty=args.pretty)


def cmd_browser_help(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} browser-help", parents=[_parent()],
                                description="Show vimbrowser's raw IPC help text")
    args = p.parse_args(argv)
    print(_send(args, "help"), end="")


def cmd_status(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} status", parents=[_parent()],
                                description="Show active browser/tab state")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args(argv)
    _print_json(_status(args), pretty=args.pretty)


def cmd_tabs(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} tabs", parents=[_parent()],
                                description="List tabs in tab-stack order")
    p.add_argument("--json", action="store_true", help="Print raw JSON")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args(argv)
    payload = _tabs(args)
    if args.json or args.pretty:
        _print_json(payload, pretty=args.pretty)
        return
    rows = payload.get("tabs", [])
    if not rows:
        print("No tabs")
        return
    active = payload.get("active_tabid")
    for tab in rows:
        print(_format_tab_line(tab, active))


def cmd_current_tab(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} current-tab", parents=[_parent()],
                                description="Show the active tab")
    p.add_argument("--id-only", action="store_true", help="Print only the stable tab ID")
    p.add_argument("--json", action="store_true", help="Print active tab JSON")
    args = p.parse_args(argv)
    payload = _tabs(args)
    active = payload.get("active_tabid")
    if args.id_only:
        print(active or "")
        return
    tab = next((t for t in payload.get("tabs", []) if t.get("id") == active), None)
    if not tab:
        ipc.die("active tab not found")
    if args.json:
        _print_json(tab)
    else:
        print(_format_tab_line(tab, active))


def cmd_focus(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} focus", parents=[_parent()],
                                description="Focus a tab; not needed for AI use unless user explicitly asks")
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("--pretty", action="store_true", help="Pretty-print returned status JSON")
    args = p.parse_args(argv)
    response = _send(args, f"tab-focus {_resolve_tab(args, args.tab)}")
    if args.pretty:
        _print_json(json.loads(response), pretty=True)
    else:
        print(response, end="" if response.endswith("\n") else "\n")


def cmd_tab_order(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} tab-order", parents=[_parent()],
                                description="Move a tab to a zero-based tab-stack index")
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("index", help="Target zero-based index; browser clamps the value")
    args = p.parse_args(argv)
    print(_send(args, f"tab-order {_resolve_tab(args, args.tab)} {args.index}"), end="")


# ─── Tab navigation/control ─────────────────────────────────────


def cmd_open(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} open", parents=[_parent()],
                                description="Open a URL/search/local path in a new active tab")
    p.add_argument("target", nargs=argparse.REMAINDER,
                   help="URL, search query, or local path")
    args = p.parse_args(argv)
    target = _joined_tail(args.target, "target", p)
    print(_send(args, f"open-tab {target}"), end="")


def cmd_open_context(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog=f"{PROG} open-context",
        parents=[_parent()],
        description=(
            "Open a URL/search/local path in a named persistent browser context. "
            "Cookies and site storage are isolated from normal tabs and other contexts."
        ),
    )
    p.add_argument("context", help="Context name (lowercase letters, numbers, '_', and '-')")
    p.add_argument("target", nargs=argparse.REMAINDER,
                   help="URL, search query, or local path")
    args = p.parse_args(argv)
    target = _joined_tail(args.target, "target", p)
    print(_send(args, f"open-context-tab {args.context} {target}"), end="")


def cmd_load(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} load", parents=[_parent()],
                                description="Load a URL/search/local path into an existing tab")
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("target", nargs=argparse.REMAINDER,
                   help="URL, search query, or local path")
    args = p.parse_args(argv)
    target = _joined_tail(args.target, "target", p)
    print(_send(args, f"open {_resolve_tab(args, args.tab)} {target}"), end="")


def cmd_close_tab(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} close-tab", parents=[_parent()],
                                description="Close the active tab or a tab by stable ID")
    p.add_argument("tab", nargs="?", default=None,
                   help="Stable tab ID, @active, @first, or @last (default: active)")
    args = p.parse_args(argv)
    if args.tab:
        command = f"tab-close {_resolve_tab(args, args.tab)}"
    else:
        command = "tab-close"
    print(_send(args, command), end="")


def _optional_tab_command(argv: list[str], *, name: str, ipc_name: str,
                          description: str) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} {name}", parents=[_parent()],
                                description=description)
    p.add_argument("tab", nargs="?", default="@active",
                   help="Stable tab ID, @active, @first, or @last (default: active)")
    args = p.parse_args(argv)
    tab = _resolve_tab(args, args.tab)
    print(_send(args, f"{ipc_name} {tab}"), end="")


def cmd_reload(argv: list[str]) -> None:
    _optional_tab_command(argv, name="reload", ipc_name="reload",
                          description="Reload the active tab or a tab by stable ID")


def cmd_reload_ignore_cache(argv: list[str]) -> None:
    _optional_tab_command(argv, name="reload-ignore-cache", ipc_name="reload-ignore-cache",
                          description="Hard-reload the active tab or a tab by stable ID")


def cmd_back(argv: list[str]) -> None:
    _optional_tab_command(argv, name="back", ipc_name="back",
                          description="Navigate back in the active tab or a tab by stable ID")


def cmd_forward(argv: list[str]) -> None:
    _optional_tab_command(argv, name="forward", ipc_name="forward",
                          description="Navigate forward in the active tab or a tab by stable ID")


def cmd_stop(argv: list[str]) -> None:
    _optional_tab_command(argv, name="stop", ipc_name="stop",
                          description="Stop loading in the active tab or a tab by stable ID")


def cmd_zoom(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} zoom", parents=[_parent()],
                                description="Zoom a tab in/out/reset/to a CEF numeric level")
    p.add_argument("parts", nargs=argparse.REMAINDER,
                   help="[tabid|@active|@first|@last] <in|out|reset|level>")
    args = p.parse_args(argv)
    parts = list(args.parts)
    if not parts:
        p.error("missing zoom action/level")
    tab = "@active"
    if len(parts) >= 2 and _looks_like_tab_spec(parts[0]):
        tab = parts.pop(0)
    value = _joined_tail(parts, "zoom action/level", p)
    print(_send(args, f"zoom {_resolve_tab(args, tab)} {value}"), end="")


def cmd_scroll(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} scroll", parents=[_parent()],
                                description="Scroll the active page by pixels")
    p.add_argument("dy", help="Vertical delta in pixels")
    p.add_argument("count", nargs="?", help="Optional repeat count")
    args = p.parse_args(argv)
    command = f"scroll {args.dy}" + (f" {args.count}" if args.count else "")
    print(_send(args, command), end="")


def cmd_scroll_tab(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} scroll-tab", parents=[_parent()],
                                description="Scroll a tab by stable ID")
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("dy", help="Vertical delta in pixels")
    p.add_argument("count", nargs="?", help="Optional repeat count")
    args = p.parse_args(argv)
    command = f"scroll-tab {_resolve_tab(args, args.tab)} {args.dy}"
    if args.count:
        command += f" {args.count}"
    print(_send(args, command), end="")


def cmd_url(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} url", parents=[_parent()],
                                description="Print the active tab URL")
    args = p.parse_args(argv)
    print(_send(args, "url"), end="")


def cmd_fps(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} fps", parents=[_parent()],
                                description="Print active-tab FPS sample")
    args = p.parse_args(argv)
    print(_send(args, "fps"), end="")


def cmd_refresh(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} refresh", parents=[_parent()],
                                description="Print active-browser refresh rate")
    args = p.parse_args(argv)
    print(_send(args, "refresh"), end="")


# ─── Page/debug commands ────────────────────────────────────────


def _optional_tab_and_tail(args, parser, tail_name: str) -> tuple[str, str]:
    parts = list(args.parts)
    if not parts:
        parser.error(f"missing {tail_name}")
    tab_spec = "@active"
    if len(parts) >= 2 and _looks_like_tab_spec(parts[0]):
        tab_spec = parts.pop(0)
    return _resolve_tab(args, tab_spec), _joined_tail(parts, tail_name, parser)


def cmd_js(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} js", parents=[_parent()],
                                description="Evaluate exact JavaScript from stdin in a tab",
                                epilog="JavaScript is required on stdin; inline payload is not accepted.")
    p.add_argument("tab", nargs="?", default="@active",
                   help="Stable tab ID, @active, @first, or @last (default: active)")
    args, extras = p.parse_known_args(argv)
    if not _looks_like_tab_spec(args.tab):
        extras.insert(0, args.tab)
    _reject_inline_payload(p, extras, "JavaScript")
    tab = _resolve_tab(args, args.tab)
    print(_send(args, f"js-base64 {tab} {_encoded_javascript(p)}"), end="")


def cmd_js_file(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} js-file", parents=[_parent()],
                                description="Evaluate JavaScript from a local file in a tab")
    p.add_argument("parts", nargs=argparse.REMAINDER,
                   help="[tabid|@active|@first|@last] path")
    args = p.parse_args(argv)
    tab, path = _optional_tab_and_tail(args, p, "path")
    print(_send(args, f"js-file {tab} {path}"), end="")


def _single_optional_tab_command(argv: list[str], *, name: str, ipc_name: str,
                                 description: str) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} {name}", parents=[_parent()],
                                description=description)
    p.add_argument("tab", nargs="?", default="@active",
                   help="Stable tab ID, @active, @first, or @last (default: active)")
    args = p.parse_args(argv)
    print(_send(args, f"{ipc_name} {_resolve_tab(args, args.tab)}"), end="")


def cmd_html(argv: list[str]) -> None:
    _single_optional_tab_command(argv, name="html", ipc_name="html",
                                 description="Dump current full document HTML for a tab")


def cmd_text(argv: list[str]) -> None:
    _single_optional_tab_command(argv, name="text", ipc_name="text",
                                 description="Dump current document text for a tab")


def cmd_screenshot(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} screenshot", parents=[_parent()],
                                description="Capture a tab screenshot as PNG")
    p.add_argument("tab", nargs="?", default="@active",
                   help="Stable tab ID, @active, @first, or @last (default: active)")
    p.add_argument("-o", "--output", help="Write PNG to this path")
    p.add_argument("--json", action="store_true", help="Print raw screenshot JSON instead of decoding")
    args = p.parse_args(argv)

    response = _send(args, f"screenshot {_resolve_tab(args, args.tab)}")
    if args.json:
        print(response, end="" if response.endswith("\n") else "\n")
        return

    try:
        payload = json.loads(response)
        image = base64.b64decode(payload["data"], validate=True)
    except Exception as exc:  # noqa: BLE001 - surface malformed protocol response.
        ipc.die(f"invalid screenshot response: {exc}\n{response[:4096]}")

    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(image)
        meta = {key: value for key, value in payload.items() if key != "data"}
        meta["path"] = str(out)
        meta["bytes"] = len(image)
        _print_json(meta)
        return

    if sys.stdout.isatty():
        tabid = payload.get("tabid", "tab")
        out = Path(tempfile.gettempdir()) / f"vimbrowser-screenshot-{tabid}.png"
        out.write_bytes(image)
        print(out)
        return

    sys.stdout.buffer.write(image)


def cmd_frame_tree(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog=f"{PROG} frame-tree", parents=[_parent()],
        description="List the current exact main/child frame tree for a tab",
    )
    p.add_argument("tab", nargs="?", default="@active",
                   help="Stable tab ID, @active, @first, or @last")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args(argv)
    payload = _json_response(args, f"frame-tree {_resolve_tab(args, args.tab)}")
    _print_json(payload, pretty=args.pretty)


def _frame_document_command(argv: list[str], *, name: str, ipc_name: str,
                            description: str) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} {name}", parents=[_parent()],
                                description=description)
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("frame", help="Opaque frame ID returned by frame-tree")
    args = p.parse_args(argv)
    print(_send(args, f"{ipc_name} {_resolve_tab(args, args.tab)} {args.frame}"), end="")


def cmd_frame_html(argv: list[str]) -> None:
    _frame_document_command(argv, name="frame-html", ipc_name="frame-html",
                            description="Dump HTML from one exact current frame")


def cmd_frame_text(argv: list[str]) -> None:
    _frame_document_command(argv, name="frame-text", ipc_name="frame-text",
                            description="Dump text from one exact current frame")


def cmd_frame_js(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog=f"{PROG} frame-js", parents=[_parent()],
        description="Evaluate exact JavaScript from stdin in one current frame",
        epilog="JavaScript is required on stdin; inline payload is not accepted.",
    )
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("frame", help="Opaque frame ID returned by frame-tree")
    args, extras = p.parse_known_args(argv)
    _reject_inline_payload(p, extras, "JavaScript")
    print(_send(
        args,
        f"frame-js-base64 {_resolve_tab(args, args.tab)} {args.frame} {_encoded_javascript(p)}",
    ), end="")


def cmd_inspect_controls(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog=f"{PROG} inspect-controls", parents=[_parent()],
        description=(
            "Inspect clickable controls in one exact frame without activating them, "
            "and mint short-lived exact-node handles"
        ),
    )
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("--frame", required=True,
                   help="Opaque frame ID returned by frame-tree")
    p.add_argument("--role", default="", help="Exact computed accessibility role")
    p.add_argument("--name-exact", default="", help="Exact computed accessible name")
    p.add_argument("--context-contains", default="",
                   help="Required case-sensitive text in bounded surrounding context")
    p.add_argument("--limit", type=int, default=100,
                   help="Maximum controls to return (1-100; default 100)")
    p.add_argument("--require-one", action="store_true",
                   help="Fail unless inspection returns exactly one control")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args(argv)
    if not 1 <= args.limit <= 100:
        p.error("--limit must be between 1 and 100")
    for label, value, maximum in (
        ("role", args.role, 128),
        ("name", args.name_exact, 256),
        ("context", args.context_contains, 512),
        ("frame", args.frame, 256),
    ):
        if len(value.encode("utf-8")) > maximum:
            p.error(f"{label} is too long")
    query = {
        "version": 1,
        "frame_id": args.frame,
        "filter": {
            "role": args.role,
            "exact_name": args.name_exact,
            "context_contains": args.context_contains,
        },
        "limit": args.limit,
    }
    token = base64.b64encode(
        json.dumps(query, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    payload = _json_response(
        args,
        f"inspect-controls {_resolve_tab(args, args.tab)} {token}",
        label="inspect-controls",
    )
    if payload.get("ok") is not True:
        _print_json(payload, pretty=args.pretty)
        raise SystemExit(1)
    inspection = payload.get("inspection", {})
    count = inspection.get("match_count")
    truncated = inspection.get("truncated") is True
    if args.require_one and (count != 1 or truncated):
        payload["ok"] = False
        payload["error"] = {
            "code": (
                "target_not_found"
                if count == 0 and not truncated
                else "ambiguous_target"
            ),
            "message": (
                "inspection found no matching controls"
                if count == 0 and not truncated
                else "inspection found more than one matching control"
            ),
            "match_count": count,
            "truncated": truncated,
        }
        _print_json(payload, pretty=args.pretty)
        raise SystemExit(1)
    _print_json(payload, pretty=args.pretty)


def cmd_activate_control(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog=f"{PROG} activate-control", parents=[_parent()],
        description=(
            "Trusted-activate one exact short-lived control handle returned by "
            "inspect-controls"
        ),
        epilog=(
            "The browser consumes HANDLE once, revalidates its frame, document, "
            "node, visibility, enabled state, and compositor hit target, then grants "
            "transient user activation only for that native click."
        ),
    )
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("handle", help="Exact eh1_ handle returned by inspect-controls")
    p.add_argument("--pretty", action="store_true", help="Pretty-print response JSON")
    args = p.parse_args(argv)
    if (not args.handle.startswith("eh1_") or
            len(args.handle.encode("utf-8")) > MAX_HANDLE_BYTES or
            any(character.isspace() for character in args.handle)):
        p.error("handle is malformed")
    payload = _json_response(
        args,
        f"activate-control {_resolve_tab(args, args.tab)} {args.handle}",
        label="activate-control",
    )
    _print_json(payload, pretty=args.pretty)
    if payload.get("ok") is not True:
        raise SystemExit(1)


def cmd_upload_file(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog=f"{PROG} upload-file",
        parents=[_parent()],
        description=(
            "Assign approved local files to one unambiguous page <input type=file>, "
            "or atomically activate a chooser control through the browser process"
        ),
        epilog=(
            "TARGET is a CSS selector (optionally prefixed css:) and must match "
            "exactly one element, or index:N for the explicit zero-based Nth "
            "input[type=file] in the main document. Use activate:SELECTOR to "
            "atomically native-activate one visible chooser control and supply "
            "the picker it opens. Use handle:HANDLE after inspect-controls for "
            "an exact control in any frame. Use chooser to arm the next native open-file "
            "chooser from the tab for 60 seconds."
        ),
    )
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("target",
                   help=("Unique CSS selector, css:SELECTOR, index:N, "
                         "activate:SELECTOR, handle:HANDLE, or chooser"))
    p.add_argument("paths", nargs="+", metavar="ABSOLUTE_PATH",
                   help="Absolute existing regular file path(s)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print response JSON")
    args = p.parse_args(argv)

    try:
        target = _parse_upload_target(args.target)
    except ValueError as exc:
        _upload_error("invalid_target", str(exc))
    try:
        paths = _validated_upload_paths(args.paths)
    except ValueError as exc:
        _upload_error("invalid_path", str(exc))

    tabid = _resolve_tab(args, args.tab)
    try:
        command = _upload_ipc_command(tabid, target, paths)
    except ValueError as exc:
        _upload_error("request_too_large", str(exc))

    payload = _json_response(args, command, label="upload-file")
    _print_json(payload, pretty=args.pretty)
    if payload.get("ok") is not True:
        raise SystemExit(1)


def _upload_file_state_command(argv: list[str], *, name: str,
                               ipc_name: str, description: str) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} {name}", parents=[_parent()],
                                description=description)
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("--pretty", action="store_true", help="Pretty-print response JSON")
    args = p.parse_args(argv)
    tabid = _resolve_tab(args, args.tab)
    payload = _json_response(args, f"{ipc_name} {tabid}", label=ipc_name)
    _print_json(payload, pretty=args.pretty)
    if payload.get("ok") is not True:
        raise SystemExit(1)


def cmd_upload_file_status(argv: list[str]) -> None:
    _upload_file_state_command(
        argv,
        name="upload-file-status",
        ipc_name="upload-file-status",
        description="Show the state of a chooser-target upload for a tab",
    )


def cmd_upload_file_cancel(argv: list[str]) -> None:
    _upload_file_state_command(
        argv,
        name="upload-file-cancel",
        ipc_name="upload-file-cancel",
        description="Cancel an armed chooser-target upload for a tab",
    )


# ─── Toggles and passthroughs ───────────────────────────────────


def cmd_shader(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} shader", parents=[_parent()],
                                description="Toggle or set the native page shader")
    p.add_argument("setting", nargs="?", choices=["on", "off"],
                   help="Set shader on/off; omit to toggle")
    args = p.parse_args(argv)
    command = "shader" if args.setting is None else f"shader {args.setting}"
    print(_send(args, command), end="")


def cmd_showfps(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} showfps", parents=[_parent()],
                                description="Toggle or set the FPS overlay")
    p.add_argument("setting", nargs="?", choices=["on", "off"],
                   help="Set overlay on/off; omit to toggle")
    args = p.parse_args(argv)
    command = "showfps" if args.setting is None else f"showfps {args.setting}"
    print(_send(args, command), end="")


def cmd_network(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} network", parents=[_parent()],
                                description="Pass through network debugging IPC commands")
    p.add_argument("parts", nargs=argparse.REMAINDER,
                   help="<tabid|@active|@first|@last> list|detail|body|replay|clear ...")
    args = p.parse_args(argv)
    if not args.parts:
        p.error("missing network arguments")
    parts = list(args.parts)
    if _looks_like_tab_spec(parts[0]):
        parts[0] = _resolve_tab(args, parts[0])
    print(_send(args, "network " + " ".join(parts)), end="")


def cmd_cookies(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} cookies", parents=[_parent()],
                                description="List cookies visible to a tab URL or explicit URL")
    p.add_argument("parts", nargs="*",
                   help="[tabid|@active|@first|@last] [url]")
    p.add_argument("--url", help="Atomically list profile cookies visible to this URL without requiring a tab")
    args = p.parse_args(argv)

    tab_spec = None
    url = args.url
    parts = list(args.parts)
    if parts and _looks_like_tab_spec(parts[0]):
        tab_spec = parts.pop(0)

    if parts:
        if url is not None:
            p.error("unexpected positional URL when --url is already set")
        url = " ".join(parts)

    if url and tab_spec is None:
        print(_send(args, f"cookies-url {url}"), end="")
        return

    command = f"cookies {_resolve_tab(args, tab_spec or '@active')}"
    if url:
        command += f" {url}"
    print(_send(args, command), end="")


def cmd_cookie_delete(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} cookie-delete", parents=[_parent()],
                                description="Delete a cookie by name for a tab URL")
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("name", help="Cookie name")
    args = p.parse_args(argv)
    print(_send(args, f"cookie-delete {_resolve_tab(args, args.tab)} {args.name}"), end="")


def cmd_cookie_set(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} cookie-set", parents=[_parent()],
                                description="Set a cookie for a tab URL")
    p.add_argument("tab", help="Stable tab ID, @active, @first, or @last")
    p.add_argument("name", help="Cookie name")
    p.add_argument("value", help="Cookie value")
    p.add_argument("domain", nargs="?", help="Optional cookie domain")
    p.add_argument("path", nargs="?", help="Optional cookie path")
    args = p.parse_args(argv)
    command = f"cookie-set {_resolve_tab(args, args.tab)} {args.name} {args.value}"
    if args.domain is not None:
        command += f" {args.domain}"
    if args.path is not None:
        command += f" {args.path}"
    print(_send(args, command), end="")


def cmd_raw(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog=f"{PROG} raw", parents=[_parent()],
                                description="Send one exact raw IPC command line from stdin",
                                epilog="The command is required on stdin; inline payload is not accepted.")
    args, extras = p.parse_known_args(argv)
    _reject_inline_payload(p, extras, "raw command")
    command = _stdin_payload(p, "raw command", one_line=True)
    print(_send(args, command), end="")
