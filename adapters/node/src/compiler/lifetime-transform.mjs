import * as t from '@babel/types';
import { Selection } from './policy.mjs';

export const TYPE_IDENTITY = '@quux.otelc.type ';

function releaseSupported(method, className) {
  if (!method.isClassMethod() || method.node.kind !== 'method' || method.node.async
      || method.node.generator || method.node.params.length) return false;
  let supported = true;
  method.traverse({
    Function(nested) { nested.skip(); },
    ReturnStatement(statement) { if (statement.node.argument) supported = false; },
    CallExpression(call) {
      if (t.isIdentifier(call.node.callee, { name: 'eval' }) && !call.scope.getBinding('eval')) supported = false;
    }
  });
  if (className && method.scope.getOwnBinding(className)) method.traverse({
    CallExpression(call) {
      if (t.isIdentifier(call.node.callee, { name: 'eval' }) && !call.scope.getBinding('eval')) supported = false;
    }
  });
  return supported;
}

function releaseKeyBinding(classPath) {
  // A body-local binding is not visible during default-parameter evaluation.
  let child = classPath;
  while (child.parentPath && !child.parentPath.isFunction()) child = child.parentPath;
  if (child.parentPath?.isFunction() && child.key !== 'body') {
    throw classPath.buildCodeFrameError('selected collection classes in parameter initialisers are not qualified');
  }
  if (classPath.findParent(parent => parent.isWithStatement())) {
    throw classPath.buildCodeFrameError('selected collection classes within with scopes are not qualified');
  }
  // Loop heads reuse their surrounding binding across class evaluations. Named
  // class methods can use the engine's per-evaluation inner class identity;
  // anonymous methods need a fresh lexical body scope for the key token.
  if (!classPath.node.id) {
    let nested = classPath;
    while (nested.parentPath && !nested.parentPath.isFunction()) {
      const parent = nested.parentPath;
      if (parent.isLoop() && nested.key !== 'body') {
        throw classPath.buildCodeFrameError('selected anonymous collection classes in loop heads are not qualified');
      }
      nested = parent;
    }
  }
  const keys = classPath.scope.generateUidIdentifier('otelc_release_keys');
  const scope = classPath.node.id ? classPath.scope.getFunctionParent() ?? classPath.scope.getProgramParent() : classPath.scope.parent;
  scope.push({ id: t.cloneNode(keys), kind: 'let' });
  return keys;
}

function instrumentReleaseBody(method, helper, name, site, owner, ownerIsClass) {
  const className = ownerIsClass ? owner.name : undefined;
  if (className && method.scope.getOwnBinding(className)) {
    method.scope.rename(className, method.scope.generateUidIdentifier(className).name);
  }
  const failed = method.scope.generateUidIdentifier('otelc_release_failed');
  const error = method.scope.generateUidIdentifier('otelc_release_error');
  const body = method.node.body;
  const declarations = body.body.filter(statement => t.isFunctionDeclaration(statement));
  const initializers = declarations.map(declaration => t.variableDeclaration('var', [t.variableDeclarator(t.cloneNode(declaration.id),
    t.functionExpression(null, declaration.params, declaration.body, declaration.generator, declaration.async))]));
  const call = action => t.expressionStatement(t.callExpression(t.memberExpression(t.cloneNode(helper), t.identifier(action)),
    [t.thisExpression(), t.stringLiteral(name), t.numericLiteral(site), t.cloneNode(owner)]));
  method.node.body = t.blockStatement([
    ...initializers,
    t.variableDeclaration('let', [t.variableDeclarator(t.cloneNode(failed), t.booleanLiteral(false))]),
    t.tryStatement(t.blockStatement(body.body.filter(statement => !t.isFunctionDeclaration(statement))),
      t.catchClause(t.cloneNode(error), t.blockStatement([
        t.expressionStatement(t.assignmentExpression('=', t.cloneNode(failed), t.booleanLiteral(true))),
        call('lifetimeReleaseFailed'), t.throwStatement(t.cloneNode(error))
      ])), t.blockStatement([t.ifStatement(t.unaryExpression('!', t.cloneNode(failed)), call('lifetimeReleased'))]))
  ]);
  method.node.body.directives = body.directives;
}

