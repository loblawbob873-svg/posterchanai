import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {PassThrough} from 'node:stream';
import {EventEmitter} from 'node:events';
import assert from 'node:assert/strict';
const path=process.argv[2]?pathToFileURL(process.argv[2]):new URL('../../desktop/localterm.js',import.meta.url);
const realRequire=createRequire(path), child=new EventEmitter();
child.stdout=new PassThrough();child.stderr=new PassThrough();child.stdin=new PassThrough();
const module={exports:{}};
vm.runInNewContext(fs.readFileSync(path,'utf8'),{module,exports:module.exports,process,Buffer,
 setTimeout:()=>0,clearTimeout:()=>{},require:name=>name==='child_process'?{spawn:()=>child}:realRequire(name)});
const T=module.exports,session=T.start({cols:100,rows:30});
let drawn='';T.subscribe(session.id,ev=>{if(ev.t==='out')drawn+=ev.d;});
const expected='┣━ emoji 🙂 café 日本語\r\n';
for(const byte of Buffer.from(expected))child.stdout.write(Buffer.from([byte]));
assert.equal(drawn,expected,'UTF-8 split across stdout chunks must not corrupt terminal glyphs or cell widths');
console.log('PASS: streamed UTF-8 survives every possible byte boundary');
