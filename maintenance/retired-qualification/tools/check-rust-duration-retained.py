"""Check the corrected oracle against old decoded data, never execute binaries."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

BASE = Path('/Users/sclarke/github/otelc-qualification')
ROOT = BASE / 'work/rust-receipt-duration-accounting'
OUT = BASE / '2026-10-26/rust-receipt-duration-accounting'
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from ci import rust_telemetry
from otelc.config import Policy
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

path = BASE / '2026-10-26/rust-prepared-execution/linux-original.zip'
with zipfile.ZipFile(path) as archive:
    capture = SimpleNamespace(metrics=[], traces=[])
    for name in archive.namelist():
        if name.startswith('public-case/decoded-metrics-'):
            capture.metrics.append(ExportMetricsServiceRequest.FromString(archive.read(name)))
        elif name.startswith('public-case/decoded-traces-'):
            capture.traces.append(ExportTraceServiceRequest.FromString(archive.read(name)))
    results = []
    for case, function, spans, calls, lifetimes in (
        ('constructor_argv_and_same_pid_live_metric_states', 'app.rs::workload', 4, 2, {}),
        ('drop_profile_preserves_plain_and_instrumented_outcomes', 'LifetimeApp.rs::workload', 1, 1,
         {'Item lifetime': 1, 'Zero lifetime': 2}),
    ):
        prefix = 'public-case/test_prepared_real_' + case + '/False/'
        request = json.loads(archive.read(prefix + 'rust-request.json'))
        health = json.loads(archive.read(prefix + 'app/health.json'))
        results.append(rust_telemetry.verify(capture, Policy.parse(request['policy']), health,
            function=function, expected_spans=spans, expected_calls=calls, lifetimes=lifetimes))
value = {'observed_head': '26eb9dda48eec940fc40a7687248dadc1163390c',
    'job': 121377916135, 'artifact': 12624728642,
    'original_zip_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    'new_helper_sha256': hashlib.sha256((ROOT / 'ci/rust_telemetry.py').read_bytes()).hexdigest(),
    'results': results, 'local_application_starts': 0, 'local_sdk_builds': 0, 'local_compiler_builds': 0,
    'scope': 'Offline old decoded reserializations only; not corrected-head execution, original wire receipt or provider admission.'}
(OUT / 'retained-decoded-crosscheck.json').write_text(json.dumps(value, indent=2) + '\n')
print(json.dumps(value))
