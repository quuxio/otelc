// Preserve original function identities through the actual TypeScript compiler emit.
import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
import { IDENTITY } from './transform.mjs';
import { identity, functionName } from './typescript-identities.mjs';
import { nativeEmit } from './typescript-native.mjs';

function error(diagnostics) {
  const failures = diagnostics.filter(value => value.category === ts.DiagnosticCategory.Error);
  if (failures.length) throw new Error(failures.map(value => ts.flattenDiagnosticMessageText(value.messageText, '\n')).join('\n'));
}
export function compilerOptions(root = process.cwd()) {
  const config = path.join(root, 'tsconfig.json');
  let options = {};
  if (fs.existsSync(config)) {
    const read = ts.readConfigFile(config, ts.sys.readFile);
    if (read.error) error([read.error]);
    const parsed = ts.parseJsonConfigFileContent(read.config, ts.sys, root);
    error(parsed.errors.filter(value => value.code !== 18003));
    options = parsed.options;
  }
  if (options.paths || options.baseUrl || options.jsx || options.emitDecoratorMetadata || options.outFile) throw new Error('TypeScript paths/baseUrl, JSX, decorator metadata and outFile are unsupported by the Node source adapter');
  if (options.target !== undefined && options.target < ts.ScriptTarget.ES2018) throw new Error('TypeScript target must be ES2018 or newer to preserve async and generator timing');
  if ([ts.ModuleKind.AMD, ts.ModuleKind.UMD, ts.ModuleKind.System, ts.ModuleKind.None].includes(options.module)) throw new Error('TypeScript module format is unsupported by the Node source adapter');
  return { ...options, target: options.target ?? ts.ScriptTarget.ES2022, module: ts.ModuleKind.Preserve,
    sourceMap: true, inlineSourceMap: false, inlineSources: true, removeComments: false,
    declaration: false, declarationMap: false, emitDeclarationOnly: false, noEmit: false,
    verbatimModuleSyntax: true, isolatedModules: true };
}
export function transpile(source, filename, sourceName, plan, root = process.cwd()) {
  if (/\.d\.(?:ts|mts|cts)$/.test(filename) || /\.tsx$/.test(filename)) throw new Error('TypeScript declarations and JSX are not executable adapter inputs');
  if (source.includes(IDENTITY)) throw new Error('TypeScript source is already prepared for instrumentation');
  const options = compilerOptions(root);
  if (/\.cts$/.test(filename)) options.module = ts.ModuleKind.CommonJS;
  if (plan.backend === 'native') return nativeEmit(source, filename, sourceName, plan, options, undefined, root);
  const transformer = context => file => {
    const prefix = sourceName.replace(/\.(?:ts|mts|cts)$/, '').replaceAll('/', '.');
    function visit(node, parents = [], names = []) {
      const callable = ts.isFunctionLike(node) && node.body;
      const name = callable ? (ts.isGetAccessorDeclaration(node) ? 'get ' : ts.isSetAccessorDeclaration(node) ? 'set ' : '') + functionName(node, parents, file) : null;
      const container = ts.isClassLike(node) || ts.isModuleDeclaration(node);
      const position = file.getLineAndCharacterOfPosition(node.getStart(file));
      const owner = parents.at(-1);
      const object = ts.isObjectLiteralExpression(node) ? owner && (ts.isVariableDeclaration(owner) || ts.isPropertyAssignment(owner)) && ts.isIdentifier(owner.name) ? owner.name.text : `<object>@${position.line + 1}:${position.character + 1}` : null;
      const nested = callable ? [...names, name] : container ? [...names, node.name?.text ?? '<class>'] : object ? [...names, object] : names;
      const updated = ts.visitEachChild(node, child => visit(child, [...parents, node], nested), context);
      if (callable) {
        const metadata = identity(node, parents, names, file, prefix, plan);
        ts.addSyntheticLeadingComment(updated, ts.SyntaxKind.MultiLineCommentTrivia, IDENTITY + Buffer.from(JSON.stringify(metadata)).toString('base64'), true);
      }
      return updated;
    }
    return visit(file);
  };
  const result = ts.transpileModule(source, { fileName: filename, compilerOptions: options, reportDiagnostics: true, transformers: { before: [transformer] } });
  error(result.diagnostics ?? []);
  const map = JSON.parse(result.sourceMapText);
  map.sources = [filename];
  return { code: result.outputText.replace(/\n\/\/# sourceMappingURL=.*(?:\n|$)/, '\n'), map, format: /\.cts$/.test(filename) ? 'commonjs' : /\.mts$/.test(filename) ? 'module' : undefined };
}
