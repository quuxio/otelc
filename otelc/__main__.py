"""User entry point for policy inspection, examples and live controls."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from otelc import benchmark, control, health, java, prepared_java
from otelc.config import LANGUAGES, Policy


def main() -> int:
    """Dispatch CLI work without embedding policy or instrumentation logic."""
    parser = argparse.ArgumentParser(prog="quux-otelc")
    parser.add_argument("--config", type=Path, default=Path("examples/otelc.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    compare = commands.add_parser("benchmark")
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--no-publish", action="store_true")
    example = commands.add_parser("example")
    example.add_argument("language", choices=LANGUAGES)
    example.add_argument("--plain", action="store_true")
    prepared = commands.add_parser("java", help="launch a prepared classpath with verified source bindings")
    prepared.add_argument("--manifest", type=Path, required=True)
    prepared.add_argument("--runtime", type=Path, required=True, help="new private directory with a short path")
    prepared.add_argument("--plain", action="store_true")
    prepared.add_argument("--prepare-only", action="store_true", help="emit JSON launch identity without starting Java")
    prepared.add_argument("--diagnostic-profile", action="store_true", help="disable JVM CDS in both comparison lanes")
    prepared.add_argument("arguments", nargs=argparse.REMAINDER)
    metrics = commands.add_parser("metrics")
    metrics.add_argument("--socket", type=Path, required=True)
    metrics.add_argument("operation", nargs="+", choices=("on", "off", "collection", "emission", "status", "flush"))
    report = commands.add_parser("health", help="inspect SDK pipeline evidence, without claiming downstream receipt")
    source = report.add_mutually_exclusive_group(required=True)
    source.add_argument("--socket", type=Path)
    source.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "metrics":
            print(json.dumps(control.request(args.socket, " ".join(args.operation)), indent=2))
            return 0
        if args.command == "health":
            value = control.request(args.socket, "health") if args.socket else health.read(args.report)
            print(json.dumps(value, indent=2))
            return 0
        policy = Policy.load(args.config)
        if args.command == "java":
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            return prepared_java.run(args.manifest, policy, args.runtime, plain=args.plain,
                                     prepare_only=args.prepare_only, arguments=tuple(arguments),
                                     diagnostic_profile=args.diagnostic_profile)
        if args.command == "plan":
            print(json.dumps({"policy": policy.data, "implemented": ["java"], "remaining": [language for language in LANGUAGES if language != "java"]}, indent=2))
            return 0
        if args.command == "benchmark":
            report = benchmark.run(policy, args.output, publish=not args.no_publish)
            print(json.dumps({"report": str(args.output / "report.json"), "run_id": report["run_id"], "medians_ns": report["median_ns_per_operation"]}, indent=2))
            return 0
        if args.language != "java":
            raise ValueError(f"{args.language} is not implemented in the rebuild yet")
        with tempfile.TemporaryDirectory(prefix="otelc-", dir="/tmp") as temporary:
            command = java.command(policy, Path(temporary), plain=args.plain)
            if not args.plain:
                print(f"Control socket: {temporary}/control.sock", file=sys.stderr)
            return subprocess.call(command)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"quux-otelc: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
