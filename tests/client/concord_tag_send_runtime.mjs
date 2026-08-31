/* The shipped typedMentionRecipients, against a small room. */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const document = {querySelector:()=>null, querySelectorAll:()=>[], createElement:()=>({dataset:{}}),
  head:{appendChild:noop}, documentElement:{appendChild:noop}, addEventListener:noop,
  body:{classList:{add:noop, remove:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};
vm.runInNewContext(src, {window, document, console, setTimeout:()=>0, clearTimeout:noop, URL, atob,
  crypto:{}, localStorage:{getItem:()=>null,setItem:noop,removeItem:noop},
  sessionStorage:{getItem:()=>null,setItem:noop}});
const api = window.PCConcord;

const ALICE = 'a'.repeat(64), BOB = 'b'.repeat(64), NONAME = 'c'.repeat(64);
const people = [ALICE, BOB, NONAME];
const profileOf = pk => pk === ALICE ? {name: 'alice', display_name: 'Alice'}
                      : pk === BOB ? {nip05: 'bobby@example.com'}     /* nip05 ONLY, no name */
                                   : {};                              /* nothing at all */
const run = text => api.typedMentionRecipients(text, people, profileOf);

process.stdout.write(JSON.stringify({
  typed:     run('hey @alice can you look'),
  stranger:  run('hey @nobodyhere can you look'),
  inWord:    run('mail me at someone@alice please'),
  byNip05:   run('ping @bobby thanks'),
  byPubkey:  run('ping @' + NONAME.slice(0,12) + ' thanks'),
}));
