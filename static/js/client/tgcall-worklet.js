/* Audio for Telegram calls (tgcall.js). Two AudioWorklet processors, run on the audio thread:
 *   pc-tg-capture — the microphone as 10 ms frames of PCM16 at the context's rate (48 kHz), which is
 *                   exactly the frame ntgcalls reads on the node (app/services/telegram_client/calls.py);
 *   pc-tg-play    — the other side's PCM16, through a small jitter buffer: 60 ms before it starts,
 *                   silence on an underrun, and never more than 400 ms behind (the oldest audio goes). */
class Capture extends AudioWorkletProcessor {
  constructor(){ super(); this.buf = new Int16Array(480); this.n = 0; }
  process(inputs){
    const ch = inputs[0] && inputs[0][0];
    if(ch){
      for(let i = 0; i < ch.length; i++){
        const v = Math.max(-1, Math.min(1, ch[i]));
        this.buf[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if(this.n === 480){ this.port.postMessage(this.buf.buffer, [this.buf.buffer]); this.buf = new Int16Array(480); this.n = 0; }
      }
    }
    return true;
  }
}
class Play extends AudioWorkletProcessor {
  constructor(){
    super(); this.q = []; this.have = 0; this.started = false;
    this.port.onmessage = e => {
      const s = new Int16Array(e.data), f = new Float32Array(s.length);
      for(let i = 0; i < s.length; i++) f[i] = s[i] / 0x8000;
      this.q.push(f); this.have += f.length;
      while(this.have > 19200){ this.have -= this.q[0].length; this.q.shift(); }     // > 400 ms behind
      if(!this.started && this.have >= 2880) this.started = true;                     // 60 ms prebuffer
    };
  }
  process(_inputs, outputs){
    const out = outputs[0] && outputs[0][0]; if(!out) return true;
    let i = 0;
    while(this.started && i < out.length && this.q.length){
      const head = this.q[0], take = Math.min(head.length, out.length - i);
      out.set(head.subarray(0, take), i); i += take; this.have -= take;
      if(take === head.length) this.q.shift(); else this.q[0] = head.subarray(take);
    }
    if(i < out.length){ out.fill(0, i); if(!this.q.length) this.started = false; }
    for(let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}
registerProcessor('pc-tg-capture', Capture);
registerProcessor('pc-tg-play', Play);
