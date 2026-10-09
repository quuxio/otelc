import ts from 'typescript';

export function functionName(node, parents, file) {
  if (ts.isConstructorDeclaration(node)) return 'constructor';
  if (node.name && (ts.isIdentifier(node.name) || ts.isPrivateIdentifier(node.name) || ts.isStringLiteral(node.name) || ts.isNumericLiteral(node.name))) return node.name.text;
  const parent = parents.at(-1);
  if (parent && (ts.isVariableDeclaration(parent) || ts.isPropertyAssignment(parent) || ts.isPropertyDeclaration(parent)) && parent.name && ts.isIdentifier(parent.name)) return parent.name.text;
  const position = file.getLineAndCharacterOfPosition(node.getStart(file));
  return `<anonymous>@${position.line + 1}:${position.character + 1}`;
}
function tag(node, parents, file) {
  let result = null;
  const locations = [node, ...parents.slice(-2)].filter(location => location && !ts.isSourceFile(location));
  for (const location of locations) {
    for (const comment of ts.getLeadingCommentRanges(file.text, location.getFullStart()) ?? []) {
      for (const raw of file.text.slice(comment.pos, comment.end).replace(/^\/\/?\*?/, '').replace(/\*\/$/, '').split('\n')) {
        const line = raw.trim().replace(/^\*\s*/, '').replace(/^@/, '');
        if (!line.startsWith('otelc.')) continue;
        if (!['otelc.instrument', 'otelc.exclude'].includes(line)) throw new Error('unsupported otelc annotation');
        if (result !== 'otelc.exclude') result = line;
      }
    }
  }
  return result;
}
export function identity(node, parents, names, file, prefix, plan) {
  const name = (ts.isGetAccessorDeclaration(node) ? 'get ' : ts.isSetAccessorDeclaration(node) ? 'set ' : '') + functionName(node, parents, file);
  return { name: [prefix, ...names, name].join('.'), line: file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1, annotation: plan.annotations.read_existing ? tag(node, parents, file) : null };
}
export function collectIdentities(source, filename, sourceName, plan) {
  const file = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
  if (file.parseDiagnostics.length) throw new Error('TypeScript native emission requires syntax understood by the pinned identity parser');
  const prefix = sourceName.replace(/\.(?:ts|mts|cts)$/, '').replaceAll('/', '.');
  const result = [];
  function visit(node, parents = [], names = []) {
    const callable = ts.isFunctionLike(node) && node.body;
    const container = ts.isClassLike(node) || ts.isModuleDeclaration(node);
    const position = file.getLineAndCharacterOfPosition(node.getStart(file));
    const owner = parents.at(-1);
    const object = ts.isObjectLiteralExpression(node) ? owner && (ts.isVariableDeclaration(owner) || ts.isPropertyAssignment(owner)) && ts.isIdentifier(owner.name) ? owner.name.text : `<object>@${position.line + 1}:${position.character + 1}` : null;
    const metadata = callable ? identity(node, parents, names, file, prefix, plan) : null;
    if (metadata) result.push({ position: ts.isBlock(node.body) ? node.body.getStart(file) + 1 : node.getStart(file), block: ts.isBlock(node.body), metadata });
    const nested = callable ? [...names, (ts.isGetAccessorDeclaration(node) ? 'get ' : ts.isSetAccessorDeclaration(node) ? 'set ' : '') + functionName(node, parents, file)] : container ? [...names, node.name?.text ?? '<class>'] : object ? [...names, object] : names;
    ts.forEachChild(node, child => visit(child, [...parents, node], nested));
  }
  visit(file); return result;
}
