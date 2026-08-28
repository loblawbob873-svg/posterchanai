"""Drive the shipped temporary external-relay subscription used by Remote Desktop."""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "static/js/client/relay.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_external_call_signaling_is_verified_delivered_and_closed(tmp_path):
    driver = tmp_path / "external-sub.js"
    driver.write_text(textwrap.dedent(f"""
      class FakeWS {{
        constructor(url){{this.url=url;this.readyState=0;this.sent=[];FakeWS.all.push(this);}}
        send(v){{this.sent.push(JSON.parse(v));}}
        close(){{this.readyState=3;this.closed=true;}}
        open(){{this.readyState=1;this.onopen&&this.onopen();}}
        receive(v){{this.onmessage&&this.onmessage({{data:JSON.stringify(v)}});}}
      }}
      FakeWS.all=[];
      global.WebSocket=FakeWS;global.window=global;global.self=global;
      global.document={{hidden:false,addEventListener(){{}}}};global.navigator={{onLine:true}};
      global.location={{origin:'https://app.test',protocol:'https:'}};
      global.Worker=class {{
        postMessage(m){{setTimeout(()=>this.onmessage({{data:{{id:m.id,ok:true,data:[{{id:m.args.events[0].id,valid:m.args.events[0].sig==='good'}}]}}}}),0);}}
      }};
      require({json.dumps(str(RELAY))});
      const got=[];
      const stop=Relay.subscribeFrom(['wss://peer.test'],[{{kinds:[25050]}}],{{onEvent:e=>got.push(e),timeout:5000}});
      let ready=false;stop.ready.then(v=>{{ready=v;}});
      const ws=FakeWS.all[0];ws.open();
      const sub=ws.sent[0][1];
      ws.receive(['EVENT',sub,{{id:'bad',sig:'bad',tags:[]}}]);
      ws.receive(['EVENT',sub,{{id:'ok',sig:'good'}}]);
      setTimeout(()=>{{const hasTargets=stop.hasTargets;stop();console.log(JSON.stringify({{sent:ws.sent[0],got,closed:ws.closed,ready,hasTargets}}));}},20);
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip())
    assert result["sent"][0] == "REQ"
    assert result["sent"][2] == {"kinds": [25050]}
    assert [event["id"] for event in result["got"]] == ["ok"]
    assert result["got"][0]["tags"] == []
    assert result["closed"] is True
    assert result["ready"] is True
    assert result["hasTargets"] is True
