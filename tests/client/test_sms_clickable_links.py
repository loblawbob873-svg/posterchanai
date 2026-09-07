"""Exercise the shipped SMS text formatter; actual pointer coverage lives in check_texts_media."""
import json
import subprocess
from pathlib import Path
from tests.client.test_encrypted_attachment import _fn

ROOT=Path(__file__).resolve().parents[2]

def render(body):
    source=(ROOT/'static/js/client/sms.js').read_text()
    function=_fn(source,'messageTextHtml','function messageTextHtml(')
    script="const PC={enc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;')};"+function
    script+=';console.log(JSON.stringify(messageTextHtml(JSON.parse(process.argv[1]))));'
    got=subprocess.run(['node','-e',script,json.dumps(body)],capture_output=True,text=True,check=True)
    return json.loads(got.stdout)

def test_encrypted_fragment_and_trailing_punctuation_are_preserved():
    link='https://poster.place/f/'+'a'*64+'#pcenc1=eyJrIjoia2V5Xy0ifQ_-'
    out=render('Download: '+link+'.\nThanks!')
    assert 'href="'+link+'"' in out
    assert '>'+link+'</a>.\nThanks!' in out
    assert 'target="_blank" rel="noopener noreferrer"' in out

def test_only_web_urls_become_links_and_message_markup_stays_plain():
    out=render('<img src=x onerror=alert(1)> javascript:alert(1) https://example.com/?a=1&b=2')
    assert '<img' not in out and '&lt;img' in out
    assert out.count('<a ')==1
    assert 'href="https://example.com/?a=1&amp;b=2"' in out

def test_multiple_links_do_not_drop_intervening_text_or_punctuation():
    out=render('A https://example.com/a, B http://example.org/b! C')
    assert '</a>, B <a ' in out and out.endswith('</a>! C')
