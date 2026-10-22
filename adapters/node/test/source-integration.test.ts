import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));

// The whole real-runtime suite has its own budget; individual scenario deadlines stay bounded.
test('ordinary source/compiler/lifecycle/shutdown integrations contribute actual adapter coverage',
  { timeout: 240000 }, async () => {
    const python = process.env.OTELC_TEST_PYTHON ?? path.join(root, '.venv/bin/python');
    await new Promise<void>((resolve, reject) => {
      execFile(python, ['-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_node_*.py', '-v'],
        { cwd: root, timeout: 230000, maxBuffer: 1024 * 1024,
          env: { ...process.env, OTELC_NODE: process.execPath, OTELC_TEST_NODE: process.execPath } },
        (error, stdout, stderr) => {
          if (error) { reject(new Error('ordinary Node integration failed\n' + stdout + stderr)); return; }
          assert.match(stderr, /\nOK\s*$/);
          resolve();
        });
    });
  });
