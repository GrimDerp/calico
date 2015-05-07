# -*- coding: utf-8 -*-
# Copyright 2015 Metaswitch Networks
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
openstack.test.lib
~~~~~~~~~~~

Common code for Neutron driver UT.
"""
import eventlet
import eventlet.queue
from eventlet.support import greenlets as greenlet
import inspect
import mock
import sys
import traceback

sys.modules['etcd'] = m_etcd = mock.Mock()
sys.modules['neutron'] = m_neutron = mock.Mock()
sys.modules['neutron.common'] = m_neutron.common
sys.modules['neutron.common.exceptions'] = m_neutron.common.exceptions
sys.modules['neutron.openstack'] = m_neutron.openstack
sys.modules['neutron.openstack.common'] = m_neutron.openstack.common
sys.modules['neutron.plugins'] = m_neutron.plugins
sys.modules['neutron.plugins.ml2'] = m_neutron.plugins.ml2
sys.modules['neutron.plugins.ml2.drivers'] = m_neutron.plugins.ml2.drivers
sys.modules['oslo'] = m_oslo = mock.Mock()
sys.modules['oslo.config'] = m_oslo.config
sys.modules['time'] = m_time = mock.Mock()

port1 = {'binding:vif_type': 'tap',
         'binding:host_id': 'felix-host-1',
         'id': 'DEADBEEF-1234-5678',
         'device_id': 'instance-1',
         'device_owner': 'compute:nova',
         'fixed_ips': [{'subnet_id': '10.65.0/24',
                        'ip_address': '10.65.0.2'}],
         'mac_address': '00:11:22:33:44:55',
         'admin_state_up': True,
         'security_groups': ['SGID-default']}

port2 = {'binding:vif_type': 'tap',
         'binding:host_id': 'felix-host-1',
         'id': 'FACEBEEF-1234-5678',
         'device_id': 'instance-2',
         'device_owner': 'compute:nova',
         'fixed_ips': [{'subnet_id': '10.65.0/24',
                        'ip_address': '10.65.0.3'}],
         'mac_address': '00:11:22:33:44:66',
         'admin_state_up': True,
         'security_groups': ['SGID-default']}

# Port with an IPv6 address.
port3 = {'binding:vif_type': 'tap',
         'binding:host_id': 'felix-host-2',
         'id': 'HELLO-1234-5678',
         'device_id': 'instance-3',
         'device_owner': 'compute:nova',
         'fixed_ips': [{'subnet_id': '2001:db8:a41:2::/64',
                        'ip_address': '2001:db8:a41:2::12'}],
         'mac_address': '00:11:22:33:44:66',
         'admin_state_up': True,
         'security_groups': ['SGID-default']}


# Define a stub class, that we will use as the base class for
# CalicoMechanismDriver.
class DriverBase(object):
    def __init__(self, agent_type, vif_type, vif_details):
        pass

# Replace Neutron's SimpleAgentMechanismDriverBase - which is the base class
# that CalicoMechanismDriver inherits from - with this stub class.
m_neutron.plugins.ml2.drivers.mech_agent.SimpleAgentMechanismDriverBase = \
    DriverBase

import calico.openstack.mech_calico as mech_calico
import calico.election as election

REAL_EVENTLET_SLEEP_TIME = 0.01

# Value used to indicate 'timeout' in poll and sleep processing.
TIMEOUT_VALUE = object()


class Lib(object):

    # Ports to return when the driver asks the OpenStack database for all
    # current ports.
    osdb_ports = []

    def setUp(self):
        # Announce the current test case.
        print "\nTEST CASE: %s" % self.id()

        # Hook eventlet.
        self.setUp_eventlet()

        # Hook logging.
        self.setUp_logging()

        # If an arg mismatch occurs, we want to see the complete diff of it.
        self.maxDiff = None

        # Create an instance of CalicoMechanismDriver.
        self.driver = mech_calico.CalicoMechanismDriver()

        # Hook the (mock) Neutron database.
        self.db = mech_calico.manager.NeutronManager.get_plugin()
        self.db_context = mech_calico.ctx.get_admin_context()

        # Arrange what the DB's get_ports will return.
        self.db.get_ports.side_effect = lambda *args: self.osdb_ports

        # Arrange DB's get_subnet call.
        self.db.get_subnet.side_effect = self.get_subnet

        # Arrange what the DB's get_security_groups query will return (the
        # default SG).
        self.db.get_security_groups.return_value = [
            {'id': 'SGID-default',
             'security_group_rules': [
                 {'remote_group_id': 'SGID-default',
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'ingress',
                  'ethertype': 'IPv4',
                  'port_range_min': -1},
                 {'remote_group_id': 'SGID-default',
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'ingress',
                  'ethertype': 'IPv6',
                  'port_range_min': -1},
                 {'remote_group_id': None,
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'egress',
                  'ethertype': 'IPv4',
                  'port_range_min': -1},
                 {'remote_group_id': None,
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'egress',
                  'ethertype': 'IPv6',
                  'port_range_min': -1}
             ]}
        ]

        # Prep a null response to the following
        # _get_port_security_group_bindings call.
        self.db._get_port_security_group_bindings.return_value = []

    def setUp_eventlet(self):
        """Setup to intercept sleep calls made by the code under test, and hence to
        (i) control when those expire, and (ii) allow time to appear to pass (to
        the code under test) without actually having to wait for that time.
        """
        # Reset the simulated time (in seconds) that has passed since the
        # beginning of the test.
        self.current_time = 0

        # Make time.time() return current_time.
        m_time.time.side_effect = lambda: self.current_time

        # Reset the dict of current sleepers.  In each dict entry, the key is
        # an eventlet.Queue object and the value is the time at which the sleep
        # should complete.
        self.sleepers = {}

        # Reset the list of spawned eventlet threads.
        self.threads = []

        # Replacement for eventlet.sleep: sleep for some simulated passage of
        # time (as directed by simulated_time_advance), instead of for real
        # elapsed time.
        def simulated_time_sleep(secs):

            # Create a new queue.
            queue = eventlet.Queue(1)
            queue.stack = inspect.stack()[1][3]

            # Add it to the dict of sleepers, together with the waking up time.
            self.sleepers[queue] = self.current_time + secs

            print "T=%s: %s: Start sleep for %ss until T=%s" % (
                self.current_time, queue.stack, secs, self.sleepers[queue]
            )

            # Do a zero time real sleep, to allow other threads to run.
            self.real_eventlet_sleep(REAL_EVENTLET_SLEEP_TIME)

            # Block until something is posted to the queue.
            ignored = queue.get(True)

            # Wake up.
            return None

        # Replacement for eventlet.spawn: track spawned threads so that we can
        # kill them all when a test case ends.
        def simulated_spawn(*args):

            # Do the real spawn.
            thread = self.real_eventlet_spawn(*args)

            # Remember this thread.
            self.threads.append(thread)

            # Also return it.
            return thread

        # Hook sleeping.
        self.real_eventlet_sleep = eventlet.sleep
        eventlet.sleep = simulated_time_sleep

        # Similarly hook spawning.
        self.real_eventlet_spawn = eventlet.spawn
        eventlet.spawn = simulated_spawn

    def setUp_logging(self):
        """Setup to intercept and display logging by the code under test.
        """
        # Print logs to stdout.
        def log_info(msg, *args):
            print "       INFO %s" % (msg % args)
            return None
        def log_debug(msg, *args):
            print "       DEBUG %s" % (msg % args)
            return None
        def log_warn(msg, *args, **kwargs):
            print "       WARN %s" % (msg % args)
            return None
        def log_error(msg, *args):
            print "       ERROR %s" % (msg % args)
            return None
        def log_exception(msg, *args):
            print "       EXCEPTION %s" % (msg % args)
            if sys.exc_type is not greenlet.GreenletExit:
                traceback.print_exc()
            return None

        # Hook logging.
        mech_calico.LOG = mock.Mock()
        mech_calico.LOG.info.side_effect = log_info
        mech_calico.LOG.debug.side_effect = log_debug
        mech_calico.LOG.warn.side_effect = log_warn
        mech_calico.LOG.warning.side_effect = log_warn
        mech_calico.LOG.error.side_effect = log_error
        mech_calico.LOG.exception.side_effect = log_exception
        election._log = mech_calico.LOG

    # Tear down after each test case.
    def tearDown(self):

        print "\nClean up remaining green threads..."

        for thread in self.threads:
            thread.kill()

        # Stop hooking eventlet.
        self.tearDown_eventlet()

    def tearDown_eventlet(self):

        # Restore the real eventlet.sleep and eventlet.spawn.
        eventlet.sleep = self.real_eventlet_sleep
        eventlet.spawn = self.real_eventlet_spawn

    # Method for the test code to call when it wants to advance the simulated
    # time.
    def simulated_time_advance(self, secs):

        while (secs > 0):
            print "T=%s: Want to advance by %s" % (self.current_time, secs)

            # Determine the time to advance to in this iteration: either the
            # full time that we've been asked for, or the time at which the
            # next sleeper should wake up, whichever of those is earlier.
            wake_up_time = self.current_time + secs
            for queue in self.sleepers.keys():
                if self.sleepers[queue] < wake_up_time:
                    # This sleeper will wake up before the time that we've been
                    # asked to advance to.
                    wake_up_time = self.sleepers[queue]

            # Advance to the determined time.
            secs -= (wake_up_time - self.current_time)
            self.current_time = wake_up_time
            print "T=%s" % self.current_time

            # Wake up all sleepers that should now wake up.
            for queue in self.sleepers.keys():
                if self.sleepers[queue] <= self.current_time:
                    print "T=%s >= %s: %s: Wake up!" % (self.current_time,
                                                        self.sleepers[queue],
                                                        queue.stack)
                    del self.sleepers[queue]
                    queue.put_nowait(TIMEOUT_VALUE)

            # Allow woken (and possibly other) threads to run.
            self.real_eventlet_sleep(REAL_EVENTLET_SLEEP_TIME)

    def give_way(self):
        """Method for test code to call when it wants to allow other eventlet threads
        to run.
        """
        self.real_eventlet_sleep(REAL_EVENTLET_SLEEP_TIME)

    def check_update_port_status_called(self, context):
        self.db.update_port_status.assert_called_once_with(
            context._plugin_context,
            context._port['id'],
            mech_calico.constants.PORT_STATUS_ACTIVE)
        self.db.update_port_status.reset_mock()

    def get_subnet(self, context, id):
        if ':' in id:
            return {'gateway_ip': '2001:db8:a41:2::1'}
        else:
            return {'gateway_ip': '10.65.0.1'}

    def notify_security_group_update(self, id, rules, port, type):
        """Notify a new or changed security group definition.
        """
        # Prep appropriate responses for next get_security_group and
        # _get_port_security_group_bindings calls.
        self.db.get_security_group.return_value = {
            'id': id,
            'security_group_rules': rules
        }
        if port is None:
            self.db._get_port_security_group_bindings.return_value = []
        else:
            self.db._get_port_security_group_bindings.return_value = [
                {'port_id': port['id']}
            ]
            self.db.get_port.return_value = port

        if type == 'rule':
            # Call security_groups_rule_updated with the new or changed ID.
            self.db.notifier.security_groups_rule_updated(mock.Mock(), [id])
        else:
            # Call security_groups_member_updated with the new or changed ID.
            self.db.notifier.security_groups_member_updated(mock.Mock(), [id])
