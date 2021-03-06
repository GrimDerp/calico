# -*- coding: utf-8 -*-
#
# Copyright (c) 2014, 2015 Metaswitch Networks
# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# Calico/OpenStack Plugin
#
# This module is the OpenStack-specific implementation of the Plugin component
# of the new Calico architecture (described by the "Calico Architecture"
# document at http://docs.projectcalico.org/en/latest/architecture.html).
#
# It is implemented as a Neutron/ML2 mechanism driver.
import os
import eventlet

from collections import namedtuple
from functools import wraps

# OpenStack imports.
from neutron.common import constants
from neutron.plugins.ml2 import driver_api as api
from neutron.plugins.ml2.drivers import mech_agent
from neutron import context as ctx
from neutron import manager

try:  # Icehouse, Juno
    from neutron.openstack.common import log
except ImportError:  # Kilo
    from oslo_log import log

# Calico imports.
import etcd
from calico.openstack.t_etcd import CalicoTransportEtcd

LOG = log.getLogger(__name__)

# An OpenStack agent type name for Felix, the Calico agent component in the new
# architecture.
AGENT_TYPE_FELIX = 'Felix (Calico agent)'

# The interval between period resyncs, in seconds.
# TODO: Increase this to a longer interval for product code.
RESYNC_INTERVAL_SECS = 60
# When we're not the master, how often we check if we have become the master.
MASTER_CHECK_INTERVAL_SECS = 5

# We wait for a short period of time before we initialize our state to avoid
# problems with Neutron forking.
STARTUP_DELAY_SECS = 30

# A single security profile.
SecurityProfile = namedtuple(
    'SecurityProfile', ['id', 'inbound_rules', 'outbound_rules']
)


def requires_state(f):
    """
    This decorator is used to ensure that any method that requires that
    state be initialized will do that. This is to make sure that, if a user
    attempts an action before STARTUP_DELAY_SECS have passed, they don't
    have to wait.

    This decorator only needs to be applied to top-level functions of the
    CalicoMechanismDriver class: specifically, those that are called directly
    from Neutron.
    """
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        self._init_state()
        return f(self, *args, **kwargs)

    return wrapper


def retry_on_cluster_id_change(f):
    """
    This decorator ensures that the appropriate methods will retry if an
    etcd EtcdClusterIdChanged exception is raised. This ensures that if etcd
    is moved under their feet these methods don't fail, but instead do their
    best to succeed.

    To avoid infinite loops, however, these methods attempt to retry a finite
    number of times before abandoning the attempt.

    This decorator can only be applied to functions of the
    CalicoMechanismDriver.
    """
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        retries = 5
        while True:
            try:
                return f(self, *args, **kwargs)
            except etcd.EtcdClusterIdChanged:
                LOG.info("etcd cluster moved, retrying")
                retries -= 1
                if not retries:
                    raise

    return wrapper


