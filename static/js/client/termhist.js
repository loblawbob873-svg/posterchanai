/* Shell history for the PosterChanOS terminal, as EPHEMERAL Nostr events.
 *
 * Your history follows you between your own devices and is stored by nobody: ephemeral kinds
 * (20000–29999) are forwarded by relays and never written down, so a command you ran is visible to
 * the terminals you have open right now and then it is gone. That is the whole design — not a
 * synced document you would then have to think about deleting.
 *
 * THE DANGER IS PASSWORDS, AND IT IS NOT HYPOTHETICAL. A terminal's input stream carries every
 * keystroke, including the ones typed at `sudo`, at an ssh passphrase prompt, at `mysql -p`. Publish
 * the input stream and you publish those — encrypted to yourself, but published, to a relay, for
 * ever in any log that happens to keep them. So this module NEVER decides on its own that a line is
 * safe. A line is publishable only when the shell ECHOED it back, because echo is exactly what a
 * password prompt turns off — the terminal driver stops echoing, which is why the screen shows
 * nothing while you type one. That is not a heuristic about what the text looks like; it is the same
 * signal the operating system uses.
 *
 * Two further rules, because echo is necessary and not sufficient:
 *   - a line that never ended in Enter is not a command, it is what somebody is still typing;
 *   - a line matching a secret-shaped pattern is dropped even if it was echoed, because `export
 *     TOKEN=…` and `curl -H "Authorization: …"` are echoed perfectly and are still secrets.
 *
 * DOM-free on purpose: tests/test_term_history.py runs this file under node.
 */
(function(root){
  'use strict';

  const KIND = 21078;              // ephemeral; mirrors 30078, which is the app's stored kind
  const LABEL = 'pcai-shell';
  const MAX_LINE = 4096;           // a pasted blob is not history
  const KEEP = 500;                // the ring a device offers to ↑

  /* Echoed but still secret. Deliberately narrow: this is a backstop for the obvious shapes, not a
   * classifier — the echo rule is what does the real work, and a broad regex here would quietly
   * drop ordinary commands and leave somebody wondering where their history went. */
  const SECRET_RX = new RegExp([
    '(^|\\s)(export|set|env)\\s+\\w*(TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|NSEC|PRIVATE)\\w*=',
    '(^|\\s)-{1,2}(password|passwd|token|secret|api-?key)[= ]',
    '(^|\\s)(Authorization|Bearer)\\s*[: ]',
    'nsec1[0-9a-z]{20,}',
    '(^|\\s)(mysql|psql|curl|wget)\\b.*\\s-{1,2}p(assword)?[= ]\\S',
  ].join('|'), 'i');

  /** Assembles keystrokes into candidate lines, and answers whether the shell echoed them. */
  function makeCollector(){
    let buf = '';                  // what has been typed since the last Enter
    let echoed = 0;                // how much of `buf` has been seen coming back
    let armed = true;              // false once we know this line is not echoed (a password)

    /* Keystrokes on the way to the shell. Control characters are handled rather than concatenated:
     * a line the user backspaced over is not the line they ran, and ^C or ^U abandons it entirely. */
    let esc = 0;                   // 0 = text, 1 = just saw ESC, 2 = inside a CSI/OSC sequence

    function typed(data){
      const out = [];
      for(const ch of String(data == null ? '' : data)){
        const c = ch.charCodeAt(0);
        /* ESCAPE SEQUENCES ARE CONSUMED, NOT JUST THEIR FIRST BYTE. An arrow key is ESC [ A — three
         * bytes, of which only ESC is a control character. Skipping the control byte alone leaves
         * "[A" in the line, so pressing ↑ mid-command silently corrupts what gets published, and the
         * history fills with commands nobody typed. */
        if(esc === 1){ esc = (ch === '[' || ch === ']' || ch === 'O') ? 2 : 0; continue; }
        if(esc === 2){ if(c >= 0x40 && c <= 0x7e) esc = 0; continue; }
        if(c === 27){ esc = 1; continue; }
        if(ch === '\r' || ch === '\n'){
          const line = buf.trim();
          const ok = armed && line && echoed >= Math.min(line.length, 3) && line.length <= MAX_LINE
                     && !SECRET_RX.test(line);
          if(line) out.push({ line, publish: !!ok,
                              why: !armed ? 'not echoed — a password prompt'
                                   : !line ? 'empty'
                                   : line.length > MAX_LINE ? 'too long to be history'
                                   : SECRET_RX.test(line) ? 'looks like a secret'
                                   : echoed < Math.min(line.length, 3) ? 'not echoed' : '' });
          buf = ''; echoed = 0; armed = true; esc = 0;
          continue;
        }
        if(c === 3 || c === 21 || c === 23){        // ^C, ^U, ^W — the line is abandoned
          buf = ''; echoed = 0; armed = true; esc = 0; continue;
        }
        if(c === 127 || c === 8){                    // backspace
          buf = buf.slice(0, -1); echoed = Math.min(echoed, buf.length); continue;
        }
        if(c < 32) continue;                         // arrows, tabs, escapes — not content
        buf += ch;
      }
      return out;
    }

    /* Bytes coming BACK from the shell. Echo is the permission: the terminal driver stops echoing
     * for a password prompt, which is why the screen stays blank while you type one. Counted rather
     * than matched exactly, because a shell rewrites the line for completion and colouring. */
    function saw(data){
      if(!buf) return;
      const s = String(data == null ? '' : data);
      let i = echoed;
      while(i < buf.length && s.indexOf(buf.slice(echoed, i + 1)) >= 0) i++;
      if(i > echoed) echoed = i;
      /* A prompt that asks for a secret by name, with nothing echoed, disarms the line outright —
       * belt and braces for the case where a stray byte happens to match. */
      if(/pass(word|phrase)|\bPIN\b|secret key/i.test(s) && echoed === 0) armed = false;
    }

    return { typed, saw,
             pending: () => buf,
             disarm: () => { armed = false; },
             _state: () => ({ buf, echoed, armed }) };
  }

  /** The ring a device offers to ↑ — newest last, deduped against the immediately preceding entry. */
  function makeRing(limit){
    const cap = limit || KEEP;
    const rows = [];
    return {
      add(line, at, from){
        if(!line) return false;
        const last = rows[rows.length - 1];
        if(last && last.line === line) return false;     // ↑↑ on the same command is noise
        rows.push({ line, at: at || 0, from: from || '' });
        while(rows.length > cap) rows.shift();
        return true;
      },
      all: () => rows.slice(),
      /* Merged for display: another device's history interleaves by TIME, not by arrival — a phone
       * that reconnects and replays five minutes of commands must not bury what you just ran. */
      merged(){ return rows.slice().sort((a, b) => (a.at || 0) - (b.at || 0)); },
      size: () => rows.length,
    };
  }

  /** The event a publishable line becomes. The command itself is the ciphertext's business. */
  function historyEvent(ciphertext, now){
    return { kind: KIND, content: String(ciphertext || ''),
             created_at: Math.floor((now || 0) / 1000) || Math.floor(Date.now() / 1000),
             tags: [['l', LABEL]] };
  }

  const API = { makeCollector, makeRing, historyEvent, KIND, LABEL, SECRET_RX, KEEP };
  root.PCTermHistory = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
