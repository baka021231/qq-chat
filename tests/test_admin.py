import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.admin import whitelist
from qq_chat import prompts


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'
        self.config = {'allowed_groups': [100], 'private_whitelist': [],
                       'allow_all_groups': True, 'owner_qq': '1',
                       'onebot_token': 'test-secret', 'group_user_blacklist': ['9']}
        self.path.write_text(json.dumps(self.config))

    def test_add_preserves_other_settings_and_is_idempotent(self):
        result = whitelist(self.path, 'group', 'add', '200')
        self.assertTrue(result['restart_required'])
        self.assertTrue(result['allow_all_groups'])
        self.assertNotIn('test-secret', json.dumps(result))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved.pop('allowed_groups'), ['100', '200'])
        expected = dict(self.config)
        expected.pop('allowed_groups')
        self.assertEqual(saved, expected)
        before = self.path.read_bytes()
        self.assertFalse(whitelist(self.path, 'group', 'add', 200)['changed'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_private_add_list_remove_isolated_from_groups(self):
        whitelist(self.path, 'private', 'add', '00200')
        self.assertEqual(whitelist(self.path, 'private', 'list')['entries'], ['200'])
        self.assertEqual(whitelist(self.path, 'group', 'list')['entries'], ['100'])
        self.assertTrue(whitelist(self.path, 'private', 'remove', 200)['changed'])
        self.assertFalse(whitelist(self.path, 'private', 'remove', 200)['changed'])

    def test_invalid_input_and_failed_write_leave_config_intact(self):
        before = self.path.read_bytes()
        for ident in ('0', '-1', 'abc', '１２', None):
            with self.assertRaises(ValueError):
                whitelist(self.path, 'group', 'add', ident)
        with patch('qq_chat.admin.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                whitelist(self.path, 'group', 'add', 200)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.config-*')), [])

    def test_registry_only_supplied_to_owner(self):
        self.assertIn('tools/qq_admin.py', prompts.instructions(True))
        self.assertNotIn('tools/qq_admin.py', prompts.instructions(False))
        self.assertNotIn('tools/qq_admin.py', prompts.instructions(False, group_moderation=False))
