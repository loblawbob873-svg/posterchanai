'use strict';
const fs = require('fs');

const app = fs.readFileSync(process.env.PC_INSTALLED_APP_JS, 'utf8');
const os = fs.readFileSync(process.env.PC_INSTALLED_OS_JS, 'utf8');
let failures = 0;
function check(name, ok) {
  if (ok) console.log('  ok   ' + name);
  else { failures++; console.log('  FAIL ' + name); }
}

const start = os.indexOf('  function documentWindow(w)');
const end = os.indexOf('\n  function unsnap(w)', start);
check('installed window manager contains document workspace policy', start >= 0 && end > start);
let snap = '';
const snapTo = (w, zone) => { snap = zone; w.max = zone === 'max'; };
const documentWindow = eval('(' + os.slice(start, end).trim().replace(/^function /, 'function ') + ')');
const classes = new Set();
const w = { el: { classList: { add: c => classes.add(c) } } };
documentWindow(w);
check('document workspace uses neutral chrome', classes.has('osw-document'));
check('document workspace requests all usable space', snap === 'max' && w.max);

const office = app.slice(app.indexOf('  async function _officeSession('),
                         app.indexOf('  async function openOfficeFile(', app.indexOf('  async function _officeSession(')));
check('Office owns a no-feed desktop window', /PCOS\.openDoc\([^;]+true\)/s.test(office));
check('Office applies the document workspace policy', office.includes('PCOS.documentWindow(w)'));
check('Office posts to the server-provided editor URL', office.includes('action="${enc(session.editor_url)}"'));
check('Email applies the document workspace policy', os.includes("else if(view==='mail') documentWindow(w)"));

console.log(failures ? 'FAILED ' + failures : 'OK installed document workspaces hold');
process.exit(failures ? 1 : 0);
