/* What PosterChanOS asks for on first boot, and in what order.
 *
 * The order is not cosmetic. A fresh install has NO NETWORK, so every later step fails in a way
 * that looks like the step itself is broken: the instance picker cannot reach anything, Tor cannot
 * bootstrap, and a remote signer cannot be contacted. Ask for wifi first and each subsequent screen
 * is answerable; ask for it fourth and somebody is typing an instance URL at a machine with no
 * radio, being told the instance is down.
 *
 * A STEP IS SKIPPED WHEN IT IS ALREADY SATISFIED, NEVER WHEN IT IS MERELY DIFFICULT. Ethernet
 * plugged in means the network step has nothing to ask; an instance already configured means the
 * picker has nothing to ask. But "the wifi scan failed" is not "the network is fine" — an
 * unanswerable step stops the flow and says so, because carrying on produces four screens of
 * failures whose real cause was two screens ago.
 *
 * AND IT IS RESUMABLE. Somebody who closes the laptop half way through must come back to the step
 * they were on, not to the beginning and not to a half-configured machine. State is derived from
 * the WORLD each time — is there a route, is there an instance, is there a key — rather than from a
 * "which step were we on" counter, which is the thing that goes stale when somebody fixes something
 * outside the wizard.
 *
 * DOM-free: tests/client/test_os_firstrun.py runs this file under node.
 */
