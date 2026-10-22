// Original type identities must survive both compiler lowering paths.
import ts from 'typescript-api';
import { Selection } from './policy.mjs';
import { functionName, identity } from './typescript-identities.mjs';
import { TYPE_IDENTITY } from './lifetime-transform.mjs';
export { TYPE_IDENTITY } from './lifetime-transform.mjs';

function classRecord(node, parents, names, file, prefix, plan, selection) {
  if (!ts.isClassLike(node) || [node, ...parents].some(parent => ts.getCombinedModifierFlags(parent) & ts.ModifierFlags.Ambient)) return null;
  const metadata = identity(node, parents, names, file, prefix, plan);
  if (metadata.annotation === 'otelc.exclude' || !selection.accepts(metadata.name, metadata.annotation === 'otelc.instrument')) return null;
  const decorated = [node, ...node.members, ...node.members.flatMap(member => member.parameters ?? [])]
    .some(member => ts.canHaveDecorators(member) && ts.getDecorators(member)?.length);
  if (node.heritageClauses?.some(clause => clause.token === ts.SyntaxKind.ExtendsKeyword) || decorated) {
    throw new Error('selected TypeScript collection classes with inheritance or decorators are not qualified');
  }
  const constructor = node.members.find(member => ts.isConstructorDeclaration(member) && member.body);
  return { start: node.getStart(file), position: constructor ? constructor.body.getStart(file) + 1 : node.end - 1,
    implicit: !constructor, metadata };
}
function childNames(node, parents, names, file) {
  if (ts.isFunctionLike(node) && node.body) return [...names, (ts.isGetAccessorDeclaration(node) ? 'get ' : ts.isSetAccessorDeclaration(node) ? 'set ' : '') + functionName(node, parents, file)];
  if (ts.isClassLike(node) || ts.isModuleDeclaration(node)) return [...names, node.name?.text ?? '<class>'];
  if (!ts.isObjectLiteralExpression(node)) return names;
  const owner = parents.at(-1);
  if (owner && (ts.isVariableDeclaration(owner) || ts.isPropertyAssignment(owner)) && ts.isIdentifier(owner.name)) return [...names, owner.name.text];
  const position = file.getLineAndCharacterOfPosition(node.getStart(file));
  return [...names, `<object>@${position.line + 1}:${position.character + 1}`];
}

/** Collect selected classes before lowering changes names, fields or namespaces. */
export function collectLifetimeClasses(source, filename, sourceName, plan) {
  if (!plan.lifetimes?.enabled) return [];
  const file = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
  if (file.parseDiagnostics.length) throw new Error('TypeScript lifetime identities require valid supported syntax');
  const prefix = sourceName.replace(/\.(?:ts|mts|cts)$/, '').replaceAll('/', '.');
  const selection = new Selection(plan.lifetime_matchers), result = [];
  function visit(node, parents = [], names = []) {
    // Unselected classes keep ordinary emission, especially implicit derived
    // constructors whose default argument forwarding is observable.
    const record = classRecord(node, parents, names, file, prefix, plan, selection);
    if (record) result.push(record);
    const nested = childNames(node, parents, names, file);
    ts.forEachChild(node, child => visit(child, [...parents, node], nested));
  }
  visit(file); return result;
}

/** Add a private constructor directive before the API compiler emits JavaScript. */
export function markLifetimeClass(node, metadata) {
  const members = [...node.members];
  let index = members.findIndex(member => ts.isConstructorDeclaration(member) && member.body);
  if (index < 0) {
    index = members.length;
    members.push(ts.factory.createConstructorDeclaration(undefined, [], ts.factory.createBlock([], true)));
  }
  const constructor = members[index], marker = ts.factory.createExpressionStatement(ts.factory.createStringLiteral(TYPE_IDENTITY + Buffer.from(JSON.stringify(metadata)).toString('base64')));
  members[index] = ts.factory.updateConstructorDeclaration(constructor, constructor.modifiers, constructor.parameters,
    ts.factory.updateBlock(constructor.body, [marker, ...constructor.body.statements]));
  return ts.isClassDeclaration(node)
    ? ts.factory.updateClassDeclaration(node, node.modifiers, node.name, node.typeParameters, node.heritageClauses, members)
    : ts.factory.updateClassExpression(node, node.modifiers, node.name, node.typeParameters, node.heritageClauses, members);
}
