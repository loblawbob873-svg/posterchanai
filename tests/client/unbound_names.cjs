// Scope-aware unbound-name finder. Reports identifiers READ where no enclosing scope declares them, but which
// ARE declared somewhere else in the same file — the bug the file-wide checker cannot see (Concord's Invite
// button passed `current`, a name that only existed inside the header renderer: "current is not defined").
const acorn = require(process.env.PC_ACORN), fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const ast = acorn.parse(src, {ecmaVersion: 'latest', sourceType: 'script', locations: true, allowHashBang: true,
  allowReturnOutsideFunction: true});
const declaredAnywhere = new Set(), reads = [];
function patNames(p, out) {
  if (!p) return;
  if (p.type === 'Identifier') out.add(p.name);
  else if (p.type === 'ObjectPattern') p.properties.forEach(q => patNames(q.type === 'RestElement' ? q.argument : q.value, out));
  else if (p.type === 'ArrayPattern') p.elements.forEach(e => patNames(e, out));
  else if (p.type === 'RestElement') patNames(p.argument, out);
  else if (p.type === 'AssignmentPattern') patNames(p.left, out);
}
function hoisted(fnBody, out) {           // var + function declarations anywhere in a function body (not nested fns)
  (function walk(n) {
    if (!n || typeof n.type !== 'string') return;
    if (n.type === 'VariableDeclaration' && n.kind === 'var') n.declarations.forEach(d => patNames(d.id, out));
    if (n.type === 'FunctionDeclaration') { out.add(n.id.name); return; }
    if (/Function|Class/.test(n.type) && n !== fnBody) return;
    for (const k in n) { if (k === 'loc') continue; const v = n[k];
      if (Array.isArray(v)) v.forEach(walk); else if (v && typeof v.type === 'string') walk(v); }
  })(fnBody);
}
function blockDecls(stmts, out) {
  for (const s of stmts || []) {
    if (s.type === 'VariableDeclaration' && s.kind !== 'var') s.declarations.forEach(d => patNames(d.id, out));
    if (s.type === 'ClassDeclaration' && s.id) out.add(s.id.name);
    if (s.type === 'FunctionDeclaration') out.add(s.id.name);
  }
}
function walk(n, scopes, parent, key) {
  if (!n || typeof n.type !== 'string') return;
  let sc = scopes;
  const push = set => { set.forEach(x => declaredAnywhere.add(x)); sc = sc.concat([set]); };
  if (n.type === 'Program') { const s = new Set(); hoisted(n, s); blockDecls(n.body, s); push(s); }
  else if (/Function/.test(n.type)) {
    const s = new Set(); if (n.id && n.type === 'FunctionExpression') s.add(n.id.name);
    n.params.forEach(p => patNames(p, s)); s.add('arguments');
    if (n.body.type === 'BlockStatement') { hoisted(n.body, s); blockDecls(n.body.body, s); }
    push(s);
  } else if (n.type === 'BlockStatement' || n.type === 'StaticBlock') { const s = new Set(); blockDecls(n.body, s); push(s); }
  else if (n.type === 'SwitchStatement') { const s = new Set(); n.cases.forEach(c => blockDecls(c.consequent, s)); push(s); }
  else if (/^For(In|Of)?Statement$/.test(n.type)) { const s = new Set(); const d = n.init || n.left;
    if (d && d.type === 'VariableDeclaration') d.declarations.forEach(x => patNames(x.id, s)); push(s); }
  else if (n.type === 'CatchClause' && n.param) { const s = new Set(); patNames(n.param, s); push(s); }
  else if (n.type === 'ClassExpression' && n.id) { push(new Set([n.id.name])); }
  if (n.type === 'Identifier') {
    const notRead = parent && (
      (parent.type === 'MemberExpression' && key === 'property' && !parent.computed) ||
      ((parent.type === 'Property' || parent.type === 'MethodDefinition' || parent.type === 'PropertyDefinition') && key === 'key' && !parent.computed) ||
      parent.type === 'LabeledStatement' || parent.type === 'BreakStatement' || parent.type === 'ContinueStatement' ||
      ((parent.type === 'VariableDeclarator') && key === 'id') || (/Function|Class/.test(parent.type) && key === 'id') ||
      (parent.type === 'ExportSpecifier' || parent.type === 'ImportSpecifier'));
    if (!notRead && !sc.some(s => s.has(n.name))) reads.push({name: n.name, line: n.loc.start.line});
  }
  for (const k in n) { if (k === 'loc') continue; const v = n[k];
    if (Array.isArray(v)) v.forEach(c => c && typeof c.type === 'string' && walk(c, sc, n, k));
    else if (v && typeof v.type === 'string') walk(v, sc, n, k); }
}
walk(ast, [], null, null);
console.log(JSON.stringify(reads.filter(r => declaredAnywhere.has(r.name))));
