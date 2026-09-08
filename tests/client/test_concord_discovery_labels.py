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