function instrumentReleases(classPath, helper, name, plan) {
  const members = classPath.get('body.body').filter(member => (member.isClassMethod() || member.isClassProperty() || member.isClassAccessorProperty()) && !member.node.static
    && !t.isPrivateName(member.node.key) && member.node.kind !== 'constructor');
  if (!members.length || !plan.lifetimes.release_methods.length) return t.arrayExpression([]);
  const keys = releaseKeyBinding(classPath);
  for (const [index, method] of members.entries()) {
    const site = index + 1;
    const originalKey = method.node.key;
    const knownName = !method.node.computed && t.isIdentifier(originalKey) ? originalKey.name
      : t.isStringLiteral(originalKey) ? originalKey.value : undefined;
    const supported = releaseSupported(method, classPath.node.id?.name);
    const expression = !method.node.computed && t.isIdentifier(originalKey) ? t.stringLiteral(originalKey.name) : originalKey;
    const token = index === 0 ? t.assignmentExpression('=', t.cloneNode(keys), t.arrayExpression([])) : t.cloneNode(keys);
    // The engine performs ToPropertyKey in application scope exactly once. The
    // private helper observes that primitive key; no application formatter runs there.
    method.node.key = t.callExpression(t.memberExpression(t.cloneNode(helper), t.identifier('lifetimeKey')),
      [token, t.numericLiteral(site), t.objectExpression([t.objectProperty(expression, t.numericLiteral(0), true)]),
        t.booleanLiteral(supported), t.booleanLiteral(!method.isClassMethod())]);
    method.node.computed = true;
    if (!supported || (knownName !== undefined && !plan.lifetimes.release_methods.includes(knownName))) continue;
    instrumentReleaseBody(method, helper, name, site, classPath.node.id ?? keys, Boolean(classPath.node.id));
  }
  return keys;
}

// Register the actual constructor in a static block: no added instance fields,
// prototype members, wrapper class or application-visible constructor alias.
export function instrumentLifetime(path, helper, name, plan, tag) {
  if (!plan.lifetimes?.enabled) return false;
  const selected = tag !== 'otelc.exclude' && new Selection(plan.lifetime_matchers).accepts(name, tag === 'otelc.instrument');
  if (!selected) return false;
  if (path.node.superClass) throw path.buildCodeFrameError('selected JavaScript collection classes with inheritance are not qualified');
  const resource = instrumentReleases(path, helper, name, plan);
  let constructor = path.get('body.body').find(member => member.isClassMethod({ kind: 'constructor' }));
  if (constructor) constructor.traverse({
    Function(nested) { nested.skip(); },
    ReturnStatement(statement) {
      if (statement.node.argument) throw statement.buildCodeFrameError('selected collection constructors returning values are not qualified');
    },
    CallExpression(call) {
      if (t.isIdentifier(call.node.callee, { name: 'eval' }) && !call.scope.getBinding('eval')) throw call.buildCodeFrameError('direct eval in a selected collection constructor is unsupported');
    }
  });
  if (!constructor) {
    path.node.body.body.push(t.classMethod('constructor', t.identifier('constructor'), [], t.blockStatement([])));
    constructor = path.get('body.body').at(-1);
  }
  const call = (method, args) => t.callExpression(t.memberExpression(t.cloneNode(helper), t.identifier(method)), args);
  const body = constructor.node.body;
  const declarations = body.body.filter(statement => t.isFunctionDeclaration(statement));
  const initializers = declarations.map(declaration => t.variableDeclaration('var', [t.variableDeclarator(t.cloneNode(declaration.id), t.functionExpression(null, declaration.params, declaration.body, declaration.generator, declaration.async))]));
  const original = body.body.filter(statement => !t.isFunctionDeclaration(statement));
  const creation = constructor.scope.generateUidIdentifier('otelc_creation');
  const failed = constructor.scope.generateUidIdentifier('otelc_failed');
  const error = constructor.scope.generateUidIdentifier('otelc_failure');
  constructor.node.body = t.blockStatement([
    ...initializers,
    t.variableDeclaration('const', [t.variableDeclarator(creation, call('lifetimeCreation', []))]),
    t.variableDeclaration('let', [t.variableDeclarator(failed, t.booleanLiteral(false))]),
    t.tryStatement(t.blockStatement(original), t.catchClause(error, t.blockStatement([
      t.expressionStatement(t.assignmentExpression('=', t.cloneNode(failed), t.booleanLiteral(true))),
      t.expressionStatement(call('lifetimeFailed', [t.metaProperty(t.identifier('new'), t.identifier('target')), t.stringLiteral(name)])), t.throwStatement(t.cloneNode(error))
    ])), t.blockStatement([
      t.ifStatement(t.unaryExpression('!', t.cloneNode(failed)), t.expressionStatement(call('lifetimeAdmit', [t.thisExpression(), t.metaProperty(t.identifier('new'), t.identifier('target')), t.stringLiteral(name), t.cloneNode(creation)])))
    ]))
  ]);
  constructor.node.body.directives = body.directives;
  const register = t.staticBlock([t.expressionStatement(call('lifetimeType', [t.thisExpression(), t.stringLiteral(name), resource]))]);
  register.leadingComments = [{ type: 'CommentBlock', value: '@quux.otelc.generated' }];
  path.node.body.body.unshift(register);
  return true;
}
