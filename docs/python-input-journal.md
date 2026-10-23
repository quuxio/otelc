# Native Python entry input component

<!-- markdownlint-disable MD013 -->

This is the native entry building block used by the Python candidate's [prepared launcher](python-prepared-launch.md). It does not establish complete Python provenance or oteleq admission. Developers do not add imports, annotations, decorators or startup code to their application.

The component uses CPython's public [PyConfig and Py_RunMain APIs](https://docs.python.org/3.12/c-api/init_config.html), retaining native argument parsing, main frames, metadata, exception printing and shutdown. A platform-specific stdio boundary copies the main script's opened bytes into an anonymous real-FD snapshot. CPython consumes that exact snapshot under the original filename. The implementation does not replace a developer source file or construct a Python runner frame.

## Build and check

From the prepared repository environment:

```sh
make python-inputs
make python-inputs-check
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_input_journal test_python_input_build \
  test_python_native_inputs test_python_input_coverage -v
make check
```

`python-inputs` prints the tool-owned driver path. Its directory contains `build.json`, the shared library and the driver. The build identity binds the exact selected interpreter, Python library/config/header files, compiler, maintained native sources and flags. Products are published in a content-addressed directory; altered or partial products are refused on reuse. Profiling builds use a separate namespace. Clang and matching LLVM coverage tools are required for the native coverage gate; `CC` and `OTELC_LLVM_BIN` select installed tools.

These are component commands. The public `otelc python` launcher uses the fixed [prepare/command recipe and inherited-descriptor supervisor](python-prepared-launch.md). The full import/code journal and immutable provider index still need integration. Do not launch the private driver from a command copied out of a receipt.

## Evidence and failure boundary

The private native journal retains exact filesystem path bytes, actual application PID, original opened-file device/inode/size/mode/timestamp and consumed script bytes. Each record has ordered framing. Final counters distinguish source capture failure, journal I/O failure, interpreter finalization and whether a native entry was expected. The supervisor must separately retain the actual process exit. A successful entry receipt is not evidence of a successful application, SDK drain or downstream delivery.

Limits are 16 MiB per source, 64 MiB per journal, 4096 total records and 65535 bytes per path. Source admission reserves room for the final counters. Truncated, reordered, foreign-PID and inconsistent journals are refused. A limit or capture failure preserves native execution through the original input and cannot report complete entry evidence. A closed/reused journal descriptor cannot redirect writes into, or close, an unrelated application file. Interpreter `_exit` without finalization remains incomplete.

Raw C journal writes hold a private lock without calling Python, allocating Python objects, formatting application values or acquiring SDK/runtime locks. The stdio boundary checks the native main thread and the absence of an active Python frame before consuming its single capture arm. It does not treat an application-side open of the same path as the native entry read. Snapshot success is recorded only after the complete snapshot is ready.

This first component covers ordinary source-file entries on shared-library/framework GIL CPython 3.12+ on macOS/Linux. A module can still run natively, but this component deliberately does not claim its import/source evidence. Source snapshots can change the timing of concurrent source mutation; full qualification requires the original/resolved request, stable input profile, post-drain rechecks and independent behaviour checks. Custom loaders, native interposers, free-threaded/static interpreters, forks/subinterpreters, dynamic AST/code replacement and full source/import/pyc/header/code binding remain unqualified. The build index binds explicit Python inputs; the complete compiler/system-header and loaded-library closure remains provider work. In particular, path hashes alone must not stand in for bytes consumed by an interpreter.

## Validation scope

Native tests compare raw stdout/stderr, exits, encodings, BOM/CRLF, error printing, main-frame identity, relative/Unicode/symlink spelling, flags and entry metadata. They retain actual consumed bytes even when the running app later rewrites its own file. Other cases cover size/count bounds, invalid launch descriptors/options, actual failed I/O, FD reuse and abrupt exit. The C writer fixture tests protocol/status boundaries; its synthetic source records are not presented as real application provenance.

The native gate profiles real driver/library execution and requires at least 80% LLVM line coverage in each maintained native `.c` file. It retains function and branch counts separately in `build/python-native-coverage.json`. This gate is separate from Sonar's Python/Java/Node reports. The Python decoder, builder and gate also have focused tests. Coverage percentages alone do not qualify the complete language or untested allocation/platform failure paths.
