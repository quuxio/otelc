import ts from 'typescript-api';

export function functionName(node, parents, file) {
  if (ts.isConstructorDeclaration(node)) return 'constructor';
  if (node.name && (ts.isIdentifier(node.name) || ts.isPrivateIdentifier(node.name) || ts.isStringLiteral(node.name) || ts.isNumericLiteral(node.name))) return node.name.text;
  const parent = parents.at(-1);
  if (parent && (ts.isVariableDeclaration(parent) || ts.isPropertyAssignment(parent) || ts.isPropertyDeclaration(parent)) && parent.name && ts.isIdentifier(parent.name)) return parent.name.text;
  const position = file.getLineAndCharacterOfPosition(node.getStart(file));
  return `<anonymous>@${position.line + 1}:${position.character + 1}`;
}
function annotationLines(location, file) {
  return (ts.getLeadingCommentRanges(file.text, location.getFullStart()) ?? []).flatMap(comment =>
    file.text.slice(comment.pos, comment.end).replace(/^\/\/?\*?/, '').replace(/\*\/$/, '').split('\n'));
}
function tag(node, parents, file) {
  let result = null;
  const locations = [node, ...parents.slice(-2)].filter(location => location && !ts.isSourceFile(location));
  for (const location of locations) {
    for (const raw of annotationLines(location, file)) {
      const line = raw.trim().replace(/^\*\s*/, '').replace(/^@/, '');
      if (!line.startsWith('otelc.')) continue;
      if (!['otelc.instrument', 'otelc.exclude'].includes(line)) throw new Error('unsupported otelc annotation');
      if (result !== 'otelc.exclude') result = line;
    }
  }
  return result;
}
/** Name the original callable, including accessor kind, before compiler lowering. */
export function callableName(node, parents, file) {
  let prefix = '';
  if (ts.isGetAccessorDeclaration(node)) prefix = 'get ';
  if (ts.isSetAccessorDeclaration(node)) prefix = 'set ';
  return prefix + functionName(node, parents, file);
}
function objectName(node, parents, file) {
  const owner = parents.at(-1);
  if (owner && (ts.isVariableDeclaration(owner) || ts.isPropertyAssignment(owner)) && ts.isIdentifier(owner.name)) return owner.name.text;
  const position = file.getLineAndCharacterOfPosition(node.getStart(file));
  return `<object>@${position.line + 1}:${position.character + 1}`;
}
/** Keep the same lexical owner names for API/native and lifetime metadata. */
export function childNames(node, parents, names, file) {
  if (ts.isFunctionLike(node) && node.body) return [...names, callableName(node, parents, file)];
  if (ts.isClassLike(node) || ts.isModuleDeclaration(node)) return [...names, node.name?.text ?? '<class>'];
  if (ts.isObjectLiteralExpression(node)) return [...names, objectName(node, parents, file)];
  return names;
}
export function identity(node, parents, names, file, prefix, plan) {
  const name = callableName(node, parents, file);
  return { name: [prefix, ...names, name].join('.'), line: file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1, annotation: plan.annotations.read_existing ? tag(node, parents, file) : null };
}
export function collectIdentities(source, filename, sourceName, plan) {
  const file = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
  if (file.parseDiagnostics.length) throw new Error('TypeScript native emission requires syntax understood by the pinned identity parser');
  const prefix = sourceName.replace(/\.(?:ts|mts|cts)$/, '').replaceAll('/', '.');
  const result = [];
  function visit(node, parents = [], names = []) {
    const callable = ts.isFunctionLike(node) && node.body;
    const metadata = callable ? identity(node, parents, names, file, prefix, plan) : null;
    if (metadata) result.push({ position: ts.isBlock(node.body) ? node.body.getStart(file) + 1 : node.getStart(file), block: ts.isBlock(node.body), metadata });
    const nested = childNames(node, parents, names, file);
    ts.forEachChild(node, child => visit(child, [...parents, node], nested));
  }
  visit(file); return result;
}
