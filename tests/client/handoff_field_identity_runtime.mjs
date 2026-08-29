import fs from 'node:fs';

const source = fs.readFileSync(new URL('../../static/js/client/os.js', import.meta.url), 'utf8');
const declarations = source.slice(source.indexOf('const _handoffFields='), source.indexOf('function captureHandoffUI('));
const findField = Function(`${declarations}; return _findHandoffField;`)();

const field = (tagName, type, name, id = '') => ({tagName, type, name, id});
const original = [
  field('INPUT', 'search', 'q'),
  field('INPUT', 'radio', 'audience'),
  field('INPUT', 'radio', 'audience'),
  field('TEXTAREA', 'textarea', 'draft'),
];
const rendered = [
  field('INPUT', 'hidden', 'feature'),
  field('INPUT', 'search', 'q'),
  field('INPUT', 'radio', 'audience'),
  field('INPUT', 'radio', 'audience'),
  field('TEXTAREA', 'textarea', 'draft'),
];
const root = {
  querySelectorAll(selector) {
    return selector === '[id]' ? rendered.filter(element => element.id) : rendered;
  },
};
const snapshot = index => {
  const current = original[index];
  const signature = [current.tagName, current.type, current.name].join('\n');
  return {
    i: index,
    name: current.name,
    sig: signature,
    n: original.slice(0, index).filter(element =>
      [element.tagName, element.type, element.name].join('\n') === signature).length,
  };
};

if (findField(root, snapshot(3)) !== rendered[4]) {
  throw new Error('draft restored by shifted global ordinal');
}
if (findField(root, snapshot(2)) !== rendered[3]) {
  throw new Error('second same-name control lost its group ordinal');
}

console.log('handoff field identity runtime: ok');