(function(root){
  'use strict';

  const STEPS = ['network', 'instance', 'tor', 'signin', 'account'];

  /** What each step needs before it can be considered answered. */
  function stepState(world){
    const w = world || {};
    const out = {};

    /* NETWORK. Satisfied by ANY route out, not by wifi specifically — a machine on ethernet has
     * nothing to ask, and asking anyway is the wizard being pleased with itself. */
    out.network = w.online === true ? 'done'
                : w.netReadable === false ? 'blocked'      // NetworkManager could not be asked
                : 'todo';

    /* INSTANCE. A PosterChan node to talk to, or the deliberate choice to run without one — the
     * client works relay-only, and "no instance" is an answer rather than an omission. */
    out.instance = (w.instance || w.instanceSkipped) ? 'done' : 'todo';

    /* TOR is OPTIONAL and always has been. It is offered, and a person who says no has answered
     * it — which is why this is not `w.tor === true`. */
    out.tor = (w.torChosen === true || w.torSkipped === true) ? 'done' : 'todo';

    out.signin = w.pubkey ? 'done' : 'todo';

    /* THE ACCOUNT is not something a person answers, it is something the machine does once it
     * knows who they are — and it cannot be attempted before that. */
    out.account = !w.pubkey ? 'todo'
                : w.homeReady === true ? 'done'
                : w.provisionFailed ? 'blocked'
                : 'todo';
    return out;
  }

  /* THE STEP TO SHOW. The first that is not done — except that a BLOCKED step wins outright, even
   * if a later one is still todo: continuing past something that could not be answered is how a
   * wizard ends up reporting four unrelated failures whose cause was the first one. */
  function nextStep(world){
    const st = stepState(world);
    for(const s of STEPS) if(st[s] === 'blocked') return { step: s, blocked: true, state: st };
    for(const s of STEPS) if(st[s] !== 'done') return { step: s, blocked: false, state: st };
    return { step: null, blocked: false, state: st, done: true };
  }

  /** Is there anything left to ask at all? */
  const firstRunNeeded = (world) => !nextStep(world).done;

  /* WHETHER TO TAKE THE SCREEN AT BOOT, WHICH IS A DIFFERENT QUESTION FROM WHETHER ANYTHING IS
   * UNANSWERED — and using the second one for the first is how a setup wizard seizes a machine
   * somebody was already using.
   *
   * Tor is optional, an instance is optional, and this client runs perfectly well signed out; so
   * on a machine that already has a desktop, every remaining step is a question, not an obstacle,
   * and a question is not a reason to stand in front of somebody's computer. Worse, the flow would
   * then walk them to the sign-in step — which is deliberately not skippable — and a machine that
   * was working as a guest a minute ago would refuse to show its desktop without a key.
   *
   * A machine is UNUSABLE, and worth interrupting for, in exactly two cases: it has no way to reach
   * the network, or nothing about it has ever been decided — no instance, no key. That second one
   * is a computer out of a box, which is what this wizard is for. Everything else is offered from
   * Settings, where a question belongs. */
  function machineUnusable(world){
    const st = stepState(world);
    if(st.network !== 'done') return true;
    /* A MACHINE NOBODY HAS SIGNED INTO YET IS NOT A USABLE MACHINE, whatever else is configured.
     *
     * The rule used to be "an instance OR a signin makes it usable", which is right for a machine
     * whose owner signed out and wrong for the first boot of a new one -- and the live disc is
     * always the second. On a hypervisor it has DHCP and it ships an instance, so both halves said
     * "fine": the welcome never ran and a fresh boot landed on a desktop with a sign-in prompt and
     * no explanation. Reported as "it booted me to a desktop, but not logged in" and "you miss that
     * welcome screen".
     *
     * `everHadAccount` is what separates the two, and it is read from the account switcher's own
     * list rather than from a flag: that list survives signing out, exactly so somebody can sign
     * back in. Never signed in on this machine -> walk them through it. Signed out with accounts
     * remembered -> they know where the button is, do not nag. */
    /* ...unless they have said so. `signinSkipped` is an ANSWER, recorded like the instance and Tor
     * ones beside it -- without it somebody who deliberately browses signed out is asked again on
     * every boot, because `everHadAccount` stays false for ever. The welcome would then nag the one
     * person it can never help. */
    if(st.signin !== 'done' && !world.everHadAccount && !world.signinSkipped) return true;
    return st.instance !== 'done' && st.signin !== 'done';
  }

  /* WHAT A STEP MAY BE SKIPPED WITH. Deliberately per-step rather than a general "skip" button:
   * the instance and Tor are genuine choices, the network is not one you can decline your way past,
   * and an account is not a question.
   *
   * SIGNIN IS SKIPPABLE, AND IT HAS TO BE NOW THAT A FIRST BOOT IS INTERRUPTED. The client reads
   * the public timeline signed out, so "no key yet" is a real way to use this and not a broken
   * state. Left unskippable while `machineUnusable` seizes a machine nobody has signed into, the
   * welcome becomes a wall across the only screen -- somebody who booted a live disc to look at it
   * could not reach the desktop at all. That is a worse failure than the one the interruption
   * fixes, so the two changes go together. */
  const SKIPPABLE = { instance: true, tor: true, network: false, signin: true, account: false };
  const canSkip = (step) => SKIPPABLE[step] === true;

  /* DEDUPED BY SSID, STRONGEST KEPT — the same rule the network module applies, repeated here
   * because the wizard also renders a list and two lists of the same networks that disagree about
   * their order is worse than either. */
  function networksForPicker(list){
    const best = new Map();
    for(const n of (list || [])){
      if(!n || !n.ssid) continue;
      const had = best.get(n.ssid);
      if(!had || n.active || (n.signal || 0) > (had.signal || 0)) best.set(n.ssid, n);
    }
    /* COERCED, because a row from a scan may simply not carry `active` — and `undefined - undefined`
     * is NaN, which a comparator treats as "no opinion". The list then comes back in whatever order
     * the map happened to hold, with the network you are CONNECTED TO somewhere in the middle. */
    return [...best.values()].sort((a, b) =>
      ((b.active ? 1 : 0) - (a.active ? 1 : 0)) || ((b.signal || 0) - (a.signal || 0)));
  }

  const API = { STEPS, stepState, nextStep, firstRunNeeded, machineUnusable, canSkip,
                networksForPicker };
  root.PCFirstRun = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
