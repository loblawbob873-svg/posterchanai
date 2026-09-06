"""Exercise the shipped picker callback without transferring a large file over desktop IPC."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('mode', ['large', 'cancel', 'unsupported', 'browser'])
def test_texts_picker_uses_disk_backed_file_handle_and_preserves_fallbacks(mode):
    source = (ROOT / 'static/js/client/sms.js').read_text()
    callback = source[source.index('const fromDevice = async () => {'):source.index('    attachBtn.onclick = async () => {', source.index('const fromDevice = async () => {'))]
    script = r'''
const mode=process.argv[1], calls=[];
const file={name:'holiday.mp4',size:300*1024*1024,type:'video/mp4'};
const pcHost={pickFile:async()=>{calls.push('ipc');return {data:new Uint8Array([1]),name:'small.jpg',type:'image/jpeg'};}};
const window={pcHost};
if(mode==='browser')delete window.pcHost;
window.showOpenFilePicker=async()=>{
  calls.push('disk-picker');
  if(mode==='cancel')throw Object.assign(new Error('cancelled'),{name:'AbortError'});
  if(mode==='unsupported')throw Object.assign(new Error('unsupported origin'),{name:'SecurityError'});
  return [{getFile:async()=>file}];
};
const File=require('buffer').File;
const acceptFile=file=>{calls.push(['selected',file.name,file.size]);return true;};
const pick={click:()=>calls.push('input')};
const PC={toast:message=>calls.push(['error',message])};
''' + callback + '\nfromDevice().then(result=>console.log(JSON.stringify({result,calls})));'
    proc = subprocess.run(['node', '-e', script, mode], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    if mode == 'large':
        assert got == {'result': True, 'calls': ['disk-picker', ['selected', 'holiday.mp4', 300*1024*1024]]}
    elif mode == 'cancel':
        assert got == {'result': False, 'calls': ['disk-picker']}
    elif mode == 'unsupported':
        assert got['result'] and got['calls'][1] == 'ipc'
    else:
        assert got == {'result': True, 'calls': ['input']}
