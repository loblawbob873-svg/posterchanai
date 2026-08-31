from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_trailing_slash_spellings_share_one_socket():
    driver = r"""
global.self=global; global.window=global; global.document={hidden:false};
global.Worker=class { postMessage(){} };
class FakeWS { static all=[]; constructor(url){this.url=url;this.readyState=0;FakeWS.all.push(this)} close(){} }
global.WebSocket=FakeWS;
require(process.argv[1]);
Relay.configure({urls:['wss://poster.place/relay/','wss://poster.place/relay'],verify:false});
if(FakeWS.all.length!==1) throw new Error('opened '+FakeWS.all.length+' sockets');
Relay.configure({urls:['wss://example.test/nostr/'],verify:false});
if(FakeWS.all.at(-1).url!=='wss://example.test/nostr/') throw new Error('rewrote external relay path');
"""
    proc = subprocess.run(["node", "-e", driver, str(ROOT / "static/js/client/relay.js")],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