class CalicoMechanismDriver(mech_agent.SimpleAgentMechanismDriverBase):
    """Neutron/ML2 mechanism driver for Project Calico.

    CalicoMechanismDriver communicates information about endpoints and security
    configuration, over the Endpoint and Network APIs respectively, to the
    other components of the Calico architecture; namely to the Felix instances
    running on each compute host.
    """

    def __init__(self):
        super(CalicoMechanismDriver, self).__init__(
            constants.AGENT_TYPE_DHCP,
            'tap',
            {'port_filter': True,
             'mac_address': '00:61:fe:ed:ca:fe'})

        # Initialize fields for the database object and transport.  We will
        # initialize these properly when we first need them.
        self.db = None
        self.transport = None
        self._my_pid = None
        self._periodic_resync_greenlet = None
        self._epoch = 0

        # Make sure we initialise even if we don't see any API calls.
        eventlet.spawn_after(STARTUP_DELAY_SECS, self._init_state)

    def _init_state(self):
        """
        Creates the connection state required for talking to the Neutron DB
        and to etcd. This is a no-op if it has been executed before.
        """
        current_pid = os.getpid()
        if self._my_pid == current_pid:
            # We've initialised our PID and it hasn't changed since last time,
            # nothing to do.
            return
        # else: either this is the first call or our PID has changed:
        # (re)initialise.

        if self._my_pid is not None:
            # This is unexpected but we can deal with it: Neutron should
            # fork before we trigger the first call to _init_state().
            LOG.warning("PID changed; unexpected fork after initialisation.  "
                        "Reinitialising Calico driver.")

        # Start our resynchronization process.  Just in case we ever get two
        # threads running, use an epoch counter to tell the old thread to die.
        # This is defensive: our greenlets don't actually seem to get forked
        # with the process.
        self._epoch += 1
        eventlet.spawn(self.periodic_resync_thread, self._epoch)

        # (Re)init the DB.
        self.db = None
        self._get_db()

        # Use Etcd-based transport.
        if self.transport:
            # If we've been forked then the old transport will incorrectly
            # share file handles with the other process.
            LOG.warning("Shutting down previous transport instance.")
            self.transport.stop()
        self.transport = CalicoTransportEtcd(self)

        self._my_pid = current_pid

    def _get_db(self):
        if not self.db:
            self.db = manager.NeutronManager.get_plugin()
            LOG.info("db = %s" % self.db)

            # Installer a notifier proxy in order to catch security group
            # changes, if we haven't already.
            if self.db.notifier.__class__ != CalicoNotifierProxy:
                self.db.notifier = CalicoNotifierProxy(self.db.notifier, self)
            else:
                # In case the notifier proxy already exists but the current
                # CalicoMechanismDriver instance has changed, ensure that the
                # notifier proxy will delegate to the current
                # CalicoMechanismDriver instance.
                self.db.notifier.calico_driver = self

    def check_segment_for_agent(self, segment, agent):
        LOG.debug("Checking segment %s with agent %s" % (segment, agent))
        if segment[api.NETWORK_TYPE] in ['local', 'flat']:
            return True
        else:
            return False

    def get_allowed_network_types(self, agent=None):
        return ('local', 'flat')

    def get_mappings(self, agent):
        # We override this primarily to satisfy the ABC checker: this method
        # never actually gets called because we also override
        # check_segment_for_agent.
        assert False

    def _port_is_endpoint_port(self, port):
        # Return True if port is a VM port.
        if port['device_owner'].startswith('compute:'):
            return True

        # Otherwise log and return False.
        LOG.debug("Not a VM port: %s" % port)
        return False

    # For network and subnet actions we have nothing to do, so we provide these
    # no-op methods.
    def create_network_postcommit(self, context):
        LOG.info("CREATE_NETWORK_POSTCOMMIT: %s" % context)

    def update_network_postcommit(self, context):
        LOG.info("UPDATE_NETWORK_POSTCOMMIT: %s" % context)

    def delete_network_postcommit(self, context):
        LOG.info("DELETE_NETWORK_POSTCOMMIT: %s" % context)

    def create_subnet_postcommit(self, context):
        LOG.info("CREATE_SUBNET_POSTCOMMIT: %s" % context)

    def update_subnet_postcommit(self, context):
        LOG.info("UPDATE_SUBNET_POSTCOMMIT: %s" % context)

    def delete_subnet_postcommit(self, context):
        LOG.info("DELETE_SUBNET_POSTCOMMIT: %s" % context)

    # Idealised method forms.
    @retry_on_cluster_id_change
    @requires_state
    def create_port_postcommit(self, context):
        """
        Called after Neutron has committed a port creation event to the
        database.

        Process this event by taking and holding a database transaction and
        re-reading the port. Once we do that, we know the port will remain
        unchanged while we hold the transaction. We can then write the port to
        etcd, along with any other information we may need (security profiles).
        """
        LOG.info('CREATE_PORT_POSTCOMMIT: %s', context)
        port = context._port

        # Immediately halt processing if this is not an endpoint port.
        if not self._port_is_endpoint_port(port):
            return

        # If the port binding VIF type is 'unbound', this port doesn't actually
        # need to be networked yet. We can simply return immediately.
        if port['binding:vif_type'] == 'unbound':
            LOG.info("Creating unbound port: no work required.")
            return

        with context._plugin_context.session.begin(subtransactions=True):
            # First, regain the current port. This protects against concurrent
            # writes breaking our state.
            port = self.db.get_port(context._plugin_context, port['id'])

            # Next, fill out other information we need on the port.
            self.add_port_gateways(port, context._plugin_context)
            self.add_port_interface_name(port)
            port['security_groups'] = self.get_security_groups_for_port(
                context._plugin_context, port
            )

            # Next, we need to work out what security profiles apply to this
            # port and grab information about it.
            profiles = self.get_security_profiles(
                context._plugin_context, port
            )

            # Pass this to the transport layer.
            # Implementation note: we could arguably avoid holding the
            # transaction for this length and instead release it here, then
            # use atomic CAS. The problem there is that we potentially have to
            # repeatedly respin and regain the transaction. Let's not do that
            # for now, and performance test to see if it's a problem later.
            self.transport.endpoint_created(port)

            for profile in profiles:
                self.transport.write_profile_to_etcd(profile)

            # Update Neutron that we succeeded.
            self.db.update_port_status(context._plugin_context,
                                       port['id'],
                                       constants.PORT_STATUS_ACTIVE)

    @retry_on_cluster_id_change
    @requires_state
    def update_port_postcommit(self, context):
        """
        Called after Neutron has committed a port update event to the
        database.

        This is a tricky event, because it can be called in a number of ways
        during VM migration. We farm out to the appropriate method from here.
        """
        LOG.info('UPDATE_PORT_POSTCOMMIT: %s', context)
        port = context._port
        original = context.original

        # Abort early if we're manging non-endpoint ports.
        if not self._port_is_endpoint_port(port):
            return

        # If this port update is purely for a status change, don't do anything:
        # we don't care about port statuses.
        if port_status_change(port, original):
            LOG.info('Called for port status change, no action.')
            return

        # Now, re-read the port.
        with context._plugin_context.session.begin(subtransactions=True):
            port = self.db.get_port(context._plugin_context, port['id'])

            # Now, fork execution based on the type of update we're performing.
            # There are a few:
            # - a port becoming bound (binding vif_type from unbound to bound);
            # - a port becoming unbound (binding vif_type from bound to
            #   unbound);
            # - an Icehouse migration (binding host id changed and port bound);
            # - an update (port bound at all times);
            # - a change to an unbound port (which we don't care about, because
            #   we do nothing with unbound ports).
            if port_bound(port) and not port_bound(original):
                self._port_bound_update(context, port)
            elif port_bound(original) and not port_bound(port):
                self._port_unbound_update(context, original)
            elif original['binding:host_id'] != port['binding:host_id']:
                LOG.info("Icehouse migration")
                self._icehouse_migration_step(context, port, original)
            elif port_bound(original) and port_bound(port):
                LOG.info("Port update")
                self._update_port(context, port)
            else:
                LOG.info("Update on unbound port: no action")
                pass

    @retry_on_cluster_id_change
    @requires_state
    def delete_port_postcommit(self, context):
        """
        Called after Neutron has committed a port deletion event to the
        database.

        There's no database row for us to lock on here, so don't bother.
        """
        LOG.info('DELETE_PORT_POSTCOMMIT: %s', context)
        port = context._port

        # Immediately halt processing if this is not an endpoint port.
        if not self._port_is_endpoint_port(port):
            return

        # Pass this to the transport layer.
        self.transport.endpoint_deleted(port)

    @retry_on_cluster_id_change
    @requires_state
    def send_sg_updates(self, sgids, context):
        """
        Called whenever security group rules or membership change.

        When a security group rule is added, we need to do the following steps:

        1. Reread the security rules from the Neutron DB.
        2. Write the profile to etcd.
        """
        LOG.info("Updating security group IDs %s", sgids)
        with context.session.begin(subtransactions=True):
            rules = self.db.get_security_group_rules(
                context, filters={'security_group_id': sgids}
            )

            # For each profile, build its object and send it down.
            # TODO: Sending this to etcd could legitimately fail because of a
            # CAS problem. Come back to handle retries.
            profiles = (
                profile_from_neutron_rules(sgid, rules) for sgid in sgids
            )

            for profile in profiles:
                self.transport.write_profile_to_etcd(profile)

    def _port_unbound_update(self, context, port):
        """
        This is called when a port is unbound during a port update. This
        destroys the port in etcd.
        """
        LOG.info("Port becoming unbound: destroy.")
        self.transport.endpoint_deleted(port)

    def _port_bound_update(self, context, port):
        """
        This is called when a port is bound during a port update. This creates
        the port in etcd.

        This method expects to be called from within a database transaction,
        and does not create one itself.
        """
        # TODO: Can we avoid re-writing the security profile here? Put another
        # way, does the security profile change during migration steps, or does
        # a separate port update event occur?
        LOG.info("Port becoming bound: create.")
        port = self.db.get_port(context._plugin_context, port['id'])
        self.add_port_gateways(port, context._plugin_context)
        self.add_port_interface_name(port)
        port['security_groups'] = self.get_security_groups_for_port(
            context._plugin_context, port
        )
        profiles = self.get_security_profiles(
            context._plugin_context, port
        )
        self.transport.endpoint_created(port)

        for profile in profiles:
            self.transport.write_profile_to_etcd(profile)

        # Update Neutron that we succeeded.
        self.db.update_port_status(context._plugin_context,
                                   port['id'],
                                   constants.PORT_STATUS_ACTIVE)

    def _icehouse_migration_step(self, context, port, original):
        """
        This is called when migrating on Icehouse. Here, we basically just
        perform an unbinding and a binding at exactly the same time, but we
        hold a DB lock the entire time.

        This method expects to be called from within a database transaction,
        and does not create one itself.
        """
        # TODO: Can we avoid re-writing the security profile here? Put another
        # way, does the security profile change during migration steps, or does
        # a separate port update event occur?
        LOG.info("Migration as implemented in Icehouse")
        self._port_unbound_update(context, original)
        self._port_bound_update(context, port)

    def _update_port(self, context, port):
        """
        Called during port updates that have nothing to do with migration.
        """
        # TODO: There's a lot of redundant code in these methods, with the only
        # key difference being taking out transactions. Come back and shorten
        # these.
        LOG.info("Updating port %s", port)

        # If the binding VIF type is unbound, we consider this port 'disabled',
        # and should attempt to delete it. Otherwise, the port is enabled:
        # re-process it.
        port_disabled = port['binding:vif_type'] == 'unbound'
        if not port_disabled:
            LOG.info("Port enabled, attempting to update.")

            with context._plugin_context.session.begin(subtransactions=True):
                port = self.db.get_port(context._plugin_context, port['id'])
                self.add_port_gateways(port, context._plugin_context)
                self.add_port_interface_name(port)
                port['security_groups'] = self.get_security_groups_for_port(
                    context._plugin_context, port
                )
                profiles = self.get_security_profiles(
                    context._plugin_context, port
                )
                self.transport.endpoint_created(port)

                for profile in profiles:
                    self.transport.write_profile_to_etcd(profile)

                # Update Neutron that we succeeded.
                self.db.update_port_status(context._plugin_context,
                                           port['id'],
                                           constants.PORT_STATUS_ACTIVE)
        else:
            # Port unbound, attempt to delete.
            LOG.info("Port disabled, attempting delete if needed.")
            self.transport.endpoint_deleted(port)

    def add_port_gateways(self, port, context):
        """
        Determine the gateway IP addresses for a given port's IP addresses, and
        adds them to the port dict.

        This method assumes it's being called from within a database
        transaction and does not take out another one.
        """
        for ip in port['fixed_ips']:
            subnet = self.db.get_subnet(context, ip['subnet_id'])
            ip['gateway'] = subnet['gateway_ip']

    def get_security_profiles(self, context, port):
        """
        Obtain information about the security profile that applies to a given
        port.

        This method expects to be called from within a database transaction,
        and does not create its own.

        :returns: A generator of ``SecurityProfile`` objects.
        """
        # For each security group get its rules. Given that we don't need
        # anything else about the security group, we can do this as a single
        # query.
        # CB2: I am concerned that this does not adequately prevent new
        # security group rules being added and racing us in.
        sgids = port['security_groups']
        rules = self.db.get_security_group_rules(
            context, filters={'security_group_id': sgids}
        )

        # Now, return a generator that provides profile objects for each
        # profile.
        return (
            profile_from_neutron_rules(sgid, rules) for sgid in sgids
        )

    def periodic_resync_thread(self, expected_epoch):
        """
        This method acts as a the periodic resynchronization logic for the
        Calico mechanism driver.

        On a fixed interval, it spins over the entire database and reconciles
        it with etcd, ensuring that the etcd database and Neutron are in
        synchronization with each other.
        """
        try:
            LOG.info("Periodic resync thread started")
            while self._epoch == expected_epoch:
                # Only do the resync logic if we're actually the master node.
                if self.transport.is_master:
                    LOG.info("I am master: doing periodic resync")
                    context = ctx.get_admin_context()

                    try:
                        # First, resync endpoints.
                        self.resync_endpoints(context)

                        # Second, profiles.
                        self.resync_profiles(context)

                        # Now, set the config flags.
                        self.transport.provide_felix_config()
                    except Exception:
                        LOG.exception("Error in periodic resync thread.")
                    # Reschedule ourselves.
                    eventlet.sleep(RESYNC_INTERVAL_SECS)
                else:
                    # Shorter sleep interval before we check if we've become
                    # the master.  Avoids waiting a whole RESYNC_INTERVAL_SECS
                    # if we just miss the master update.
                    eventlet.sleep(MASTER_CHECK_INTERVAL_SECS)
        except:
            # TODO Should we tear down the process.
            LOG.exception("Periodic resync thread died!")
            if self.transport:
                # Stop the transport so that we give up the mastership.
                self.transport.stop()
            raise
        else:
            LOG.warning("Periodic resync thread exiting.")

    def resync_endpoints(self, context):
        """
        Handles periodic resynchronization for endpoints.
        """
        LOG.info("Resyncing endpoints")

        # Work out all the endpoints in etcd. Do this outside a database
        # transaction to try to ensure that anything that gets created is in
        # our Neutron snapshot.
        endpoints = list(self.transport.get_endpoints())
        endpoint_ids = set(ep.id for ep in endpoints)

        # Then, grab all the ports from Neutron. Quickly work out whether
        # a given port is missing from etcd, or if etcd has too many ports.
        # Then, add all missing ports and remove all extra ones.
        # This explicit with statement is technically unnecessary, but it helps
        # keep our transaction scope really clear.
        with context.session.begin(subtransactions=True):
            ports = dict((port['id'], port)
                         for port in self.db.get_ports(context)
                         if self._port_is_endpoint_port(port))

        port_ids = set(ports.keys())
        missing_ports = port_ids - endpoint_ids
        extra_ports = endpoint_ids - port_ids

        # We need to do one more check: are any ports in the wrong place? The
        # way we handle this is to treat this as a port that is both missing
        # and extra, where the old version is extra and the new version is
        # missing.
        for endpoint in endpoints:
            try:
                port = ports[endpoint.id]
            except KeyError:
                # Port already in extra_ports.
                continue

            if endpoint.host != port['binding:host_id']:
                LOG.info(
                    "Port %s is incorrectly on %s, should be %s",
                    endpoint.id,
                    endpoint.host,
                    port['binding:host_id']
                )
                missing_ports.add(endpoint.id)
                extra_ports.add(endpoint.id)

        if missing_ports or extra_ports:
            LOG.info("Missing ports: %s", missing_ports)
            LOG.info("Extra ports: %s", extra_ports)

        # First, handle the extra ports. Each of them needs to be atomically
        # deleted.
        eps_to_delete = (e for e in endpoints if e.id in extra_ports)

        for endpoint in eps_to_delete:
            try:
                self.transport.atomic_delete_endpoint(endpoint)
            except (ValueError, etcd.EtcdKeyNotFound):
                # If the atomic CAD doesn't successfully delete, that's ok, it
                # means the endpoint was created or updated elsewhere.
                continue

        # Next, for each missing port, do a quick port creation. This takes out
        # a db transaction and regains all the ports. Note that this
        # transaction is potentially held for quite a while.
        with context.session.begin(subtransactions=True):
            missing_ports = self.db.get_ports(
                context, filters={'id': missing_ports}
            )

            for port in missing_ports:
                # Fill out other information we need on the port and write to
                # etcd.
                self.add_port_gateways(port, context)
                self.add_port_interface_name(port)
                port['security_groups'] = self.get_security_groups_for_port(
                    context, port
                )
                self.transport.endpoint_created(port)

    def resync_profiles(self, context):
        """
        Resynchronize security profiles.
        """
        LOG.info("Resyncing profiles")
        # Work out all the security groups in etcd. Do this outside a database
        # transaction to try to ensure that anything that gets created is in
        # our Neutron snapshot.
        profiles = list(self.transport.get_profiles())
        profile_ids = set(profile.id for profile in profiles)

        # Next, grab all the security groups from Neutron. Quickly work out
        # whether a given group is missing from etcd, or if etcd has too many
        # groups. Then, add all missing groups and remove all extra ones.
        # This explicit with statement is technically unnecessary, but it helps
        # keep our transaction scope really clear.
        with context.session.begin(subtransactions=True):
            sgs = self.db.get_security_groups(context)

        sgids = set(sg['id'] for sg in sgs)
        missing_groups = sgids - profile_ids
        extra_groups = profile_ids - sgids

        if missing_groups or extra_groups:
            LOG.info("Missing groups: %s", missing_groups)
            LOG.info("Extra groups: %s", extra_groups)

        # For each missing profile, do a quick profile creation. This takes out
        # a db transaction and regains all the rules. Note that this
        # transaction is potentially held for quite a while.
        with context.session.begin(subtransactions=True):
            rules = self.db.get_security_group_rules(
                context, filters={'security_group_id': missing_groups}
            )

            profiles_to_write = (
                profile_from_neutron_rules(sgid, rules)
                for sgid in missing_groups
            )

            for profile in profiles_to_write:
                self.transport.write_profile_to_etcd(profile)

        # Next, handle the extra profiles. Each of them needs to be atomically
        # deleted.
        profiles_to_delete = (p for p in profiles if p.id in extra_groups)

        for profile in profiles_to_delete:
            try:
                self.transport.atomic_delete_profile(profile)
            except (ValueError, etcd.EtcdKeyNotFound):
                # If the atomic CAD doesn't successfully delete, that's ok, it
                # means the profile was created or updated elsewhere.
                continue

    def add_port_interface_name(self, port):
        port['interface_name'] = 'tap' + port['id'][:11]

    def felix_status(self, hostname, up, start_flag):
        # Get a DB context for this processing.
        db_context = ctx.get_admin_context()

        if up:
            agent_state = {'agent_type': AGENT_TYPE_FELIX,
                           'binary': '',
                           'host': hostname,
                           'topic': constants.L2_AGENT_TOPIC}
            if start_flag:
                agent_state['start_flag'] = True
            self.db.create_or_update_agent(db_context, agent_state)

    def get_security_groups_for_port(self, context, port):
        """
        Checks which security groups apply for a given port.

        Frustratingly, the port dict provided to us when we call get_port may
        actually be out of date, and I don't know why. This change ensures that
        we get the most recent information.
        """
        filters = {'port_id': [port['id']]}
        bindings = self.db._get_port_security_group_bindings(
            context, filters=filters
        )
        return [binding['security_group_id'] for binding in bindings]


