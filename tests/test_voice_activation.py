import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools.activate_voice import disable_voice, reply_finished


class ActivationTests(unittest.TestCase):
    def test_restart_gate_requires_current_conversation_and_committed_reply(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE history (role TEXT, content TEXT, conversation TEXT)')
            def insert(role, content, conversation='group:1:owner'):
                db.execute('INSERT INTO history VALUES (?, ?, ?)', (role, content, conversation))
            insert('user', 'install voice')
            insert('assistant', 'old result')
            baseline = 2
            self.assertFalse(reply_finished(db, 'group:1:owner', baseline, 'install voice'))
            insert('user', 'install voice', 'group:2:owner')
            insert('assistant', 'other group', 'group:2:owner')
            self.assertFalse(reply_finished(db, 'group:1:owner', baseline, 'install voice'))
            insert('user', 'install voice')
            self.assertFalse(reply_finished(db, 'group:1:owner', baseline, 'install voice'))
            insert('assistant', 'finished')
            self.assertTrue(reply_finished(db, 'group:1:owner', baseline, 'install voice'))

    def test_rollback_disables_only_voice_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = {'owner_qq': 123, 'group_user_blacklist': [789],
                        'voice': {'enabled': True, 'command': ['unused']}, 'unrelated': 'preserve'}
            (base / 'config.json').write_text(json.dumps(original))
            with patch('tools.activate_voice.BASE', base):
                disable_voice()
            result = json.loads((base / 'config.json').read_text())
            original['voice']['enabled'] = False
            self.assertEqual(result, original)
            self.assertEqual((base / 'config.json').stat().st_mode & 0o777, 0o600)
