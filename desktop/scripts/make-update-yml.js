/* Write electron-updater's feed file (latest.yml / latest-linux.yml) for the artifact just built, if
 * electron-builder didn't already emit one. `--publish never` is what CI uses (the release is created
 * by the workflow, not by electron-builder), and whether the feed file is written in that mode has
 * changed between versions — without it the shipped app silently never sees an update. So: generate it
 * ourselves when it's missing, from the exact bytes we're about to upload.
 *
 *   node scripts/make-update-yml.js <dist-dir> <primary-artifact> <yml-name> [extra-artifact ...]
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const [dist, primary, ymlName, ...extras] = process.argv.slice(2);
const ymlPath = path.join(dist, ymlName);
if (fs.existsSync(ymlPath)) {
  console.log(`${ymlName} already written by electron-builder — leaving it alone`);
  process.exit(0);
}

const version = require(path.resolve(__dirname, '..', 'package.json')).version;
const sha512 = (f) => crypto.createHash('sha512').update(fs.readFileSync(f)).digest('base64');
const entry = (name) => {
  const f = path.join(dist, name);
  return { url: name, sha512: sha512(f), size: fs.statSync(f).size };
};

const files = [primary, ...extras].filter((n) => fs.existsSync(path.join(dist, n))).map(entry);
if (!files.length) { console.error(`no artifacts found in ${dist}`); process.exit(1); }
const head = files[0];

const yml = [
  `version: ${version}`,
  'files:',
  ...files.flatMap((f) => [`  - url: ${f.url}`, `    sha512: ${f.sha512}`, `    size: ${f.size}`]),
  `path: ${head.url}`,
  `sha512: ${head.sha512}`,
  `releaseDate: '${new Date().toISOString()}'`,
  '',
].join('\n');

fs.writeFileSync(ymlPath, yml);
console.log(`wrote ${ymlPath}:\n${yml}`);
