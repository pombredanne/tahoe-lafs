
# This contains a test harness that creates a full Tahoe grid in a single
# process (actually in a single MultiService) which does not use the network.
# It does not use an Introducer, and there are no foolscap Tubs. Each storage
# server puts real shares on disk, but is accessed through loopback
# RemoteReferences instead of over serialized SSL. It is not as complete as
# the common.SystemTestMixin framework (which does use the network), but
# should be considerably faster: on my laptop, it takes 50-80ms to start up,
# whereas SystemTestMixin takes close to 2s.

# This should be useful for tests which want to examine and/or manipulate the
# uploaded shares, checker/verifier/repairer tests, etc. The clients have no
# Tubs, so it is not useful for tests that involve a Helper or the
# control.furl .

import os
from zope.interface import implements
from twisted.application import service
from twisted.internet import defer, reactor
from twisted.python.failure import Failure
from foolscap.api import Referenceable, fireEventually, RemoteException
from base64 import b32encode
from allmydata import uri as tahoe_uri
from allmydata.client import Client
from allmydata.storage.server import StorageServer, storage_index_to_dir
from allmydata.util import fileutil, idlib, hashutil
from allmydata.util.hashutil import sha1
from allmydata.test.common_web import HTTPClientGETFactory
from allmydata.interfaces import IStorageBroker, IServer
from allmydata.test.common import TEST_RSA_KEY_SIZE


class IntentionalError(Exception):
    pass

class Marker:
    pass

class LocalWrapper:
    def __init__(self, original):
        self.original = original
        self.broken = False
        self.hung_until = None
        self.post_call_notifier = None
        self.disconnectors = {}
        self.counter_by_methname = {}

    def _clear_counters(self):
        self.counter_by_methname = {}

    def callRemoteOnly(self, methname, *args, **kwargs):
        d = self.callRemote(methname, *args, **kwargs)
        del d # explicitly ignored
        return None

    def callRemote(self, methname, *args, **kwargs):
        # this is ideally a Membrane, but that's too hard. We do a shallow
        # wrapping of inbound arguments, and per-methodname wrapping of
        # selected return values.
        def wrap(a):
            if isinstance(a, Referenceable):
                return LocalWrapper(a)
            else:
                return a
        args = tuple([wrap(a) for a in args])
        kwargs = dict([(k,wrap(kwargs[k])) for k in kwargs])

        def _really_call():
            def incr(d, k): d[k] = d.setdefault(k, 0) + 1
            incr(self.counter_by_methname, methname)
            meth = getattr(self.original, "remote_" + methname)
            return meth(*args, **kwargs)

        def _call():
            if self.broken:
                if self.broken is not True: # a counter, not boolean
                    self.broken -= 1
                raise IntentionalError("I was asked to break")
            if self.hung_until:
                d2 = defer.Deferred()
                self.hung_until.addCallback(lambda ign: _really_call())
                self.hung_until.addCallback(lambda res: d2.callback(res))
                def _err(res):
                    d2.errback(res)
                    return res
                self.hung_until.addErrback(_err)
                return d2
            return _really_call()

        d = fireEventually()
        d.addCallback(lambda res: _call())
        def _wrap_exception(f):
            return Failure(RemoteException(f))
        d.addErrback(_wrap_exception)
        def _return_membrane(res):
            # rather than complete the difficult task of building a
            # fully-general Membrane (which would locate all Referenceable
            # objects that cross the simulated wire and replace them with
            # wrappers), we special-case certain methods that we happen to
            # know will return Referenceables.
            if methname == "allocate_buckets":
                (alreadygot, allocated) = res
                for shnum in allocated:
                    allocated[shnum] = LocalWrapper(allocated[shnum])
            if methname == "get_buckets":
                for shnum in res:
                    res[shnum] = LocalWrapper(res[shnum])
            return res
        d.addCallback(_return_membrane)
        if self.post_call_notifier:
            d.addCallback(self.post_call_notifier, self, methname)
        return d

    def notifyOnDisconnect(self, f, *args, **kwargs):
        m = Marker()
        self.disconnectors[m] = (f, args, kwargs)
        return m
    def dontNotifyOnDisconnect(self, marker):
        del self.disconnectors[marker]

