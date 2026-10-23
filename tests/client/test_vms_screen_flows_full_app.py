"""Virtual Machines, end to end: the shipped client (vms.js / vmrpc.js) -> THIS node's real relay -> the SHIPPED
VM host (app/services/vmhost: Transport + VmHostService + VirshBackend) -> a hypervisor faked at its real API
boundary, the `virsh` command line.

  Virsh    VirshBackend's injectable runner (argv, timeout) -> (code, stdout, stderr). Every answer is shaped
           like the libvirt 12 output captured on nas.lan (tests/fixtures/vmhost_real/): dominfo, nodeinfo,
           version, domiflist, domblklist, metadata. `virsh shutdown` is a REQUEST to the guest, so the domain
           reads "in shutdown" once and then "shut off", as a real ACPI shutdown does. A refused start answers
           exit 1 with libvirt's own two-line error.
  Hosts    two VM hosts on the node's relay, each with its own node key: "Fixture host" is the instance's own
           (/client/config `vmhost`), "Lab host" is one a member ADDS (it lives only in their encrypted
           `pcai:vmhosts` document on the relay). Each subscribes with the shipped app relay client
           (app/services/nostr/relay.subscribe/publish) exactly as transport._run does on a node.
  Faults   a host that is SILENT for one account (its requests never reach the service: an offline host and a
           host that drops strangers look identical), a host that answers LATE, and the home relay never
           answering one device's read of the host list (discover_flows_harness.Gate).

There is no HTTP endpoint on this screen to fail with a 503: listing and power go over Nostr; the router
(app/routers/vmhost.py) only carries the console WebSocket, ISO upload and migration transfer.

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/vms.js=...".
"""
import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('vms',)
FIRST_FILL = 60           # 60 .. 83 (account keys are one byte)
SCENARIOS = {}
COST = {'a_host_that_stops_answering_keeps_the_last_list_and_says_so': 45,
        'a_fresh_device_whose_host_list_read_is_unanswered_never_writes_over_it': 40,
        'an_added_host_reaches_a_fresh_device_of_the_same_account': 30}
CAP = Path(__file__).resolve().parents[1] / 'fixtures' / 'vmhost_real'
INSTANCE_SK = bytes([0xE1]) * 32
LAB_SK = bytes([0xE2]) * 32


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def cap(name):
    return (CAP / (name + '.out')).read_text()


def vm_uuid(base, k):
    return '%08d-0000-4000-8000-%012d' % (base, k)


