// Parser-based probes are inserted in memory; original source and signatures stay intact.
import { transformSync } from '@babel/core';
import * as t from '@babel/types';
import { Selection } from './policy.mjs';
import { fileURLToPath } from 'node:url';

const probes = fileURLToPath(new URL('./probes.cjs', import.meta.url));

export const MARKER = '@quux.otelc.generated';
function localName(path) {
  const node = path.node;
  if (node.id?.name) return node.id.name;
  if (node.key?.name) return (t.isPrivateName(node.key) ? '#' : '') + node.key.name;
  if (node.key?.id?.name) return '#' + node.key.id.name;
  if (node.key?.value !== undefined) return String(node.key.value);
  if (path.parentPath.isVariableDeclarator() && t.isIdentifier(path.parentPath.node.id)) return path.parentPath.node.id.name;
  if (path.parentPath.isObjectProperty() && !path.parentPath.node.computed) return path.parentPath.node.key.name ?? String(path.parentPath.node.key.value);
  return `<anonymous>@${node.loc.start.line}`;
}
function displayName(path, source) {
  const parts = [localName(path)];
  for (let parent = path.parentPath; parent; parent = parent.parentPath) {
    if (parent.isFunction()) parts.unshift(localName(parent));
    else if (parent.isClass()) parts.unshift(parent.node.id?.name ?? '<class>');
  }
  return source.replace(/\.(?:mjs|cjs|js)$/, '').replaceAll('/', '.') + '.' + parts.join('.');
}
function annotation(path) {
  let result = null;
  const comments = [...(path.node.leadingComments ?? []), ...(path.parentPath.node.leadingComments ?? []), ...(path.parentPath.parentPath?.node.leadingComments ?? [])];
  for (const comment of comments) {
    for (const raw of comment.value.split('\n')) {
      const line = raw.trim().replace(/^\*\s*/, '').replace(/^@/, '');
      if (!line.startsWith('otelc.')) continue;
      if (!['otelc.instrument', 'otelc.exclude'].includes(line)) throw path.buildCodeFrameError('unsupported otelc annotation');
      if (result !== 'otelc.exclude') result = line;
    }
  }
  return result;
}
export function transform(source, filename, sourceName, plan, runtime) {
  if (source.includes(MARKER)) throw new Error('source is already instrumented');
  const selection = new Selection(plan.function_matchers);
  const inventory = [];
  let helper;
  let instrumented = false;
  const result = transformSync(source, {
    filename, configFile: false, babelrc: false, sourceType: 'unambiguous', sourceMaps: true,
    sourceFileName: filename, parserOpts: { allowReturnOutsideFunction: true },
    plugins: [() => ({ visitor: { Program: { enter(path) {
      helper = path.scope.generateUidIdentifier('otelc_probes');
    }, exit(path) {
      if (!instrumented) return;
      if (path.node.sourceType === 'module') path.unshiftContainer('body', t.importDeclaration([t.importDefaultSpecifier(helper)], t.stringLiteral(probes)));
      else {
        if (path.scope.hasOwnBinding('require')) throw path.buildCodeFrameError('CommonJS top-level require shadowing is unsupported');
        path.unshiftContainer('body', t.variableDeclaration('const', [t.variableDeclarator(helper, t.callExpression(t.identifier('require'), [t.stringLiteral(probes)]))]));
      }
    } }, Function: { exit(path) {
      if (!path.node.body) return;
      const name = displayName(path, sourceName);
      const tag = plan.annotations.read_existing ? annotation(path) : null;
      const selected = tag !== 'otelc.exclude' && selection.accepts(name, tag === 'otelc.instrument');
      inventory.push({ name, selected, line: path.node.loc.start.line });
      if (!selected || !runtime.register(name)) return;
      instrumented = true;
      path.traverse({ CallExpression(call) {
        if (t.isIdentifier(call.node.callee, { name: 'eval' }) && !call.scope.getBinding('eval')) throw call.buildCodeFrameError('direct eval in a selected function is unsupported');
      } });
      const body = t.isBlockStatement(path.node.body) ? path.node.body : t.blockStatement([t.returnStatement(path.node.body)]);
      const token = path.scope.generateUidIdentifier('otelc_token');
      const unwound = path.scope.generateUidIdentifier('otelc_unwound');
      const error = path.scope.generateUidIdentifier('otelc_error');
      const runtimeNode = () => t.cloneNode(helper);
      const begin = t.variableDeclaration('const', [t.variableDeclarator(token, t.callExpression(t.memberExpression(runtimeNode(), t.identifier('enter')), [t.stringLiteral(name)]))]);
      begin.leadingComments = [{ type: 'CommentBlock', value: MARKER }];
      if (plan.annotations.inject_generated) begin.leadingComments.push({ type: 'CommentBlock', value: 'otelc.instrument' });
      const statements = [begin, t.variableDeclaration('let', [t.variableDeclarator(unwound, t.booleanLiteral(false))]), t.tryStatement(t.blockStatement(body.body), t.catchClause(error, t.blockStatement([t.expressionStatement(t.assignmentExpression('=', unwound, t.booleanLiteral(true))), t.throwStatement(error)])), t.blockStatement([t.expressionStatement(t.callExpression(t.memberExpression(runtimeNode(), t.identifier('exit')), [token, unwound]))]))];
      path.node.body = t.blockStatement(statements);
      path.node.body.directives = body.directives;
      path.node.expression = false;
    } } } })]
  });
  return { code: result.code + '\n//# sourceMappingURL=data:application/json;base64,' + Buffer.from(JSON.stringify(result.map)).toString('base64'), functions: inventory };
}
