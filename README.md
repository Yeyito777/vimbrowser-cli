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

## Bash tool timeout units

When invoking this CLI through Exocortex's `bash` tool, its `timeout` field is
in **milliseconds**. Use `30000` for a 30-second outer timeout, not `30`.
Python-backed commands normally take tens of milliseconds just to start, so a
value such as `30` can terminate an otherwise successful invocation after it
has already received and printed its IPC response. This produces a misleading
message such as `command timed out after 0.1s` followed by `SIGTERM`.

Wrapping the command in coreutils `timeout 5s ...` does not override a shorter
outer `bash` tool timeout.

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
vimbrowser-cli open-context discord-paramount https://discord.com/login
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
printf '%s' 'document.title' | vimbrowser-cli js @active
vimbrowser-cli js-file @active /tmp/script.js
vimbrowser-cli html @active
vimbrowser-cli text @active
vimbrowser-cli screenshot @active -o /tmp/tab.png
vimbrowser-cli frame-tree @active --pretty
vimbrowser-cli frame-text @active FRAME_ID
printf '%s' 'document.title' | vimbrowser-cli frame-js @active FRAME_ID
vimbrowser-cli inspect-controls @active --frame FRAME_ID --role button --name-exact Browse --require-one --pretty
vimbrowser-cli upload-file @active '#attachment' /home/me/report.pdf
vimbrowser-cli upload-file @active index:1 /tmp/front.png /tmp/back.png
vimbrowser-cli upload-file @active 'activate:#browse-button' /home/me/resume.pdf
vimbrowser-cli upload-file @active 'handle:eh1_INSPECTED_TARGET' /home/me/resume.pdf
vimbrowser-cli upload-file @active chooser /home/me/resume.pdf
vimbrowser-cli upload-file-status @active --pretty
vimbrowser-cli upload-file-cancel @active
vimbrowser-cli shader on
vimbrowser-cli showfps off
vimbrowser-cli cookies @active
vimbrowser-cli cookies --url https://www.google.com/
vimbrowser-cli cookie-delete @active session
vimbrowser-cli cookie-set @active debug true
vimbrowser-cli network @active list
printf '%s' 'status' | vimbrowser-cli raw
```

`vimbrowser-cli cookies --url URL` uses the browser's profile-level
`cookies-url` IPC command and does not require a matching tab to exist.

Tab arguments accept stable vimbrowser tab IDs plus these convenience aliases:

- `@active`
- `@first`
- `@last`

`js`, `frame-js`, and `raw` accept their primary opaque payload only on stdin.
JavaScript is strict UTF-8, preserved byte-for-byte, and transported to the
browser as base64 so IPC framing and whitespace tokenization cannot alter it.
`raw` accepts one exact UTF-8 IPC command line without CR, LF, or NUL. Inline
payload arguments are rejected before connecting to the browser.

### Secure local-file upload

`upload-file TAB TARGET ABSOLUTE_PATH [ABSOLUTE_PATH ...]` assigns explicit local
files to a page `<input type=file>`, or atomically native-activates one custom
Browse control and supplies the chooser it opens. It does **not** inject file
contents into JavaScript or ask page script to open arbitrary paths.

Targets are deliberately strict:

- a bare target or `css:SELECTOR` is a CSS selector in the main document and
  must match exactly one element; zero or multiple matches are errors
- `index:N` explicitly selects the zero-based Nth `input[type=file]`; use this
  only when the input ordering is a stable part of the controlled page
- `activate:SELECTOR` asks the customized Chromium backend to resolve one visible
  element, activate it through Blink's trusted mouse input path, and supply the
  open-file chooser it causes; this is one synchronous IPC command and no native
  file-picker window is shown
- `handle:HANDLE` consumes one short-lived exact-node capability returned by
  `inspect-controls`; use this for cross-origin frame/OOPIF controls
- `chooser` arms the next browser-native open-file request from that stable tab
  for 60 seconds; after it reports `armed`, the user must click the intended
  upload control
- the resolved element must actually be `<input type=file>`

Every path must be absolute, existing, readable, and a regular file. Both the
CLI and browser validate paths, and the browser canonicalizes them before the
CEF call. The browser rejects multiple paths for an input without `multiple`
and checks `accept` extensions/MIME types where they can be inferred from file
extensions. Responses are structured JSON and contain counts/constraint
metadata, never file contents or local path strings. Example:

```json
{"ok":true,"tabid":3,"file_count":1,"target":{"kind":"css","match_count":1},"input":{"multiple":false,"accept":"application/pdf"}}
```

For dynamic pages, wait until the intended control exists and prefer a
site-specific unique selector. The command never falls back to a different
input when a target is missing or ambiguous. This command requires a rebuilt
vimbrowser that advertises `upload-file` in `vimbrowser-cli commands`.

Prefer `activate:SELECTOR` for sites that expose only a button, create an input
ephemerally during a click, or use a File System Access picker. Chromium verifies
that native hit testing reaches the selected control, gives the click real
transient user activation, and carries a browser-generated nonce through the
resulting chooser. Only that causally matching chooser can receive the files; an
unrelated chooser from the same tab is canceled rather than consuming the arm.
The IPC reply is held until the picker is consumed or fails.
Zero/multiple/invisible/obscured targets and controls that do not open a chooser
return structured errors. The plain `chooser` target remains for human-directed
workflows: its arm is tab-bound, one-shot, and automatically expires, and the user
then clicks the intended control. Use `upload-file-status` to inspect it and
`upload-file-cancel` whenever an arm is no longer intended.

### Exact cross-origin frame controls

`frame-tree TAB` exposes the current primary frame hierarchy using opaque CEF
frame IDs. `frame-html`, `frame-text`, and `frame-js` address one exact frame, so
an agent can inspect an embedded cross-origin picker without violating the
page's same-origin boundary or guessing from a screenshot.

`inspect-controls` is read-only. It lists all matching native clickable
candidates in one frame and returns bounded role/name/text/context metadata plus
one random handle per exact DOM node. It never activates the first match.
`--require-one` turns zero/multiple matches into a structured nonzero result
while retaining the candidate list for diagnosis.

Handles expire after 15 seconds, are one-shot, and are bound inside Chromium to
the browser, frame, document, and original DOM node. `upload-file handle:...`
revalidates node identity, visibility, disabled state, local hit testing, and the
OOPIF compositor target before activating. Navigation, clone replacement,
coverage, replay, and cross-tab use fail closed. No coordinates, renderer DOM
IDs, process IDs, paths, or filenames are included in inspection responses.

`open-context NAME TARGET` creates a tab backed by a named persistent CEF request
context. Its cookies and site storage are isolated from ordinary tabs and from
other named contexts. This requires a vimbrowser build that advertises the
`open-context-tab` IPC command.

## Validation

Typical smoke test:

```bash
python3 -m py_compile src/*.py
python3 -m json.tool manifest.json >/dev/null
bin/vimbrowser-cli -h
bin/vimbrowser-cli status --pretty
bin/vimbrowser-cli tabs --json | python3 -m json.tool >/dev/null
printf '%s' 'document.location.href' | bin/vimbrowser-cli js @active
bin/vimbrowser-cli screenshot @active -o /tmp/vimbrowser-cli-test.png
```

The non-mutating automated tests use a temporary fake IPC socket and do not
connect to or change a running browser:

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT. See [`LICENSE`](LICENSE).
