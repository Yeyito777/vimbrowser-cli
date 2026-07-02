"""Command implementations for vimbrowser-cli."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

from src import ipc

PROG = "vimbrowser-cli"


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


def _json_response(args, command: str) -> dict:
    response = _send(args, command)
    try:
        value = json.loads(response)
    except json.JSONDecodeError as exc:
        ipc.die(f"invalid JSON response for {command!r}: {exc}\n{response[:4096]}")
    if not isinstance(value, dict):
        ipc.die(f"unexpected non-object JSON response for {command!r}")
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
                                description="Evaluate JavaScript in a tab")
    p.add_argument("parts", nargs=argparse.REMAINDER,
                   help="[tabid|@active|@first|@last] JavaScript")
    args = p.parse_args(argv)
    tab, script = _optional_tab_and_tail(args, p, "JavaScript")
    print(_send(args, f"js {tab} {script}"), end="")


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
    _single_optional_tab_command(argv, name="cookies", ipc_name="cookies",
                                 description="List cookies visible to a tab URL")


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
                                description="Send a raw vimbrowser IPC command line")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="Raw command line to send")
    args = p.parse_args(argv)
    command = _joined_tail(args.command, "raw command", p)
    print(_send(args, command), end="")
