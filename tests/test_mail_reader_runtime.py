"""Responsive Email reader and packaged attachment behavior in shipped code."""
from pathlib import Path
import json, re, shutil, subprocess, tempfile
import pytest

ROOT=Path(__file__).resolve().parents[1]
APP=(ROOT/'static/js/client/app.js').read_text()
CSS=(ROOT/'static/css/client.css').read_text()
CHROME=shutil.which('google-chrome-stable') or shutil.which('chromium')

def extract(name):
    pos=APP.index('function '+name+'('); start=pos-6 if APP[pos-6:pos]=='async ' else pos
    brace=APP.index('{',pos); depth=0
    for i in range(brace,len(APP)):
        if APP[i]=='{':depth+=1
        elif APP[i]=='}':
            depth-=1
            if depth==0:return APP[start:i+1]
    raise AssertionError(name)

def test_packaged_attachment_runtime_uses_instance_and_preview():
    js=extract('_mailAttachmentUrl')+'\n'+extract('_openMailAttachment')
    script=f'''let BASE='https://poster.example',fetched='',opened=0,saved=0,toasts=[];const BUNDLED=true;
    const _instanceBase=()=>BASE,_aiToken='token',toast=x=>toasts.push(x),saveBlobAs=async()=>saved++;
    const _withModule=async()=>({{open:o=>{{opened++;return o.name==='photo.png'&&o.mime==='image/png'}}}});
    globalThis.fetch=async u=>{{fetched=u;return new Response(new Blob(['png'],{{type:'image/png'}}),{{status:200}})}};
    {js}
    (async()=>{{
      const u=_mailAttachmentUrl({{account:'me@example.test',folder:'IN BOX',uid:'7'}},'bad','bad',2);
      const a={{dataset:{{mailUrl:u,mailPreview:'1',name:'photo.png',mime:'image/png'}},setAttribute(){{}},removeAttribute(){{}}}};
      const ok=await _openMailAttachment(a); BASE=''; const absent=_mailAttachmentUrl({{uid:'1'}},'INBOX','a',0);
      BASE='https://localhost';const local=_mailAttachmentUrl({{uid:'1'}},'INBOX','a',0);
      BASE='http://127.0.0.1:8080';const ipv4=_mailAttachmentUrl({{uid:'1'}},'INBOX','a',0);
      BASE='http://[::1]:8080';const ipv6=_mailAttachmentUrl({{uid:'1'}},'INBOX','a',0);
      process.stdout.write(JSON.stringify({{u,fetched,opened,saved,ok,absent,local,ipv4,ipv6,toasts}}));
    }})().catch(e=>{{console.error(e);process.exitCode=1}});'''
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'mail.js';p.write_text(script)
        r=subprocess.run(['node',p],text=True,capture_output=True,timeout=20)
    assert r.returncode==0,r.stderr
    got=json.loads(r.stdout)
    assert got['u']=='https://poster.example/api/mail/dl/me%40example.test/IN%20BOX/7/2'
    assert got['fetched']==got['u'] and got['opened']==1 and got['saved']==0 and got['ok'] is True
    assert got['absent']==got['local']==got['ipv4']==got['ipv6']==''
    assert 'localhost' not in json.dumps(got).lower()

def test_office_attachment_runtime_opens_editor_and_saves_edited_copy():
    js=extract('_openMailAttachment')
    script=f'''let opened=0,saved=0,toasts=[];const _aiToken='',CFG={{office_enabled:true}};
    const toast=x=>toasts.push(x),_officeable=(n,m)=>/\\.docx$/i.test(n),
      fileFromBytes=(b,n,t)=>new File([b],n,{{type:t}}),saveBlobAs=async(f,n)=>{{saved++;}},
      _officeSession=async(file,saveBack)=>{{opened++;await saveBack(new File(['edited'],file.name,{{type:file.type}}));}};
    globalThis.fetch=async()=>new Response(new Blob(['doc'],{{type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}}),{{status:200}});
    {js}
    (async()=>{{const a={{dataset:{{mailUrl:'https://poster.example/api/mail/dl/a/INBOX/7/0',name:'report.docx',
      mime:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}},setAttribute(){{}},removeAttribute(){{}}}};
      const ok=await _openMailAttachment(a);process.stdout.write(JSON.stringify({{opened,saved,ok,toasts}}));
    }})().catch(e=>{{console.error(e);process.exitCode=1}});'''
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'mail-office.js';p.write_text(script)
        r=subprocess.run(['node',p],text=True,capture_output=True,timeout=20)
    assert r.returncode==0,r.stderr
    assert json.loads(r.stdout)=={'opened':1,'saved':1,'ok':True,'toasts':[]}

