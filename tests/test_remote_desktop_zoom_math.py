"""The viewer's zoom/pan arithmetic, as PROPERTIES, over thousands of generated views.

The browser tests (tests/client/test_remote_desktop_zoom_full_app.py) drive the shipped controls
against a real transformed element, which is the only way to prove a click lands where it looks. They
are also slow, so they sample a handful of scales and offsets — and the failures this arithmetic has
already had (a translate resolved in the wrong pixel space; a centre computed from the stage instead
of the letterboxed picture) are the kind that appear at ONE aspect ratio or ONE end of the range.

So the pure half is sliced out of the shipped file and run under node, with no DOM at all, over every
combination of window, remote screen, scale and pan offset worth asking about. Nothing here is a copy
of the implementation: each check is an invariant the mapping has to satisfy however it is written.
"""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'static/js/client/app.js').read_text(encoding='utf-8')
# The pure block, verbatim from the shipped viewer. `_rdStageBox` is where the DOM starts.
MATHS = APP[APP.index('  const RD_ZOOM_TOP='):APP.index('  function _rdStageBox(')]
SLIDER = APP[APP.index('  function _rdZoomSlider('):APP.index('  /* Ask the host for the resolution')]

PRELUDE = """const assert=require('node:assert/strict');
// The preference seed reads localStorage at module scope; under node it simply has none.
const localStorage={getItem:()=>null,setItem(){},removeItem(){}};
"""


