import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { Selection, sourceName } from '../src/selection.ts';

describe('common Unicode selections', () => {
  it('uses literal punctuation and one Unicode code point per question mark', () => {
    const literal = new Selection({ include: ['a+b[0].- #λ💡'], exclude: [] });
    assert(literal.accepts('a+b[0].- #λ💡'));
    assert(!literal.accepts('aab0X- #λ💡'));
    const scalar = new Selection({ include: ['?'], exclude: [] });
    assert(scalar.accepts('💡'));
    assert(scalar.accepts('λ'));
    assert(!scalar.accepts('aa'));
    assert(!scalar.accepts('x\n'), 'a final newline is part of the value');
    assert(scalar.accepts('\n'));
  });

  it('implements zero-depth globstars and slash-aware source wildcards', () => {
    const nested = new Selection({ include: ['examples/**/*.ts'], exclude: ['**/generated/**'] }, true);
    assert(nested.accepts('examples/app.ts'));
    assert(nested.accepts('examples/deep/app.ts'));
    assert(!nested.accepts('examples/generated/app.ts'));
    assert(!nested.accepts('examples/appXts'));
    const immediate = new Selection({ include: ['examples/*.ts'], exclude: [] }, true);
    assert(!immediate.accepts('examples/deep/app.ts'));
    assert(new Selection({ include: ['examples/**'], exclude: [] }, true).accepts('examples/deep/app.ts'));
  });

  it('retains explicit opt-in and exclusion precedence for existing annotations', () => {
    const selection = new Selection({ include: [], exclude: ['*excluded*'] });
    assert(!selection.accepts('workload'));
    assert(selection.accepts('workload', true));
    assert(!selection.accepts('excluded', true));
    assert(new Selection({ include: ['*workload*'], exclude: [] }).accepts('app.workload'));
  });

  it('keeps original project-relative names and refuses external/dependency roots', () => {
    assert.equal(sourceName('/project/examples/app.ts', '/project'), 'examples/app.ts');
    assert.equal(sourceName('/other/app.ts', '/project'), undefined);
    assert.equal(sourceName('/project/node_modules/lib/app.js', '/project'), undefined);
    assert.equal(sourceName('/project/.venv/app.py', '/project'), undefined);
    assert.equal(sourceName('/project', '/project'), undefined);
  });
});
