"""Build and identify the pinned official Go compiler-instrumentation tool."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from ci import go_scope_patch
from otelc import go_context_bundle as context

ROOT = Path(__file__).resolve().parents[1]
GO_VERSION = 'go1.27.3'
PACKAGE = context.UPSTREAM_MODULE + '/tool/cmd/otelc'


def environment(output: Path) -> dict[str, str]:
    """Keep public tool dependencies and compiler caches outside application input."""
    value = {key: data for key, data in os.environ.items()
             if not key.startswith(('OTEL', 'GH_', 'GITHUB_', 'SONAR_', 'AWS_'))}
    value.update(GOWORK='off', GOFLAGS='', GO111MODULE='on', GOTOOLCHAIN=GO_VERSION,
                 GOPROXY='https://proxy.golang.org', GOSUMDB='sum.golang.org',
                 GOPRIVATE='', GONOPROXY='', GONOSUMDB='', GOBIN=str(output / 'bin'))
    value.setdefault('GOPATH', str(ROOT / 'build/go-path'))
    value.setdefault('GOMODCACHE', str(ROOT / 'build/go-module-cache'))
    value.setdefault('GOCACHE', str(ROOT / 'build/go-build-cache'))
    return value


def compiler_environment(toolchain: dict[str, Any], output: Path) -> dict[str, str]:
    """Pin the upstream wrapper's nested `go` command as well as direct calls."""
    compiler = Path(toolchain['go'])
    if not compiler.is_absolute() or context.digest(compiler.read_bytes()) != toolchain['go_sha256']:
        raise ValueError('selected Go compiler identity changed')
    value = environment(output)
    value.update(GOTOOLCHAIN='local', GOROOT=str(compiler.parent.parent),
                 PATH=str(compiler.parent) + os.pathsep + value.get('PATH', ''))
    return value


def command(arguments: list[str], *, directory: Path, env: dict[str, str]) -> bytes:
    """Retain subprocess errors while using argv without a shell."""
    result = subprocess.run(arguments, cwd=directory, env=env, check=False,
                            capture_output=True, timeout=600)
    if result.returncode:
        (directory / 'go-tool-error.log').write_bytes(result.stderr)
        detail = result.stderr.decode('utf-8', errors='replace')[-4096:]
        raise RuntimeError(f'Go upstream build failed (exit {result.returncode}): {detail}')
    return result.stdout


def build(go: str, output: Path, *, scope_safe: bool = False) -> dict[str, Any]:
    """Verify the selected release and checksums before publishing its identity."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = environment(output)
    observed = json.loads(command([go, 'env', '-json', 'GOROOT', 'GOVERSION',
                                   'GOOS', 'GOARCH', 'GOHOSTOS', 'GOHOSTARCH'],
                                  directory=output, env=env))
    if (observed.get('GOVERSION') != GO_VERSION
            or observed.get('GOOS') != observed.get('GOHOSTOS')
            or observed.get('GOARCH') != observed.get('GOHOSTARCH')):
        raise ValueError('Go source profile requires native Go1.27.3')
    compiler = Path(observed['GOROOT']) / 'bin/go'
    env.update(GOTOOLCHAIN='local', GOROOT=str(compiler.parent.parent),
               PATH=str(compiler.parent) + os.pathsep + env.get('PATH', ''))
    module = json.loads(command([str(compiler), 'mod', 'download', '-json',
                                 context.UPSTREAM_MODULE + '@' + context.UPSTREAM_VERSION],
                                directory=output, env=env))
    if (module.get('Path') != context.UPSTREAM_MODULE or module.get('Version') != context.UPSTREAM_VERSION
            or module.get('Sum') != context.MODULE_SUM or module.get('GoModSum') != context.MODULE_GO_SUM):
        raise ValueError('Go instrumentation module differs from the pinned release')
    bundle = Path(module['Dir']) / 'tool/data/otelc-bundle.tgz'
    if context.digest(bundle.read_bytes()) != context.BUNDLE_SHA256:
        raise ValueError('Go instrumentation asset differs from the pinned release')
    # Go verifies module-cache source contents against its downloaded checksums.
    verification = output / 'verify'
    verification.mkdir(exist_ok=True)
    (verification / 'go.mod').write_text('module io.quux.otelc/upstream-verification\n\ngo 1.26.0\n\n'
                                        f'require {context.UPSTREAM_MODULE} {context.UPSTREAM_VERSION}\n')
    command([str(compiler), 'mod', 'verify'], directory=verification, env=env)
    tool = output / 'bin/otelc'
    if scope_safe:
        go_scope_patch.prepare(Path(module['Dir']), output)
        tool.parent.mkdir(exist_ok=True)
        command([str(compiler), 'build', '-mod=readonly', '-buildvcs=false',
                 '-ldflags=-X=' + context.UPSTREAM_MODULE + '/tool/util.Version=' + go_scope_patch.VERSION,
                 '-o', str(tool), './tool/cmd/otelc'], directory=output / 'source', env=env)
    else:
        command([str(compiler), 'install', PACKAGE + '@' + context.UPSTREAM_VERSION], directory=output, env=env)
    details = command([str(compiler), 'version', '-m', str(tool)], directory=output, env=env).decode()
    expected = '\tmod\t' + context.UPSTREAM_MODULE + ('\t(devel)\t' if scope_safe else
                 '\t' + context.UPSTREAM_VERSION + '\t' + context.MODULE_SUM)
    if expected not in details or '\tpath\t' + PACKAGE not in details:
        raise ValueError('Go instrumentation executable has unexpected build metadata')
    version = command([str(tool), 'version'], directory=output, env=env).decode().strip()
    if version != 'otelc version ' + (go_scope_patch.VERSION if scope_safe else context.UPSTREAM_VERSION):
        raise ValueError('Go instrumentation executable reports an unexpected version')
    value = {'schema_version': 1, 'go_version': GO_VERSION,
             'go': str(compiler), 'go_sha256': context.digest(compiler.read_bytes()),
             'platform': {key: observed[key] for key in ('GOOS', 'GOARCH')},
             'module': context.UPSTREAM_MODULE, 'version': context.UPSTREAM_VERSION,
             'module_sum': context.MODULE_SUM, 'module_go_sum': context.MODULE_GO_SUM,
             'weaver': str(tool), 'weaver_sha256': context.digest(tool.read_bytes()),
             'bundle': str(bundle), 'bundle_sha256': context.BUNDLE_SHA256,
             'build_metadata': details}
    if scope_safe:
        source_sha256 = go_scope_patch.digest((output / go_scope_patch.MANIFEST).read_bytes())
        go_scope_patch.verify(output, source_sha256)
        value.update(schema_version=2, patch_profile=go_scope_patch.PROFILE,
                     source_manifest_sha256=source_sha256, version_output=version)
    (output / 'toolchain.json').write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    return value


def main() -> None:
    """Build only the operator-selected toolchain; never compile an application."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--go', default=os.environ.get('OTELC_GO', 'go'))
    parser.add_argument('--output', type=Path, default=ROOT / 'build/go-upstream')
    parser.add_argument('--scope-safe', action='store_true',
                        help='build the explicitly identified private raw-rule backport')
    args = parser.parse_args()
    print(json.dumps(build(args.go, args.output, scope_safe=args.scope_safe), indent=2))


if __name__ == '__main__':
    main()
