"""Received disks are PROBED before they are defined: a qcow2 can name a second file on the TARGET host.

A qcow2 header carries a backing-file name and (qcow2 v3, `data_file` incompatible feature) an external data file.
Both are paths opened by qemu on the host that runs the VM — so a hostile source that sends a 3 MiB qcow2 whose
backing file is `/dev/sda` or `/var/lib/posterchan/vms/<victim>/disk-vda.qcow2` hands the guest a read-through view
of that host file. The source's own header check (`qcow2_has_backing`) protects nothing on the target: the target
must ask qemu-img itself, then pin libvirt's `<driver type>` to what it measured (libvirt never probes then).

The JSON below is `qemu-img info --output=json` as qemu 8.x/9.x prints it (QAPI `ImageInfo`, block/qapi.c):
top-level `format`, `virtual-size`, `backing-filename`/`full-backing-filename`/`backing-filename-format` when a
backing file is set, `format-specific: {type: qcow2, data: {..., data-file, data-file-raw}}` when an external data
file is set, and `children: [{name: "file"|"data-file"|"backing", info: {...}}]` (qemu >= 8.0).
"""
import asyncio
import json
import xml.etree.ElementTree as ET

import pytest

from app.services.vmhost import migrate
from app.services.vmhost.backend import BackendError, VirshBackend, parse_img_info
from tests.vmhost_migration_fake import T, VM, World, seed_vm, until

QCOW2 = {
    "children": [{"name": "file", "info": {"children": [], "virtual-size": 197120, "filename": "disk-vda.qcow2",
                                           "format": "file", "actual-size": 200704,
                                           "format-specific": {"type": "file", "data": {}}, "dirty-flag": False}}],
    "virtual-size": 21474836480, "filename": "disk-vda.qcow2", "cluster-size": 65536, "format": "qcow2",
    "actual-size": 200704,
    "format-specific": {"type": "qcow2", "data": {"compat": "1.1", "compression-type": "zlib", "lazy-refcounts": False,
                                                  "refcount-bits": 16, "corrupt": False, "extended-l2": False}},
    "dirty-flag": False}
BACKING = dict(QCOW2, **{"backing-filename": "/dev/sda", "full-backing-filename": "/dev/sda",
                         "backing-filename-format": "raw"})
DATA_FILE = dict(QCOW2, **{"format-specific": {"type": "qcow2", "data": dict(QCOW2["format-specific"]["data"],
                                                                             **{"data-file": "/etc/shadow",
                                                                                "data-file-raw": True})}})
DATA_CHILD = dict(QCOW2, children=QCOW2["children"] + [{"name": "data-file", "info": {"filename": "/etc/shadow",
                                                                                     "format": "file"}}])
RAW = {"children": [{"name": "file", "info": {"children": [], "virtual-size": 3145745, "filename": "disk-vda.qcow2",
                                              "format": "file", "actual-size": 3149824, "dirty-flag": False}}],
       "virtual-size": 3145745, "filename": "disk-vda.qcow2", "format": "raw", "actual-size": 3149824,
       "dirty-flag": False}
VMDK = dict(RAW, format="vmdk")


def run(coro):
    return asyncio.run(coro)


def probing_backend(answer):
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append((argv, timeout))
        return (0, json.dumps(answer), "") if answer is not None else (1, "", "qemu-img: Could not open 'x': No such file")
    return VirshBackend("qemu:///system", runner=runner), seen


def test_img_info_runs_qemu_img_with_force_share_json_and_a_timeout():
    be, seen = probing_backend(QCOW2)
    got = run(be.img_info("/var/lib/posterchan/vms/.incoming/m/disk-vda.qcow2.part"))
    argv, timeout = seen[-1]
    assert argv == ["qemu-img", "info", "--output=json", "-U", "--",
                    "/var/lib/posterchan/vms/.incoming/m/disk-vda.qcow2.part"]
    assert 0 < timeout <= 120
    assert got == {"format": "qcow2", "backing": "", "data_file": "", "virtual_size": 21474836480, "snapshots": []}
    be2, _ = probing_backend(None)
    with pytest.raises(BackendError):
        run(be2.img_info("/nope"))


@pytest.mark.parametrize("shape,want", [(BACKING, ("qcow2", "/dev/sda", "")), (DATA_FILE, ("qcow2", "", "/etc/shadow")),
                                        (DATA_CHILD, ("qcow2", "", "/etc/shadow")), (RAW, ("raw", "", "")),
                                        (VMDK, ("vmdk", "", ""))])
def test_parse_img_info_reads_the_real_shapes(shape, want):
    got = parse_img_info(json.dumps(shape))
    assert (got["format"], got["backing"], got["data_file"]) == want


@pytest.mark.parametrize("bad", ["", "[]", "not json", '{"virtual-size": 1}'])
def test_parse_img_info_refuses_what_it_cannot_read(bad):
    with pytest.raises(BackendError):
        parse_img_info(bad)


def world_with_probe(tmp_path, answer_for):
    """The target's backend probes through the REAL VirshBackend.img_info + parser, fed qemu-img JSON."""
    w = World(tmp_path)
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append(argv[-1])
        return 0, json.dumps(answer_for(argv[-1])), ""
    w.T.backend.img_info = VirshBackend("qemu:///system", runner=runner).img_info
    return w, seen


@pytest.mark.parametrize("shape", ["backing", "data-file", "data-file child", "vmdk"])
def test_a_disk_naming_a_second_host_file_refuses_the_migration(tmp_path, monkeypatch, shape):
    answer = {"backing": BACKING, "data-file": DATA_FILE, "data-file child": DATA_CHILD, "vmdk": VMDK}[shape]
    monkeypatch.setattr(migrate, "qcow2_has_backing", lambda p: False)       # the SOURCE's own check skipped

    async def go():
        w, seen = world_with_probe(tmp_path, lambda p: answer)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz()})
            assert res["ok"], res
            mig = res["result"]["migration"]["id"]
            await until(lambda: (w.T.rec(mig) or {}).get("state") == "aborted"
                        and (w.S.rec(mig) or {}).get("state") == "aborted", what="refused")
            assert VM not in w.T.backend.domains and not (w.T.root / VM).exists()
            assert any(p.endswith("disk-vda.qcow2.part") for p in seen), "the probe ran on the received bytes"
            assert VM in w.S.backend.domains
        finally:
            await w.close()
    run(go())


def test_the_driver_type_is_the_probed_format(tmp_path):
    async def go():
        w, seen = world_with_probe(tmp_path, lambda p: RAW)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz()})
            mig = res["result"]["migration"]["id"]
            await until(lambda: (w.T.rec(mig) or {}).get("state") == "done", what="done")
            root = ET.fromstring(w.T.backend.domains[VM]["xml"])
            assert [d.find("driver").get("type") for d in root.findall("devices/disk") if d.get("device") == "disk"] \
                == ["raw"], "the source's claim (qcow2) must not decide how libvirt reads the bytes"
            # The varstore is probed too (libvirt 12 runs qcow2 varstores — see test_vmhost_live_findings.py).
            assert any(p.endswith("disk-vda.qcow2.part") for p in seen) and any(p.endswith("nvram.fd.part") for p in seen)
        finally:
            await w.close()
    run(go())
