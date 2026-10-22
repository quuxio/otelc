import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { readdirSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const modules = readdirSync(path.join(root, 'tests')).filter(name => /^test_node_.*\.py$/.test(name))
  .sort((left, right) => left.localeCompare(right));
if (!modules.length) throw new Error('ordinary Node integration modules are missing');

// Bound each module; adding cases must not exhaust an unrelated suite's budget.
// The real scenarios retain their own shorter process/request deadlines.
for (const filename of modules) {
  test('ordinary Node integration: ' + filename, { timeout: 240000 }, async context => {
    const python = process.env.OTELC_TEST_PYTHON ?? path.join(root, '.venv/bin/python');
    await new Promise<void>((resolve, reject) => {
      execFile(python, ['-m', 'unittest', 'discover', '-s', 'tests', '-p', filename, '-v'],
        { cwd: root, timeout: 230000, maxBuffer: 1024 * 1024,
          env: { ...process.env, OTELC_NODE: process.execPath, OTELC_TEST_NODE: process.execPath } },
        (error, stdout, stderr) => {
          if (error) { reject(new Error('ordinary Node integration failed: ' + filename + '\n' + stdout + stderr)); return; }
          try {
            assert.match(stderr, /\nRan [1-9]\d* tests? in /);
            assert.match(stderr, /\nOK\s*$/);
          } catch (failure) { reject(failure); return; }
          context.diagnostic(filename + ': ' + /Ran (\d+) tests?/.exec(stderr)![1] + ' Python tests passed');
          resolve();
        });
    });
  });
}
