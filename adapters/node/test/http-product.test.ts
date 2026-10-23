import assert from 'node:assert/strict';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { loadHttpProduct } from '../src/http-instrumentation.ts';

function fixture() {
  const root = mkdtempSync(path.join(tmpdir(), 'otelc-http-product-'));
  const producer = fileURLToPath(new URL('../../../', import.meta.url));
  const manifest = JSON.parse(readFileSync(path.join(producer, 'build/tools/otelc-http.json'), 'utf8')) as
    ReturnType<typeof loadHttpProduct>['manifest'];
  const directory = manifest.directory;
  const helper = path.join(root, 'adapters/node/src/http-preservation.ts');
  mkdirSync(path.dirname(helper), { recursive: true });
  cpSync(path.join(producer, 'adapters/node/src/http-preservation.ts'), helper);
  cpSync(path.join(producer, 'build/tools', directory), path.join(root, 'build/tools', directory), { recursive: true });
  const pointer = path.join(root, 'build/tools/otelc-http.json');
  const publish = (): void => writeFileSync(pointer, JSON.stringify(manifest));
  publish();
  return { root, directory, helper, pointer, manifest, publish,
    close() { rmSync(root, { recursive: true, force: true }); } };
}

test('validated product metadata binds its exact runtime files and helper source', t => {
  const input = fixture(); t.after(() => input.close());
  const product = loadHttpProduct(input.root);
  assert.equal(product.directory, path.join(input.root, 'build/tools', input.directory));
  assert.deepEqual(product.manifest, input.manifest);
});

test('a stale helper or unsupported manifest version is refused before any runtime import', t => {
  const input = fixture(); t.after(() => input.close());
  input.manifest.patch_version = 2; input.publish();
  assert.throws(() => loadHttpProduct(input.root), /rebuild/);
  input.manifest.patch_version = 1; input.publish(); writeFileSync(input.helper, 'changed helper');
  assert.throws(() => loadHttpProduct(input.root), /rebuild/);
});

test('path escape, changed runtime bytes or a different file set cannot become a qualified product', t => {
  const input = fixture(); t.after(() => input.close());
  input.manifest.directory = '../escape'; input.publish();
  assert.throws(() => loadHttpProduct(input.root), /rebuild/);
  input.manifest.directory = input.directory;
  input.manifest.files['extra.js'] = 'b'.repeat(64); input.publish();
  assert.throws(() => loadHttpProduct(input.root), /checksum mismatch/);
  delete input.manifest.files['extra.js']; input.publish();
  writeFileSync(path.join(input.root, 'build/tools', input.directory, 'build/src/http.js'), 'changed runtime');
  assert.throws(() => loadHttpProduct(input.root), /checksum mismatch/);
});

test('a stale product name cannot identify different manifest bytes', t => {
  const input = fixture(); t.after(() => input.close());
  input.manifest.upstream_sha256['http.js'] = 'b'.repeat(64); input.publish();
  assert.throws(() => loadHttpProduct(input.root), /identity mismatch/);
});