def wrap_storage_server(original):
    # Much of the upload/download code uses rref.version (which normally
    # comes from rrefutil.add_version_to_remote_reference). To avoid using a
    # network, we want a LocalWrapper here. Try to satisfy all these
    # constraints at the same time.
    wrapper = LocalWrapper(original)
    wrapper.version = original.remote_get_version()
    return wrapper

class NoNetworkServer:
    implements(IServer)
    def __init__(self, serverid, rref):
        self.serverid = serverid
        self.rref = rref
    def __repr__(self):
        return "<NoNetworkServer for %s>" % self.get_name()
    # Special method used by copy.copy() and copy.deepcopy(). When those are
    # used in allmydata.immutable.filenode to copy CheckResults during
    # repair, we want it to treat the IServer instances as singletons.
    def __copy__(self):
        return self
    def __deepcopy__(self, memodict):
        return self
    def get_serverid(self):
        return self.serverid
    def get_permutation_seed(self):
        return self.serverid
    def get_lease_seed(self):
        return self.serverid
    def get_foolscap_write_enabler_seed(self):
        return self.serverid

    def get_name(self):
        return idlib.shortnodeid_b2a(self.serverid)
    def get_longname(self):
        return idlib.nodeid_b2a(self.serverid)
    def get_nickname(self):
        return "nickname"
    def get_rref(self):
        return self.rref
    def get_version(self):
        return self.rref.version

class NoNetworkStorageBroker:
    implements(IStorageBroker)
    def get_servers_for_psi(self, peer_selection_index):
        def _permuted(server):
            seed = server.get_permutation_seed()
            return sha1(peer_selection_index + seed).digest()
        return sorted(self.get_connected_servers(), key=_permuted)
    def get_connected_servers(self):
        return self.client._servers
    def get_nickname_for_serverid(self, serverid):
        return None

class NoNetworkClient(Client):
    def create_tub(self):
        pass
    def init_introducer_client(self):
        pass
    def setup_logging(self):
        pass
    def startService(self):
        service.MultiService.startService(self)
    def stopService(self):
        service.MultiService.stopService(self)
    def init_control(self):
        pass
    def init_helper(self):
        pass
    def init_key_gen(self):
        pass
    def init_storage(self):
        pass
    def init_client_storage_broker(self):
        self.storage_broker = NoNetworkStorageBroker()
        self.storage_broker.client = self
    def init_stub_client(self):
        pass
    #._servers will be set by the NoNetworkGrid which creates us

class SimpleStats:
    def __init__(self):
        self.counters = {}
        self.stats_producers = []

    def count(self, name, delta=1):
        val = self.counters.setdefault(name, 0)
        self.counters[name] = val + delta

    def register_producer(self, stats_producer):
        self.stats_producers.append(stats_producer)

    def get_stats(self):
        stats = {}
        for sp in self.stats_producers:
            stats.update(sp.get_stats())
        ret = { 'counters': self.counters, 'stats': stats }
        return ret

