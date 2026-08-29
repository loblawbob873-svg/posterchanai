/* A POLL FROM ARMADA WAS A QUESTION WITH NO ANSWERS.
 *
 * Armada's reader accepts kind 1068 beside 9 and 1111, so a poll posted there arrives in Concord as
 * an ordinary message: the question as its content, the options in `option` tags. concord.js had no
 * reference to 1068 at all, so it drew the question as bare text and the options nowhere.
 *
 * The answers are kind-1018 votes sealed inside the same channel stream — foldTimeline folds them
 * into pollVotes and inspectChat used to drop that on the floor, so nothing outside the stream can
 * count them either.
 *
 * This drives the real pollOf/pollHtml.
 */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const reader = fs.readFileSync(new URL('../../static/js/client/cord-reader.js', import.meta.url), 'utf8');
const noop = () => {};
const store = {};
const localStorage = {getItem:k=>(k in store?store[k]:null), setItem:(k,v)=>{store[k]=String(v);},
                     removeItem:k=>{delete store[k];}};
const document = {querySelector:()=>null, querySelectorAll:()=>[], createElement:()=>({dataset:{}}),
  head:{appendChild:noop}, documentElement:{appendChild:noop}, addEventListener:noop,
  body:{classList:{add:noop, remove:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};
vm.runInNewContext(src, {window, document, console, setTimeout:()=>0, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

/* THE READER MUST HAND THE VOTES OVER AT ALL. Concord cannot count them itself. */
if (!/pollVotes: \[\.\.\.timeline\.pollVotes\]/.test(reader))
  throw new Error('inspectChat no longer surfaces pollVotes — the tally has nothing to count');

const ME = 'c'.repeat(64);
const p = {enc: s => String(s).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),
           viewer: () => ({pubkey: ME})};

const poll = {id:'poll1', pubkey:'d'.repeat(64), text:'Lunch?', at:1000, kind:1068,
  tags:[['option','a','Pizza'],['option','b','Sushi']],
  votes:[{pubkey:'e'.repeat(64), optionIds:['a'], ms:10},
         {pubkey:'f'.repeat(64), optionIds:['a'], ms:11},
         {pubkey:ME,             optionIds:['b'], ms:12}]};

const parsed = api.pollOf(poll);
if (!parsed) throw new Error('a kind-1068 message was not recognised as a poll');
if (parsed.options.length !== 2) throw new Error('the options were not read from the tags');

const html = api.pollHtml(p, poll);
if (!/Pizza/.test(html) || !/Sushi/.test(html))
  throw new Error('the poll drew no options: ' + html);
if (!/3 votes/.test(html)) throw new Error('the tally is wrong: ' + html);
if (!/data-cc-option="a"/.test(html)) throw new Error('the options are not votable: ' + html);
/* YOUR OWN ANSWER IS SHOWN AS YOURS. */
const sushi = html.slice(html.indexOf('data-cc-option="b"') - 200, html.indexOf('Sushi'));
if (!/voted/.test(sushi)) throw new Error('your own vote is not marked: ' + html);

/* ONE PERSON, ONE ANSWER — THEIR LATEST. A vote is an ordinary event, so somebody who changes
   their mind leaves two behind, and counting both lets anyone inflate a poll by voting again. */
const swayed = {...poll, votes:[...poll.votes, {pubkey:'e'.repeat(64), optionIds:['b'], ms:99}]};
const h2 = api.pollHtml(p, swayed);
if (!/3 votes/.test(h2))
  throw new Error('a changed vote was counted twice: ' + h2);

/* A poll with no options is not a poll — it must not draw an empty widget over its own text. */
if (api.pollOf({kind:1068, tags:[]})) throw new Error('an option-less 1068 was treated as a poll');
if (api.pollOf({kind:9, tags:[['option','a','x']]})) throw new Error('an ordinary message drew a poll');

/* AN ENDED POLL CANNOT BE VOTED IN. */
const ended = {...poll, tags:[...poll.tags, ['endsAt','1']]};
if (!api.pollOf(ended).ended) throw new Error('an expired poll is still open');
if (!/disabled/.test(api.pollHtml(p, ended))) throw new Error('an ended poll is still clickable');

/* The bar must not be able to close the attribute it sits in — a poll is relay input. */
const nasty = {...poll, tags:[['option','a','" onmouseover="x']]};
const h3 = api.pollHtml(p, nasty);
if (/onmouseover="x/.test(h3.replace(/&quot;/g, ''))) throw new Error('option labels are not escaped');

console.log('concord polls runtime ok');
