// Parser-based probes are inserted in memory; original source and signatures stay intact.
import { transformSync, parseSync } from '@babel/core';
import * as t from '@babel/types';
import { Selection } from './policy.mjs';
import { fileURLToPath } from 'node:url';

const probes = fileURLToPath(new URL('./probes.cjs', import.meta.url));

let nextAsyncSite = 1;
export const MARKER = '@quux.otelc.generated';
export const IDENTITY = '@quux.otelc.identity ';
function instrumentTraceBody(path, body, helper, name, id, plan) {
  if (path.node.async && path.node.generator) throw path.buildCodeFrameError('selected async generators with traces are not qualified');
  if (path.findParent(parent => parent.isWithStatement())) throw path.buildCodeFrameError('selected functions within with scopes with traces are not qualified');
  const token = path.scope.generateUidIdentifier('otelc_token');
  const scope = path.scope.generateUidIdentifier('otelc_scope');
  const resume = path.scope.generateUidIdentifier('otelc_resume');
  const value = path.scope.generateUidIdentifier('otelc_value');
  const error = path.scope.generateUidIdentifier('otelc_error');
  const unwound = path.scope.generateUidIdentifier('otelc_unwound');
  const call = (method, args) => t.callExpression(t.memberExpression(t.cloneNode(helper), t.identifier(method)), args.map(arg => t.cloneNode(arg, true)));
  const attach = () => t.expressionStatement(t.assignmentExpression('=', t.cloneNode(scope), call('attach', [token, scope])));
  path.traverse({
    Function(nested) { nested.skip(); },
    WithStatement(statement) { throw statement.buildCodeFrameError('selected with statements with traces are not qualified'); },
    ForOfStatement(loop) { if (loop.node.await) throw loop.buildCodeFrameError('selected for-await loops with traces are not qualified'); },
    AwaitExpression: { exit(awaited) {
      const original = awaited.node.argument;
      awaited.replaceWith(t.callExpression(t.cloneNode(resume), [t.awaitExpression(call('suspend', [scope, original]))])); awaited.skip();
    } },
    YieldExpression: { exit(yielded) {
      if (yielded.node.delegate) throw yielded.buildCodeFrameError('selected delegated yields with traces are not qualified');
      const original = yielded.node.argument ?? t.unaryExpression('void', t.numericLiteral(0));
      yielded.replaceWith(t.callExpression(t.cloneNode(resume), [t.yieldExpression(call('suspend', [scope, original]))])); yielded.skip();
    } },
    TryStatement: { exit(statement) {
      const handler = statement.node.handler;
      if (handler) {
        if (handler.param && !t.isIdentifier(handler.param)) {
          const caught = path.scope.generateUidIdentifier('otelc_caught');
          const pattern = handler.param; handler.param = t.cloneNode(caught);
          handler.body.body.unshift(attach(), t.variableDeclaration('let', [t.variableDeclarator(pattern, t.cloneNode(caught))]));
        } else handler.body.body.unshift(attach());
      }
      if (statement.node.finalizer) statement.node.finalizer.body.unshift(attach());
    } }
  });
  // Concise arrows have no body path to mutate during the traversal.
  if (!t.isBlockStatement(path.node.body)) body = t.blockStatement([t.returnStatement(path.node.body)]);
  const declarations = body.body.filter(statement => t.isFunctionDeclaration(statement));
  if (declarations.length) body.body = [
    ...declarations.map(declaration => t.variableDeclaration('var', [t.variableDeclarator(t.cloneNode(declaration.id), t.functionExpression(null, declaration.params, declaration.body, declaration.generator, declaration.async))])),
    ...body.body.filter(statement => !t.isFunctionDeclaration(statement))
  ];
  const begin = t.variableDeclaration('const', [t.variableDeclarator(t.cloneNode(token), call(id === null ? 'enter' : 'enterAsync', id === null ? [t.stringLiteral(name)] : [t.stringLiteral(name), t.numericLiteral(id)]))]);
  begin.leadingComments = [{ type: 'CommentBlock', value: MARKER }];
  if (plan.annotations.inject_generated) begin.leadingComments.push({ type: 'CommentBlock', value: 'otelc.instrument' });
  const finish = [attach(), t.expressionStatement(call('detach', [scope]))];
  if (id === null) finish.push(t.expressionStatement(call('exit', [token, unwound])));
  path.node.body = t.blockStatement([
    begin,
    t.variableDeclaration('let', [t.variableDeclarator(t.cloneNode(scope), call('attach', [token]))]),
    t.variableDeclaration('const', [t.variableDeclarator(t.cloneNode(resume), t.arrowFunctionExpression([t.cloneNode(value)], t.blockStatement([attach(), t.returnStatement(t.cloneNode(value))])))]),
    t.variableDeclaration('let', [t.variableDeclarator(t.cloneNode(unwound), t.booleanLiteral(false))]),
    t.tryStatement(t.blockStatement(body.body), t.catchClause(t.cloneNode(error), t.blockStatement([t.expressionStatement(t.assignmentExpression('=', t.cloneNode(unwound), t.booleanLiteral(true))), t.throwStatement(t.cloneNode(error))])), t.blockStatement(finish))
  ]);
  path.node.body.directives = body.directives;
  path.node.expression = false;
}
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
  const asyncSites = new Map();
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
      if (!selected || (!plan.traces?.enabled && !runtime.register(name))) return;
      instrumented = true;
      path.traverse({ CallExpression(call) {
        if (t.isIdentifier(call.node.callee, { name: 'eval' }) && !call.scope.getBinding('eval')) throw call.buildCodeFrameError('direct eval in a selected function is unsupported');
      } });
      const body = t.isBlockStatement(path.node.body) ? path.node.body : t.blockStatement([t.returnStatement(path.node.body)]);
      if (plan.traces?.enabled) {
        const id = path.node.async ? nextAsyncSite++ : null;
        if (id !== null) asyncSites.set(id, name);
        instrumentTraceBody(path, body, helper, name, id, plan);
        return;
      }
      if (path.node.async && !path.node.generator) {
        const id = nextAsyncSite++;
        asyncSites.set(id, name);
        const begin = t.expressionStatement(t.callExpression(t.memberExpression(t.cloneNode(helper), t.identifier('enterAsync')), [t.stringLiteral(name), t.numericLiteral(id)]));
        begin.leadingComments = [{ type: 'CommentBlock', value: MARKER }];
        if (plan.annotations.inject_generated) begin.leadingComments.push({ type: 'CommentBlock', value: 'otelc.instrument' });
        body.body.unshift(begin);
        path.node.body = body;
        path.node.expression = false;
        return;
      }
      // Body-level declarations have function scope, unlike declarations inside
      // the generated try block. Initialise anonymous expressions first so their
      // closures retain the original lexical bindings and mutable self-reference.
      const declarations = body.body.filter(statement => t.isFunctionDeclaration(statement));
      if (declarations.length) body.body = [
        ...declarations.map(declaration => t.variableDeclaration('var', [t.variableDeclarator(t.cloneNode(declaration.id), t.functionExpression(null, declaration.params, declaration.body, declaration.generator, declaration.async))])),
        ...body.body.filter(statement => !t.isFunctionDeclaration(statement))
      ];
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
  if (asyncSites.size && runtime.registerAsyncSites) {
    // Origins refer to the generated text seen by V8, before source-map remapping.
    const ast = parseSync(result.code, { filename, configFile: false, babelrc: false, sourceType: format === 'module' ? 'module' : 'script', parserOpts: { tokens: true, allowReturnOutsideFunction: true } });
    const sites = [];
    const visit = node => {
      if (!node || typeof node !== 'object') return;
      if (node.async && !node.generator && t.isBlockStatement(node.body)) {
        const first = node.body.body[0];
        const call = first?.expression ?? first?.declarations?.[0]?.init;
        const id = call?.arguments?.[1]?.value;
        if (t.isMemberExpression(call?.callee) && t.isIdentifier(call.callee.object, { name: helper.name }) && t.isIdentifier(call.callee.property, { name: 'enterAsync' }) && asyncSites.has(id)) {
          const token = ast.tokens.find(token => token.start >= (node.key?.end ?? node.id?.end ?? node.start) && token.end <= node.body.start && token.value === undefined && token.type.label === '(');
          const origin = t.isArrowFunctionExpression(node) ? node.loc.start : token?.loc.start;
          if (!origin) throw new Error('Cannot identify the generated async Promise origin');
          sites.push({ id, name: asyncSites.get(id), filename, start: [node.body.loc.start.line, node.body.loc.start.column + 1], end: [node.body.loc.end.line, node.body.loc.end.column + 1], origin: [origin.line, origin.column + 1] });
        }
      }
      for (const key of t.VISITOR_KEYS[node.type] ?? []) {
        const children = node[key];
        if (Array.isArray(children)) children.forEach(visit); else visit(children);
      }
    };
    visit(ast.program);
    runtime.registerAsyncSites(sites);
  }
  return { code: result.code + '\n//# sourceMappingURL=data:application/json;base64,' + Buffer.from(JSON.stringify(result.map)).toString('base64'), functions: inventory, format };
}
