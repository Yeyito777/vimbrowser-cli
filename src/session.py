"""Offline, exact-ID state recovery. Never connects to a browser or writes a live profile."""
import json
import os
from pathlib import Path
import re

MAX_ID = (1 << 64) - 1
CONTEXT = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}\Z')
TAB_KEYS = {'tab', 'tab_id', 'tab_folder', 'tab_order', 'tab_pinned', 'context_tab', 'active', 'next_tab_id'}


def escape(value):
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_ID:
        raise ValueError(f'invalid {label}')
    return value


def recovered_state(snapshot, source, active_tab_id=None):
    tabs = snapshot.get('tabs')
    if not isinstance(tabs, list) or not tabs:
        raise ValueError('snapshot must contain a nonempty tabs list')
    lines = source.splitlines()
    folders = {0}
    next_id = 1
    for line in lines:
        key, _, value = line.partition('=')
        if key == 'folder':
            folders.add(number(int(value.split('\t', 1)[0]), 'folder ID', 1))
        if key == 'next_tab_id':
            next_id = number(int(value), 'next tab ID')
    ids = set()
    records = []
    for tab in tabs:
        if not isinstance(tab, dict):
            raise ValueError('invalid tab record')
        ident = number(tab.get('id'), 'tab ID', 1)
        if ident in ids:
            raise ValueError(f'duplicate tab ID {ident}')
        ids.add(ident)
        folder = number(tab.get('folder_id', 0), 'tab folder')
        if folder not in folders:
            raise ValueError(f'tab {ident} references a folder missing from state source')
        order = number(tab.get('sidebar_sort_order', 0), 'tab order')
        pinned = tab.get('pinned', False)
        if not isinstance(pinned, bool):
            raise ValueError('invalid pinned flag')
        context = tab.get('context')
        if context is None:
            context = ''  # Native tabs JSON uses null for the global context.
        if not isinstance(context, str) or (context and not CONTEXT.fullmatch(context)):
            raise ValueError(f'invalid context on tab {ident}; refusing default-context fallback')
        url = tab.get('url')
        if not isinstance(url, str) or not url or '\0' in url:
            raise ValueError(f'invalid URL on tab {ident}')
        if context:
            records.append(f'context_tab={context}\t{ident}\t{folder}\t{order}\t{"on" if pinned else "off"}\t{escape(url)}')
        else:
            records.extend([f'tab={escape(url)}', f'tab_id={ident}', f'tab_folder={folder}',
                            f'tab_order={order}', f'tab_pinned={"on" if pinned else "off"}'])
    active = active_tab_id if active_tab_id is not None else snapshot.get('active_tabid')
    if active not in ids:
        raise ValueError('active tab ID missing from snapshot; specify --active-tab-id')
    active_index = next(i for i, tab in enumerate(tabs) if tab['id'] == active)
    if next_id != 0:
        next_id = 0 if max(ids) == MAX_ID else max(next_id, max(ids) + 1)
    preserved = [line for line in lines if line.partition('=')[0] not in TAB_KEYS]
    result = '\n'.join(preserved + [f'active={active_index}', f'next_tab_id={next_id}'] + records) + '\n'
    return result, {'tabs': len(tabs), 'named_context_tabs': sum(bool(t.get('context')) for t in tabs),
                    'active_tabid': active, 'next_tab_id': next_id}


def recover(snapshot_path, state_source, output_path, active_tab_id=None):
    snapshot_path, state_source, output = map(lambda p: Path(p).expanduser().resolve(),
                                            (snapshot_path, state_source, output_path))
    if output in (snapshot_path, state_source):
        raise ValueError('output must be a separate new file, never the state source/snapshot')
    snapshot = json.loads(snapshot_path.read_text())
    content, summary = recovered_state(snapshot, state_source.read_text(), active_tab_id)
    # Exclusive creation protects both live profiles and previous recovery artifacts.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return {**summary, 'output': str(output), 'live_browser_modified': False,
            'requires_context_aware_browser': True,
            'next_step': 'parent: stop browser, back up final state, install candidate state, relaunch fixed runtime'}
