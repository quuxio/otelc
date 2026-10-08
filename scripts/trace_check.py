"""Verify unchanged examples through the local Collector and Tempo, for every adapter."""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Expected invocations, complete trees and escaping-error spans in the fixed fixtures.
EXAMPLES = {
    "c": ("c-traces.c", 7, 3, 0),
    "cpp": ("cpp-traces.cpp", 31, 4, 3),
    "rust": ("rust_trace_app.rs", 8, 4, 2),
    "python": ("python_trace_app.py", 9, 5, 3),
    "java": ("JavaTraceApp.java", 10, 6, 2),
    "javascript": ("javascript_trace_app.mjs", 11, 5, 1),
    "typescript": ("typescript_trace_app.mts", 11, 5, 1),
    "go": ("go_app.go", 10, 7, 1),
}
TEMPO = "http://127.0.0.1:3200"


def get_json(path):
    with urllib.request.urlopen(TEMPO + path, timeout=5) as response:
        return json.load(response)


def identity(encoded, length):
    value = base64.b64decode(encoded, validate=True)
    if len(value) != length or not any(value):
        raise ValueError("invalid stored trace/span ID")
    return value.hex()


def search_identity(value):
    # Tempo search omits leading zeroes; stored protobuf IDs remain 16 bytes.
    if (not isinstance(value, str) or not 1 <= len(value) <= 32
            or any(c not in "0123456789abcdef" for c in value) or int(value, 16) == 0):
        raise ValueError("invalid Tempo search trace ID")
    return value.zfill(32)


def instrumented_spans(document, service):
    for batch in document.get("batches", []):
        attrs = batch.get("resource", {}).get("attributes", [])
        if not any(a.get("key") == "service.name" and a.get("value", {}).get("stringValue") == service for a in attrs):
            raise ValueError("stored trace has the wrong service")
        for scope in batch.get("scopeSpans", []):
            if scope.get("scope", {}).get("name") != "quux.otelc":
                raise ValueError("stored trace has the wrong instrumentation scope")
            yield from scope.get("spans", [])


