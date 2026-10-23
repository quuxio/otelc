# Native Python input component

<!-- markdownlint-disable MD013 -->

This is the native entry and ordinary-loader building block used by the Python candidate's [prepared launcher](python-prepared-launch.md). It does not establish complete Python provenance or oteleq admission. Developers do not add imports, annotations, decorators or startup code to their application.

The component uses CPython's public [PyConfig and Py_RunMain APIs](https://docs.python.org/3.12/c-api/init_config.html), retaining native argument parsing, main frames, metadata, exception printing and shutdown. A platform-specific stdio boundary copies the main script's opened bytes into an anonymous real-FD snapshot. CPython consumes that exact snapshot under the original filename. The implementation does not replace a developer source file or construct a Python runner frame.

## Build and check

From the prepared repository environment:

```sh
make python-inputs
make python-inputs-check
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_input_journal test_python_input_build \
  test_python_native_inputs test_python_import_inputs test_python_input_coverage -v
make check
```

`python-inputs` prints the tool-owned driver path. Its directory contains `build.json`, the shared library and the driver. The build identity binds the exact selected interpreter, Python library/config/header files, compiler, maintained native sources and flags. Products are published in a content-addressed directory; altered or partial products are refused on reuse. Profiling builds use a separate namespace. Clang and matching LLVM coverage tools are required for the native coverage gate; `CC` and `OTELC_LLVM_BIN` select installed tools.

These are component commands. The public `otelc python` launcher uses the fixed [prepare/command recipe and inherited-descriptor supervisor](python-prepared-launch.md). Selected function starts now retain native code identities; source-to-code attribution and the immutable provider index still need integration. Do not launch the private driver from a command copied out of a receipt.

## Evidence and failure boundary

The private native journal retains exact filesystem path bytes, actual application PID, original opened-file device/inode/size/mode/timestamp and consumed script bytes. A separate record kind retains complete ordinary-loader reads, including full `.pyc` headers and payloads, even when Python rejects that cache and reads source instead. Entry records and loader reads stay separate; final counts cover both. A loader read does not prove that the file was compiled or executed. Each record has ordered framing. Final counters distinguish source capture failure, journal I/O failure, interpreter finalization and whether a native entry was expected. The supervisor must separately retain the actual process exit. A successful entry receipt is not evidence of a successful application, SDK drain or downstream delivery.

Limits are 16 MiB per source, 64 MiB per journal, 4096 total records and 65535 bytes per path. Source admission reserves room for the final counters. Truncated, reordered, foreign-PID and inconsistent journals are refused. A limit or capture failure preserves native execution through the original input and cannot report complete entry evidence. A closed/reused journal descriptor cannot redirect writes into, or close, an unrelated application file. Interpreter `_exit` without finalization remains incomplete.

Raw C journal writes hold a private lock without calling Python, allocating Python objects, formatting application values or acquiring SDK/runtime locks. The stdio boundary checks the native main thread and the absence of an active Python frame before consuming its single capture arm. It does not treat an application-side open of the same path as the native entry read. Snapshot success is recorded only after the complete snapshot is ready.

Ordinary imports use CPython's public [open-code hook](https://docs.python.org/3.12/c-api/file.html#c.PyFile_SetOpenCodeHook). The observer pins the original frozen `FileLoader.get_data` code object before application work and wraps only normal builtin `_io.open` files for that loader. The wrapper delegates the normal context manager and read, records the immutable bytes returned by that same read, then returns those bytes directly. No pre-read, byte-buffer copy or rewind is added. Explicit application `io.open_code` calls keep their normal real file type, name, descriptor, seek and read behaviour. Filename text cannot impersonate the pinned code object. An application override of `_io.open` passes through unchanged and records capture loss for loader reads; no extra file methods run on its wrapper.

This component covers ordinary script entries and ordinary `SourceFileLoader`/`SourcelessFileLoader` file reads on shared-library/framework GIL CPython 3.12+ on macOS/Linux. Module entry can now retain loader reads, but still has no native script entry and does not claim complete provenance. Captures preserve full stale unchecked-hash bytecode and invalid-header/source-fallback reads. Builtin/frozen code, ZIP/custom loaders, compile/exec/AST inputs, whole-program executed-code coverage and dependency closure remain separate work. `source_complete=false` remains explicit in the public launcher.

The process-wide open-code hook is exclusively owned by this candidate and cannot be replaced through CPython's setter. Tools that require their own open-code hook are outside this profile.

