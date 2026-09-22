/* ES-module face of tests/client/client_source.cjs — see there for why harnesses read this. */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const cs = require('./client_source.cjs');
export const { CLIENT, splitModules, clientSource, clientSourceAt, installStateGlobals } = cs;