def decode_span(span, trace_id):
    if identity(span["traceId"], 16) != trace_id:
        raise ValueError("mixed trace identities")
    start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
    if start <= 0 or end < start or not span.get("name"):
        raise ValueError("invalid stored span timestamps/name")
    return {"parent": identity(span["parentSpanId"], 8) if span.get("parentSpanId") else None,
            "start": start, "end": end, "name": span["name"],
            "error": span.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR")}


def verify_ancestor_chain(key, nodes):
    visited = {key}
    parent = nodes[key]["parent"]
    while parent is not None:
        if parent in visited:
            raise ValueError("stored span parents contain a cycle")
        if parent not in nodes:
            raise ValueError("stored span has a missing parent")
        visited.add(parent)
        parent = nodes[parent]["parent"]


def verify_parent(key, node, nodes):
    parent = node["parent"]
    if parent is None:
        return
    if parent not in nodes or parent == key:
        raise ValueError("stored span has a missing/self parent")
    ancestor = nodes[parent]
    if not ancestor["start"] <= node["start"] <= node["end"] <= ancestor["end"]:
        raise ValueError("child span lies outside its parent")
    verify_ancestor_chain(key, nodes)


def stored_tree(document, trace_id, service):
    nodes = {}
    for span in instrumented_spans(document, service):
        key = identity(span["spanId"], 8)
        if key in nodes:
            raise ValueError("duplicate span ID")
        nodes[key] = decode_span(span, trace_id)
    if not nodes or sum(n["parent"] is None for n in nodes.values()) != 1:
        raise ValueError("stored tree must have exactly one root")
    for key, node in nodes.items():
        verify_parent(key, node, nodes)
    return {"trace_id": trace_id, "spans": list(nodes.values())}


def service_trees(service, query):
    search = get_json("/api/search?" + query)
    ids = [search_identity(entry["traceID"]) for entry in search.get("traces", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate Tempo search trace IDs")
    return [stored_tree(get_json("/api/traces/" + key), key, service) for key in ids]


def complete_counts(trees, expected_spans, expected_trees, expected_errors):
    spans = [span for tree in trees for span in tree["spans"]]
    if len(trees) > expected_trees or len(spans) > expected_spans:
        raise ValueError("unexpected duplicate/extra stored traces")
    if len(trees) != expected_trees or len(spans) != expected_spans:
        return False
    if sum(span["error"] for span in spans) != expected_errors:
        raise ValueError("stored escaping-error span count differs")
    return True


def wait_for_storage(service, expected_spans, expected_trees, expected_errors, timeout):
    deadline = time.monotonic() + timeout
    query = urllib.parse.urlencode({"q": '{ resource.service.name = ' + json.dumps(service) + ' }', "limit": 100})
    while True:
        try:
            trees = service_trees(service, query)
            if complete_counts(trees, expected_spans, expected_trees, expected_errors):
                return trees
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        if time.monotonic() >= deadline:
            raise TimeoutError("Tempo did not retain all expected spans before the deadline")
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def report_complete(report, spans, trees):
    tracing = report.get("traces")
    if (not isinstance(tracing, dict) or not isinstance(report.get("losses"), dict)
            or not isinstance(tracing.get("losses"), dict)
            or not any(key in report for key in ("export_loss", "export_dropped_batches"))
            or report.get("export_finished") is not True
            or report.get("drained", True) is not True
            or report.get("export_loss", 0) or report.get("export_dropped_batches", 0)
            or any(report.get("losses", {}).values()) or any(tracing.get("losses", {}).values())
            or tracing.get("active_trees") != 0 or tracing.get("queued_trees") != 0
            or tracing.get("completed_trees") != trees or report.get("function_calls") != spans):
        raise ValueError("application report contains incomplete telemetry or unexpected counts")


def execute(command, root, environment):
    result = subprocess.run([str(item) for item in command], cwd=root, env=environment,
                            text=True, capture_output=True, timeout=180, check=True)
    return result.stdout


def commands(root, folder, language, source, config, environment):
    cli = root / "target/debug/quux-otelc"
    prefix = [cli, "--config", config, "--language", language]
    if language in ("c", "cpp", "rust"):
        plain, instrumented = folder / "plain", folder / "instrumented"
        if language == "rust":
            execute([environment.get("OTELC_RUSTC", "rustc"), "--edition=2024", source, "-o", plain], root, environment)
            return [plain], prefix + ["rust", source]
        tool = "clang++" if language == "cpp" else "clang"
        compiler = root / "target/debug/otelc-llvm-toolchain.json"
        flags = ["-O2", "-g", "-pthread", "-std=c++17"] if language == "cpp" else ["-O1", "-g", "-pthread"]
        llvm = json.loads(compiler.read_text())
        execute([Path(llvm["bindir"]) / tool, *flags, source, "-o", plain], root, environment)
        execute(prefix + [tool, *flags, source, "-o", instrumented], root, environment)
        return [plain], prefix + ["run", instrumented]
    interpreter = {"python": sys.executable, "java": environment.get("OTELC_JAVA", "java"),
                   "go": environment.get("OTELC_GO", "go"), "javascript": environment.get("OTELC_NODE", "node"),
                   "typescript": environment.get("OTELC_NODE", "node")}[language]
    plain = [interpreter, source]
    if language == "go":
        plain = [interpreter, "run", source]
    elif language == "typescript":
        plain = [interpreter, "--import", root / "adapters/node/plain.mjs", source]
    adapter = {"javascript": "node", "typescript": "ts"}.get(language, language)
    return plain, prefix + [adapter, source]


def run_example(root, output, language, timeout, run_id):
    filename, spans, trees, errors = EXAMPLES[language]
    source = root / "examples/apps" / filename
    original = source.read_bytes()
    policy_path = root / "examples" / (language + "-traces.toml")
    policy = policy_path.read_bytes()
    folder = output / language
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / "policy.toml"
    config.write_bytes(policy)
    service = "otelc-" + language + "-trace-check-" + run_id
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "OTELC_"))}
    # Preserve only explicit toolchain choices, never ambient signal configuration.
    environment.update({key: value for key, value in os.environ.items() if key in ("OTELC_NODE", "OTELC_JAVA", "OTELC_GO", "OTELC_RUSTC")})
    environment.update(OTELC_PYTHON=sys.executable, OTEL_SERVICE_NAME=service)
    report_path = folder / "runtime.json"
    report_path.unlink(missing_ok=True)
    try:
        plain, instrumented = commands(root, folder, language, source, config, environment)
        baseline = execute(plain, root, environment)
        environment["OTELC_REPORT_PATH"] = str(report_path)
        actual = execute(instrumented, root, environment)
        if actual != baseline:
            raise ValueError("plain and instrumented application outputs differ")
        report = json.loads(report_path.read_text())
        report_complete(report, spans, trees)
        stored = wait_for_storage(service, spans, trees, errors, timeout)
        return {"language": language, "service": service, "source_sha256": hashlib.sha256(original).hexdigest(),
                "output": actual, "runtime": report, "stored_trees": stored,
                "dashboard": "http://localhost:3000/d/otelc-traces?" + urllib.parse.urlencode({
                    "var-service": service, "var-traceId": stored[0]["trace_id"]})}
    finally:
        if source.read_bytes() != original or policy_path.read_bytes() != policy:
            raise ValueError("instrumentation changed original source/configuration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", action="append", choices=list(EXAMPLES))
    parser.add_argument("--output", type=Path, default=Path("build/trace-check"))
    parser.add_argument("--timeout", type=int, default=60, help="Tempo ingestion budget per language, in seconds")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300:
        parser.error("timeout must be 1..300 seconds")
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    destination = output / "results.json"
    destination.unlink(missing_ok=True)
    run_id = uuid.uuid4().hex[:12]
    for language in args.language or EXAMPLES:
        result = run_example(root, output, language, args.timeout, run_id)
        results.append(result)
        print(language + ": stored " + str(sum(len(tree["spans"]) for tree in result["stored_trees"])) + " spans; " + result["dashboard"], flush=True)
    destination.write_text(json.dumps({"complete": True, "results": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
