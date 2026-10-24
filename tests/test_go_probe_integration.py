"""Real upstream compiler injects owned function and weak lifetime observers."""
from __future__ import annotations

import hashlib
from itertools import product
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ci import build_go_upstream as tooling
from otelc import go_context_bundle as context

ROOT = Path(__file__).resolve().parents[1]
HOOKS = '''package main
import (
 "context"
 "encoding/json"
 "os"
 "time"
 "go.opentelemetry.io/otel/metric/noop"
 sdkmetric "go.opentelemetry.io/otel/sdk/metric"
 "go.opentelemetry.io/otel/sdk/metric/metricdata"
 sdktrace "go.opentelemetry.io/otel/sdk/trace"
 "go.opentelemetry.io/otel/sdk/trace/tracetest"
 official "go.opentelemetry.io/otelc/pkg/runtime"
 "io.quux.otelc/go/metriccontrol"
 "io.quux.otelc/go/pipeline"
 "io.quux.otelc/go/probe"
)
var __otelcControls=metriccontrol.New(os.Getenv("OTELC_TEST_COLLECTION")!="off",true)
var __otelcReader=sdkmetric.NewManualReader()
var __otelcMetrics=sdkmetric.NewMeterProvider(sdkmetric.WithReader(__otelcReader))
var __otelcExporter=tracetest.NewInMemoryExporter()
var __otelcProcessor=func()*pipeline.Traces{p,e:=pipeline.NewTraces(__otelcExporter,128,32,0,time.Second);if e!=nil{panic(e)};return p}()
var __otelcTraces=func()*sdktrace.TracerProvider{
 options:=[]sdktrace.TracerProviderOption{sdktrace.WithSpanProcessor(__otelcProcessor),sdktrace.WithoutPanicRecording()}
 if os.Getenv("OTELC_TEST_SAMPLING")=="off"{options=append(options,sdktrace.WithSampler(sdktrace.NeverSample()))}
 return sdktrace.NewTracerProvider(options...)
}()
var __otelcProbes=func()*probe.Recorder {
 meter:=__otelcControls.MeterProvider(__otelcMetrics,noop.NewMeterProvider(),func(string)bool{return true}).Meter(pipeline.Scope)
 p,e:=probe.New(meter,__otelcTraces.Tracer(pipeline.Scope),probe.Options{
 Functions:[]string{"workload","NewObject","Close","child"},Lifetimes:[]probe.Lifetime{{Name:"Object",Resource:true}},
 MaxActive:128,MaxLive:128,Collecting:__otelcControls.CollectionEnabled,
 Parent:official.GetSpanFromGLS,Detach:official.DetachSpanFromGLS})
 if e!=nil{panic(e)};return p
}()
type __otelcLink struct{TraceID,SpanID string}
type __otelcSpan struct{Name,TraceID,SpanID,Parent,Error,Boundary string;Status int;Links []__otelcLink}
func __otelcTrackObject(object *Object){probe.Track(__otelcProbes,object,0)}
func __otelcReleaseObject(object *Object,succeeded bool){probe.Release(__otelcProbes,object,succeeded)}
func __otelcFinish(){
 __otelcProbes.Close()
 if e:=__otelcTraces.ForceFlush(context.Background());e!=nil{panic(e)}
 spans:=[]__otelcSpan{}
 for _,span:=range __otelcExporter.GetSpans(){
 row:=__otelcSpan{Name:span.Name,TraceID:span.SpanContext.TraceID().String(),SpanID:span.SpanContext.SpanID().String(),Parent:span.Parent.SpanID().String(),Status:int(span.Status.Code)}
 for _,a:=range span.Attributes{if a.Key=="error.type"{row.Error=a.Value.AsString()};if a.Key=="boundary"{row.Boundary=a.Value.AsString()}}
 for _,link:=range span.Links{row.Links=append(row.Links,__otelcLink{link.SpanContext.TraceID().String(),link.SpanContext.SpanID().String()})}
 spans=append(spans,row)
 }
 var metrics metricdata.ResourceMetrics
 if e:=__otelcReader.Collect(context.Background(),&metrics);e!=nil{panic(e)}
 counts:=map[string]int64{}
 for _,scope:=range metrics.ScopeMetrics{for _,m:=range scope.Metrics{if sum,ok:=m.Data.(metricdata.Sum[int64]);ok{for _,point:=range sum.DataPoints{counts[m.Name]+=point.Value}}}}
 report:=struct{Spans []__otelcSpan;Health probe.Health;Metrics map[string]int64}{spans,__otelcProbes.Snapshot(),counts}
 data,e:=json.Marshal(report);if e!=nil{panic(e)};if e=os.WriteFile(os.Getenv("OTELC_PROBE_REPORT"),data,0600);e!=nil{panic(e)}
 if e=__otelcTraces.Shutdown(context.Background());e!=nil{panic(e)}
 if e=__otelcMetrics.Shutdown(context.Background());e!=nil{panic(e)}
}
'''
RULES = '''finish:
  target: main
  where:
    func: main
  do:
    - inject_code:
        raw: 'defer __otelcFinish()'
function:
  target: main
  where:
    func: workload
  do:
    - inject_code:
        raw: 'defer __otelcProbes.Start(0).End(false)'
factory:
  target: main
  where:
    func: NewObject
  do:
    - inject_code:
        raw: |
          __otelcFactory := __otelcProbes.Start(1)
          defer func(){if __otelcFactory.End(false){__otelcTrackObject(_unnamedRetVal0)}}()
release:
  target: main
  where:
    func: Close
    recv: '*Object'
  do:
    - inject_code:
        raw: |
          __otelcRelease := __otelcProbes.Start(2)
          defer func(){__otelcReleaseObject(o,__otelcRelease.End(_unnamedRetVal0!=nil))}()
child:
  target: main
  where:
    func: child
  do:
    - inject_code:
        raw: 'defer __otelcProbes.Start(3).End(false)'
'''