class NoNetworkGrid(service.MultiService):
    def __init__(self, basedir, num_clients=1, num_servers=10,
                 client_config_hooks={}):
        service.MultiService.__init__(self)
        self.basedir = basedir
        fileutil.make_dirs(basedir)

        self.servers_by_number = {} # maps to StorageServer instance
        self.wrappers_by_id = {} # maps to wrapped StorageServer instance
        self.proxies_by_id = {} # maps to IServer on which .rref is a wrapped
                                # StorageServer
        self.clients = []

        for i in range(num_servers):
            ss = self.make_server(i)
            self.add_server(i, ss)
        self.rebuild_serverlist()

        for i in range(num_clients):
            clientid = hashutil.tagged_hash("clientid", str(i))[:20]
            clientdir = os.path.join(basedir, "clients",
                                     idlib.shortnodeid_b2a(clientid))
            fileutil.make_dirs(clientdir)
            f = open(os.path.join(clientdir, "tahoe.cfg"), "w")
            f.write("[node]\n")
            f.write("nickname = client-%d\n" % i)
            f.write("web.port = tcp:0:interface=127.0.0.1\n")
            f.write("[storage]\n")
            f.write("enabled = false\n")
            f.close()
            c = None
            if i in client_config_hooks:
                # this hook can either modify tahoe.cfg, or return an
                # entirely new Client instance
                c = client_config_hooks[i](clientdir)
            if not c:
                c = NoNetworkClient(clientdir)
                c.set_default_mutable_keysize(TEST_RSA_KEY_SIZE)
            c.nodeid = clientid
            c.short_nodeid = b32encode(clientid).lower()[:8]
            c._servers = self.all_servers # can be updated later
            c.setServiceParent(self)
            self.clients.append(c)

    def make_server(self, i, readonly=False):
        serverid = hashutil.tagged_hash("serverid", str(i))[:20]
        serverdir = os.path.join(self.basedir, "servers",
                                 idlib.shortnodeid_b2a(serverid), "storage")
        fileutil.make_dirs(serverdir)
        ss = StorageServer(serverdir, serverid, stats_provider=SimpleStats(),
                           readonly_storage=readonly)
        ss._no_network_server_number = i
        return ss

    def add_server(self, i, ss):
        # to deal with the fact that all StorageServers are named 'storage',
        # we interpose a middleman
        middleman = service.MultiService()
        middleman.setServiceParent(self)
        ss.setServiceParent(middleman)
        serverid = ss.my_nodeid
        self.servers_by_number[i] = ss
        wrapper = wrap_storage_server(ss)
        self.wrappers_by_id[serverid] = wrapper
        self.proxies_by_id[serverid] = NoNetworkServer(serverid, wrapper)
        self.rebuild_serverlist()

    def get_all_serverids(self):
        return self.proxies_by_id.keys()

    def rebuild_serverlist(self):
        self.all_servers = frozenset(self.proxies_by_id.values())
        for c in self.clients:
            c._servers = self.all_servers

    def remove_server(self, serverid):
        # it's enough to remove the server from c._servers (we don't actually
        # have to detach and stopService it)
        for i,ss in self.servers_by_number.items():
            if ss.my_nodeid == serverid:
                del self.servers_by_number[i]
                break
        del self.wrappers_by_id[serverid]
        del self.proxies_by_id[serverid]
        self.rebuild_serverlist()
        return ss

    def break_server(self, serverid, count=True):
        # mark the given server as broken, so it will throw exceptions when
        # asked to hold a share or serve a share. If count= is a number,
        # throw that many exceptions before starting to work again.
        self.wrappers_by_id[serverid].broken = count

    def hang_server(self, serverid):
        # hang the given server
        ss = self.wrappers_by_id[serverid]
        assert ss.hung_until is None
        ss.hung_until = defer.Deferred()

    def unhang_server(self, serverid):
        # unhang the given server
        ss = self.wrappers_by_id[serverid]
        assert ss.hung_until is not None
        ss.hung_until.callback(None)
        ss.hung_until = None

    def nuke_from_orbit(self):
        """ Empty all share directories in this grid. It's the only way to be sure ;-) """
        for server in self.servers_by_number.values():
            for prefixdir in os.listdir(server.sharedir):
                if prefixdir != 'incoming':
                    fileutil.rm_dir(os.path.join(server.sharedir, prefixdir))