# ------------------------------------------------------------------------------------------------ the hypervisor
class Virsh:
    """The `virsh` CLI as VirshBackend runs it. `fail[(verb, uuid)]` = stderr of a refused command;
    `calls` = every argv after `--connect <uri>`."""

    def __init__(self):
        self.domains = {}        # uuid -> dict(name, state, vcpus, kib, meta)
        self.fail = {}
        self.calls = []

    def add(self, uuid, name, assigned, state='shut off'):
        from app.services.vmhost import domainxml
        self.domains[uuid] = dict(name=name, state=state, vcpus=2, kib=2097152,
                                  meta=domainxml.VmMeta(owner='', created=1789610241, disk_gib=20, assigned=list(assigned)))

    def dominfo(self, u):
        d = self.domains[u]
        running = d['state'] in ('running', 'in shutdown')
        out = ('Id:             %s\nName:           %s\nUUID:           %s\nOS Type:        hvm\nState:          %s\n'
               'CPU(s):         %d\n%sMax memory:     %d KiB\nUsed memory:    %d KiB\nPersistent:     yes\n'
               'Autostart:      disable\nAutostart Once: disable\nManaged save:   no\nSecurity model: none\n'
               'Security DOI:   0\n\n') % ('13' if running else '-', d['name'], u, d['state'], d['vcpus'],
                                         'CPU time:       0.0s\n' if running else '', d['kib'], d['kib'])
        if d['state'] == 'in shutdown':          # the guest has acted on the ACPI request by the next look
            d['state'] = 'shut off'
        return out

    async def run(self, argv, timeout):
        args = argv[3:]
        verb, u = args[0], (args[1] if len(args) > 1 else '')
        self.calls.append(tuple(args))
        if (verb, u) in self.fail:
            return 1, '', self.fail[(verb, u)]
        if verb == 'version':
            return 0, cap('virsh-version'), ''
        if verb == 'nodeinfo':
            return 0, cap('virsh-nodeinfo'), ''
        if verb == 'nodememstats':
            return 0, cap('virsh-nodememstats'), ''
        if verb == 'net-list':
            return 0, cap('virsh-net-list-names'), ''
        if verb == 'list':
            return 0, ''.join(x + '\n' for x in self.domains) + '\n', ''
        if u and u not in self.domains:
            return 1, '', "error: failed to get domain '%s'\n" % u
        d = self.domains.get(u)
        if verb == 'dominfo':
            return 0, self.dominfo(u), ''
        if verb == 'metadata':
            return 0, d['meta'].to_xml(prefixed=False) + '\n', ''
        if verb == 'domblklist':
            return 0, (' Type   Device   Target   Source\n' + '-' * 60 + '\n file   disk     vda      /var/lib/posterchan/vms/%s/disk-vda.qcow2\n\n' % u), ''
        if verb == 'domiflist':
            return 0, cap('virsh-domiflist-inactive'), ''
        if verb == 'dumpxml':             # the captured definition, renamed to this domain
            x = cap('virsh-dumpxml-inactive-efi').replace('pcprobe-d14cd2', d['name'])
            x = x.replace('00000000-0000-4000-8000-000000000002', u)
            x = re.sub(r'<pc:vm [^>]*/>', lambda _m: d['meta'].to_xml(prefixed=True), x)
            return 0, x, ''
        if verb == 'domifaddr':
            return 0, ' Name       MAC address          Protocol     Address\n' + '-' * 64 + '\n\n', ''
        if verb == 'start':
            if d['state'] != 'shut off':
                return 1, '', 'error: Failed to start domain \'%s\'\nerror: Requested operation is not valid: domain is already running\n' % u
            d['state'] = 'running'
            return 0, "Domain '%s' started\n\n" % u, ''
        if verb == 'shutdown':
            d['state'] = 'in shutdown'
            return 0, "Domain '%s' is being shutdown\n\n" % u, ''
        if verb == 'destroy':
            d['state'] = 'shut off'
            return 0, "Domain '%s' destroyed\n\n" % u, ''
        return 1, '', "error: unknown command: '%s'\n" % verb


# ------------------------------------------------------------------------------------------------ a VM host
class Host:
    """The shipped host stack on this node's relay, as transport._run starts it -- minus the settings and the lock."""

    def __init__(self, n, sk, name, allowed):
        from app.services.nostr import bip340
        from app.services.vmhost import transport
        from app.services.vmhost.backend import VirshBackend
        from app.services.vmhost.config import VmHostConfig
        from app.services.vmhost.service import VmHostService
        from app.services.vmhost.storage import Storage
        from app.services.nostr import relay as nostr_relay
        self.n, self.name = n, name
        self.pk = bip340.pubkey_from_seckey(sk).hex()
        self.dir = tempfile.mkdtemp(prefix='pc-vmhost-')
        self.virsh = Virsh()
        self.silent, self.late = set(), {}      # requester pubkeys this host never hears / hears N s late
        self.heard = []
        storage = Storage(self.dir)
        storage.ensure()
        cfg = VmHostConfig(enabled=True, display_name=name, storage_dir=self.dir, allowed_pubkeys=list(allowed))

        async def no_admins():
            return set()
        self.svc = VmHostService(cfg, VirshBackend(runner=self.virsh.run), node_pubkey=self.pk,
                                 admin_provider=no_admins, storage=storage)
        url = n.home.url

        async def publish(ev):
            return bool(await nostr_relay.publish(url, ev, direct=True))
        self.tr = transport.Transport(self.svc, sk, publish)
        self.stop_ev = None

        async def handler(ev):
            who = ev.get('pubkey')
            self.heard.append(who)
            if who in self.silent:
                return
            if who in self.late:        # heard late -- without holding up the subscription for anyone else
                async def later(s=self.late[who]):
                    await asyncio.sleep(s)
                    self.tr.spawn(ev)
                asyncio.ensure_future(later())
                return
            self.tr.spawn(ev)

        async def start():
            import time
            self.stop_ev = asyncio.Event()
            flt = [{'kinds': [5310], '#p': [self.pk], 'since': int(time.time()) - 120}]
            self.task = asyncio.ensure_future(nostr_relay.subscribe(url, flt, handler, self.stop_ev, direct=True))
        n.loop.run(start())

    def stop(self):
        try:
            self.n.loop.loop.call_soon_threadsafe(self.stop_ev.set)
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


