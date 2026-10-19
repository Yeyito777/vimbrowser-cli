import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.session import recovered_state, recover

class SessionTests(unittest.TestCase):
    def snapshot(self):
        return {'active_tabid': 581, 'tabs': [
            {'id': 1, 'url': 'http://fixture/owner', 'context': None, 'folder_id': 7, 'pinned': True, 'sidebar_sort_order': 99},
            {'id': 581, 'url': 'http://fixture/mail?text=back\\slash\tnewline\n', 'context': 'fixture-mail', 'folder_id': 7, 'sidebar_sort_order': 101, 'pinned': False}]}
    def test_exact_metadata_and_new_counter(self):
        content, result = recovered_state(self.snapshot(), 'folder=7\t0\t11\tFixture\nfolder_pinned=7\nnext_tab_id=700\nshader=on\nopen_history=safe\ntab=old\ntab_id=9\n')
        self.assertIn('tab_id=1\ntab_folder=7\ntab_order=99\ntab_pinned=on', content)
        self.assertIn('context_tab=fixture-mail\t581\t7\t101\toff\t', content)
        self.assertIn('back\\\\slash\\tnewline\\n', content)
        self.assertIn('active=1\nnext_tab_id=700', content)
        self.assertIn('folder_pinned=7', content)
        self.assertNotIn('tab=old', content)
        self.assertEqual(result['tabs'], 2)
    def test_duplicate_or_missing_folder_or_bad_context_rejected(self):
        for mutate in (lambda t: t.update(id=1), lambda t: t.update(folder_id=9), lambda t: t.update(context='../escape')):
            snapshot=self.snapshot();mutate(snapshot['tabs'][1])
            with self.assertRaises(ValueError):
                recovered_state(snapshot, 'folder=7\t0\t11\tFixture\n')
    def test_never_overwrites_source_or_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);snapshot=root/'snapshot';source=root/'state';output=root/'candidate'
            snapshot.write_text(json.dumps(self.snapshot()));source.write_text('folder=7\t0\t11\tFixture\n')
            with self.assertRaises(ValueError): recover(snapshot, source, source)
            recover(snapshot, source, output)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): recover(snapshot, source, output)
            self.assertEqual(source.read_text(), 'folder=7\t0\t11\tFixture\n')

if __name__=='__main__': unittest.main()
