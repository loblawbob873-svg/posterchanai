"""My Analytics is reachable everywhere and its event accounting is deterministic."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_analytics_is_a_registered_offline_module():
    html = (ROOT / "templates/client.html").read_text()
    app = (ROOT / "static/js/client/app.js").read_text()
    sw = (ROOT / "static/js/client/sw.js").read_text()
    assert 'data-view="analytics"' in html
    assert "renderModuleView('analytics','user-analytics.js','PCUserAnalytics','render')" in app
    assert "analytics:'My Analytics" in app
    assert "'/static/js/client/user-analytics.js'" in sw


def test_analytics_counts_dedupes_targets_and_ranks_posts():
    js = r"""
global.window={__PC:{enc:String,isView:()=>false}};
global.document={};
require('./static/js/client/user-analytics.js');
const now=2000000000, a={id:'a',kind:1,created_at:now-100,content:'A'}, b={id:'b',kind:1,created_at:now-200,content:'B'};
const ev=(id,kind,target,age,tags=[])=>({id,kind,created_at:now-age,tags:[['e',target,'','root'],...tags]});
const events=[ev('r1',7,'a',10),ev('r1',7,'a',10),ev('q1',1,'a',20),ev('x1',6,'b',30),ev('z1',9735,'a',40,[['amount','21000']])];
const out=window.PCUserAnalytics._compute([a,b,a],events,now);
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(["node", "-e", js], cwd=ROOT, text=True, capture_output=True, check=True)
    out = json.loads(proc.stdout)
    assert out["totals"] == {"posts": 2, "replies": 1, "reactions": 1, "reposts": 1, "zaps": 21, "engagement": 3, "rate": 1.5}
    assert out["top"][0]["post"]["id"] == "a"
    assert sum(day["posts"] for day in out["daily"]) == 2
    assert sum(day["engagement"] for day in out["daily"]) == 4


def test_analytics_mobile_layout_and_accessible_chart_label_ship():
    css = (ROOT / "static/css/client.css").read_text()
    js = (ROOT / "static/js/client/user-analytics.js").read_text()
    assert "@media(max-width:700px)" in css
    assert ".ua-chartgrid{grid-template-columns:1fr}" in css
    assert 'aria-label="${range} day ${key} chart"' in js
    assert "Couldn’t load analytics from your relays" in js
