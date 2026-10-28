"""Run the actual discovery parser against the public directory post shape."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_directory_invites_keep_their_individual_labels():
    labels = ['Vegans', 'Gamers', 'Shitposters', 'Elder Dragon Highlander',
              'Discord Refugees', 'Neurodivergents Convergence', 'The Basement',
              'Book Nook', 'Health and Fitness']
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('static/js/client/concord.js','utf8');
const parts=source.slice(source.indexOf('  function inviteParts('),source.indexOf('  /* An invite is an in-app'));
const parser=source.slice(source.indexOf('  function discoverInvites('),source.indexOf('  /* v2 BECAUSE'));
vm.runInThisContext(parts+parser);
const labels=LABELS, urls=labels.map((_,i)=>'https://armada.buzz/invite/naddr1'+'023456789'[i]+'qq#BAADAwQBfixture'+i);
const directory='Armada now has a Discover page, allowing you to find various topical communities. Find one that fits your liking and join the conversation!\n\n'+labels.map((name,i)=>name+': '+urls[i]).join('\n\n');
const event={id:'public-directory'}, cards=discoverInvites(directory,event);
assert.equal(cards.length,9);assert.deepEqual(cards.map(c=>c.name),labels);
assert.deepEqual(cards.map(c=>c.url),urls);assert.ok(cards.every(c=>c.source===event));
assert.ok(cards.every(c=>!c.description.includes('/invite/')),'other invites must not pollute descriptions');
assert.equal(discoverInvites('A single community '+urls[0],event)[0].name,'A single community');
assert.equal(discoverInvites('Book Nook:\n'+urls[0],event)[0].name,'Book Nook');
const sameName=discoverInvites('Gamers: '+urls[0]+'\nGamers: '+urls[1],event);
assert.equal(sameName.length,2,'same label does not collapse distinct invitations');
assert.deepEqual(sameName.map(c=>c.name),['Gamers','Gamers']);
""".replace('LABELS', json.dumps(labels))
    result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_a_mention_never_names_or_renames_a_listed_room():
    """"In Discover, our room is not appearing right ... I mean nostr:naddr" (2026-10-27). The room's own listing post
    (tagged concord + community) said "PosterChan"; a newer post carrying the same invite opened with a nostr:npub
    mention, and newest-wins made that the room's title. The two posts below are the real ones from this relay."""
    invite = ('https://poster.place/invite/naddr1qvzqqqyzz5pzq3hjhx37t4u9uw5gthnmm3v62q3a5tqfxxqkze640quspp5rydekqqqqxw75uz'
              '#BAADAQIDAfAUHIPFIc91LplzbDY0bg')
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('static/js/client/concord.js','utf8');
const parts=source.slice(source.indexOf('  function inviteParts('),source.indexOf('  /* An invite is an in-app'));
const parser=source.slice(source.indexOf('  function discoverInvites('),source.indexOf('  /* v2 BECAUSE'));
vm.runInThisContext(parts+parser);
const INVITE=URL_;
const listing={id:'l',created_at:1792705286,tags:[['t','concord'],['t','community'],['client','PosterChan AI']]};
const mention={id:'m',created_at:1793075164,tags:[['p','d4ad3b6eeaff13fe0ccac86d2294049798ae2e397796ada5be3196592e56e2a7']]};
const L=discoverInvites('PosterChan\n\n'+INVITE,listing)[0];
const M=discoverInvites('nostr:npub16jknkmh2luflurx2epkj99qyj7v2ut3ew7t2mfd7xxt9jtjku2nsj2gqp0 \n\nAre you checking out Communities? Pretty cool. Our Armada-compatible rooms\n\n\n'+INVITE,mention)[0];
assert.equal(L.name,'PosterChan');
assert.ok(!/npub|naddr|nostr:/.test(M.name),'a nostr: reference became a name: '+M.name);
assert.ok(!/nostr:/.test(discoverInvites('nostr:naddr1qvzqqqyzz5pzq3hjhx37t4u9uw5gthnmm3v62q3a5tqfxx '+INVITE,mention)[0].name));
assert.equal(discoveryWins(L,M),false,'a later mention renamed a listed room');
assert.equal(discoveryWins(M,L),true,'the listing did not take over from a mention');
const newer=discoverInvites('PosterChan (renamed)\n\n'+INVITE,{...listing,id:'l2',created_at:listing.created_at+10})[0];
assert.equal(discoveryWins(L,newer),true,'among listings the newest still wins');
const m2=discoverInvites('another mention '+INVITE,{...mention,id:'m2',created_at:mention.created_at+10})[0];
assert.equal(discoveryWins(M,m2),true,'among mentions the newest still wins');
""".replace('URL_', json.dumps(invite))
    result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
