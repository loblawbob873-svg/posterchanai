"""Streams owns its external one-shot sockets for exactly as long as the Streams view."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
RELAY = ROOT / "static/js/client/relay.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_switching_from_streams_aborts_damus_and_nos_lol_sockets(tmp_path):
    helper = re.search(
        r"let _streamsReadOwner=null;\s*function _stopStreamsReads\(\).*?\s*function _beginStreamsReads\(\).*?\}",
        APP,
        re.S,
    )
    assert helper, "Streams read-owner lifecycle is missing"
    assert "if(VIEW==='streams')_stopStreamsReads()" in APP
    assert "{signal:readSignal,purpose:'streams directory'}" in APP
    assert "readSignal&&readSignal.aborted" in APP
    stale_sweep = APP.split("async function _sweepStaleOwnLive()", 1)[1].split("async function _maybeOfferAnnounce", 1)[0]
    assert "Relay.queryFrom(STREAM_RELAYS" not in stale_sweep
    assert "Relay.query([{ kinds:[30311], authors:[ME.pubkey] }])" in stale_sweep

    driver = tmp_path / "streams-leave.js"
    driver.write_text(f"""
      class FakeWS {{
        constructor(url){{this.url=url;FakeWS.all.push(this);}}
        send(){{}}
        close(){{this.closed=true;}}
      }}
      FakeWS.all=[];global.WebSocket=FakeWS;global.window=global;global.self=global;
      global.document={{hidden:false,addEventListener(){{}}}};global.navigator={{onLine:true}};
      global.location={{origin:'https://app.test',protocol:'https:'}};
      global.Worker=class{{postMessage(){{}}}};
      require({json.dumps(str(RELAY))});
      {helper.group(0)}
      const signal=_beginStreamsReads();
      const pending=Relay.queryFrom(['wss://nos.lol','wss://relay.damus.io'],[{{kinds:[30311]}}],
        {{timeout:60000,exact:true,signal}});
      _stopStreamsReads(); // switchView('concord')
      pending.then(events=>console.log(JSON.stringify({{events,urls:FakeWS.all.map(x=>x.url),closed:FakeWS.all.map(x=>!!x.closed)}})));
    """, encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["urls"] == ["wss://nos.lol", "wss://relay.damus.io"]
    assert result["closed"] == [True, True]
    assert result["events"] == []
