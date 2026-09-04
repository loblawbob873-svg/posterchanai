'use strict';
const path = require('path');

function value(argv, name) {
  const prefix = `--${name}=`;
  const found = argv.find(arg => String(arg).startsWith(prefix));
  return found ? String(found).slice(prefix.length) : '';
}

/* An installed verifier is a separate application instance, not a second request for the desktop
 * singleton. Every field is redundant on purpose: a typo must exit before Electron can signal or
 * focus the canonical process. */
function resolve(argv, env) {
  const token = value(argv, 'pc-diagnostic-token');
  const profile = value(argv, 'pc-diagnostic-profile');
  /* `--pc-diagnostic-swaysock` was the flag's name when Sway was the only compositor. The new name
   * is what the launcher passes; the old one is still read so a verifier started from an older
   * script fails its checks rather than skipping them (an unrecognised flag makes `socket` empty,
   * which is exactly the "no diagnostic launch" case and would silently let it join the canonical
   * singleton instead). */
  const socket = value(argv, 'pc-diagnostic-socket') || value(argv, 'pc-diagnostic-swaysock');
  const any = token || profile || socket || env.PC_DIAGNOSTIC_TOKEN;
  if (!any) return null;
  if (!/^[a-z0-9]{12,64}$/.test(token)) throw new Error('invalid diagnostic token');
  const root = `/tmp/pc-installed-diagnostic.${token}`;
  const expectedProfile = path.join(root, 'profile');
  const runtime = path.join(root, 'runtime');
  if (path.resolve(profile) !== expectedProfile) throw new Error('diagnostic profile is outside its token domain');
  if (env.PC_DIAGNOSTIC_TOKEN !== token) throw new Error('diagnostic environment token does not match');
  if (path.resolve(env.XDG_RUNTIME_DIR || '') !== runtime) throw new Error('diagnostic runtime directory does not match');
  const live = env.WAYFIRE_SOCKET || env.SWAYSOCK || '';
  if (!socket || path.resolve(socket) !== path.resolve(live) ||
      !path.resolve(socket).startsWith(runtime + path.sep))
    throw new Error('diagnostic compositor socket does not match');
  return { token, root, profile, runtime, socket };
}

module.exports = { resolve };