class GoProbeIntegrationTests(unittest.TestCase):
    def test_actual_compiler_preserves_app_and_records_boundaries(self) -> None:
        tools = tooling.build('go', ROOT / 'build/go-upstream')
        environment = tooling.compiler_environment(tools, ROOT / 'build/go-upstream')

        def run(argv: list[str], cwd: Path, *, application: bool = False) -> bytes:
            result = subprocess.run(argv, cwd=cwd, env=environment,
                                    capture_output=True, timeout=300)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace')[-6000:])
            if application:
                self.assertEqual(result.stderr, b'')
            return result.stdout

        with tempfile.TemporaryDirectory(prefix='go-probe-', dir=ROOT / 'build') as temporary:
            root = Path(temporary).resolve()
            scratch = root / 'compiler-temp'
            scratch.mkdir()
            # The wrapper uses `go build -work`; keep its retained intermediates
            # inside this fixture's lifetime rather than leaking system temp data.
            environment.update(GOTMPDIR=str(scratch), TMPDIR=str(scratch))
            observed = json.loads(run(['go', 'env', '-json', 'GOVERSION', 'GOROOT'], root))
            self.assertEqual(observed, {'GOVERSION': tools['go_version'], 'GOROOT': str(Path(tools['go']).parent.parent)})
            original = root / 'original'
            original.mkdir()
            source = (ROOT / 'tests/fixtures/go-probes/App.go').read_bytes()
            module = b'module example.com/otelc-probe-fixture\n\ngo 1.26.0\n'
            (original / 'go.mod').write_bytes(module)
            (original / 'App.go').write_bytes(source)
            private = root / 'private'
            shutil.copytree(original, private)
            sdk = root / 'sdk'
            shutil.copytree(ROOT / 'adapters/go', sdk)
            frozen_sdk = {str(p.relative_to(sdk)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sdk.rglob('*') if p.is_file()}
            modules = root / 'context'
            context.prepare(Path(tools['bundle']), modules, tasks=True)
            rules = private / 'otelc_rules'
            rules.mkdir()
            (rules / 'doc.go').write_text('package rules\n')
            (rules / 'otelc.yaml').write_text(RULES)
            (private / 'otelc_probe.go').write_text(HOOKS)
            (private / 'otel.instrumentation.go').write_text('''//go:build tools

package tools
import (
 _ "example.com/otelc-probe-fixture/otelc_rules"
 _ "go.opentelemetry.io/otelc/instrumentation/runtime"
 _ "go.opentelemetry.io/otelc/instrumentation/go.opentelemetry.io/otel/sdk/trace"
)
''')
            for name, path in {**context.replacements(modules), 'io.quux.otelc/go': sdk}.items():
                run([tools['go'], 'mod', 'edit', '-replace=' + name + '=' + str(path),
                     '-require=' + name + '@v0.0.0'], private)
            run([tools['go'], 'mod', 'edit', '-require=go.opentelemetry.io/otel/sdk@v1.47.0'], private)
            run([tools['go'], 'mod', 'tidy'], private)
            run([tools['go'], 'build', '-o', str(root / 'stock'), '.'], original)
            run([tools['weaver'], '--work-dir', str(root / 'weaver-work'), 'go', 'build',
                 '-o', str(root / 'instrumented'), '.'], private)
            receipts = []
            for collection, sampling in product(('on', 'off'), repeat=2):
                environment['OTELC_TEST_COLLECTION'] = collection
                environment['OTELC_TEST_SAMPLING'] = sampling
                for mode in ('normal', 'failed', 'panic'):
                    with self.subTest(collection=collection, sampling=sampling, mode=mode):
                        baseline = run([str(root / 'stock'), mode], original, application=True)
                        report = root / f'{collection}-{sampling}-{mode}.json'
                        environment['OTELC_PROBE_REPORT'] = str(report)
                        self.assertEqual(run([str(root / 'instrumented'), mode], original, application=True), baseline)
                        self.assertEqual(baseline, b'42 0\n')
                        data = json.loads(report.read_bytes())
                        state = data['Health']
                        self.assertEqual({k: state[k] for k in ('FunctionActive', 'FunctionLoss', 'UnwindUnknown', 'LifetimeLoss', 'LiveObjects')},
                                         dict.fromkeys(('FunctionActive', 'FunctionLoss', 'UnwindUnknown', 'LifetimeLoss', 'LiveObjects'), 0))
                        self.assertEqual(state['GCObserved'], 1)
                        self.assertEqual(state['Released'], int(mode != 'panic'))
                        self.assertEqual(state['OpenResources'], int(mode == 'panic'))
                        if sampling == 'on':
                            workload = next(s for s in data['Spans'] if s['Name'] == 'workload')
                            for span in data['Spans']:
                                if span['Name'] == 'child':
                                    self.assertEqual(span['Parent'], workload['SpanID'])
                                if span['Name'] == 'Object lifetime':
                                    self.assertEqual(span['Parent'], '0000000000000000')
                                    self.assertNotEqual(span['TraceID'], workload['TraceID'])
                                    self.assertEqual(span['Links'], [{'TraceID': workload['TraceID'], 'SpanID': workload['SpanID']}])
                            self.assertEqual(sum(s['Boundary'] == 'resource' for s in data['Spans']), int(mode != 'panic'))
                            self.assertEqual(sum(s['Boundary'] == 'observed_gc' for s in data['Spans']), 1)
                        else:
                            self.assertEqual(data['Spans'], [])
                        if collection == 'off':
                            self.assertEqual(data['Metrics'], {})
                        else:
                            self.assertEqual(data['Metrics']['otelc.function.calls'], state['FunctionEnds'])
                            self.assertEqual(data['Metrics']['otelc.lifetime.admitted'], 2)
                            self.assertEqual(data['Metrics']['otelc.lifetime.completed'], 2 if mode != 'panic' else 1)
                            self.assertEqual(data['Metrics'].get('otelc.lifetime.incomplete', 0), int(mode == 'panic'))
                            self.assertEqual(data['Metrics'].get('otelc.function.errors', 0), 0 if mode == 'normal' else 2)
                        receipts.append({'collection': collection, 'sampling': sampling, 'mode': mode, 'health': state,
                                         'span_count': len(data['Spans']), 'output': baseline.decode()})
            self.assertEqual((original / 'App.go').read_bytes(), source)
            self.assertEqual((original / 'go.mod').read_bytes(), module)
            self.assertEqual((private / 'App.go').read_bytes(), source)
            self.assertEqual({str(p.relative_to(sdk)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sdk.rglob('*') if p.is_file()}, frozen_sdk)
            (ROOT / 'build/go-probe-integration.json').write_text(json.dumps({
                'compiler': observed, 'cases': receipts, 'actual_application_launches': 24,
                'original_source_sha256': context.digest(source), 'original_inputs_held': True,
                'private_sdk_files_held': len(frozen_sdk),
                'scope': 'Actual compiler-generated function/weak GC/resource probes only; public launch/control/health and provider admission remain work.'}, indent=2) + '\n')