class Ctx:
    def __init__(self, n):
        self.n = n
        self.gate = F.Gate(n.home)
        accounts = [R.pubkey(FIRST_FILL + i) for i in range(4 * len(SCENARIOS))]
        self.inst = Host(n, INSTANCE_SK, 'Fixture host', accounts)
        self.lab = Host(n, LAB_SK, 'Lab host', accounts)
        R.NODE['config']['vmhost'] = {'pubkey': self.inst.pk, 'relay': n.home.url, 'name': 'Fixture host', 'https': ''}
        for i, name in enumerate(SCENARIOS):
            base = FIRST_FILL + 4 * i
            me = R.pubkey(base)
            self.inst.virsh.add(vm_uuid(base, 1), 'alpha-%d' % base, [me])
            self.inst.virsh.add(vm_uuid(base, 2), 'beta-%d' % base, [me])
            self.lab.virsh.add(vm_uuid(base, 3), 'lab-%d' % base, [me], state='running')
        for h in (self.inst, self.lab):
            n.loop.run(h.svc.refresh_index())

    def stop(self):
        self.inst.stop()
        self.lab.stop()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


# ------------------------------------------------------------------------------------------------ what a person does
FEED = "((document.querySelector('#feed')||{}).innerText||'')"


async def open_vms(d):
    await F.view(d, 'vms', "!!document.querySelector('#feed .vms')")


async def open_host(d, pk):
    sel = '#feed .vms-host[data-host="%s"]' % pk
    await F.wait(d, '!!document.querySelector(%s)' % json.dumps(sel), 30, 'the host card')
    await d.b.js('document.querySelector(%s).click();true' % json.dumps(sel))


async def open_vm(d, uuid):
    sel = '#feed .vms-vm[data-vm="%s"]' % uuid
    await F.wait(d, '!!document.querySelector(%s)' % json.dumps(sel), 30, 'the VM in the list')
    await d.b.js('document.querySelector(%s).click();true' % json.dumps(sel))
    await F.wait(d, "!!document.querySelector('#feed .vms-vmhead')", 15, 'the VM screen')


async def pill(d):
    return await d.b.js("(document.querySelector('#feed .vms-vmhead .vms-pill')||{}).textContent||''")


async def press_power(d, action):
    sel = '#feed [data-power="%s"]' % action
    await F.wait(d, '(b=>b&&!b.disabled)(document.querySelector(%s))' % json.dumps(sel), 20, 'the %s button' % action)
    await d.b.js('document.querySelector(%s).click();true' % json.dumps(sel))


async def answer_prompt(d, value):
    await F.wait(d, "!!document.querySelector('.uiconfirm .uiprompt-in')", 15, 'the prompt')
    await d.b.js("(v=>{const i=document.querySelector('.uiconfirm .uiprompt-in');i.value=v;const c=i.closest('.uiconfirm');"
                 "c.dataset.answered='1';c.querySelector('[data-uc=\"1\"]').click();return true})(%s)" % json.dumps(value))
    await F.wait(d, "!document.querySelector('.uiconfirm[data-answered]')", 10, 'the prompt closed')


async def add_host(d, host):
    from app.services.nostr import nostr_service
    await F.wait(d, "!!document.querySelector('#feed [data-act=add]')", 20, 'the Add host button')
    await d.b.js("document.querySelector('#feed [data-act=add]').click();true")
    await answer_prompt(d, nostr_service.npub_of(host.pk))
    await answer_prompt(d, host.n.home.url)


def host_docs(n, pk):
    return F.stored(n, {'kinds': [30078], 'authors': [pk], '#d': ['pcai:vmhosts']})


# ------------------------------------------------------------------------------------------------ scenarios
@scenario
async def the_host_lists_my_vms_and_one_starts_and_shuts_down(S):
    """Phone: the instance host -> its VMs (only mine) -> alpha -> Start (virsh start) -> Running ->
    Shut down (virsh shutdown, an ACPI request) -> Stopping -> Off on the next look."""
    v = S.ctx.inst.virsh
    u = vm_uuid(S.base, 1)
    d = await S.device(0, phone=True)
    await open_vms(d)
    await open_host(d, S.ctx.inst.pk)
    await F.wait(d, "%s.includes('alpha-%d') && %s.includes('beta-%d')" % (FEED, S.base, FEED, S.base), 30, 'my two VMs listed')
    text = await d.b.js(FEED)
    others = [n for n in ('alpha-%d' % (FIRST_FILL + 4 * i) for i in range(len(SCENARIOS))) if n != 'alpha-%d' % S.base]
    assert not [o for o in others if o in text], ('another member\'s VMs listed', text)
    await open_vm(d, u)
    assert await pill(d) == 'Off'
    await press_power(d, 'start')
    await F.wait(d, "(document.querySelector('#feed .vms-vmhead .vms-pill')||{}).textContent==='Running'", 30, 'Running after Start')
    assert ('start', u) in v.calls and v.domains[u]['state'] == 'running'
    assert 'Starting' in await d.toasts()
    await press_power(d, 'shutdown')
    await F.until(lambda: ('shutdown', u) in v.calls, 30, 'virsh shutdown', d)
    await F.wait(d, "(document.querySelector('#feed .vms-vmhead .vms-pill')||{}).textContent==='Off'", 30, 'Off once the guest stopped')
    assert v.calls.count(('start', u)) == 1


