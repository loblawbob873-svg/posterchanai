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
      global.document={{hidden:false,addEventListener(){{}}}};Object.defineProperty(global,'navigator',{{value:{{onLine:true}}}});
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
      const published=stop.publish({{id:'out'}});
      setTimeout(()=>{{const hasTargets=stop.hasTargets,outbound=ws.sent[1];stop();console.log(JSON.stringify({{sent:ws.sent[0],got,closed:ws.closed,ready,hasTargets,published,outbound}}));}},20);
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
    assert result["published"] == 1
    assert result["outbound"] == ["EVENT", {"id": "out"}]


def test_one_shot_external_query_closes_immediately_when_owner_aborts(tmp_path):
    driver = tmp_path / "abort-query.js"
    driver.write_text(textwrap.dedent(f"""
      class FakeWS {{
        constructor(url){{this.url=url;this.readyState=0;FakeWS.all.push(this);}}
        send(){{}}
        close(){{this.readyState=3;this.closed=true;}}
      }}
      FakeWS.all=[];
      global.WebSocket=FakeWS;global.window=global;global.self=global;
      global.document={{hidden:false,addEventListener(){{}}}};Object.defineProperty(global,'navigator',{{value:{{onLine:true}}}});
      global.location={{origin:'https://app.test',protocol:'https:'}};
      global.Worker=class {{ postMessage(){{}} }};
      require({json.dumps(str(RELAY))});
      const owner=new AbortController();
      const pending=Relay.queryFrom(['wss://relay.dreamith.to','wss://relay.good-two.example'],
        [{{kinds:[1]}}],{{timeout:60000,max:2,exact:true,signal:owner.signal}});
      const before=FakeWS.all.map(ws=>ws.closed===true);
      owner.abort();
      pending.then(events=>console.log(JSON.stringify({{before,events,closed:FakeWS.all.map(ws=>ws.closed===true)}})));
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip())
    assert result == {"before": [False, False], "events": [], "closed": [True, True]}


def test_failed_external_relay_is_single_flight_and_circuit_broken(tmp_path):
    driver = tmp_path / "query-circuit.js"
    driver.write_text(textwrap.dedent(f"""
      class FakeWS {{
        constructor(url){{this.url=url;FakeWS.all.push(this);}}
        send(){{}} close(){{this.closed=true;}}
        fail(){{this.onerror&&this.onerror(new Error('refused'));}}
      }}
      FakeWS.all=[];global.WebSocket=FakeWS;global.window=global;global.self=global;
      global.document={{hidden:false,addEventListener(){{}}}};Object.defineProperty(global,'navigator',{{value:{{onLine:true}}}});
      global.location={{origin:'https://app.test',protocol:'https:'}};
      global.Worker=class{{postMessage(){{}}}};console.warn=()=>{{}};
      require({json.dumps(str(RELAY))});
      (async()=>{{
      const first=Relay.queryFrom(['wss://relay.dead.example'],[{{kinds:[1]}}],{{exact:true,purpose:'concord explicit room'}});
      const concurrent=await Relay.queryFrom(['wss://relay.dead.example'],[{{kinds:[9]}}],{{exact:true,purpose:'other poll'}});
      const madeWhileBusy=FakeWS.all.length;FakeWS.all[0].fail();await first;
      const cooled=await Relay.queryFrom(['wss://relay.dead.example'],[{{kinds:[1]}}],{{exact:true,purpose:'retry'}});
      console.log(JSON.stringify({{madeWhileBusy,total:FakeWS.all.length,concurrent,cooled}}));
      }})().catch(error=>{{console.error(error);process.exit(1);}});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"madeWhileBusy": 1, "total": 1, "concurrent": [], "cooled": []}


def test_ditto_and_damus_are_rejected_by_external_and_pool_constructors(tmp_path):
    driver = tmp_path / "blocked-query.js"
    driver.write_text(textwrap.dedent(f"""
      class FakeWS {{ constructor(url){{this.url=url;FakeWS.all.push(url);}} close(){{this.closed=true;}} }} FakeWS.all=[];
      global.WebSocket=FakeWS;global.window=global;global.self=global;
      global.document={{hidden:false,addEventListener(){{}}}};Object.defineProperty(global,'navigator',{{value:{{onLine:true}}}});
      global.location={{origin:'https://app.test',protocol:'https:'}};global.Worker=class{{postMessage(){{}}}};
      require({json.dumps(str(RELAY))});
      Relay.queryFrom(['wss://relay.ditto.pub/','wss://relay.damus.io/'],[{{kinds:[1059]}}],{{exact:true,purpose:'legacy explicit room'}})
        .then(events=>{{
          Relay.configure({{urls:['wss://relay.ditto.pub/','wss://relay.damus.io/','wss://relay.good.example/'],verify:true}});
          const configured=Relay.urls();Relay.connect('wss://relay.damus.io/');
          console.log(JSON.stringify({{events,sockets:FakeWS.all,configured,afterConnect:Relay.urls()}}));
        }});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {
        "events": [], "sockets": ["wss://relay.good.example/"],
        "configured": ["wss://relay.good.example/"], "afterConnect": [],
    }


def test_successful_background_poll_is_throttled_and_view_leave_closes_pending_socket(tmp_path):
    driver = tmp_path / "query-throttle.js"
    driver.write_text(textwrap.dedent(f"""
      class FakeWS {{
        constructor(url){{this.url=url;FakeWS.all.push(this);}}
        send(value){{this.sent=JSON.parse(value);}}
        close(){{this.closed=true;}}
        open(){{this.onopen&&this.onopen();}}
        receive(value){{this.onmessage&&this.onmessage({{data:JSON.stringify(value)}});}}
      }}
      FakeWS.all=[];global.WebSocket=FakeWS;global.window=global;global.self=global;
      global.document={{hidden:false,addEventListener(){{}}}};Object.defineProperty(global,'navigator',{{value:{{onLine:true}}}});
      global.location={{origin:'https://app.test',protocol:'https:'}};global.Worker=class{{postMessage(){{}}}};
      require({json.dumps(str(RELAY))});
      (async()=>{{
        const opts={{exact:true,purpose:'concord room live fixture',minInterval:60000}};
        const first=Relay.queryFrom(['wss://relay.dreamith.to'],[{{kinds:[1059]}}],opts);
        const ws=FakeWS.all[0];ws.open();ws.receive(['EOSE',ws.sent[1]]);await first;
        const repeated=await Promise.all(Array.from({{length:20}},()=>Relay.queryFrom(['wss://relay.dreamith.to'],[{{kinds:[1059]}}],opts)));
        const nextPage=Relay.queryFrom(['wss://relay.dreamith.to'],[{{kinds:[1059],until:10}}],{{...opts,minInterval:0}});
        const pageWs=FakeWS.all[1];pageWs.open();pageWs.receive(['EOSE',pageWs.sent[1]]);await nextPage;
        const pending=Relay.queryFrom(['wss://relay.dreamith.to'],[{{kinds:[1059]}}],{{...opts,purpose:'concord room metadata fixture'}});
        const leaving=FakeWS.all[2];Relay.abortQueries();await pending;
        console.log(JSON.stringify({{count:FakeWS.all.length,repeated:repeated.flat(),closed:!!leaving.closed}}));
      }})().catch(error=>{{console.error(error);process.exit(1);}});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"count": 3, "repeated": [], "closed": True}