@pytest.mark.skipif(not CHROME,reason='Chrome unavailable')
@pytest.mark.parametrize('width,mobile',[(360,True),(1280,False)])
def test_reader_toolbar_stays_inside_viewport_in_real_chromium(width,mobile):
    buttons=''.join('<button class="btn icon-only">x</button>' for _ in range(7))
    html=f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style>
    <div class="mail-read has-open"><div class="mail-read-hd"><div class="mr-subj">A deliberately long subject that must not widen the reader</div></div><div class="mail-actions">{buttons}</div><div class="mail-body">message</div></div><pre id=o></pre><script>
    const bar=document.querySelector('.mail-actions'),bs=[...bar.children],r=bar.getBoundingClientRect(),br=bs.map(x=>x.getBoundingClientRect());
    document.querySelector('#o').textContent=JSON.stringify({{iw:innerWidth,left:r.left,right:r.right,overflow:document.documentElement.scrollWidth-innerWidth,widths:br.map(x=>x.width),cssWidths:bs.map(x=>getComputedStyle(x).width),tops:[...new Set(br.map(x=>Math.round(x.top)))]}});
    </script>'''
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'mail.html';p.write_text(html)
        r=subprocess.run([CHROME,'--headless=new','--no-sandbox','--disable-gpu',f'--window-size={width},800','--force-device-scale-factor=1','--dump-dom',p.as_uri()],text=True,capture_output=True,timeout=30)
    assert r.returncode==0,r.stderr[-1000:]
    m=re.search(r'<pre id="o">(.*?)</pre>',r.stdout,re.S);assert m
    got=json.loads(m.group(1).replace('&quot;','"'))
    assert got['left']>=0 and got['right']<=got['iw']+1 and got['overflow']<=0
    if mobile:
        assert len(got['tops'])==1 and min(got['widths'])>=40
    else:
        assert all(39.9<=float(x[:-2])<=40.1 for x in got['cssWidths'])
        assert max(got['widths'])-min(got['widths'])<.1


@pytest.mark.skipif(not CHROME,reason='Chrome unavailable')
def test_single_html_message_uses_the_available_reader_width():
    html=f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>
    html,body{{margin:0;width:100%;height:100%}}{CSS}</style>
    <div class="mail-read" style="width:900px;height:700px">
      <div class="mail-read-hd">subject</div><div class="mail-actions">actions</div>
      <div class="mail-thread"><div class="mail-msg open"><div class="mail-msg-hd">sender</div>
        <div class="mail-msg-body"><div class="mail-body"><iframe class="mail-html"></iframe></div></div>
      </div>
      <!-- The reply row the reader ALWAYS renders inside .mail-thread. Without it the fixture is a
           thread of one, which the shipped `:first-child:nth-last-child(2)` rule does not match —
           so this measured a layout the app never produces and failed on a correct change. -->
      <div class="mail-thread-reply"><button class="btn">Reply</button></div>
      </div></div><pre id=o></pre><script>
    const pane=document.querySelector('.mail-read').getBoundingClientRect();
    const body=document.querySelector('.mail-body'), frame=document.querySelector('.mail-html').getBoundingClientRect();
    const holder=document.querySelector('.mail-msg-body').getBoundingClientRect();
    const floor=document.querySelector('.mail-msg').getBoundingClientRect().bottom;
    o.textContent=JSON.stringify({{pane:pane.width,frame:frame.width,holder:holder.width,padding:getComputedStyle(body).padding,bottom:frame.bottom,paneBottom:pane.bottom,floor:floor}});
    </script>'''
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'mail-fill.html';p.write_text(html)
        r=subprocess.run([CHROME,'--headless=new','--no-sandbox','--disable-gpu','--window-size=1000,800',
                          '--force-device-scale-factor=1','--dump-dom',p.as_uri()],text=True,capture_output=True,timeout=30)
    assert r.returncode==0,r.stderr[-1000:]
    m=re.search(r'<pre id="o">(.*?)</pre>',r.stdout,re.S);assert m
    got=json.loads(m.group(1).replace('&quot;','"'))
    assert got['padding']=='0px'
    # THE MESSAGE FILLS THE SPACE IT IS GIVEN — measured against its own container, not the pane.
    # The reader draws each message as an inset CARD now (`.mail-thread` has horizontal padding and
    # `.mail-msg` a border), so comparing the iframe to the whole pane failed a deliberate design
    # change while saying nothing about the thing this test is for: a decorative inset INSIDE the
    # message, which is what used to leave an HTML mail in a narrow column with dead space beside it.
    assert got['frame'] >= got['holder']-1, got
    # ...AND DOWN TO THE BOTTOM OF ITS OWN MESSAGE. It used to be measured against the PANE, which
    # stopped being the right floor when the reader became a column of inset cards with a
    # Reply/Forward row under them: the thread's padding and its 8px gap are chrome, not dead space.
    # The property is unchanged and is the one that actually mattered — an HTML mail must not sit in
    # a short box with empty room beneath it INSIDE its own message.
    assert got['bottom'] >= got['floor']-1, got
