"""A MUTED CONVERSATION LEAVES NOTIFICATIONS -- the client half of #196 ("mute conversation not working for
fedi conversations").

Muting a conversation hid it from the TIMELINE (isMutedView) and nowhere else: the Notifications list, the
bell count it feeds and the toast/OS notification only ever asked about muted AUTHORS. In a fediverse thread
that is the whole experience -- Mastodon and Pleroma mention every participant in every reply, so each new
reply in the muted thread arrived as a fresh "mentioned you". Runs the SHIPPED notifList/notifPing with the
SHIPPED _mutedThread rule (app.js), so a change to either is what is tested.
"""
import subprocess

from tests.client_source import client_source, state_shim


def test_a_reply_in_a_muted_conversation_is_neither_listed_nor_toasted():
    source = client_source()

    def segment(a, b):
        return source[source.index(a):source.index(b, source.index(a))]
    notifs = segment('  const _quoteHits=', '  // `html` is trusted') + '\n' + segment('  function notifList(', '  // Follows DO light')
    code = state_shim(notifs) + '\n' + notifs + '\n' + segment('  function _mutedThread(', '  // JSON-BLOB SPAM')
    harness = r'''
const assert=require('node:assert/strict');
const ME={pubkey:'a'.repeat(64)},LOGO='logo',FEDI='b'.repeat(64),ROOT='1'.repeat(64),OTHER='2'.repeat(64);
const reply=(id,root,parent)=>({id,pubkey:FEDI,kind:1,created_at:5,content:'@you @everyone hi',
  tags:[['e',root,'','root'],...(parent?[['e',parent,'','reply']]:[]),['p',ME.pubkey]]});
let MUTED_THREADS=new Set([ROOT]), events=[], toasts=[], os=[];
const Store={all:()=>events,get:()=>undefined},_followSeeded=false,isMutedAuthor=()=>false;
const _reminderRows=()=>[],notificationAllowed=()=>true;
const _notifTs=e=>e.created_at,profOf=()=>({name:'fedi person'}),_tipNote=()=>null,zapSender=()=>'';
const isReply=()=>true,emojiName=(pk,n)=>n,enc=s=>s,_notifSaid=()=>'',_notifCtx=()=>'',timeAgo=()=>'';
const notifToast=s=>toasts.push(s),osNotify=(t,b,o)=>os.push(o),_notifCtxId=()=>'',openThread=()=>{},switchView=()=>{};
CODE
const inMuted=reply('3'.repeat(64),ROOT), deep=reply('4'.repeat(64),'5'.repeat(64),ROOT), elsewhere=reply('6'.repeat(64),OTHER);
events=[inMuted,deep,elsewhere];
assert.deepEqual(notifList().map(e=>e.id),[elsewhere.id],'a reply in the muted conversation is still listed');
notifPing(inMuted); notifPing(deep);
assert.deepEqual([toasts.length,os.length],[0,0],'a reply in the muted conversation still notified');
notifPing(elsewhere);
assert.equal(os.length,1,'an unmuted conversation no longer notifies');
MUTED_THREADS=new Set();
assert.equal(notifList().length,3,'unmuting did not bring the conversation back');
'''.replace('CODE', code)
    result = subprocess.run(['node', '-e', harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr[-2000:]