Source snapshots can change concurrent mutation timing and loader-internal debugger observations; this is not universal debugger/custom-loader compatibility. Explicit sources, original/resolved requests, stable toolchain inputs and post-drain rechecks remain required. Free-threaded/static interpreters, forks/subinterpreters and arbitrary native interposers remain unqualified. The build index binds explicit Python inputs; the complete compiler/system-header and loaded-library closure remains provider work. Path hashes alone must not stand in for bytes consumed by an interpreter.

## Selected code descriptors

For selected function starts, the native builtin binds the original monitoring callback and verifies the live application frame, exact code object, instruction offset and `RESUME` opcode. Calling a callback directly or raising a synthetic audit event cannot invent that observation. Each first observation retains a version-bound descriptor and assigns a positive process-local `otelc.python.code_id`. Function spans and metrics carry this ID; `python-execution.json` indexes it by path bytes, payload hash, first observed offset, CPython version and descriptor version. Plain runs do not enable monitoring or collect these descriptors. Lifetime-only observations do not yet carry code IDs.

Descriptor version 2 is an owned ASCII JSON graph with the application's filesystem encoding/error mode, retaining bytecode, names, closure variables, line/exception tables, argument/stack/flag metadata, immutable constants and graph aliases. Integer bytes and float/complex bits are retained without application formatting. It does not serialise closure cell contents or execute a retained code object. The native boundary refuses mutable/custom constants, then checks a 16 MiB allocation/payload bound, 65,536 traversed nodes, depth 128 and 4,096 live code identities. Exceeding a capture bound preserves the original call result and marks the final receipt incomplete. Empty, unencodable, oversized or NUL-containing code filenames are refused as capture loss; surrogateescaped path bytes remain supported. Weak code caches do not extend code or constant lifetime. Concurrent starts can share a pending ID; descriptors may finish in a different order. A failed descriptor prevents a complete final receipt even if another start already used that pending ID.

The encoder uses the selected CPython `_json` C encoder and immutable builtin operations on an owned primitive graph. It adds no `marshal.dumps` or `builtins.id` audit calls. It runs before raw journal writes; no Python encoder or application callback runs inside the private writer lock. The runtime pins the encoder's code and keyword-default identities. This is a qualified component boundary, not general compatibility with tools that modify monitoring callbacks, the interpreter or producer internals.

The supervisor independently validates version-two descriptors after app/SDK completion. The separate reader never calls the application encoder, constructs `CodeType`, unmarshals bytes, compiles a target or imports a target module. It checks exact keys and primitive types, duplicate JSON keys, metadata widths, consecutive node IDs, valid references, code-field shapes, reachability, cycles and depth, and the entry offset's byte range. It returns immutable typed data while preserving reference aliases and exact integer/float/complex bytes. Its limits are 65,536 nodes, 262,144 edges, depth 128 and the enclosing 16 MiB payload bound. A malformed descriptor fails final evidence while preserving the actual application exit and retained journal.

Filename bytes are checked with the codec/error mode recorded in the application, rather than the isolated tooling process's defaults. Codec lookup uses the tooling interpreter's standard codec registry; custom application codecs and foreign-platform codecs are not qualified. The reader preserves CPython/descriptor version, path and offset with its result. It validates data format, not VM instruction semantics or source origin. Legacy version-one records can still be retained by the journal reader as opaque data, but cannot gain version-two validation retrospectively. These records bind an actual selected monitoring start, not a compilation claim: mapping a descriptor back to a particular source/pyc read, unselected code, native dependencies, whole-program closure and independent oteleq consumer admission remain outstanding. `source_complete=false` stays explicit.

## Validation scope

Native tests compare raw stdout/stderr, exits, encodings, BOM/CRLF, error printing, main-frame identity, relative/Unicode/symlink spelling, flags and entry metadata. They retain actual consumed bytes even when the running app later rewrites its own file. Other cases cover size/count bounds, invalid launch descriptors/options, actual failed I/O, FD reuse and abrupt exit. The C writer fixture tests protocol/status boundaries. A separate native fault fixture checks read errors with context-manager cleanup errors, invalid metadata/path encoding and wrapper allocation failure. Its synthetic/error records are component tests, not application provenance.

The native gate profiles real driver/library execution and requires at least 80% LLVM line coverage in each maintained native `.c` file. It retains function and branch counts separately in `build/python-native-coverage.json`. This gate is separate from Sonar's Python/Java/Node reports. The Python decoder, builder and gate also have focused tests. Coverage percentages alone do not qualify the complete language or untested allocation/platform failure paths.
