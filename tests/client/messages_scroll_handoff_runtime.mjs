import fs from 'node:fs';

const src=fs.readFileSync(new URL('../../static/js/client/app.js',import.meta.url),'utf8');
function functionText(name){
  const start=src.indexOf(`function ${name}(`);if(start<0)throw Error(`missing ${name}`);
  const brace=src.indexOf('{',start);let depth=0;
  for(let i=brace;i<src.length;i++){
    if(src[i]==='{')depth++;else if(src[i]==='}'&&!--depth)return src.slice(start,i+1);
  }
  throw Error(`unterminated ${name}`);
}
const make=new Function(`${functionText('_dmScrollState')};${functionText('_restoreDmScroll')};return {_dmScrollState,_restoreDmScroll}`);
const {_dmScrollState,_restoreDmScroll}=make();

const pinned={scrollHeight:1800,clientHeight:500,scrollTop:1300};
const pinnedState=_dmScrollState(pinned);
if(!pinnedState.pinned)throw Error('bottom viewport was not captured as pinned');
const grownPinned={scrollHeight:2400,clientHeight:500,scrollTop:0};
_restoreDmScroll(grownPinned,pinnedState);
if(grownPinned.scrollTop!==1900)throw Error('pinned handoff did not follow grown transcript');

const reading={scrollHeight:1800,clientHeight:500,scrollTop:900};
const readingState=_dmScrollState(reading);
if(readingState.pinned||readingState.aboveBottom!==400)throw Error('history distance was not captured');
const grownReading={scrollHeight:2400,clientHeight:500,scrollTop:0};
_restoreDmScroll(grownReading,readingState);
if(grownReading.scrollTop!==1500)throw Error('history distance was not restored');
console.log('messages scroll handoff runtime: ok');
