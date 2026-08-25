/* Capacitor 6 compiles `import tar from "tar"` into a default-export shim. Secure tar 7 is ESM-aware
 * CommonJS and intentionally has no `.default`, so `cap sync` otherwise dies at template extraction.
 * Keep the patched dependency and repair the one generated call after every npm ci. */
const fs = require('fs');
const path = require('path');
const file = path.join(__dirname, '..', 'node_modules', '@capacitor', 'cli', 'dist', 'util', 'template.js');
let src = fs.readFileSync(file, 'utf8');
if (src.includes('tar_1.default.extract')) {
  src = src.replace('tar_1.default.extract', 'tar_1.extract');
  fs.writeFileSync(file, src);
} else if (!src.includes('tar_1.extract')) {
  throw new Error('Capacitor template layout changed; refusing an unverified tar compatibility patch');
}