class GridTestMixin:
    def setUp(self):
        self.s = service.MultiService()
        self.s.startService()

    def tearDown(self):
        return self.s.stopService()

    def set_up_grid(self, num_clients=1, num_servers=10,
                    client_config_hooks={}):
        # self.basedir must be set
        self.g = NoNetworkGrid(self.basedir,
                               num_clients=num_clients,
                               num_servers=num_servers,
                               client_config_hooks=client_config_hooks)
        self.g.setServiceParent(self.s)
        self.client_webports = [c.getServiceNamed("webish").getPortnum()
                                for c in self.g.clients]
        self.client_baseurls = [c.getServiceNamed("webish").getURL()
                                for c in self.g.clients]

    def get_clientdir(self, i=0):
        return self.g.clients[i].basedir

    def get_serverdir(self, i):
        return self.g.servers_by_number[i].storedir

    def iterate_servers(self):
        for i in sorted(self.g.servers_by_number.keys()):
            ss = self.g.servers_by_number[i]
            yield (i, ss, ss.storedir)

    def find_uri_shares(self, uri):
        si = tahoe_uri.from_string(uri).get_storage_index()
        prefixdir = storage_index_to_dir(si)
        shares = []
        for i,ss in self.g.servers_by_number.items():
            serverid = ss.my_nodeid
            basedir = os.path.join(ss.sharedir, prefixdir)
            if not os.path.exists(basedir):
                continue
            for f in os.listdir(basedir):
                try:
                    shnum = int(f)
                    shares.append((shnum, serverid, os.path.join(basedir, f)))
                except ValueError:
                    pass
        return sorted(shares)

    def copy_shares(self, uri):
        shares = {}
        for (shnum, serverid, sharefile) in self.find_uri_shares(uri):
            shares[sharefile] = open(sharefile, "rb").read()
        return shares

    def restore_all_shares(self, shares):
        for sharefile, data in shares.items():
            open(sharefile, "wb").write(data)

    def delete_share(self, (shnum, serverid, sharefile)):
        os.unlink(sharefile)

    def delete_shares_numbered(self, uri, shnums):
        for (i_shnum, i_serverid, i_sharefile) in self.find_uri_shares(uri):
            if i_shnum in shnums:
                os.unlink(i_sharefile)

    def delete_all_shares(self, serverdir):
        sharedir = os.path.join(serverdir, "shares")
        for prefixdir in os.listdir(sharedir):
            if prefixdir != 'incoming':
                fileutil.rm_dir(os.path.join(sharedir, prefixdir))

    def corrupt_share(self, (shnum, serverid, sharefile), corruptor_function):
        sharedata = open(sharefile, "rb").read()
        corruptdata = corruptor_function(sharedata)
        open(sharefile, "wb").write(corruptdata)

    def corrupt_shares_numbered(self, uri, shnums, corruptor, debug=False):
        for (i_shnum, i_serverid, i_sharefile) in self.find_uri_shares(uri):
            if i_shnum in shnums:
                sharedata = open(i_sharefile, "rb").read()
                corruptdata = corruptor(sharedata, debug=debug)
                open(i_sharefile, "wb").write(corruptdata)

    def corrupt_all_shares(self, uri, corruptor, debug=False):
        for (i_shnum, i_serverid, i_sharefile) in self.find_uri_shares(uri):
            sharedata = open(i_sharefile, "rb").read()
            corruptdata = corruptor(sharedata, debug=debug)
            open(i_sharefile, "wb").write(corruptdata)

    def GET(self, urlpath, followRedirect=False, return_response=False,
            method="GET", clientnum=0, **kwargs):
        # if return_response=True, this fires with (data, statuscode,
        # respheaders) instead of just data.
        assert not isinstance(urlpath, unicode)
        url = self.client_baseurls[clientnum] + urlpath
        factory = HTTPClientGETFactory(url, method=method,
                                       followRedirect=followRedirect, **kwargs)
        reactor.connectTCP("localhost", self.client_webports[clientnum],factory)
        d = factory.deferred
        def _got_data(data):
            return (data, factory.status, factory.response_headers)
        if return_response:
            d.addCallback(_got_data)
        return factory.deferred

    def PUT(self, urlpath, **kwargs):
        return self.GET(urlpath, method="PUT", **kwargs)
