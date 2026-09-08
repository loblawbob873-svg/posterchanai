"""Shared compatibility vectors execute the actual Java and JavaScript implementations."""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
VECTORS=ROOT/'tests/fixtures/sms_reactions.json'


def test_javascript_sms_reaction_vectors():
    script=r'''
const assert=require('node:assert/strict');
const api=require(process.argv[1]),vectors=require(process.argv[2]);
for(const v of vectors.parse) assert.deepEqual(api.parse(v.body),v.expected,v.body);
for(const v of vectors.projection){
 const original=JSON.stringify(v.rows);
 const result=api.project(v.rows,v.historyComplete);
 assert.equal(JSON.stringify(v.rows),original,'mutated '+v.name);
 assert.deepEqual([...result.consumedIds].sort(),[...v.consumedIds].sort(),v.name);
 const chips=Object.values(result.chipsByTarget).flat().map(c=>[c.target,c.actor,c.kind,c.sourceId]);
 assert.deepEqual(chips.sort(),[...v.chips].sort(),v.name);
}
console.log(vectors.parse.length+vectors.projection.length+' vectors passed');
'''
    result=subprocess.run(['node','-e',script,str(ROOT/'static/js/client/sms-reactions.js'),str(VECTORS)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_java_sms_reaction_vectors(tmp_path):
    vectors=json.loads(VECTORS.read_text())
    q=lambda value:json.dumps(value,ensure_ascii=False)
    statements=[]
    for i,v in enumerate(vectors['parse']):
        statements.append('{SmsReactions.Parsed p=SmsReactions.parse('+q(v['body'])+');')
        if v['expected'] is None:statements.append(f'check(p==null,"parse {i}");')
        else:
            statements.append(f'check(p!=null,"parse {i}");')
            for k,value in v['expected'].items():statements.append('check(p.'+k+'.equals('+q(value)+'),"parse '+str(i)+' '+k+'");')
        statements.append('}')
    for v in vectors['projection']:
        statements.append('{List<SmsReactions.Message> rows=new ArrayList<>();')
        for m in v['rows']:
            args=[q(m[k]) for k in ['id','thread','actor','body']]+[str(m['date'])+'L']+[str(m[k]).lower() for k in ['incoming','eligible','group']]
            statements.append('rows.add(new SmsReactions.Message('+','.join(args)+'));')
        statements.append('SmsReactions.Projection p=SmsReactions.project(rows,'+str(v['historyComplete']).lower()+');')
        expected=','.join(q(x) for x in v['consumedIds'])
        statements.append('check(p.consumedIds.equals(new HashSet<String>(Arrays.asList('+expected+'))),'+q(v['name']+' consumed')+');')
        statements.append('Set<List<String>> chips=new HashSet<>();for(List<SmsReactions.Chip> cs:p.chipsByTarget.values())for(SmsReactions.Chip c:cs)chips.add(Arrays.asList(c.target,c.actor,c.kind,c.sourceId));')
        expected=','.join('Arrays.asList('+','.join(q(x) for x in chip)+')' for chip in v['chips'])
        statements.append('check(chips.equals(new HashSet<List<String>>(Arrays.asList('+expected+'))),'+q(v['name']+' chips')+');}')
    harness='import java.util.*;import place.poster.app.sms.SmsReactions;public class Probe {static void check(boolean b,String m){if(!b)throw new AssertionError(m);}public static void main(String[]a){'+''.join(statements)+'}}'
    (tmp_path/'Probe.java').write_text(harness)
    result=subprocess.run(['javac','-encoding','UTF-8','-d',str(tmp_path),str(ROOT/'mobile/android/app/src/main/java/place/poster/app/sms/SmsReactions.java'),str(tmp_path/'Probe.java')],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    result=subprocess.run(['java','-cp',str(tmp_path),'Probe'],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