@CHROME
def test_the_host_lists_my_vms_and_one_starts_and_shuts_down(ran):
    ran('the_host_lists_my_vms_and_one_starts_and_shuts_down')


@scenario
async def a_start_the_hypervisor_refuses_says_why_and_the_vm_stays_off(S):
    """libvirt refuses the start (no KVM module): its own message is shown, the VM is not drawn as running,
    and the Start button comes back for another try -- which works once libvirt does."""
    v = S.ctx.inst.virsh
    u = vm_uuid(S.base, 2)
    v.fail[('start', u)] = ("error: Failed to start domain '%s'\nerror: internal error: process exited while connecting "
                            "to monitor: Could not access KVM kernel module: No such file or directory\n" % u)
    d = await S.device(0)
    await open_vms(d)
    await open_host(d, S.ctx.inst.pk)
    await open_vm(d, u)
    await press_power(d, 'start')
    await F.until(lambda: ('start', u) in v.calls, 30, 'virsh start asked', d)
    await F.wait(d, "(window.__toastLog||[]).some(t=>t.includes('Could not access KVM kernel module'))", 20, 'libvirt\'s reason shown')
    await F.wait(d, "(b=>b&&!b.disabled&&b.textContent.includes('Start'))(document.querySelector('#feed [data-power=start]'))", 15, 'Start offered again')
    assert await pill(d) == 'Off'
    assert not [t for t in await d.toasts() if t == 'Starting']
    del v.fail[('start', u)]
    await press_power(d, 'start')
    await F.wait(d, "(document.querySelector('#feed .vms-vmhead .vms-pill')||{}).textContent==='Running'", 30, 'Running on the second try')


@CHROME
def test_a_start_the_hypervisor_refuses_says_why_and_the_vm_stays_off(ran):
    ran('a_start_the_hypervisor_refuses_says_why_and_the_vm_stays_off')


@scenario
async def a_slow_host_shows_the_press_and_a_second_press_does_not_start_twice(S):
    """The host takes 4 s to hear the request: the button shows it is working (…, disabled), pressing again does
    nothing, and the answer, when it comes, is drawn."""
    host, v = S.ctx.inst, S.ctx.inst.virsh
    u = vm_uuid(S.base, 1)
    d = await S.device(0)
    await open_vms(d)
    await open_host(d, host.pk)
    await open_vm(d, u)
    host.late[S.pk()] = 4
    await press_power(d, 'start')
    await F.wait(d, "(b=>b&&b.disabled&&b.textContent.includes('…'))(document.querySelector('#feed [data-power=start]'))", 5, 'the press shown')
    await d.b.js("document.querySelector('#feed [data-power=start]').click();true")
    await F.wait(d, "(document.querySelector('#feed .vms-vmhead .vms-pill')||{}).textContent==='Running'", 30, 'Running once the host answered')
    host.late.pop(S.pk(), None)
    assert v.calls.count(('start', u)) == 1, v.calls


@CHROME
def test_a_slow_host_shows_the_press_and_a_second_press_does_not_start_twice(ran):
    ran('a_slow_host_shows_the_press_and_a_second_press_does_not_start_twice')


