# -*- coding: utf-8 -*-
# Copyright 2014, 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
test.test_common
~~~~~~~~~~~

Test common utility code.
"""
import copy
from collections import namedtuple
import eventlet
import logging
import mock
import os
import sys

if sys.version_info < (2, 7):
    import unittest2 as unittest
else:
    import unittest


import calico.common as common
from calico.common import ValidationFailed

Config = namedtuple("Config", ["IFACE_PREFIX"])

# Logger
_log = logging.getLogger(__name__)


MISSING = object()


class TestCommon(unittest.TestCase):
    def setUp(self):
        self.m_config = mock.Mock()
        self.m_config.IFACE_PREFIX = "tap"

    def tearDown(self):
        pass

    def test_validate_port(self):
        self.assertFalse(common.validate_port(-1))
        self.assertFalse(common.validate_port(0))
        self.assertTrue(common.validate_port(3))
        self.assertTrue(common.validate_port(3))
        self.assertTrue(common.validate_port(65535))
        self.assertFalse(common.validate_port(65536))
        self.assertFalse(common.validate_port("-1"))
        self.assertFalse(common.validate_port("0"))
        self.assertTrue(common.validate_port("3"))
        self.assertTrue(common.validate_port("3"))
        self.assertTrue(common.validate_port("65535"))
        self.assertFalse(common.validate_port("65536"))
        self.assertFalse(common.validate_port("1-10"))
        self.assertFalse(common.validate_port("blah"))

    def test_validate_endpoint_mainline(self):
        endpoint = {
            "state": "active",
            "name": "tap1234",
            "mac": "AA:bb:cc:dd:ee:ff",
            "ipv4_nets": ["10.0.1/32"],
            "ipv4_gateway": "11.0.0.1",
            "ipv6_nets": ["2001:0::1/64"],
            "ipv6_gateway": "fe80:0::1",
            "profile_id": "prof1",
        }
        common.validate_endpoint(self.m_config, "endpoint_id", endpoint)
        self.assertEqual(endpoint, {
            'state': 'active',
            'name': 'tap1234',
            'mac': 'aa:bb:cc:dd:ee:ff',
            'ipv4_nets': ['10.0.1.0/32'],
            'ipv4_gateway': '11.0.0.1',
            'ipv6_nets': ['2001::1/64'],
            'ipv6_gateway': 'fe80::1',
            'profile_ids':['prof1'],
        })

    def test_validate_endpoint_mainline_profile_ids(self):
        endpoint = {
            "state": "active",
            "name": "tap1234",
            "mac": "AA-bb-cc-dd-ee-ff",
            "ipv4_nets": ["10.0.1/32"],
            "profile_ids": ["prof1", "prof2"],
        }
        common.validate_endpoint(self.m_config, "endpoint_id", endpoint)
        self.assertEqual(endpoint, {
            'state': 'active',
            'name': 'tap1234',
            'mac': 'aa:bb:cc:dd:ee:ff',
            'ipv4_nets': ['10.0.1.0/32'],
            "ipv6_nets": [],
            "profile_ids": ["prof1", "prof2"],
        })

    def test_validate_endpoint_failures(self):
        self.assert_invalid_endpoint([])
        self.assert_invalid_endpoint("foo")
        self.assert_invalid_endpoint("foo")

        self.assert_tweak_invalidates_endpoint(state=MISSING)
        self.assert_tweak_invalidates_endpoint(state=None)
        self.assert_tweak_invalidates_endpoint(state="foo")

        self.assert_tweak_invalidates_endpoint(name=MISSING)
        self.assert_tweak_invalidates_endpoint(name=None)
        self.assert_tweak_invalidates_endpoint(name=object())
        self.assert_tweak_invalidates_endpoint(name=[])
        self.assert_tweak_invalidates_endpoint(name="incorrect_prefix")

        self.assert_tweak_invalidates_endpoint(mac=MISSING)
        self.assert_tweak_invalidates_endpoint(mac=None)
        self.assert_tweak_invalidates_endpoint(mac=object())
        self.assert_tweak_invalidates_endpoint(mac="bad MAC")

        self.assert_tweak_invalidates_endpoint(profile_id=MISSING)
        self.assert_tweak_invalidates_endpoint(profile_id=None)
        self.assert_tweak_invalidates_endpoint(profile_id=[])

        self.assert_tweak_invalidates_endpoint(ipv4_gateway="not an IP")
        self.assert_tweak_invalidates_endpoint(ipv4_gateway=[])

        self.assert_tweak_invalidates_endpoint(ipv6_gateway="not an IP")
        self.assert_tweak_invalidates_endpoint(ipv6_gateway=[])

        self.assert_tweak_invalidates_endpoint(ipv4_nets="not a list")
        self.assert_tweak_invalidates_endpoint(ipv4_nets={})
        self.assert_tweak_invalidates_endpoint(ipv4_nets=["not an IP"])
        self.assert_tweak_invalidates_endpoint(ipv4_nets=["12345"])
        self.assert_tweak_invalidates_endpoint(ipv4_nets=["1234::1/64"])

        self.assert_tweak_invalidates_endpoint(ipv6_nets="not a list")
        self.assert_tweak_invalidates_endpoint(ipv6_nets={})
        self.assert_tweak_invalidates_endpoint(ipv6_nets=["not an IP"])
        self.assert_tweak_invalidates_endpoint(ipv6_nets=["12345"])
        self.assert_tweak_invalidates_endpoint(ipv6_nets=["10.0.0.0/8"])

    def assert_invalid_endpoint(self, bad_value):
        self.assertRaises(common.ValidationFailed, common.validate_endpoint,
                          self.m_config, "endpoint_id", bad_value)

    def assert_endpoint_valid(self, original_endpoint):
        endpoint = copy.deepcopy(original_endpoint)
        try:
            # First pass at validation, may canonicalise the data.
            common.validate_endpoint(self.m_config, "endpoint_id", endpoint)
            canonical_endpoint = copy.deepcopy(endpoint)
            # Second pass, should make no changes.
            common.validate_endpoint(self.m_config, "endpoint_id", canonical_endpoint)
            self.assertEqual(endpoint, canonical_endpoint)
        except common.ValidationFailed as e:
            _log.exception("Validation unexpectedly failed for %s",
                           original_endpoint)
            self.fail("Validation unexpectedly failed for %s: %r" %
                      original_endpoint, e)

    def assert_tweak_invalidates_endpoint(self, **tweak):
        valid_endpoint = {
            "state": "active",
            "name": "tap1234",
            "mac": "AA:bb:cc:dd:ee:ff",
            "ipv4_nets": ["10.0.1/32"],
            "ipv4_gateway": "11.0.0.1",
            "ipv6_nets": ["2001:0::1/64"],
            "ipv6_gateway": "fe80:0::1",
            "profile_id": "prof1",
        }
        self.assert_endpoint_valid(valid_endpoint)
        invalid_endpoint = valid_endpoint.copy()
        for key, value in tweak.iteritems():
            if value is MISSING:
                invalid_endpoint.pop(key)
            else:
                invalid_endpoint[key] = value
        self.assert_invalid_endpoint(invalid_endpoint)

    def test_validate_rules_canon(self):
        rules = {
            "inbound_rules": [
                {"protocol": "tcp", "ip_version": 4, "src_net": "10/8",
                 "dst_net": "11.0/16", "src_ports": [10, "11:12"],
                 "action": "allow"},
                {"protocol": "tcp", "src_net": None},
            ],
            "outbound_rules": [
                {"protocol": "tcp", "ip_version": 6,
                 "src_net": "2001:0::1/128", "dst_net": "2001:0::/64",
                 "icmp_type": 7, "icmp_code": 10,
                 "action": "deny"}
            ],
        }
        common.validate_rules("profile_id", rules)
        # Check IPs get made canonical.
        self.assertEqual(rules, {
            "inbound_rules": [
                {"protocol": "tcp", "ip_version": 4, "src_net": "10.0.0.0/8",
                 "dst_net": "11.0.0.0/16", "src_ports": [10, "11:12"],
                 "action": "allow"},
                {"protocol": "tcp"},
            ],
            "outbound_rules": [
                {"protocol": "tcp", "ip_version": 6,
                 "src_net": "2001::1/128", "dst_net": "2001::/64",
                 "icmp_type": 7, "icmp_code": 10,
                 "action": "deny"}
            ],
        })

    def test_validate_ip_addr(self):
        self.assertTrue(common.validate_ip_addr("1.2.3.4", 4))
        self.assertFalse(common.validate_ip_addr("1.2.3.4.5", 4))
        self.assertFalse(common.validate_ip_addr("1.2.3.4/32", 4))
        self.assertTrue(common.validate_ip_addr("1.2.3", 4))
        self.assertFalse(common.validate_ip_addr("bloop", 4))
        self.assertFalse(common.validate_ip_addr("::", 4))
        self.assertFalse(common.validate_ip_addr("2001::abc", 4))
        self.assertFalse(common.validate_ip_addr("2001::a/64", 4))

        self.assertFalse(common.validate_ip_addr("1.2.3.4", 6))
        self.assertFalse(common.validate_ip_addr("1.2.3.4.5", 6))
        self.assertFalse(common.validate_ip_addr("1.2.3.4/32", 6))
        self.assertFalse(common.validate_ip_addr("1.2.3", 6))
        self.assertFalse(common.validate_ip_addr("bloop", 6))
        self.assertTrue(common.validate_ip_addr("::", 6))
        self.assertTrue(common.validate_ip_addr("2001::abc", 6))
        self.assertFalse(common.validate_ip_addr("2001::a/64", 6))

        self.assertTrue(common.validate_ip_addr("1.2.3.4", None))
        self.assertFalse(common.validate_ip_addr("1.2.3.4.5", None))
        self.assertFalse(common.validate_ip_addr("1.2.3.4/32", None))
        self.assertTrue(common.validate_ip_addr("1.2.3", None))
        self.assertFalse(common.validate_ip_addr("bloop", None))
        self.assertTrue(common.validate_ip_addr("::", None))
        self.assertTrue(common.validate_ip_addr("2001::abc", None))
        self.assertFalse(common.validate_ip_addr("2001::a/64", None))

        self.assertFalse(common.validate_ip_addr(None, None))

    def test_validate_cidr(self):
        self.assertTrue(common.validate_cidr("1.2.3.4", 4))
        self.assertFalse(common.validate_cidr("1.2.3.4.5", 4))
        self.assertTrue(common.validate_cidr("1.2.3.4/32", 4))
        self.assertTrue(common.validate_cidr("1.2.3", 4))
        self.assertFalse(common.validate_cidr("bloop", 4))
        self.assertFalse(common.validate_cidr("::", 4))
        self.assertFalse(common.validate_cidr("2001::abc", 4))
        self.assertFalse(common.validate_cidr("2001::a/64", 4))

        self.assertFalse(common.validate_cidr("1.2.3.4", 6))
        self.assertFalse(common.validate_cidr("1.2.3.4.5", 6))
        self.assertFalse(common.validate_cidr("1.2.3.4/32", 6))
        self.assertFalse(common.validate_cidr("1.2.3", 6))
        self.assertFalse(common.validate_cidr("bloop", 6))
        self.assertTrue(common.validate_cidr("::", 6))
        self.assertTrue(common.validate_cidr("2001::abc", 6))
        self.assertTrue(common.validate_cidr("2001::a/64", 6))

        self.assertTrue(common.validate_cidr("1.2.3.4", None))
        self.assertFalse(common.validate_cidr("1.2.3.4.5", None))
        self.assertTrue(common.validate_cidr("1.2.3.4/32", None))
        self.assertTrue(common.validate_cidr("1.2.3", None))
        self.assertFalse(common.validate_cidr("bloop", None))
        self.assertTrue(common.validate_cidr("::", None))
        self.assertTrue(common.validate_cidr("2001::abc", None))
        self.assertTrue(common.validate_cidr("2001::a/64", None))

        self.assertFalse(common.validate_cidr(None, None))

    def test_canonicalise_ip(self):
        self.assertTrue(common.canonicalise_ip("1.2.3.4", 4), "1.2.3.4")
        self.assertTrue(common.canonicalise_ip("1.2.3", 4), "1.2.3.0")

        self.assertTrue(common.canonicalise_ip("2001::0:1", 6), "2001::1")
        self.assertTrue(common.canonicalise_ip("abcd:eff::", 6), "abcd:eff::")
        self.assertTrue(common.canonicalise_ip("abcd:0000:eff::", 6),
                        "abcd:0:eff::")
        self.assertTrue(common.canonicalise_ip("::", 6), "::")

    def test_validate_endpoint(self):
        endpoint_id = "valid_name-ok."
        endpoint_dict = {'profile_id': "valid.prof-name",
                         'state': "active",
                         'name': "tapabcdef",
                         'mac': "78:2b:cb:9f:ae:1c",
                         'ipv4_nets': [],
                         'ipv6_nets': []}
        config = Config('tap')
        ep_copy = endpoint_dict.copy()
        common.validate_endpoint(config, endpoint_id, ep_copy)
        self.assertTrue(ep_copy.get('profile_id') is None)
        self.assertEqual(ep_copy.get('profile_ids'), ["valid.prof-name"])

        # Now break it various ways.
        # Bad endpoint ID.
        for bad_id in ("with spaces", "$stuff", "^%@"):
            with self.assertRaisesRegexp(ValidationFailed,
                                         "Invalid endpoint ID"):
                common.validate_endpoint(config, bad_id, endpoint_dict.copy())

        # Bad dictionary.
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Expected endpoint to be a dict"):
            common.validate_endpoint(config, endpoint_id, [1,2,3])

        # No state, invalid state.
        bad_dict = endpoint_dict.copy()
        del bad_dict['state']
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Missing 'state' field"):
            common.validate_endpoint(config, endpoint_id, bad_dict)
        bad_dict['state'] = "invalid"
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Expected 'state' to be"):
            common.validate_endpoint(config, endpoint_id, bad_dict)

        # Missing mac and name; both must be reported as two errors
        bad_dict = endpoint_dict.copy()
        del bad_dict['name']
        del bad_dict['mac']
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Missing 'name' field"):
            common.validate_endpoint(config, endpoint_id, bad_dict)
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Missing 'mac' field"):
            common.validate_endpoint(config, endpoint_id, bad_dict)

        bad_dict['name'] = [1, 2, 3]
        bad_dict['mac'] = 73
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Expected 'name' to be a string.*" +
                                     "Expected 'mac' to be a string"):
            common.validate_endpoint(config, endpoint_id, bad_dict)

        # Bad profile ID
        bad_dict = endpoint_dict.copy()
        bad_dict['profile_id'] = "str£ing"
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid profile ID"):
            common.validate_endpoint(config, endpoint_id, bad_dict)

        bad_dict = endpoint_dict.copy()
        del bad_dict['profile_id']
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Missing 'profile_id\(s\)' field"):
            common.validate_endpoint(config, endpoint_id, bad_dict)

        bad_dict = endpoint_dict.copy()
        del bad_dict['profile_id']
        bad_dict['profile_ids'] = [1, 2, 3]
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Expected profile IDs to be strings"):
            common.validate_endpoint(config, endpoint_id, bad_dict)

        # Bad interface name.
        bad_dict = endpoint_dict.copy()
        bad_dict['name'] = "vethabcdef"
        with self.assertRaisesRegexp(ValidationFailed,
                                     "does not start with"):
            common.validate_endpoint(config, endpoint_id, bad_dict)

        # Valid networks.
        good_dict = endpoint_dict.copy()
        good_dict['ipv4_nets'] = [ "1.2.3.4/32", "172.0.0.0/8", "3.4.5.6"]
        good_dict['ipv6_nets'] = [ "::1/128", "::", "2001:db8:abc:1400::/54"]
        common.validate_endpoint(config, endpoint_id, good_dict.copy())

        # Invalid networks
        bad_dict = good_dict.copy()
        bad_dict['ipv4_nets'] = [ "1.2.3.4/32", "172.0.0.0/8", "2001:db8:abc:1400::/54"]
        with self.assertRaisesRegexp(ValidationFailed,
                                     "not a valid IPv4 CIDR"):
            common.validate_endpoint(config, endpoint_id, bad_dict.copy())
        bad_dict['ipv4_nets'] = [ "1.2.3.4/32", "172.0.0.0/8", "nonsense"]
        with self.assertRaisesRegexp(ValidationFailed,
                                     "not a valid IPv4 CIDR"):
            common.validate_endpoint(config, endpoint_id, bad_dict.copy())

        bad_dict = good_dict.copy()
        bad_dict['ipv6_nets'] = [ "::1/128", "::", "1.2.3.4/8"]
        with self.assertRaisesRegexp(ValidationFailed,
                                     "not a valid IPv6 CIDR"):
            common.validate_endpoint(config, endpoint_id, bad_dict.copy())
        bad_dict['ipv6_nets'] = [ "::1/128", "::", "nonsense"]
        with self.assertRaisesRegexp(ValidationFailed,
                                     "not a valid IPv6 CIDR"):
            common.validate_endpoint(config, endpoint_id, bad_dict.copy())

        # Gateway IPs.
        good_dict['ipv4_gateway'] = "1.2.3.4"
        good_dict['ipv6_gateway'] = "2001:db8:abc:1400::"
        common.validate_endpoint(config, endpoint_id, good_dict.copy())

        bad_dict = good_dict.copy()
        bad_dict['ipv4_gateway'] = "2001:db8:abc:1400::"
        with self.assertRaisesRegexp(ValidationFailed,
                                     "not a valid IPv4 gateway"):
            common.validate_endpoint(config, endpoint_id, bad_dict.copy())
        bad_dict['ipv4_gateway'] = "nonsense"
        with self.assertRaisesRegexp(ValidationFailed,
                                     "not a valid IPv4 gateway"):
            common.validate_endpoint(config, endpoint_id, bad_dict.copy())

        bad_dict = good_dict.copy()
        bad_dict['ipv6_gateway'] = "1.2.3.4"
        with self.assertRaisesRegexp(ValidationFailed,
                                     "not a valid IPv6 gateway"):
            common.validate_endpoint(config, endpoint_id, bad_dict.copy())
        bad_dict['ipv6_gateway'] = "nonsense"
        with self.assertRaisesRegexp(ValidationFailed,
                                     "not a valid IPv6 gateway"):
            common.validate_endpoint(config, endpoint_id, bad_dict.copy())

    def test_validate_rules(self):
        profile_id = "valid_name-ok."
        rules = {'inbound_rules': [],
                 'outbound_rules': []}
        common.validate_rules(profile_id, rules.copy())

        with self.assertRaisesRegexp(ValidationFailed,
                                     "Expected rules to be a dict"):
            common.validate_rules(profile_id, [])

        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid profile_id"):
            common.validate_rules("a&b", rules.copy())

        # No rules.
        with self.assertRaisesRegexp(ValidationFailed,
                                     "No outbound_rules"):
            common.validate_rules(profile_id, {'inbound_rules':[]})
        with self.assertRaisesRegexp(ValidationFailed,
                                     "No inbound_rules"):
            common.validate_rules(profile_id, {'outbound_rules':[]})

        rules = {'inbound_rules': 3,
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                            "Expected rules\[inbound_rules\] to be a list"):
            common.validate_rules(profile_id, rules.copy())

        rule = "not a dict"
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                            "Rules should be dicts"):
            common.validate_rules(profile_id, rules.copy())

        rule = {'bad_key': ""}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Rule contains unknown keys"):
            common.validate_rules(profile_id, rules)

        rule = {'protocol': "bloop"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid protocol in rule"):
            common.validate_rules(profile_id, rules)

        rule = {'ip_version': 5}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid ip_version in rule"):
            common.validate_rules(profile_id, rules)

        rule = {'ip_version': 4,
                'protocol': "icmpv6"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Using icmpv6 with IPv4"):
            common.validate_rules(profile_id, rules)

        rule = {'ip_version': 6,
                'protocol': "icmp"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Using icmp with IPv6"):
            common.validate_rules(profile_id, rules)

        rule = {'src_tag': "abc",
                'protocol': "icmp"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        common.validate_rules(profile_id, rules)

        rule = {'src_tag': "a!b",
                'protocol': "icmp"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid src_tag"):
            common.validate_rules(profile_id, rules)

        rule = {'dst_tag': "x,y",
                'protocol': "icmp"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid dst_tag"):
            common.validate_rules(profile_id, rules)

        rule = {'src_net': "nonsense"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid CIDR"):
            common.validate_rules(profile_id, rules)

        rule = {'dst_net': "1.2.3.4/16",
                'ip_version': 6}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid CIDR"):
            common.validate_rules(profile_id, rules)

        rule = {'src_ports': "nonsense"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Expected ports to be a list"):
            common.validate_rules(profile_id, rules)

        rule = {'dst_ports': [32, "nonsense"]}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid port"):
            common.validate_rules(profile_id, rules)

        rule = {'action': "nonsense"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid action"):
            common.validate_rules(profile_id, rules)

        rule = {'icmp_type': "nonsense"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "ICMP type is not an integer"):
            common.validate_rules(profile_id, rules)

        rule = {'icmp_type': -1}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "ICMP type is out of range"):
            common.validate_rules(profile_id, rules)

        rule = {'icmp_type': 256}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "ICMP type is out of range"):
            common.validate_rules(profile_id, rules)

        rule = {'icmp_type': 22,
                'icmp_code': "2"}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "ICMP code is not an integer"):
            common.validate_rules(profile_id, rules)

        rule = {'icmp_type': 0,
                'icmp_code': -1}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "ICMP code is out of range"):
            common.validate_rules(profile_id, rules)

        rule = {'icmp_type': 0,
                'icmp_code': 256}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "ICMP code is out of range"):
            common.validate_rules(profile_id, rules)

        rule = {'icmp_code': 2}
        rules = {'inbound_rules': [rule],
                 'outbound_rules': []}
        with self.assertRaisesRegexp(ValidationFailed,
                                     "ICMP code specified without ICMP type"):
            common.validate_rules(profile_id, rules)

    def test_validate_rule_port(self):
        self.assertEqual(common.validate_rule_port(73), None)
        self.assertEqual(common.validate_rule_port("57:123"), None)
        self.assertEqual(common.validate_rule_port(0),
                         "integer out of range")
        self.assertEqual(common.validate_rule_port(65536),
                         "integer out of range")
        self.assertEqual(common.validate_rule_port([]),
                         "neither integer nor string")
        self.assertEqual(common.validate_rule_port("1:2:3"),
                         "range unparseable")
        self.assertEqual(common.validate_rule_port("1"),
                         "range unparseable")
        self.assertEqual(common.validate_rule_port(""),
                         "range unparseable")
        self.assertEqual(common.validate_rule_port("a:b"),
                         "range invalid")
        self.assertEqual(common.validate_rule_port("3:1"),
                         "range invalid")
        self.assertEqual(common.validate_rule_port("0:3"),
                         "range invalid")
        self.assertEqual(common.validate_rule_port("5:65536"),
                         "range invalid")

    def test_validate_tags(self):
        profile_id = "valid_name-ok."
        tags = [ "name", "_name-with.chars.-_" ]
        common.validate_tags(profile_id, tags)

        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid profile"):
            common.validate_tags('bad"value', tags)

        with self.assertRaisesRegexp(ValidationFailed,
                                     "Expected tags to be a list"):
            common.validate_tags(profile_id, "not a list")

        with self.assertRaisesRegexp(ValidationFailed,
                                     "Expected tag.* to be a string"):
            common.validate_tags(profile_id, ["value", 3])

        with self.assertRaisesRegexp(ValidationFailed,
                                     "Invalid tag"):
            common.validate_tags(profile_id, ["value", "bad value"])

    def test_greenlet_id(self):
        def greenlet_run():
            tid = common.greenlet_id()
            return tid

        tid = common.greenlet_id()
        child = eventlet.spawn(greenlet_run)
        child_tid = child.wait()
        new_tid = common.greenlet_id()

        self.assertTrue(child_tid > tid)
        self.assertEqual(tid, new_tid)
