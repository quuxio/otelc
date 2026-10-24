"""Generated Go hooks keep fixed upstream bytes and explicit task semantics."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from otelc import go_context_bundle as bundle

CLONE = b'''package trace
import (
 "sync/atomic"
 "go.opentelemetry.io/otel/trace"
)
func (tc *traceContext) Clone() interface{} {
 last := tc.tail()
 if last == nil { return &traceContext{nil, 0, nil} }
 sw := &spanWrapper{last, nil, tc.sw.ended}
 return &traceContext{sw, 1, nil}
}
'''
RULES = b'''add_gls_field:
  target: runtime
gls_linker:
  target: runtime
goroutine_propagate:
  target: runtime
  where:
    func: newproc1
'''


def archive_bytes(rows: list[tuple[str, bytes, bytes]]) -> bytes:
    """Build a small archive independent of Go downloads and compiler availability."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, data, kind in rows:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


class GoContextBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / 'bundle.tgz'
        self.output = self.root / 'context'
        self.rows = [(name, b'package fixed\n', tarfile.REGTYPE) for name in bundle.INPUTS]
        self.rows = [(name, CLONE if name.endswith('otel_trace_context.go') else
                      RULES if name == 'instrumentation_temp/runtime/otelc.yaml' else data, kind)
                     for name, data, kind in self.rows]
        self.write_archive(self.rows)

    def write_archive(self, rows: list[tuple[str, bytes, bytes]]) -> None:
        data = archive_bytes(rows)
        self.bundle.write_bytes(data)
        patch = mock.patch.object(bundle, 'BUNDLE_SHA256', bundle.digest(data))
        patch.start()
        self.addCleanup(patch.stop)

    def test_tasks_copy_only_fixed_files_and_capture_immutable_inherited_context(self) -> None:
        self.rows.append(('../outside.go', b'unsafe', tarfile.REGTYPE))
        self.write_archive(self.rows)
        before = self.bundle.read_bytes()
        value = bundle.prepare(self.bundle, self.output, tasks=True)
        self.assertEqual(value['schema_version'], 1)
        self.assertEqual((value['upstream_module'], value['upstream_version'], value['upstream_commit']),
                         ('go.opentelemetry.io/otelc', 'v1.0.1', '3b9beed7a1200cf16e59bafa431dd4c4a0601f41'))
        self.assertIs(value['tasks'], True)
        source = (self.output / bundle.TRACE_OUTPUT / 'otel_trace_context.go').read_bytes()
        self.assertIn(b'trace.SpanFromContext(trace.ContextWithSpanContext(context.Background(), last.SpanContext()))', source)
        self.assertIn(b'sw := &spanWrapper{inherited, nil, &atomic.Bool{}}', source)
        self.assertNotIn(b'tc.sw.ended', source)
        self.assertEqual((self.output / 'instrumentation/runtime/otelc.yaml').read_bytes(), RULES)
        self.assertNotIn('pkg/runtime/setup.go', value['files'])
        self.assertNotIn('pkg/runtime/go.sum', value['files'])
        self.assertIn(b'go 1.26.0\n', (self.output / 'pkg/runtime/go.mod').read_bytes())
        self.assertIn(b'v1.47.0\n', (self.output / 'pkg/runtime/go.mod').read_bytes())
        self.assertIn(b'Apache-2.0', (self.output / 'NOTICE.upstream').read_bytes())
        self.assertTrue((self.output / 'LICENSE.upstream').is_file())
        self.assertFalse((self.root / 'outside.go').exists())
        self.assertEqual(bundle.verify(self.bundle, self.output, tasks=True), value)
        self.assertEqual(self.bundle.read_bytes(), before)
        modules = bundle.replacements(self.output)
        self.assertEqual(len(modules), 4)
        self.assertTrue(all((path / 'go.mod').is_file() for path in modules.values()))

    def test_disabled_tasks_keep_local_span_hooks_and_original_clone(self) -> None:
        value = bundle.prepare(self.bundle, self.output, tasks=False)
        self.assertIs(value['tasks'], False)
        self.assertEqual((self.output / bundle.TRACE_OUTPUT / 'otel_trace_context.go').read_bytes(), CLONE)
        rules = (self.output / 'instrumentation/runtime/otelc.yaml').read_bytes()
        self.assertIn(b'add_gls_field:', rules)
        self.assertIn(b'gls_linker:', rules)
        self.assertNotIn(b'goroutine_propagate:', rules)
        self.assertIn(b'goroutine_context_reset:', rules)
        self.assertIn(b'otel_trace_context = nil', rules)
        self.assertIn(b'otel_baggage_container = nil', rules)
        self.assertEqual(bundle.verify(self.bundle, self.output, tasks=False), value)
        with self.assertRaisesRegex(ValueError, 'manifest'):
            bundle.verify(self.bundle, self.output, tasks=True)

    def test_changed_bundle_invalid_switch_and_existing_output_refuse(self) -> None:
        for operation in (bundle.prepare, bundle.verify):
            with self.subTest(operation=operation.__name__):
                for tasks in (None, 0, 1, 'true'):
                    with self.assertRaisesRegex(ValueError, 'boolean'):
                        operation(self.bundle, self.output, tasks=tasks)
                with mock.patch.object(bundle, 'BUNDLE_SHA256', '0' * 64), self.assertRaisesRegex(ValueError, 'pinned'):
                    operation(self.bundle, self.output, tasks=True)
                with mock.patch.object(bundle, 'MAX_BUNDLE_BYTES', 2), self.assertRaisesRegex(ValueError, 'pinned'):
                    operation(self.bundle, self.output, tasks=True)
        self.output.mkdir()
        marker = self.output / 'original'
        marker.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            bundle.prepare(self.bundle, self.output, tasks=True)
        self.assertEqual(marker.read_bytes(), b'keep')

    def test_incomplete_duplicate_and_nonfile_archive_members_refuse(self) -> None:
        name, data, _ = self.rows[0]
        for rows in (self.rows[1:], self.rows + [self.rows[0]],
                     [(name, data, tarfile.SYMTYPE), *self.rows[1:]]):
            self.write_archive(rows)
            with self.subTest(rows=len(rows)), self.assertRaisesRegex(ValueError, 'member|incomplete'):
                bundle.prepare(self.bundle, self.output, tasks=True)
            self.assertFalse(self.output.exists())

    def test_upstream_patch_or_task_rule_drift_refuses_before_publication(self) -> None:
        for suffix, replacement, tasks in (('otel_trace_context.go', b'changed', True),
                                           ('otel_trace_context.go', CLONE.replace(b'import (', b'import('), True),
                                           ('runtime/otelc.yaml', b'changed', False)):
            rows = [(name, replacement if name.endswith(suffix) else data, kind)
                    for name, data, kind in self.rows]
            self.write_archive(rows)
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, 'pinned'):
                bundle.prepare(self.bundle, self.output, tasks=tasks)
            self.assertFalse(self.output.exists())

    def test_changed_metadata_sources_and_extra_inputs_cannot_be_self_authorised(self) -> None:
        bundle.prepare(self.bundle, self.output, tasks=True)
        path = self.output / bundle.TRACE_OUTPUT / 'gls.go'
        manifest = self.output / bundle.MANIFEST
        original = path.read_bytes()
        original_manifest = manifest.read_bytes()
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'source bytes'):
            bundle.verify(self.bundle, self.output, tasks=True)
        value = json.loads(original_manifest)
        value['files'][str(path.relative_to(self.output))] = bundle.digest(b'changed')
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'manifest'):
            bundle.verify(self.bundle, self.output, tasks=True)
        manifest.write_bytes(original_manifest)
        path.write_bytes(original)
        extra = self.output / 'pkg/runtime/extra.go'
        extra.write_bytes(b'package runtime')
        with self.assertRaisesRegex(ValueError, 'source set'):
            bundle.verify(self.bundle, self.output, tasks=True)
        extra.unlink()
        path.unlink()
        target = self.root / 'same.go'
        target.write_bytes(original)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'source bytes'):
            bundle.verify(self.bundle, self.output, tasks=True)
        path.unlink()
        path.write_bytes(original)
        manifest.unlink()
        saved = self.root / 'same.json'
        saved.write_bytes(original_manifest)
        manifest.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, 'manifest'):
            bundle.verify(self.bundle, self.output, tasks=True)
