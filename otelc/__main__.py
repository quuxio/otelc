"""User entry point for policy inspection, examples and live controls."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    # Normal module startup has already run site hooks. Refuse this CLI path
    # before SDK imports instead of returning contaminated application/JSON data.
    print("Use bin/quux-otelc for isolated CLI startup.", file=sys.stderr)
    raise SystemExit(2)

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from otelc import benchmark, control, health, java, node, node_benchmark, prepared_java, witness
from otelc import prepared_python as python_adapter
from otelc import python_benchmark, python_recipe, native_benchmark
from otelc import native_launch
from otelc import native_build, prepared_native, rust_compiler, rust_build, rust_launch
from otelc.config import LANGUAGES, Policy


def run_example(policy: Policy, language: str, *, plain: bool) -> int:
    """Keep example runtime files through the selected adapter's complete shutdown."""
    if language not in ("java", "javascript", "typescript", "python"):
        raise ValueError(f"{language} is not implemented in the rebuild yet")
    with tempfile.TemporaryDirectory(prefix="otelc-", dir="/tmp") as temporary:
        if language == "python":
            runtime = Path(temporary) / 'runtime'
            if not plain:
                print(f"Control descriptor: {runtime}/control.json", file=sys.stderr)
            return python_adapter.run(policy, runtime, root=python_adapter.ROOT,
                entry=python_adapter.ROOT / 'examples/python/BenchmarkApp.py', plain=plain)
        if language != "java":
            return node.run(policy, Path(temporary), language, plain=plain)
        command = java.command(policy, Path(temporary), plain=plain)
        if not plain:
            print(f"Control socket: {temporary}/control.sock", file=sys.stderr)
        return subprocess.call(command)


