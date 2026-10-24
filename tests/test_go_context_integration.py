"""Actual upstream weaving respects common task context without source edits."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ci import build_go_upstream as tooling
from otelc import go_context_bundle as context
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]
HOOKS = '''package main
import (
 "context"
 "encoding/json"
 "os"
 "runtime"
 "time"
 "go.opentelemetry.io/otel/trace"
 sdk "go.opentelemetry.io/otel/sdk/trace"
 "go.opentelemetry.io/otel/sdk/trace/tracetest"
 official "go.opentelemetry.io/otelc/pkg/runtime"
 "io.quux.otelc/go/pipeline"
)
var __otelcExporter = tracetest.NewInMemoryExporter()
var __otelcProcessor = func()*pipeline.Traces {
 processor,err:=pipeline.NewTraces(__otelcExporter,1024,256,0,time.Second)
 if err!=nil {panic(err)}
 return processor
}()
var __otelcProvider = sdk.NewTracerProvider(sdk.WithSpanProcessor(__otelcProcessor),sdk.WithoutPanicRecording())
func init() {
 if os.Getenv("OTELC_GOROUTINE_REUSE_PROBE")=="1" {
  runtime.GOMAXPROCS(1)
  for i:=0;i<100;i++ {
   done:=make(chan struct{})
   go func(){__otelcProvider.Tracer("fixture").Start(context.Background(),"unended fixture span");close(done)}()
   <-done
   runtime.Gosched()
  }
 }
}
func __otelcStart(name string) func() {
 ctx:=context.Background()
 if parent:=official.GetSpanFromGLS();parent!=nil {ctx=trace.ContextWithSpan(ctx,parent)}
 _,span:=__otelcProvider.Tracer("io.quux.otelc.go.context-fixture").Start(ctx,name)
 return func(){span.End()}
}
func __otelcShutdown() {
 if err:=__otelcProvider.ForceFlush(context.Background());err!=nil {panic(err)}
 type row struct { Name,TraceID,SpanID,Parent string }
 rows:=[]row{}
 for _,span:=range __otelcExporter.GetSpans(){rows=append(rows,row{span.Name,span.SpanContext.TraceID().String(),span.SpanContext.SpanID().String(),span.Parent.SpanID().String()})}
 data,err:=json.Marshal(rows);if err!=nil {panic(err)}
 if err=os.WriteFile(os.Getenv("OTELC_CONTEXT_REPORT"),data,0600);err!=nil {panic(err)}
 if err=__otelcProvider.Shutdown(context.Background());err!=nil {panic(err)}
}
'''
RULES = '''finish:
  target: main
  where:
    func: main
  do:
    - inject_code:
        raw: 'defer __otelcShutdown()'
workload:
  target: main
  where:
    func: workload
  do:
    - inject_code:
        raw: 'defer __otelcStart("workload")()'
child:
  target: main
  where:
    func: workloadChild
  do:
    - inject_code:
        raw: 'defer __otelcStart("workloadChild")()'
'''


class GoContextIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = tooling.build(os.environ.get('OTELC_GO', 'go'), ROOT / 'build/go-upstream')

    def command(self, arguments: list[str], cwd: Path) -> subprocess.CompletedProcess:
        result = subprocess.run(arguments, cwd=cwd, env=self.environment,
                                capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')[-6000:])
        return result

    def check_profile(self, tasks: bool, *, reuse: bool = False) -> None:
        with tempfile.TemporaryDirectory(prefix='go-context-', dir=ROOT / 'build') as temporary:
            root = Path(temporary).resolve()
            scratch = root / 'compiler-temp'
            scratch.mkdir()
            self.environment = tooling.environment(ROOT / 'build/go-upstream')
            self.environment.update(GOTOOLCHAIN='local', GOTMPDIR=str(scratch), TMPDIR=str(scratch))
            if reuse:
                self.environment['OTELC_GOROUTINE_REUSE_PROBE'] = '1'
            original = root / 'original'
            app = original / 'examples/go'
            app.mkdir(parents=True)
            source = (ROOT / 'tests/fixtures/go-context/ContextApp.go').read_bytes()
            (app / 'ContextApp.go').write_bytes(source)
            module = b'module example.com/otelc-context-fixture\n\ngo 1.26.0\n'
            (original / 'go.mod').write_bytes(module)
            private = root / 'private'
            shutil.copytree(original, private)
            runtime_source = ROOT / 'adapters/go'
            runtime_files = {str(path.relative_to(runtime_source)): path.read_bytes()
                             for path in runtime_source.rglob('*') if path.is_file()}
            runtime_copy = root / 'sdk'
            shutil.copytree(runtime_source, runtime_copy)
            policy = Policy.parse({'schema_version': 3, 'languages': ['go'], 'context': {'tasks': tasks}})
            modules = root / 'context'
            prepared = context.prepare(Path(self.tools['bundle']), modules, tasks=policy.data['context']['tasks'])
            rules = private / 'otelc_rules'
            rules.mkdir()
            (rules / 'doc.go').write_text('package rules\n')
            (rules / 'otelc.yaml').write_text(RULES)
            (private / 'otel.instrumentation.go').write_text('''//go:build tools

package tools
import (
 _ "example.com/otelc-context-fixture/otelc_rules"
 _ "go.opentelemetry.io/otelc/instrumentation/runtime"
 _ "go.opentelemetry.io/otelc/instrumentation/go.opentelemetry.io/otel/sdk/trace"
)
''')
            (private / 'examples/go/otelc_probe.go').write_text(HOOKS)
            for name, path in context.replacements(modules).items():
                self.command([self.tools['go'], 'mod', 'edit', '-replace=' + name + '=' + str(path),
                              '-require=' + name + '@v0.0.0'], private)
            self.command([self.tools['go'], 'mod', 'edit', '-replace=io.quux.otelc/go=' + str(runtime_copy),
                          '-require=io.quux.otelc/go@v0.0.0'], private)
            self.command([self.tools['go'], 'mod', 'edit', '-require=go.opentelemetry.io/otel/sdk@v1.47.0',
                          '-require=go.opentelemetry.io/otel/trace@v1.47.0'], private)
            self.command([self.tools['go'], 'mod', 'tidy'], private)
            self.assertNotIn('go.opentelemetry.io/otel/sdk/log', (private / 'go.mod').read_text())
            self.command([self.tools['go'], 'build', '-o', str(root / 'stock'), './examples/go'], original)
            self.command([self.tools['weaver'], '--work-dir', str(root / 'weaver-work'), 'go', 'build',
                          '-o', str(root / 'instrumented'), './examples/go'], private)
            for mode in ('immediate', 'delayed'):
                with self.subTest(tasks=tasks, mode=mode):
                    baseline = self.command([str(root / 'stock'), mode], original)
                    report = root / (mode + '.json')
                    self.environment['OTELC_CONTEXT_REPORT'] = str(report)
                    active = self.command([str(root / 'instrumented'), mode], private)
                    self.assertEqual((baseline.stdout, baseline.stderr), (b'42\n', b''))
                    self.assertEqual((active.stdout, active.stderr), (baseline.stdout, baseline.stderr))
                    spans = json.loads(report.read_bytes())
                    self.assertEqual(len(spans), 2)
                    by_name = {row['Name']: row for row in spans}
                    parent, child = by_name['workload'], by_name['workloadChild']
                    self.assertEqual(parent['Parent'], '0000000000000000')
                    if tasks:
                        self.assertEqual(child['Parent'], parent['SpanID'])
                        self.assertEqual(child['TraceID'], parent['TraceID'])
                    else:
                        self.assertEqual(child['Parent'], '0000000000000000')
                        self.assertNotEqual(child['TraceID'], parent['TraceID'])
            self.assertEqual(context.verify(Path(self.tools['bundle']), modules, tasks=tasks), prepared)
            self.assertEqual((app / 'ContextApp.go').read_bytes(), source)
            self.assertEqual((private / 'examples/go/ContextApp.go').read_bytes(), source)
            self.assertEqual((original / 'go.mod').read_bytes(), module)
            self.assertEqual({str(path.relative_to(runtime_source)): path.read_bytes()
                              for path in runtime_source.rglob('*') if path.is_file()}, runtime_files)
            self.assertEqual({str(path.relative_to(runtime_copy)): path.read_bytes()
                              for path in runtime_copy.rglob('*') if path.is_file()}, runtime_files)
            self.assertEqual(hashlib.sha256(Path(self.tools['weaver']).read_bytes()).hexdigest(), self.tools['weaver_sha256'])

    def test_original_tasks_keep_parent_after_immediate_and_delayed_completion(self) -> None:
        self.check_profile(True)

    def test_disabled_automatic_tasks_do_not_inherit_parent_context(self) -> None:
        self.check_profile(False)

    def test_disabled_tasks_clear_unended_context_from_reused_goroutines(self) -> None:
        self.check_profile(False, reuse=True)
