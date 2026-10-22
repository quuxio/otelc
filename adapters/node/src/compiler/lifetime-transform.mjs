import * as t from '@babel/types';
import { Selection } from './policy.mjs';

export const TYPE_IDENTITY = '@quux.otelc.type ';

function instrumentReleases(classPath, helper, name, plan) {
  let resource = false;
  for (const method of classPath.get('body.body')) {
    if (!method.isClassMethod() || method.node.static || method.node.kind === 'constructor') continue;
    const key = method.node.key;
    const methodName = t.isIdentifier(key) ? key.name : t.isStringLiteral(key) ? key.value : undefined;
    if (!plan.lifetimes.release_methods.includes(methodName)) continue;
    if (method.node.kind !== 'method' || method.node.async || method.node.generator || method.node.params.length) {
      throw method.buildCodeFrameError('selected release methods require a synchronous zero-argument void profile');
    }
    method.traverse({
      Function(nested) { nested.skip(); },
      ReturnStatement(statement) {
        if (statement.node.argument) throw statement.buildCodeFrameError('selected release methods returning values are not qualified');
      },
      CallExpression(call) {
        if (t.isIdentifier(call.node.callee, { name: 'eval' }) && !call.scope.getBinding('eval')) {
          throw call.buildCodeFrameError('direct eval in a selected release method is unsupported');
        }
      }
    });
    resource = true;
    const failed = method.scope.generateUidIdentifier('otelc_release_failed');
    const error = method.scope.generateUidIdentifier('otelc_release_error');
    const body = method.node.body;
    const declarations = body.body.filter(statement => t.isFunctionDeclaration(statement));
    const initializers = declarations.map(declaration => t.variableDeclaration('var', [t.variableDeclarator(t.cloneNode(declaration.id),
      t.functionExpression(null, declaration.params, declaration.body, declaration.generator, declaration.async))]));
    const call = action => t.expressionStatement(t.callExpression(t.memberExpression(t.cloneNode(helper), t.identifier(action)),
      [t.thisExpression(), t.stringLiteral(name)]));
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
  return resource;
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
  const register = t.staticBlock([t.expressionStatement(call('lifetimeType', [t.thisExpression(), t.stringLiteral(name), t.booleanLiteral(resource)]))]);
  register.leadingComments = [{ type: 'CommentBlock', value: '@quux.otelc.generated' }];
  path.node.body.body.unshift(register);
  return true;
}
