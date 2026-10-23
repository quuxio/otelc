// Preserve original function identities through the actual TypeScript compiler emit.
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript-api';
import { IDENTITY } from './transform.mjs';
import { identity, childNames } from './typescript-identities.mjs';
import { nativeEmit } from './typescript-native.mjs';
import { collectLifetimeClasses, markLifetimeClass, TYPE_IDENTITY } from './typescript-lifetimes.mjs';

function error(diagnostics) {
  const failures = diagnostics.filter(value => value.category === ts.DiagnosticCategory.Error);
  if (failures.length) throw new Error(failures.map(value => ts.flattenDiagnosticMessageText(value.messageText, '\n')).join('\n'));
}
export function compilerOptions(root = process.cwd(), readFile = ts.sys.readFile) {
  const config = path.join(root, 'tsconfig.json');
  let options = {};
  if (readFile(config) !== undefined) {
    const read = ts.readConfigFile(config, readFile);
    if (read.error) error([read.error]);
    const parsed = ts.parseJsonConfigFileContent(read.config, { ...ts.sys, readFile }, root);
    error(parsed.errors.filter(value => value.code !== 18003));
    options = parsed.options;
  }
  if (options.paths || options.baseUrl || options.jsx || options.emitDecoratorMetadata || options.outFile) throw new Error('TypeScript paths/baseUrl, JSX, decorator metadata and outFile are unsupported by the Node source adapter');
  if (options.target !== undefined && options.target < ts.ScriptTarget.ES2018) throw new Error('TypeScript target must be ES2018 or newer to preserve async and generator timing');
  if (['AMD', 'UMD', 'System', 'None'].includes(ts.ModuleKind[options.module])) throw new Error('TypeScript module format is unsupported by the Node source adapter');
  return { ...options, target: options.target ?? ts.ScriptTarget.ES2022, module: options.module ?? ts.ModuleKind.Preserve,
    sourceMap: true, inlineSourceMap: false, inlineSources: true, removeComments: false,
    declaration: false, declarationMap: false, emitDeclarationOnly: false, noEmit: false,
    // Modules execute at original URLs; rewritten output extensions have no file.
    allowImportingTsExtensions: false, rewriteRelativeImportExtensions: false, verbatimModuleSyntax: true, isolatedModules: true };
}
function executionFormat(filename, moduleKind, hint) {
  if (filename.endsWith('.cts')) return 'commonjs';
  if (filename.endsWith('.mts')) return 'module';
  if (['module', 'commonjs'].includes(hint)) return hint;
  if (moduleKind === ts.ModuleKind.CommonJS) return 'commonjs';
  // This is the concrete fallback already returned by the plain loader for
  // Preserve output. Keep compiler receipts and both execution lanes aligned.
  return 'module';
}
export function transpile(source, filename, sourceName, plan, root = process.cwd(), hintedFormat = undefined, readFile = ts.sys.readFile) {
  if (/\.d\.(?:ts|mts|cts)$/.test(filename) || filename.endsWith('.tsx')) throw new Error('TypeScript declarations and JSX are not executable adapter inputs');
  if (source.includes(IDENTITY) || source.includes(TYPE_IDENTITY)) throw new Error('TypeScript source is already prepared for instrumentation');
  const options = compilerOptions(root, readFile);
  const format = executionFormat(filename, options.module, hintedFormat);
  // Emit the format Node will execute, before parsing or inserting probes.
  options.module = format === 'commonjs' ? ts.ModuleKind.CommonJS : ts.ModuleKind.Preserve;
  options.verbatimModuleSyntax = format !== 'commonjs';
  if (plan.backend === 'native') return { ...nativeEmit(source, filename, sourceName, plan, options, undefined, root), format };
  const classes = new Map(collectLifetimeClasses(source, filename, sourceName, plan).map(item => [item.start, item.metadata]));
  const transformer = context => file => {
    const prefix = sourceName.replace(/\.(?:ts|mts|cts)$/, '').replaceAll('/', '.');
    function visit(node, parents = [], names = []) {
      const callable = ts.isFunctionLike(node) && node.body;
      const nested = childNames(node, parents, names, file);
      let updated = ts.visitEachChild(node, child => visit(child, [...parents, node], nested), context);
      if (ts.isClassLike(node) && classes.has(node.getStart(file))) updated = markLifetimeClass(updated, classes.get(node.getStart(file)));
      if (callable) {
        const metadata = identity(node, parents, names, file, prefix, plan);
        ts.addSyntheticLeadingComment(updated, ts.SyntaxKind.MultiLineCommentTrivia, IDENTITY + Buffer.from(JSON.stringify(metadata)).toString('base64'), true);
      }
      return updated;
    }
    return visit(file);
  };
  const requestedOptions = JSON.stringify(options);
  const result = ts.transpileModule(source, { fileName: filename, compilerOptions: options, reportDiagnostics: true,
    transformers: plan.plain ? undefined : { before: [transformer] } });
  error(result.diagnostics ?? []);
  const map = JSON.parse(result.sourceMapText);
  map.sources = [filename];
  return { code: result.outputText.replace(/\n\/\/# sourceMappingURL=.*(?:\n|$)/, '\n'), map, format,
    compilation: { backend: 'api', version: ts.version, compiler_path: fileURLToPath(import.meta.resolve('typescript-api')),
      cwd: process.cwd(), method: 'transpileModule', transformer_profile: plan.plain ? 'none' : 'original-identities-v1',
      files: { input: { path: filename, text: source }, options: { text: requestedOptions },
        output: { text: result.outputText }, map: { text: result.sourceMapText } } } };
}