class CalicoNotifierProxy(object):
    """Proxy pattern class used to intercept security-related notifications
    from the ML2 plugin.
    """

    def __init__(self, ml2_notifier, calico_driver):
        self.ml2_notifier = ml2_notifier
        self.calico_driver = calico_driver

    def __getattr__(self, name):
        return getattr(self.ml2_notifier, name)

    def security_groups_rule_updated(self, context, sgids):
        LOG.info("security_groups_rule_updated: %s %s" % (context, sgids))
        self.calico_driver.send_sg_updates(sgids, context)
        self.ml2_notifier.security_groups_rule_updated(context, sgids)


def profile_from_neutron_rules(profile_id, rules):
    """
    Given a set of Neutron rules, build them into a ``SecurityProfile`` object.
    """
    # Split the rules based on direction.
    inbound_rules = []
    outbound_rules = []

    # Only use the rules that have the right profile id.
    sg_rules = (r for r in rules if r['security_group_id'] == profile_id)

    for rule in sg_rules:
        if rule['direction'] == 'ingress':
            inbound_rules.append(rule)
        else:
            outbound_rules.append(rule)

    return SecurityProfile(profile_id, inbound_rules, outbound_rules)


def port_status_change(port, original):
    """
    Checks whether a port update is being called for a port status change
    event.

    Port activation events are triggered by our own action: if the only change
    in the port dictionary is activation state, we don't want to do any
    processing.
    """
    # Be defensive here: if Neutron is going to use these port dicts later we
    # don't want to have taken away data they want. Take copies.
    port = port.copy()
    original = original.copy()

    port.pop('status')
    original.pop('status')

    if port == original:
        return True
    else:
        return False


def port_bound(port):
    """
    Returns true if the port is bound.
    """
    return port['binding:vif_type'] != 'unbound'
