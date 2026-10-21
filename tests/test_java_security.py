"""The actual launched agent uses the fixed upstream parser and refuses altered patches."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from otelc import health, java, prepared_java
from otelc.config import Policy

SOURCE = r'''
import java.lang.reflect.Proxy;
import java.util.Map;
public final class ParserApp {
  public static void main(String[] args) throws ReflectiveOperationException {
    String prefix = "io.opentelemetry.javaagent.shaded.io.opentelemetry.";
    Class<?> parser = Class.forName(prefix + "api.trace.propagation.internal.W3CTraceContextEncoding", true, null);
    String header = args[0].equals("large") ? "a=b" + " ".repeat(131072) + "!" : args[0];
    Class<?> contextType = Class.forName(prefix + "context.Context", true, null);
    Class<?> getterType = Class.forName(prefix + "context.propagation.TextMapGetter", true, null);
    Class<?> propagatorType = Class.forName(prefix + "api.trace.propagation.W3CTraceContextPropagator", true, null);
    Class<?> spanType = Class.forName(prefix + "api.trace.Span", true, null);
    Class<?> spanContextType = Class.forName(prefix + "api.trace.SpanContext", true, null);
    Class<?> stateType = Class.forName(prefix + "api.trace.TraceState", true, null);
    Map<String, String> carrier = Map.of("traceparent", "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01", "tracestate", header);
    Object getter = Proxy.newProxyInstance(null, new Class<?>[] {getterType}, (proxy, method, values) -> {
      if (method.getName().equals("get")) return carrier.get((String) values[1]);
      if (method.getName().equals("keys")) return carrier.keySet();
      throw new UnsupportedOperationException(method.getName());
    });
    Object propagator = propagatorType.getMethod("getInstance").invoke(null);
    Object root = contextType.getMethod("root").invoke(null);
    long start = System.nanoTime();
    Object extracted = propagatorType.getMethod("extract", contextType, Object.class, getterType).invoke(propagator, root, carrier, getter);
    Object span = spanType.getMethod("fromContext", contextType).invoke(null, extracted);
    Object spanContext = spanType.getMethod("getSpanContext").invoke(span);
    Object state = spanContextType.getMethod("getTraceState").invoke(spanContext);
    String outcome = (String) parser.getMethod("encodeTraceState", stateType).invoke(null, state);
    long elapsed = System.nanoTime() - start;
    System.out.println("{\"bootstrap\":" + (parser.getClassLoader() == null)
      + ",\"origin\":\"" + parser.getResource("W3CTraceContextEncoding.class")
      + "\",\"outcome\":\"" + outcome + "\",\"trace_id\":\"" + spanContextType.getMethod("getTraceId").invoke(spanContext)
      + "\",\"elapsed_ns\":" + elapsed + "}");
  }
}
'''


class ParserPatchTests(unittest.TestCase):
    def test_refuses_missing_changed_or_additional_bootstrap_code(self) -> None:
        original = java.parser_patch()
        with zipfile.ZipFile(original) as archive:
            entries = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
        with tempfile.TemporaryDirectory() as directory, patch.object(java, "ROOT", Path(directory)):
            path = Path(directory) / "adapters/java/target" / original.name
            path.parent.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "build the Java bootstrap"):
                java.parser_patch()
            cases = [entries | {java.PARSER_CLASS: b"changed"},
                     entries | {"Unexpected.class": b"injected"},
                     {name: value for name, value in entries.items() if "LICENSE" not in name},
                     entries | {"unrelated/": b""},
                     entries | {java.PARSER_CLASS: b"large" * 4_000}]
            for values in cases:
                with self.subTest(names=sorted(values)), zipfile.ZipFile(path, "w") as archive:
                    for name, value in values.items():
                        archive.writestr(name, value)
                with self.assertRaises(ValueError):
                    java.parser_patch()
            with zipfile.ZipFile(path, "w") as archive, warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                for name, value in entries.items():
                    archive.writestr(name, value)
                archive.writestr(java.PARSER_CLASS, entries[java.PARSER_CLASS])
            with self.assertRaisesRegex(ValueError, "unexpected"):
                java.parser_patch()
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_BZIP2) as archive:
                for name, value in entries.items():
                    archive.writestr(name, value)
            with self.assertRaisesRegex(ValueError, "unsupported"):
                java.parser_patch()
            with zipfile.ZipFile(path, "w") as archive:
                for name, value in entries.items():
                    info = zipfile.ZipInfo(name)
                    if name == java.PARSER_CLASS:
                        info.filename = name + "\x00alias"
                    archive.writestr(info, value)
            with self.assertRaisesRegex(ValueError, "unsupported"):
                java.parser_patch()
            path.write_bytes(b"not a zip")
            with self.assertRaisesRegex(ValueError, "invalid bootstrap"):
                java.parser_patch()
            path.write_bytes(b"x" * 262_145)
            with self.assertRaisesRegex(ValueError, "too large"):
                java.parser_patch()
            with patch.object(java, "ROOT", Path(directory) / ("bad" + os.pathsep + "path")):
                with self.assertRaisesRegex(ValueError, "classpath separator"):
                    java.parser_patch()

    def test_actual_agent_loads_patched_parser_and_preserves_valid_state(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            source = root / "ParserApp.java"
            source.write_text(SOURCE)
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            subprocess.run([java.executable("javac"), "-d", str(classes), str(source)], check=True)
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "ParserApp", "classpath": ["classes"],
                "bindings": {"ParserApp": {"source": str(source), "policy_path": "src/ParserApp.java",
                    "source_sha256": original, "class_sha256": hashlib.sha256((classes / "ParserApp.class").read_bytes()).hexdigest(),
                    "provenance": "test fixture compiled directly by the selected javac"}}}))
            application = prepared_java.PreparedApplication.load(manifest)
            policy = Policy.parse({"schema_version": 3, "languages": ["java"],
                                   "traces": {"enabled": False}})
            for index, (header, expected) in enumerate([
                    ("large", ""), ("a=b,c=d", "a=b,c=d"),
                    (" \ta=b , c=d\t ", "a=b,c=d"), ("a=b,a=c", "")]):
                runtime = root / str(index)
                runtime.mkdir(mode=0o700)
                launch = prepared_java.prepare(application, policy, runtime, arguments=(header,), diagnostic_profile=True)
                result = subprocess.run(launch.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=15, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                launch.verify_inputs()
                observed = json.loads(result.stdout)
                self.assertTrue(observed["bootstrap"])
                self.assertTrue(observed["origin"].startswith("jar:"))
                archive_url, member = observed["origin"][4:].split("!/", 1)
                self.assertEqual(Path(unquote(urlsplit(archive_url).path)), java.parser_patch().resolve())
                self.assertEqual(member, java.PARSER_CLASS)
                self.assertEqual(observed["outcome"], expected)
                self.assertEqual(observed["trace_id"], "0123456789abcdef0123456789abcdef")
                self.assertLess(observed["elapsed_ns"], 2_000_000_000)
                self.assertEqual(result.stderr, "")
                report = health.read(runtime / "health.json")
                self.assertEqual(report["phase"], "final")
                self.assertTrue(report["sdk_attached"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)


if __name__ == "__main__":
    unittest.main()
