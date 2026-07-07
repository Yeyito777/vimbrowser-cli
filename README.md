# vimbrowser-cli

External Exocortex tool for controlling vimbrowser through its native Unix-domain IPC socket.

This tool follows `TOOL_STANDARD.md` and mirrors the `qutebrowser-cli` layout:

```text
vimbrowser-cli/
  manifest.json
  bin/vimbrowser-cli
  src/
    ipc.py
    commands.py
  config/.gitkeep
```

No third-party Python dependencies are required; it uses the system `python3` unless a local `.venv/bin/python3` exists.

## Socket resolution

Commands connect to the first available socket in this order:

1. `--socket PATH`
2. `--profile-dir DIR` → `DIR/ipc.sock`
3. `VIMBROWSER_IPC=PATH`
4. `VIMBROWSER_PROFILE_DIR=DIR` → `DIR/ipc.sock`
5. `~/.runtime/vimbrowser-yeyito/ipc.sock`
6. `$XDG_STATE_HOME/vimbrowser/ipc.sock`
7. `~/.local/state/vimbrowser/ipc.sock`
8. `/tmp/vimbrowser/ipc.sock`

## Commands

```bash
vimbrowser-cli status --pretty
vimbrowser-cli protocol --pretty
vimbrowser-cli commands --pretty
vimbrowser-cli browser-help
vimbrowser-cli tabs
vimbrowser-cli current-tab --id-only
vimbrowser-cli focus 3
vimbrowser-cli tab-order @active 0
vimbrowser-cli open https://example.com
vimbrowser-cli load @active https://example.com
vimbrowser-cli close-tab @last
vimbrowser-cli reload @active
vimbrowser-cli back @active
vimbrowser-cli forward @active
vimbrowser-cli stop @active
vimbrowser-cli zoom @active reset
vimbrowser-cli scroll 600
vimbrowser-cli url
vimbrowser-cli fps
vimbrowser-cli refresh
vimbrowser-cli js @active 'document.title'
vimbrowser-cli js-file @active /tmp/script.js
vimbrowser-cli html @active
vimbrowser-cli text @active
vimbrowser-cli screenshot @active -o /tmp/tab.png
vimbrowser-cli shader on
vimbrowser-cli showfps off
vimbrowser-cli cookies @active
vimbrowser-cli cookies --url https://www.google.com/
vimbrowser-cli cookie-delete @active session
vimbrowser-cli cookie-set @active debug true
vimbrowser-cli network @active list
vimbrowser-cli raw status
```

`vimbrowser-cli cookies --url URL` uses the browser's profile-level
`cookies-url` IPC command and does not require a matching tab to exist.

Tab arguments accept stable vimbrowser tab IDs plus these convenience aliases:

- `@active`
- `@first`
- `@last`

`js` and `raw` are declared in `manifest.json` as literal-tail commands so Exocortex's bash harness can pass JavaScript/raw IPC text without manual shell escaping.

## Validation

Typical smoke test:

```bash
python3 -m py_compile src/*.py
python3 -m json.tool manifest.json >/dev/null
bin/vimbrowser-cli -h
bin/vimbrowser-cli status --pretty
bin/vimbrowser-cli tabs --json | python3 -m json.tool >/dev/null
bin/vimbrowser-cli js @active 'document.location.href'
bin/vimbrowser-cli screenshot @active -o /tmp/vimbrowser-cli-test.png
```

## License

MIT. See [`LICENSE`](LICENSE).