def run(body):
    driver = ROOT / 'tests' / '.rd_zoom_math.cjs'
    driver.write_text(PRELUDE + MATHS + SLIDER + '\n' + body + "\nconsole.log('COMPLETE');\n")
    try:
        r = subprocess.run(['node', str(driver)], capture_output=True, text=True, timeout=120)
    finally:
        driver.unlink(missing_ok=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'COMPLETE' in r.stdout, r.stdout
    return r.stdout


# Windows worth asking about: a laptop, a 16:10 laptop over a 16:9 desktop (letterboxed — the shape
# almost every real session has), a phone, a tall window, and a window bigger than the remote screen.
VIEWS = """
const VIEWS=[];
for(const [sw,sh] of [[1400,790],[1400,875],[900,562],[390,220],[390,700],[1920,1080],[2560,1400],[640,480]])
  for(const [rw,rh] of [[3840,2160],[1920,1080],[1280,1024],[1080,1920],[2560,1440]])
    VIEWS.push({sw,sh,rw,rh});
const SCALES=(b)=>{const out=[b.min,b.fit,1,b.max];
  for(let i=1;i<10;i++)out.push(Math.exp(Math.log(b.min)+(Math.log(b.max)-Math.log(b.min))*i/10));
  return out.filter(s=>s>0&&isFinite(s));};
const ATS=[[0.5,0.5],[0,0],[1,1],[0.25,0.75],[0.8,0.2],[0.03,0.97],[0.5,0],[1,0.5],[-3,7]];
let cases=0;
"""


@pytest.mark.parametrize('rule', ['roundtrip', 'window', 'monotone', 'anchor', 'source', 'slider'])
def test_the_mapping_holds_as_a_property_over_every_view(rule):
    checks = {
        # What the picture ACTUALLY shows in the middle has to be what it was asked to show, or the
        # pointer and the picture disagree by whatever the difference is. Measured while this was
        # wrong (the stage box in place of the letterboxed picture box): at 2x centred on 0.25 the
        # middle of the stage was 0.32 of the remote screen.
        'roundtrip': """
for(const v of VIEWS){
  const b=_rdZoomBounds(v.sw,v.sh,v.rw,v.rh);
  // `content` IS THE PICTURE'S OWN BOX: the remote screen's aspect ratio, scaled to fit inside the
  // stage and touching it on exactly the constrained axis. The letterbox is the difference, and a
  // transform written against the STAGE over-translates by precisely that.
  assert(Math.abs(b.content.width/b.content.height-v.rw/v.rh)<1e-9,JSON.stringify({v,c:b.content}));
  assert(b.content.width<=v.sw+1e-9&&b.content.height<=v.sh+1e-9,JSON.stringify({v,c:b.content}));
  assert(Math.abs(b.content.width-v.sw)<1e-6||Math.abs(b.content.height-v.sh)<1e-6,JSON.stringify({v,c:b.content}));
  assert(Math.abs(b.fit-b.content.width/v.rw)<1e-12,JSON.stringify({v,b}));
  for(const scale of SCALES(b)){
    const m=scale/b.fit;
    for(const [ax,ay] of ATS){
      const want=_rdClampAt({x:ax,y:ay},m);
      const t=_rdZoomTransform(m,b.content,{x:ax,y:ay});
      const got=_rdZoomCentre(t,b.content);
      // The transform is rounded to whole pixels, so the tolerance is one pixel expressed in remote
      // coordinates and nothing looser.
      const tolX=1/(b.content.width*Math.max(1,m)), tolY=1/(b.content.height*Math.max(1,m));
      if(m>=1){
        assert(Math.abs(got.x-want.x)<=tolX+1e-9,JSON.stringify({v,scale,ax,ay,got,want,tolX}));
        assert(Math.abs(got.y-want.y)<=tolY+1e-9,JSON.stringify({v,scale,ax,ay,got,want,tolY}));
      }else{
        // Below Fit there is nothing to pan; the picture stays centred whatever it is asked for.
        assert.equal(t.x,0);assert.equal(t.y,0);
      }
      cases++;
    }
  }
}
assert(cases>1000,cases);
""",
        # The visible window must always be INSIDE the remote screen. A viewer showing a band of
        # black beside the desktop and calling it zoom is the bug the clamp exists for.
        'window': """
for(const v of VIEWS){
  const b=_rdZoomBounds(v.sw,v.sh,v.rw,v.rh);
  for(const scale of SCALES(b)){
    const m=scale/b.fit;
    if(m<1)continue;
    for(const [ax,ay] of ATS){
      const t=_rdZoomTransform(m,b.content,{x:ax,y:ay});
      const c=_rdZoomCentre(t,b.content),half=0.5/m;
      for(const axis of ['x','y']){
        assert(c[axis]-half>=-0.002,JSON.stringify({v,scale,ax,ay,axis,c,half}));
        assert(c[axis]+half<=1.002,JSON.stringify({v,scale,ax,ay,axis,c,half}));
      }
      // And the offset never exceeds the room the magnified picture has.
      assert(Math.abs(t.x)<=b.content.width*(m-1)/2+1.5,JSON.stringify({v,scale,t}));
      assert(Math.abs(t.y)<=b.content.height*(m-1)/2+1.5,JSON.stringify({v,scale,t}));
      cases++;
    }
  }
}
assert(cases>1000,cases);
""",
        # Moving right across the window moves right across the remote screen, by the SAME amount
        # everywhere — a mapping that is right in the middle can still be wrong at the corners.
        'monotone': """
for(const v of VIEWS){
  const b=_rdZoomBounds(v.sw,v.sh,v.rw,v.rh);
  for(const scale of SCALES(b)){
    const m=scale/b.fit;if(m<1)continue;
    const t=_rdZoomTransform(m,b.content,{x:0.4,y:0.6});
    const c=_rdZoomCentre(t,b.content);
    // Where stage fraction f lands, from the geometry: the picture spans `content*m` of a `sw` stage.
    const at=(f,span)=>c.x+(f-0.5)*v.sw/(b.content.width*m);
    let prev=-Infinity,steps=[];
    for(let i=0;i<=20;i++){const f=i/20,x=at(f);assert(x>prev,JSON.stringify({v,scale,f,x,prev}));
      if(i)steps.push(x-prev);prev=x;}
    const lo=Math.min(...steps),hi=Math.max(...steps);
    assert(hi-lo<1e-9,JSON.stringify({v,scale,lo,hi}));   // linear, i.e. no double-applied scale
    // Magnifying always covers LESS of the remote screen per pixel of window.
    const t2=_rdZoomTransform(m*1.5,b.content,{x:0.4,y:0.6});
    const c2=_rdZoomCentre(t2,b.content);
    const span1=v.sw/(b.content.width*m), span2=v.sw/(b.content.width*m*1.5);
    assert(span2<span1,JSON.stringify({v,scale,span1,span2}));
    void c2;cases++;
  }
}
assert(cases>200,cases);
""",
        # ANCHORED ZOOM: the point under the cursor does not move. Checked in SCREEN space, before any
        # clamp, because that is the promise — and the clamped case is checked to still be visible.
        'anchor': """
for(const v of VIEWS){
  const b=_rdZoomBounds(v.sw,v.sh,v.rw,v.rh);
  const scales=SCALES(b);
  for(let i=0;i<scales.length;i++)for(let j=0;j<scales.length;j++){
    const m1=scales[i]/b.fit, m2=scales[j]/b.fit;
    if(m1<1||m2<1)continue;
    for(const [px,py] of [[0.5,0.5],[0.1,0.9],[0.77,0.33],[0,1]]){
      for(const [ax,ay] of [[0.5,0.5],[0.3,0.7]]){
        const t1=_rdZoomTransform(m1,b.content,{x:ax,y:ay});
        const c1=_rdZoomCentre(t1,b.content);
        const next=_rdZoomAnchor(m1,m2,{x:px,y:py},c1);
        // Unclamped, the pointer's offset from the centre in SCREEN pixels is identical.
        const before={x:(px-c1.x)*b.content.width*m1, y:(py-c1.y)*b.content.height*m1};
        const after={x:(px-next.x)*b.content.width*m2, y:(py-next.y)*b.content.height*m2};
        assert(Math.abs(after.x-before.x)<1e-6,JSON.stringify({v,m1,m2,px,py,before,after}));
        assert(Math.abs(after.y-before.y)<1e-6,JSON.stringify({v,m1,m2,px,py,before,after}));
        // Clamped, the weaker promise that still matters: a point that WAS on screen stays on
        // screen. Only that case — a real Ctrl+wheel pointer comes from `_rdVideoPoint` over the
        // picture, so it is inside the visible window by construction, and asserting it about a
        // point that was never visible would be asserting the clamp away.
        const held=_rdClampAt(next,m2),half=0.5/m2,was=0.5/m1;
        if(px>=c1.x-was-1e-9&&px<=c1.x+was+1e-9)
          assert(px>=held.x-half-0.002&&px<=held.x+half+0.002,JSON.stringify({v,m1,m2,px,held,half}));
        if(py>=c1.y-was-1e-9&&py<=c1.y+was+1e-9)
          assert(py>=held.y-half-0.002&&py<=held.y+half+0.002,JSON.stringify({v,m1,m2,py,held,half}));
        // A zoom at the centre must not move the view at all, or every + press drifts.
        if(px===0.5&&py===0.5&&ax===0.5&&ay===0.5){
          assert(Math.abs(next.x-0.5)<1e-9&&Math.abs(next.y-0.5)<1e-9,JSON.stringify({v,m1,m2,next}));
        }
        cases++;
      }
    }
  }
}
assert(cases>2000,cases);
""",
        # The resolution asked of the host: never fewer source pixels than the window shows (a soft
        # picture is the bug this exists to fix), never more than the screen has, and never rising as
        # the window shrinks.
        'source': """
for(const remote of [3840,2560,1920,1280,1080]){
  let prev=Infinity;
  for(let shown=40;shown<=remote*1.4;shown+=13){
    for(const dpr of [1,1.5,2,3]){
      const d=_rdSourceDownscale(shown,remote,dpr);
      assert(d>=1,JSON.stringify({remote,shown,dpr,d}));
      assert(RD_SRC_STEPS.includes(d),JSON.stringify({remote,shown,dpr,d}));
      const supplied=remote/d, wanted=Math.min(remote,shown*dpr);
      assert(supplied>=wanted-1e-6,JSON.stringify({remote,shown,dpr,d,supplied,wanted}));
      if(dpr===1){assert(d<=prev,JSON.stringify({remote,shown,d,prev}));prev=d;}
      cases++;
    }
  }
  // A thumbnail asks for a thumbnail, and a 1:1 view asks for everything.
  assert(_rdSourceDownscale(132,remote,1)>4);
  assert.equal(_rdSourceDownscale(remote,remote,1),1);
}
assert(cases>1000,cases);
""",
        # The slider maps the range both ways, and the geometric middle of the range is the middle of
        # the track — so the scales people read text at are half the slider, not its first third.
        'slider': """
for(const v of VIEWS){
  const b=_rdZoomBounds(v.sw,v.sh,v.rw,v.rh);
  for(let i=0;i<=1000;i+=7){
    const t=i/1000, scale=_rdZoomFromSlider(t,b);
    assert(scale>=b.min-1e-9&&scale<=b.max+1e-9,JSON.stringify({v,t,scale}));
    assert(Math.abs(_rdZoomSlider(scale,b)-t)<1e-9,JSON.stringify({v,t,scale}));
    cases++;
  }
  assert.equal(_rdZoomSlider(b.min,b),0);
  assert.equal(_rdZoomSlider(b.max,b),1);
  const mid=_rdZoomFromSlider(0.5,b);
  assert(Math.abs(mid-Math.sqrt(b.min*b.max))<1e-9,JSON.stringify({v,mid}));
}
assert(cases>1000,cases);
""",
    }
    run(VIEWS + checks[rule])


# The one HOST-side function the zoom drives, run under node against a fake sender. It is here rather
# than in a browser because there is nothing to render: it is the encoder agreement, and the failures
# it can have (a bitrate ceiling left at 24 Mbps for a quarter-size picture, a viewer able to ask for
# MORE work than shipped, a screen switch leaving a stale agreement) are all invisible on screen.
QUALITY = APP[APP.index('  /* SEND THE SCREEN AT THE SIZE'):APP.index('  async function _rdSwitchScreen(')]


def test_the_host_applies_the_requested_resolution_and_scales_the_bitrate_with_it(tmp_path):
    driver = tmp_path / 'quality.cjs'
    driver.write_text("""const assert=require('node:assert/strict');
let applied=[],calls=0;
const sender={track:{kind:'video'},getParameters:()=>({encodings:[{maxBitrate:24000000}]}),
              setParameters:async p=>{calls++;applied.push(p.encodings[0]);}};
let _call={remoteDesktop:true,caller:true,pc:{getSenders:()=>[sender]}};
""" + QUALITY + """
(async()=>{
  // A 4K desktop in a laptop window: the encoder is told to make a smaller picture AND given a
  // bitrate to match it, or a quarter-size frame is handed the whole 24 Mbps and spends it padding.
  await _rdApplyQuality(2.5);
  assert.equal(applied.at(-1).scaleResolutionDownBy,2.5);
  assert.equal(applied.at(-1).maxBitrate,Math.round(24000000/6.25));
  assert.equal(applied.at(-1).maxFramerate,24);
  // The same request again is a no-op: `setParameters` is not free and a dragged slider must not
  // re-tune the encoder per frame.
  const was=calls; await _rdApplyQuality(2.5); assert.equal(calls,was);
  // IT CAN ONLY EVER REDUCE WORK. A viewer asking for a bigger encoder than the one that shipped is
  // clamped to 1, and nonsense resolves to 1 rather than to NaN (which would blank the stream).
  await _rdApplyQuality(0.2); assert.equal(applied.at(-1).scaleResolutionDownBy,1);
  assert.equal(applied.at(-1).maxBitrate,24000000);
  await _rdApplyQuality('nonsense'); assert.equal(applied.at(-1).scaleResolutionDownBy,1);
  await _rdApplyQuality(9999); assert.equal(applied.at(-1).scaleResolutionDownBy,16);
  // The floor is a real bitrate: a thumbnail still has to be legible when it is restored.
  assert(applied.at(-1).maxBitrate>=1500000);
  // Only the SHARING side acts on it — a viewer that received one would be re-tuning its own
  // outgoing encoder, which in a desktop session does not exist.
  _call.caller=false; const before=calls; await _rdApplyQuality(4); assert.equal(calls,before);
  _call.caller=true;
  // A failed `setParameters` must not leave the host believing an agreement it did not make, or the
  // next identical request is skipped and the session stays at the wrong resolution for ever.
  sender.setParameters=async()=>{throw Error('refused')};
  await _rdApplyQuality(3); assert.equal(_call.rdQuality,null);
  console.log('COMPLETE');
})().catch(e=>{console.error(e);process.exitCode=1});
""")
    r = subprocess.run(['node', str(driver)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'COMPLETE' in r.stdout
