// Parser-based probes are inserted in memory; original source and signatures stay intact.
import { transformSync } from '@babel/core';
import * as t from '@babel/types';
import { Selection } from './policy.mjs';
import { fileURLToPath } from 'node:url';

const probes = fileURLToPath(new URL('./probes.cjs', import.meta.url));

export const MARKER = '@quux.otelc.generated';
export const IDENTITY = '@quux.otelc.identity ';
function localName(path) {
  const node = path.node;
  if (node.id?.name) return node.id.name;
  if (node.key?.name) return (t.isPrivateName(node.key) ? '#' : '') + node.key.name;
  if (node.key?.id?.name) return '#' + node.key.id.name;
  if (node.key?.value !== undefined) return String(node.key.value);
  if (path.parentPath.isVariableDeclarator() && t.isIdentifier(path.parentPath.node.id)) return path.parentPath.node.id.name;
  if (path.parentPath.isObjectProperty() && !path.parentPath.node.computed) return path.parentPath.node.key.name ?? String(path.parentPath.node.key.value);
  return `<anonymous>@${node.loc.start.line}:${node.loc.start.column + 1}`;
}
function displayName(path, source) {
  const parts = [(path.node.kind === 'get' || path.node.kind === 'set' ? path.node.kind + ' ' : '') + localName(path)];
  for (let parent = path.parentPath; parent; parent = parent.parentPath) {
    if (parent.isFunction()) parts.unshift(localName(parent));
    else if (parent.isClass()) parts.unshift(parent.node.id?.name ?? '<class>');
    else if (parent.isObjectExpression()) {
      const owner = parent.parentPath;
      parts.unshift(owner.isVariableDeclarator() && t.isIdentifier(owner.node.id) ? owner.node.id.name : owner.isObjectProperty() && !owner.node.computed ? String(owner.node.key.name ?? owner.node.key.value) : `<object>@${parent.node.loc.start.line}:${parent.node.loc.start.column + 1}`);
    }
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
export function transform(source, filename, sourceName, plan, runtime, prepared = null) {
  if (source.includes(MARKER)) throw new Error('source is already instrumented');
  const selection = new Selection(plan.function_matchers);
  const inventory = [];
  let helper;
  let format;
  let instrumented = false;
  const result = transformSync(source, {
    filename, configFile: false, babelrc: false, sourceType: prepared?.format === 'module' ? 'module' : prepared?.format === 'commonjs' ? 'script' : 'unambiguous', sourceMaps: true,
    sourceFileName: filename, inputSourceMap: prepared?.map, parserOpts: { allowReturnOutsideFunction: true },
    plugins: [() => ({ visitor: { Program: { enter(path) {
      format = path.node.sourceType === 'module' ? 'module' : 'commonjs';
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
      const identityComments = [...(path.node.leadingComments ?? []), ...((path.parentPath.isExportNamedDeclaration() || path.parentPath.isExportDefaultDeclaration()) ? path.parentPath.node.leadingComments ?? [] : [])];
      const identity = identityComments.find(comment => comment.value.startsWith(IDENTITY));
      if (prepared && !identity) return;
      const metadata = identity ? JSON.parse(Buffer.from(identity.value.slice(IDENTITY.length), 'base64').toString()) : null;
      const name = metadata?.name ?? displayName(path, sourceName);
      const tag = plan.annotations.read_existing ? metadata ? metadata.annotation : annotation(path) : null;
      const selected = tag !== 'otelc.exclude' && selection.accepts(name, tag === 'otelc.instrument');
      inventory.push({ name, selected, line: metadata?.line ?? path.node.loc.start.line });
      if (!selected || !runtime.register(name)) return;
      instrumented = true;
      path.traverse({ CallExpression(call) {
        if (t.isIdentifier(call.node.callee, { name: 'eval' }) && !call.scope.getBinding('eval')) throw call.buildCodeFrameError('direct eval in a selected function is unsupported');
      } });
      const body = t.isBlockStatement(path.node.body) ? path.node.body : t.blockStatement([t.returnStatement(path.node.body)]);
      // Body-level declarations have function scope, unlike declarations inside
      // the generated try block. Initialise anonymous expressions first so their
      // closures retain the original lexical bindings and mutable self-reference.
      const declarations = body.body.filter(statement => t.isFunctionDeclaration(statement));
      if (declarations.length) body.body = [
        ...declarations.map(declaration => t.variableDeclaration('var', [t.variableDeclarator(t.cloneNode(declaration.id), t.functionExpression(null, declaration.params, declaration.body, declaration.generator, declaration.async))])),
        ...body.body.filter(statement => !t.isFunctionDeclaration(statement))
      ];
      const token = path.scope.generateUidIdentifier('otelc_token');
      let completion = body.body;
      let result;
      if (path.node.async && !path.node.generator) {
        result = path.scope.generateUidIdentifier('otelc_result');
        const label = path.scope.generateUidIdentifier('otelc_body');
        // Leave user finally blocks in their original position. Await adoption
        // only after the original body has finished all of its cleanup.
        path.node.body = body;
        path.get('body').traverse({
          Function(inner) { inner.skip(); },
          ReturnStatement(returned) {
            returned.replaceWith(t.blockStatement([
              t.expressionStatement(t.assignmentExpression('=', t.cloneNode(result), returned.node.argument ?? t.unaryExpression('void', t.numericLiteral(0)))),
              t.breakStatement(t.cloneNode(label))
            ]));
            returned.skip();
          }
        });
        const needsAdoption = t.logicalExpression('&&', t.cloneNode(token),
          t.logicalExpression('&&', t.cloneNode(result), t.logicalExpression('||',
            t.binaryExpression('===', t.unaryExpression('typeof', t.cloneNode(result)), t.stringLiteral('object')),
            t.binaryExpression('===', t.unaryExpression('typeof', t.cloneNode(result)), t.stringLiteral('function')))));
        completion = [t.labeledStatement(label, t.blockStatement(body.body)), t.returnStatement(
          t.conditionalExpression(needsAdoption, t.awaitExpression(t.cloneNode(result)), t.cloneNode(result)))];
      }
      const unwound = path.scope.generateUidIdentifier('otelc_unwound');
      const error = path.scope.generateUidIdentifier('otelc_error');
      const runtimeNode = () => t.cloneNode(helper);
      const begin = t.variableDeclaration('const', [t.variableDeclarator(token, t.callExpression(t.memberExpression(runtimeNode(), t.identifier('enter')), [t.stringLiteral(name)]))]);
      begin.leadingComments = [{ type: 'CommentBlock', value: MARKER }];
      if (plan.annotations.inject_generated) begin.leadingComments.push({ type: 'CommentBlock', value: 'otelc.instrument' });
      const statements = [begin, t.variableDeclaration('let', [t.variableDeclarator(unwound, t.booleanLiteral(false)), ...(result ? [t.variableDeclarator(result)] : [])]), t.tryStatement(t.blockStatement(completion), t.catchClause(error, t.blockStatement([t.expressionStatement(t.assignmentExpression('=', unwound, t.booleanLiteral(true))), t.throwStatement(error)])), t.blockStatement([t.expressionStatement(t.callExpression(t.memberExpression(runtimeNode(), t.identifier('exit')), [token, unwound]))]))];
      path.node.body = t.blockStatement(statements);
      path.node.body.directives = body.directives;
      path.node.expression = false;
    } } } })]
  });
  return { code: result.code + '\n//# sourceMappingURL=data:application/json;base64,' + Buffer.from(JSON.stringify(result.map)).toString('base64'), functions: inventory, format };
}
