"""Unseen group reference delivery, per-session cursors and retry/reset boundaries."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.bridge import Bridge, Source
from tests.test_agents import FakeAgent


class DeltaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch('qq_chat.bridge.BASE', self.root);p.start();self.addCleanup(p.stop)
        self.bridge = Bridge({'owner_qq':10001,'codex_path':'unused'})
        self.addCleanup(lambda:self.bridge.db.close())
        self.agent = FakeAgent();self.bridge.agents['codex']=self.agent
        self.owner=Source('group',20001,10001)
        self.guest=Source('group',20001,30001)
        async def send(*args):pass
        self.bridge.send=send

    def note(self, text, source=None):
        return self.bridge.remember_group(source or self.guest,text)

    async def turn(self, text):
        ident=self.note(text,self.owner)
        await self.bridge.chat(self.owner,text,ident)
        return self.agent.requests[-1].text

    async def test_only_new_messages_after_success_and_no_current_turn_duplication(self):
        self.note('old-group-message')
        self.assertIn('old-group-message',await self.turn('trigger-one'))
        self.note('new-group-message')
        second=await self.turn('trigger-two')
        self.assertIn('new-group-message',second)
        self.assertNotIn('old-group-message',second)
        self.assertNotIn('trigger-one',second)
        self.assertEqual(second.count('trigger-two'),1)
        self.assertEqual(await self.turn('trigger-three'),'trigger-three')

    async def test_failed_turn_does_not_consume_unseen_context(self):
        self.note('must-survive-retry')
        self.agent.error=RuntimeError('backend failed')
        ident=self.note('failed-trigger',self.owner)
        with self.assertRaises(RuntimeError):
            await self.bridge.chat(self.owner,'failed-trigger',ident)
        self.agent.error=None
        self.assertIn('must-survive-retry',await self.turn('retry-trigger'))

    async def test_messages_arriving_while_model_runs_are_delivered_next_time(self):
        self.note('before-generation')
        ident=self.note('current-trigger',self.owner)
        self.agent.release.clear()
        task=asyncio.create_task(self.bridge.chat(self.owner,'current-trigger',ident))
        await asyncio.wait_for(self.agent.entered.wait(),1)
        self.note('arrived-during-generation')
        self.agent.release.set();await task
        text=await self.turn('next-trigger')
        self.assertIn('arrived-during-generation',text)
        self.assertNotIn('before-generation',text)

    async def test_owner_guest_other_group_profile_and_new_session_do_not_share_cursors(self):
        self.note('shared-visible-note')
        await self.turn('owner-trigger')
        ident=self.note('guest-trigger',self.guest)
        await self.bridge.chat(self.guest,'guest-trigger',ident)
        self.assertIn('shared-visible-note',self.agent.requests[-1].text)
        other=Source('group',20002,10001)
        otherid=self.note('other-current',other)
        self.assertNotIn('shared-visible-note',self.bridge.group_reference(other,otherid,'other-thread'))
        self.assertIn('shared-visible-note',self.bridge.group_reference(self.owner,ident,'new-session'))
        alt=FakeAgent();alt.name='alternate';self.bridge.agents['alternate']=alt
        self.bridge.cfg['agents']={'front':'alternate'}
        self.assertIn('shared-visible-note',self.bridge.group_reference(self.owner,ident,'alternate-session'))

    async def test_cursor_persists_in_database_across_restart(self):
        self.note('already-seen')
        await self.turn('first-trigger')
        self.note('after-last-turn')
        ident=self.note('next-trigger',self.owner)
        session=self.agent.session_id(self.bridge.conversation(self.owner))
        other=Bridge({'owner_qq':10001,'codex_path':'unused'})
        try:
            other.agents['codex']=self.agent
            text=other.group_reference(self.owner,ident,session)
            self.assertIn('after-last-turn',text)
            self.assertNotIn('already-seen',text)
        finally:other.db.close()

    async def test_new_resets_context_floor_and_does_not_replay_pre_reset_messages(self):
        self.note('pre-reset')
        await self.turn('first-trigger')
        self.note('more-before-reset')
        await self.agent.reset(self.bridge.conversation(self.owner))
        self.bridge.reset_group_context(self.owner);self.bridge.db.commit()
        self.note('after-reset')
        text=await self.turn('new-trigger')
        self.assertIn('after-reset',text)
        self.assertNotIn('pre-reset',text)
        self.assertNotIn('more-before-reset',text)
        self.assertIn('pre-reset',self.bridge.group_reference(self.guest,999999,'guest'))


    async def test_new_keeps_messages_queued_after_the_reset_command(self):
        self.note('before-new')
        reset_id=self.note('/new',self.owner)
        self.note('queued-after-new')
        self.bridge.reset_group_context(self.owner,reset_id);self.bridge.db.commit()
        text=await self.turn('next-trigger')
        self.assertIn('queued-after-new',text)
        self.assertNotIn('before-new',text)