def main() -> int:
    """Dispatch CLI work without embedding policy or instrumentation logic."""
    parser = argparse.ArgumentParser(prog="quux-otelc")
    parser.add_argument("--config", type=Path, default=Path("examples/otelc.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    compare = commands.add_parser("benchmark")
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--no-publish", action="store_true")
    compare.add_argument("--language", choices=("java", "javascript", "typescript", "python", "c", "cpp"), default="java")
    compare.add_argument('--sdk-build', type=Path, default=Path('build/native-launch-sdk'),
                         help='matching native SDK build for C/C++ benchmarks')
    compare.add_argument("--backend", choices=("native", "api"), default="native")
    compare.add_argument("--comparison-id", help="group equivalent language runs in Grafana")
    example = commands.add_parser("example")
    example.add_argument("language", choices=LANGUAGES)
    example.add_argument("--plain", action="store_true")
    prepared = commands.add_parser("java", help="launch a prepared classpath with verified source bindings")
    prepared.add_argument("--manifest", type=Path, required=True)
    prepared.add_argument("--runtime", type=Path, required=True, help="new private directory with a short path")
    prepared.add_argument("--plain", action="store_true")
    prepared.add_argument("--prepare-only", action="store_true", help="emit JSON launch identity without starting Java")
    prepared.add_argument("--diagnostic-profile", action="store_true", help="disable JVM CDS in both comparison lanes")
    prepared.add_argument("--witness-bindings", type=Path, help="explicit frozen synchronous Java invocation bindings")
    prepared.add_argument("--witness-profile", choices=(witness.PROFILE, witness.HANDLE_PROFILE),
                          help="explicit diagnostic binding profile; requires --witness-bindings")
    prepared.add_argument("arguments", nargs=argparse.REMAINDER)
    original = commands.add_parser("node", help="launch original JavaScript or Microsoft TypeScript sources")
    original.add_argument("--language", choices=("javascript", "typescript"), required=True)
    original.add_argument("--root", type=Path, default=Path.cwd())
    original.add_argument("--entry", type=Path, required=True)
    original.add_argument("--runtime", type=Path, required=True, help="new private directory retained for reports")
    original.add_argument("--backend", choices=("native", "api"), default="native")
    original.add_argument("--plain", action="store_true")
    original.add_argument("arguments", nargs=argparse.REMAINDER)
    native = commands.add_parser("native", help="candidate: compile and launch original C/C++ translation units")
    native.add_argument("--language", choices=("c", "cpp"), required=True)
    native.add_argument("--root", type=Path, required=True)
    native.add_argument("--source", type=Path, action="append", required=True)
    native.add_argument("--build", type=Path, required=True, help="new output directory outside the application root")
    native.add_argument("--runtime", type=Path, required=True, help="new private runtime directory")
    native.add_argument("--sdk-library", type=Path, required=True, help="explicit built native SDK launch library")
    native.add_argument("--plain", action="store_true")
    native.add_argument("arguments", nargs=argparse.REMAINDER)
    native_protocol_parser = commands.add_parser('native-protocols', help='describe explicit owned native formats without compiling')
    native_protocol_parser.add_argument('--build-version', type=int, choices=(2, 3, 4, 5), default=2)
    rust_tools = commands.add_parser('rust-tools', help='build the pinned original-source Rust compiler and private bridge')
    rust_tools.add_argument('--output', type=Path, required=True)
    rust_build_parser = commands.add_parser('rust-build', help='candidate: retain stock and woven original-source Rust applications')
    rust_build_parser.add_argument('--root', type=Path, required=True)
    rust_build_parser.add_argument('--entry', type=Path, required=True)
    rust_build_parser.add_argument('--output', type=Path, required=True)
    rust_build_parser.add_argument('--tools', type=Path, required=True)
    rust_build_parser.add_argument('--sdk-build', type=Path, required=True)
    rust_build_parser.add_argument('--optimisation', type=int, choices=(0, 1, 2, 3), default=2)
    rust_run_parser = commands.add_parser('rust-run', help='candidate: run a retained Rust build with live metric controls')
    rust_run_parser.add_argument('--build', type=Path, required=True)
    rust_run_parser.add_argument('--runtime', type=Path, required=True)
    rust_run_parser.add_argument('--plain', action='store_true')
    rust_run_parser.add_argument('arguments', nargs=argparse.REMAINDER)
    native_build_parser = commands.add_parser('native-build', help='candidate: retain a native build for owned execution')
    native_build_parser.add_argument('--language', choices=('c', 'cpp'), required=True)
    native_build_parser.add_argument('--root', type=Path, required=True)
    native_inputs = native_build_parser.add_mutually_exclusive_group(required=True)
    native_inputs.add_argument('--source', type=Path, action='append')
    native_inputs.add_argument('--recipe', type=Path, help='explicit build-3 per-unit recipe JSON')
    native_build_parser.add_argument('--output', type=Path, required=True)
    native_build_parser.add_argument('--sdk-build', type=Path, required=True)
    native_run_parser = commands.add_parser('native-run', help='candidate: execute a retained native build through its host gate')
    native_run_parser.add_argument('--build', type=Path, required=True)
    native_run_parser.add_argument('--runtime', type=Path, required=True)
    native_run_parser.add_argument('--plain', action='store_true')
    native_run_parser.add_argument('--prepare-only', action='store_true')
    native_run_parser.add_argument('arguments', nargs=argparse.REMAINDER)
    python = commands.add_parser("python", help="candidate: launch original CPython sources")
    python.add_argument("--root", default=str(Path.cwd()))
    python_entry = python.add_mutually_exclusive_group(required=True)
    python_entry.add_argument("--entry")
    python_entry.add_argument("--module")
    python.add_argument("--runtime", type=Path, required=True, help="new private directory retained for reports")
    python.add_argument("--plain", action="store_true")
    python.add_argument("--profile", choices=(python_recipe.INPUT_PROFILE, python_recipe.ORIGIN_PROFILE, python_recipe.CACHE_PROFILE),
                        default=python_recipe.INPUT_PROFILE, help="select the declared launch evidence profile")
    python.add_argument("--prepare-only", action="store_true", help="emit fixed JSON launch recipe without starting app or SDK")
    python.add_argument("arguments", nargs=argparse.REMAINDER)
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
        if args.command == 'native-protocols':
            print(json.dumps(prepared_native.protocols(build_version=args.build_version), indent=2))
            return 0
        if args.command == 'rust-tools':
            print(json.dumps(rust_compiler.build(args.output).manifest(), indent=2))
            return 0
        policy = Policy.load(args.config)
        if args.command == 'rust-build':
            value = rust_build.build(policy, args.root, args.entry, args.output,
                tools_directory=args.tools, sdk_directory=args.sdk_build, optimisation=args.optimisation)
            print(json.dumps(value.manifest(), indent=2))
            return 0
        if args.command == 'rust-run':
            return rust_launch.run(policy, args.runtime, build_directory=args.build, plain=args.plain,
                                   arguments=application_arguments(args.arguments))
        if args.command == 'native-build':
            recipe = native_build.read(args.recipe)[0] if args.recipe else None
            value = native_build.build(policy, args.language, args.root, args.source or [],
                                       args.output, sdk_directory=args.sdk_build, recipe=recipe)
            print(json.dumps(value, indent=2))
            return 0
        if args.command == 'native-run':
            return prepared_native.run(policy, args.runtime, build_directory=args.build,
                arguments=application_arguments(args.arguments), plain=args.plain, prepare_only=args.prepare_only)
        if args.command == 'native':
            built = native_launch.build(policy, args.language, args.root, args.source, args.build,
                                        library=args.sdk_library)
            if not args.plain:
                print(f'Control descriptor: {args.runtime}/control.json', file=sys.stderr)
            process = native_launch.start(built, args.runtime, plain=args.plain,
                                          arguments=application_arguments(args.arguments))
            return process.wait()
        if args.command == "python":
            arguments = application_arguments(args.arguments)
            return python_adapter.run(policy, args.runtime, root=args.root, entry=args.entry,
                                      module=args.module, plain=args.plain, arguments=tuple(arguments),
                                      prepare_only=args.prepare_only, profile=args.profile)
        if args.command == "node":
            arguments = application_arguments(args.arguments)
            return node.run(policy, args.runtime, args.language, root=args.root, entry=args.entry,
                            backend=args.backend, plain=args.plain, arguments=tuple(arguments))
        if args.command == "java":
            arguments = application_arguments(args.arguments)
            witness_options = {} if args.witness_bindings is None else {"witness_bindings": args.witness_bindings}
            if args.witness_profile is not None:
                if args.witness_bindings is None:
                    raise ValueError("--witness-profile requires --witness-bindings")
                witness_options["witness_profile"] = args.witness_profile
            return prepared_java.run(args.manifest, policy, args.runtime, plain=args.plain,
                                     prepare_only=args.prepare_only, arguments=tuple(arguments),
                                     diagnostic_profile=args.diagnostic_profile, **witness_options)
        if args.command == "plan":
            print(json.dumps({"policy": policy.data, "implemented": ["java"],
                              "candidates": ["typescript", "javascript", "python", "rust"],
                              "remaining": [language for language in LANGUAGES if language != "java"]}, indent=2))
            return 0
        if args.command == "benchmark":
            report = run_benchmark(policy, args)
            print(json.dumps({"report": str(args.output / "report.json"), "run_id": report["run_id"], "medians_ns": report["median_ns_per_operation"]}, indent=2))
            return 0
        return run_example(policy, args.language, plain=args.plain)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        notes = ''.join(f"quux-otelc: {note}\n" for note in getattr(error, '__notes__', ()))
        parser.exit(1, f"quux-otelc: {error}\n{notes}")


def application_arguments(arguments: list[str]) -> tuple[str, ...]:
    """Remove only the CLI separator, retaining literal application arguments."""
    if arguments[:1] == ['--']:
        return tuple(arguments[1:])
    return tuple(arguments)


def run_benchmark(policy: Policy, args: argparse.Namespace) -> dict[str, Any]:
    """Select the benchmark adapter while preserving the common comparison settings."""
    if args.language == "java":
        return benchmark.run(policy, args.output, publish=not args.no_publish,
                             comparison_id=args.comparison_id)
    if args.language == 'python':
        return python_benchmark.run(policy, args.output, publish=not args.no_publish,
                                    comparison_id=args.comparison_id)
    if args.language in ('c', 'cpp'):
        return native_benchmark.run(policy, args.output, language=args.language, sdk_directory=args.sdk_build,
                                    publish=not args.no_publish, comparison_id=args.comparison_id)
    return node_benchmark.run(policy, args.output, language=args.language, backend=args.backend,
                              publish=not args.no_publish, comparison_id=args.comparison_id)