@scenario
async def a_host_that_stops_answering_keeps_the_last_list_and_says_so(S):
    """The host answered once; then it goes silent. Coming back (a reload: the cache paints first) shows the last
    list marked as such and "No answer" with a Retry -- never "no VMs". Pressing Start says nothing was
    confirmed. When the host is back, Retry brings it current."""
    host = S.ctx.inst
    d = await S.device(0, phone=True)
    await open_vms(d)
    await open_host(d, host.pk)
    await F.wait(d, "%s.includes('alpha-%d')" % (FEED, S.base), 30, 'the list while the host answers')
    host.silent.add(S.pk())
    await d.b.js('location.reload();true')
    await F.wait(d, '!!window.__PC && !!__PC.me()', 30, 'reloaded')
    await open_vms(d)
    await open_host(d, host.pk)
    await F.wait(d, "%s.includes('alpha-%d')" % (FEED, S.base), 10, 'the cached list painted at once')
    await F.wait(d, "!!document.querySelector('#feed .vms-noanswer')", 40, 'No answer said')
    text = await d.b.js(FEED)
    assert 'alpha-%d' % S.base in text and 'Showing what it said' in text, text
    assert 'No VMs are assigned' not in text and 'No virtual machines' not in text, text
    host.silent.discard(S.pk())
    await d.b.js("document.querySelector('#feed .vms-noanswer [data-act=retry]').click();true")
    await F.wait(d, "!document.querySelector('#feed .vms-noanswer') && %s.includes('Last answered')" % FEED, 30, 'current again after Retry')


@CHROME
def test_a_host_that_stops_answering_keeps_the_last_list_and_says_so(ran):
    ran('a_host_that_stops_answering_keeps_the_last_list_and_says_so')


@scenario
async def an_added_host_reaches_a_fresh_device_of_the_same_account(S):
    """Add host (npub, then relay) -> the encrypted pcai:vmhosts document on the relay -> a FRESH device of the
    account lists Lab host and the VM on it, read from that host."""
    lab = S.ctx.lab
    a = await S.device(0)
    await open_vms(a)
    await F.wait(a, "%s.includes('Fixture host')" % FEED, 30, 'the instance host')
    await add_host(a, lab)
    await F.until(lambda: host_docs(S.n, S.pk()), 30, 'the host list on the relay', a)
    await F.wait(a, "%s.includes('lab-%d')" % (FEED, S.base), 30, 'the added host\'s VM')
    b = await S.device(0, phone=True)
    await open_vms(b)
    await open_host(b, lab.pk)
    await F.wait(b, "%s.includes('lab-%d') && %s.includes('Running')" % (FEED, S.base, FEED), 30, 'Lab host and its VM on a fresh device')


@CHROME
def test_an_added_host_reaches_a_fresh_device_of_the_same_account(ran):
    ran('an_added_host_reaches_a_fresh_device_of_the_same_account')


@scenario
async def a_fresh_device_whose_host_list_read_is_unanswered_never_writes_over_it(S):
    """The account's list holds Lab host. A fresh device whose read of it nobody answers must not look like
    the list is just the instance host without saying so, must not publish over it when a host is added there,
    and must pick the saved list up once the relay answers -- without a reload."""
    lab, gate = S.ctx.lab, S.ctx.gate
    a = await S.device(0)
    await open_vms(a)
    await add_host(a, lab)
    await F.until(lambda: host_docs(S.n, S.pk()), 30, 'the host list on the relay', a)
    before = host_docs(S.n, S.pk())[0]['id']
    tag = 'vms%d' % S.base
    me = S.pk()
    rule = gate.add(lambda flt, conn: 'never' if F.device_of(conn) == tag and F.asks(flt, kinds=[30078], authors=[me]) else None)
    b = await S.device(0, tag=tag)
    await open_vms(b)
    await F.wait(b, "%s.includes('Fixture host')" % FEED, 30, 'the instance host')
    # The read gives up (6 s): the screen says the saved hosts could not be read, it does not just leave them out.
    await F.wait(b, "!!document.querySelector('#feed .vms-docerr')", 20, 'the unread host list said')
    await add_host(b, S.ctx.inst)          # a host it already has: the list is not changed, but it is SAVED
    await F.wait(b, "(window.__toastLog||[]).some(t=>t.includes('could not be read'))", 20, 'the save refused')
    assert [e['id'] for e in host_docs(S.n, S.pk())] == [before], 'a device that never read the list wrote over it'
    gate.drop(rule)
    await F.view(b, 'home', "!!document.querySelector('#feed')")
    await open_vms(b)
    await F.wait(b, "%s.includes('Lab host') && !document.querySelector('#feed .vms-docerr')" % FEED, 30,
                 'the saved list picked up once the relay answered')
    assert [e['id'] for e in host_docs(S.n, S.pk())] == [before]


@CHROME
def test_a_fresh_device_whose_host_list_read_is_unanswered_never_writes_over_it(ran):
    ran('a_fresh_device_whose_host_list_read_is_unanswered_never_writes_over_it')
