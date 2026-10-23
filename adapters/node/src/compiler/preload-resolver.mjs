// Resolve original preload specifiers with Node; never import/require their code.
import { readFileSync } from 'node:fs';
import { createRequire, isBuiltin } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

try {
  const input = JSON.parse(readFileSync(0, 'utf8'));
  const parent = pathToFileURL(path.join(input.cwd, '__otelc_preload__.mjs')).href;
  const require = createRequire(parent);
  const entries = input.requests.map(request => {
    const specifier = request.specifier;
    if (isBuiltin(specifier)) return { ...request, kind: 'builtin', target: specifier.startsWith('node:') ? specifier : 'node:' + specifier };
    const target = request.mechanism === 'require'
      ? pathToFileURL(require.resolve(specifier)).href : import.meta.resolve(specifier, parent);
    return { ...request, kind: target.startsWith('file:') ? 'file' : 'url', target };
  });
  process.stdout.write(JSON.stringify({ entries }) + '\n');
} catch {
  // A specifier or package diagnostic can contain private settings. Keep those
  // in the owned request record rather than forwarding them to shared logs.
  process.stderr.write('Node preload resolution failed\n');
  process.exitCode = 1;
}
