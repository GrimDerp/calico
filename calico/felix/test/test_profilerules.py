# -*- coding: utf-8 -*-
# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
test_profilerules
~~~~~~~~~~~~~~~~~

Tests for the profilerules module.
"""

import logging
from subprocess import CalledProcessError
from mock import Mock, call
from calico.felix.fiptables import IptablesUpdater
from calico.felix.ipsets import IpsetManager, ActiveIpset
from calico.felix.profilerules import ProfileRules, RulesManager

from calico.felix.test.base import BaseTestCase


_log = logging.getLogger(__name__)


RULES_1 = {
    "id": "prof1",
    "inbound_rules": [
        {"src_tag": "src-tag"}
    ],
    "outbound_rules": [
        {"dst_tag": "dst-tag"}
    ]
}

RULES_1_CHAINS = {
    'felix-p-prof1-i': [
        '--append felix-p-prof1-i --match set '
            '--match-set src-tag-name src --jump RETURN',
        '--append felix-p-prof1-i --match comment '
            '--comment "Mark as not matched" --jump MARK --set-mark 1'],
    'felix-p-prof1-o': [
        '--append felix-p-prof1-o --match set '
            '--match-set dst-tag-name dst --jump RETURN',
        '--append felix-p-prof1-o --match comment '
            '--comment "Mark as not matched" --jump MARK --set-mark 1']
}


RULES_2 = {
    "id": "prof1",
    "inbound_rules": [
        {"src_tag": "src-tag-added"}
    ],
    "outbound_rules": [
        {"dst_tag": "dst-tag"}
    ]
}

RULES_2_CHAINS = {
    'felix-p-prof1-i': [
        '--append felix-p-prof1-i --match set '
            '--match-set src-tag-added-name src --jump RETURN',
        '--append felix-p-prof1-i --match comment '
            '--comment "Mark as not matched" --jump MARK --set-mark 1'],
    'felix-p-prof1-o': [
        '--append felix-p-prof1-o --match set '
            '--match-set dst-tag-name dst --jump RETURN',
        '--append felix-p-prof1-o --match comment '
            '--comment "Mark as not matched" --jump MARK --set-mark 1']
}


class TestProfileRules(BaseTestCase):
    def setUp(self):
        super(TestProfileRules, self).setUp()
        self.m_mgr = Mock(spec=RulesManager)
        self.m_ipt_updater = Mock(spec=IptablesUpdater)
        self.m_ips_mgr = Mock(spec=IpsetManager)
        self.rules = ProfileRules("prof1", 4, self.m_ipt_updater,
                                  self.m_ips_mgr)
        self.rules._manager = self.m_mgr
        self.rules._id = "prof1"

    def test_first_profile_update(self):
        """
        Test initial startup.

        Should acquire ipsets, program iptables and call back.
        """
        self.rules.on_profile_update(RULES_1, async=True)
        self.step_actor(self.rules)
        expected_tags = set(["src-tag", "dst-tag"])
        self.assertEqual(self.rules._ipset_refs.required_refs,
                         expected_tags)
        # Don't have all the ipsets yet.  should still be dirty.
        self.assertTrue(self.rules._dirty)
        # Simulate acquiring the ipsets.
        self._process_ipset_refs(expected_tags)
        # Got all the tags, should no longer be dirty.
        self.assertFalse(self.rules._dirty)
        self.m_ipt_updater.rewrite_chains.assert_called_once_with(
            RULES_1_CHAINS, {}, async=False)
        # Should have called back to the manager.
        self.m_mgr.on_object_startup_complete("prof1",
                                              self.rules,
                                              async=True)

    def test_coalesce_updates(self):
        """
        Test multiple updates in the same batch are squashed and only the
        last one has any effect.
        """
        self.rules.on_profile_update(RULES_1, async=True)
        self.rules.on_profile_update(RULES_2, async=True)
        self.rules.on_profile_update(RULES_1, async=True)
        self.step_actor(self.rules)
        expected_tags = set(["src-tag", "dst-tag"])
        self._process_ipset_refs(expected_tags)
        self.m_ipt_updater.rewrite_chains.assert_called_once_with(
            RULES_1_CHAINS, {}, async=False)
        # Should have called back to the manager.
        self.m_mgr.on_object_startup_complete("prof1",
                                              self.rules,
                                              async=True)

    def test_idempotent_update(self):
        """
        Test that an update that doesn't change the already-programmed
        value is squashed.
        """
        self.rules.on_profile_update(RULES_1, async=True)
        self.step_actor(self.rules)
        self._process_ipset_refs(set(["src-tag", "dst-tag"]))

        self.rules.on_profile_update(RULES_1, async=True)
        self.step_actor(self.rules)
        self._process_ipset_refs(set([]))

        self.m_ipt_updater.rewrite_chains.assert_called_once_with(
            RULES_1_CHAINS, {}, async=False)

    def test_idempotent_update_transient_ipt_error(self):
        """
        Test that the dirty flag is left set if the update fails.  Future
        updates that would normally be squashed trigger a reprogram.
        """
        self.m_ipt_updater.rewrite_chains.side_effect = CalledProcessError(1, ["foo"])
        self.rules.on_profile_update(RULES_1, async=True)
        self.step_actor(self.rules)
        self._process_ipset_refs(set(["src-tag", "dst-tag"])) # Steps the actor.

        # Second tag update will trigger single attempt to program.
        self.m_ipt_updater.rewrite_chains.assert_called_once_with(
            RULES_1_CHAINS, {}, async=False)
        self.m_ipt_updater.reset_mock()
        # Failure should leave ProfileRules dirty.
        self.assertTrue(self.rules._dirty)

        # Update should trigger retry even though there w
        # qas no change of data.
        self.m_ipt_updater.rewrite_chains.side_effect = None
        self.rules.on_profile_update(RULES_1, async=True)
        self.step_actor(self.rules)
        self._process_ipset_refs(set([]))
        self.m_ipt_updater.rewrite_chains.assert_called_once_with(
            RULES_1_CHAINS, {}, async=False)
        self.assertFalse(self.rules._dirty)


    def test_update(self):
        """
        Test a update changes ipset refs and iptables.
        """
        self.rules.on_profile_update(RULES_1, async=True)
        self.step_actor(self.rules)
        self.rules.on_profile_update(RULES_2, async=True)
        self.step_actor(self.rules)
        # Old src-tag tag should get removed.
        expected_tags = set(["src-tag-added", "dst-tag"])
        self.assertEqual(self.rules._ipset_refs.required_refs,
                         expected_tags)
        # But the ref helper will already have sent an incref for "src-tag".
        self._process_ipset_refs(expected_tags | set(["src-tag"]))
        self.m_ipt_updater.rewrite_chains.assert_called_once_with(
            RULES_2_CHAINS, {}, async=False)

    def test_early_unreferenced(self):
        """
        Test shutdown with tag references in flight.
        """
        ref_helper = self.rules._ipset_refs
        self.rules.on_profile_update(RULES_1, async=True)
        self.rules.on_unreferenced(async=True)
        self.step_actor(self.rules)
        self.assertTrue(self.rules._ipset_refs is None)
        self.assertEqual(ref_helper.required_refs, set())
        # Early on_unreferenced should have prevented any ipset requests.
        self._process_ipset_refs(set([]))
        self.assertFalse(self.m_ips_mgr.decref.called)
        self.assertTrue(self.rules._dead)
        self.m_ipt_updater.delete_chains.assert_called_once_with(
            set(['felix-p-prof1-i', 'felix-p-prof1-o']), async=False
        )

    def test_unreferenced_after_creation(self):
        """
        Test shutdown after completing initial programming.
        """
        ref_helper = self.rules._ipset_refs
        self.rules.on_profile_update(RULES_1, async=True)
        self.step_actor(self.rules)
        # Tag updates come in before unreferenced.
        self._process_ipset_refs(set(["src-tag", "dst-tag"]))

        # Then simulate a deletion.
        self.rules.on_unreferenced(async=True)
        self.step_actor(self.rules)

        self.assertTrue(self.rules._ipset_refs is None)
        self.assertEqual(ref_helper.required_refs, set())
        self.assertTrue(self.rules._dead)
        self.m_ips_mgr.decref.assert_has_calls(
            [call("src-tag", async=True), call("dst-tag", async=True)],
            any_order=True
        )
        self.m_ipt_updater.delete_chains.assert_called_once_with(
            set(['felix-p-prof1-i', 'felix-p-prof1-o']), async=False
        )

    def test_immediate_deletion(self):
        """
        Test deletion before even doing first programming.
        """
        ref_helper = self.rules._ipset_refs
        self.rules.on_profile_update(None, async=True)
        self.rules.on_unreferenced(async=True)
        self.step_actor(self.rules)
        self.assertTrue(self.rules._ipset_refs is None)
        self.assertEqual(ref_helper.required_refs, set())
        # Should never have acquired any refs.
        self._process_ipset_refs(set())
        self.assertTrue(self.rules._dead)
        self.m_ipt_updater.delete_chains.assert_called_once_with(
            set(['felix-p-prof1-i', 'felix-p-prof1-o']), async=False
        )

    def _process_ipset_refs(self, expected_tags):
        """
        Issues callbacks for all the mock calls to the mock ipset manager's
        get_and_incref.

        Steps the actor as a side-effect.

        Asserts the set of tags that were requested.
        """
        seen_tags = set()
        for name, args, kwargs in self.m_ips_mgr.get_and_incref.mock_calls:
            obj_id = args[0]
            callback = kwargs["callback"]
            seen_tags.add(obj_id)
            m_ipset = Mock(spec=ActiveIpset)
            m_ipset.name = obj_id + "-name"
            callback(obj_id, m_ipset)
            self.step_actor(self.rules)
        self.m_ips_mgr.get_and_incref.reset_mock()
        self.assertEqual(seen_tags, expected_tags)