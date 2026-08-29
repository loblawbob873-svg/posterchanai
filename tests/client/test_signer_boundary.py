import json, subprocess
from pathlib import Path


def test_core_and_exported_submodule_share_one_strict_nip44_boundary():
    p=subprocess.run(['node','--unhandled-rejections=strict',str(Path(__file__).with_name('signer_boundary_runtime.mjs'))],
                     text=True,capture_output=True,check=True)
    got=json.loads(p.stdout)
    assert got['encCalls']==1 and got['decCalls']==0
    assert got['valid']=='ct:ok'
    assert [x['meta']['op'] for x in got['errors']]==['encrypt','decrypt','encrypt']
    assert got['errors'][2]['meta']['bytes']==65536
    assert got['allModes']==['nip55','nip46','local']
