import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { it } from 'node:test';

const helper = fileURLToPath(new URL('../src/compiler/preload-resolver.mjs', import.meta.url));

it('resolves CJS/ESM/builtin and inline entries without executing preload code', t => {
  const cwd = mkdtempSync(path.join(tmpdir(), 'otelc-preload-resolver-'));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const audit = path.join(cwd, 'audit');
  writeFileSync(path.join(cwd, 'one.cjs'), `require('node:fs').writeFileSync(${JSON.stringify(audit)}, 'executed');`);
  writeFileSync(path.join(cwd, 'two.mjs'), `import fs from 'node:fs'; fs.writeFileSync(${JSON.stringify(audit)}, 'executed');`);
  const requests = [
    { mechanism: 'require', specifier: './one.cjs', option_index: 0 },
    { mechanism: 'import', specifier: './two.mjs?variant=1', option_index: 1 },
    { mechanism: 'require', specifier: 'fs', option_index: 2 },
    { mechanism: 'import', specifier: 'data:text/javascript,export{}', option_index: 3 },
  ];
  const result = spawnSync(process.execPath, ['--experimental-import-meta-resolve', helper], {
    input: JSON.stringify({ cwd, requests }), encoding: 'utf8',
    env: { ...process.env, NODE_OPTIONS: '', NODE_PATH: '' }, timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
  const entries = JSON.parse(result.stdout).entries as Array<{ kind: string; target: string }>;
  assert.deepEqual(entries.map(entry => entry.kind), ['file', 'file', 'builtin', 'url']);
  assert.match(entries[1].target, /two\.mjs\?variant=1$/);
  assert.equal(entries[2].target, 'node:fs');
  assert.equal(existsSync(audit), false);
});

it('refuses missing or malformed preload resolution input with a bounded diagnostic', () => {
  for (const input of ['{', JSON.stringify({ cwd: tmpdir(), requests: [{ mechanism: 'require', specifier: './not-present-otelc.cjs' }] })]) {
    const result = spawnSync(process.execPath, ['--experimental-import-meta-resolve', helper], {
      input, encoding: 'utf8', env: { ...process.env, NODE_OPTIONS: '', NODE_PATH: '' }, timeout: 5000,
    });
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.equal(result.stderr, 'Node preload resolution failed\n');
  }
});
