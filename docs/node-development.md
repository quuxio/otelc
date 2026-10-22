# JavaScript and Microsoft TypeScript development

<!-- markdownlint-disable MD013 -->

This adapter is under development. Java remains the delivered rebuilt language. The source-free Node adapter, its command-line integration, HTTP propagation, prepared-launch provenance and benchmark/dashboard delivery are not complete. The previous implementation remains on the archive branch for reference.

The same schema-3 policy drives ordinary JavaScript and Microsoft TypeScript examples. Tests exercise the actual Microsoft 7.0.2 native compiler and Microsoft 6.0.3 compiler API, with generated probes and original module execution URLs. Application sources contain no telemetry imports, annotations or bootstrap. Generated plans and bootstrap are producer-owned files.

## Run the current checks

Requirements: Python dependencies from `make setup`, Node 24.11+, the selected Node distribution's exact headers, and Clang/LLVM tools. On this Mac, the qualified local runtimes are Node 24.21.0 and 26.11.0; LLVM 22 is installed through Homebrew. Broader platform qualification is still required.

```sh
make setup
make node-check
.venv/bin/python -m unittest discover -s tests -p 'test_node_*.py' -v
```

`make node-check` builds the public-V8 Promise observer, enforces its own 80% LLVM line-coverage floor, checks types with Microsoft TypeScript 7, and runs Node tests with an 80% source-component coverage floor. Ordinary application integration tests contribute actual loader/compiler/runtime coverage. The coverage build restores the ordinary binary before other checks or benchmarks. Both builds publish atomically so an existing mapped binary is preserved.

Select another installed runtime explicitly:

```sh
make node-check NODE=/opt/homebrew/opt/node/bin/node
```

The integration tests use the selected Node executable. `OTELC_NODE_INCLUDE` can identify its exact installed headers; a different patch version or ABI is refused. The tests use the repository's `.venv/bin/python`; `OTELC_TEST_PYTHON` can select another prepared environment.

## Runtime ownership and final evidence

Function probes and the official trace SDK retain application scopes in the application process. Primitive measurements and ended SDK spans cross a private framed FIFO. A separate process owns official SDK metric aggregation, span batching, OTLP transport, retries and final drain. It has its own globals and does not run application code. Trace identity generation uses cryptographic randomness, leaving application `Math.random` state unchanged.

No asynchronous SDK shutdown is scheduled in application `beforeExit`. Application exit listeners run before the final synchronous checkpoint. Actual FIFO EOF, rather than a synthetic JavaScript `exit` event or IPC disconnect, tells the SDK process to drain. This preserves unreferenced timers, intentional `beforeExit` work and explicit exit codes in the qualified fixtures.

The SDK process finishes after the application. A launcher/qualifier must retain both parent PID and `sdk_pid`, wait separately for `node-status.json`, and inspect application exit status and decoded downstream telemetry. Handoff acceptance does not mean HTTP success. SDK export success does not establish downstream receipt or absence of OTLP partial rejection; the report marks those as unavailable.

Each FIFO chunk fits the POSIX 512-byte atomic-write minimum. Each record is bounded to 16 MiB and a full pipe has a 50 ms handoff deadline. Backpressure therefore has measurable application cost. Rejected or incomplete records are counted as loss and invalidate a qualified benchmark. Capacity and additional SDK-process CPU/memory must be retained in future benchmark reports.

After a clean SDK shutdown, sampled spans received by the SDK process can be reconciled against exporter success counts. Queue loss remains visible even with metric collection/emission off. A failed or missing producer checkpoint, runtime/native failure, incomplete record or queue loss prevents a clean final report. Missing reports and unknown counts never become zero loss.

## Qualified and remaining boundaries

Current local integrations cover config-only JS/TS functions, exception identity, async Promise adoption, application AsyncLocalStorage, common task propagation, independent live metrics states, observed GC, synchronous resource release, source preservation and shutdown failure cases. The native observer reads public Promise state without handlers or result access; weak records are finalised at a V8 checkpoint before kept weak references clear. Tests check returned-object collection before flush, repeated subscriptions and worker cleanup.

Observed GC is distinct from destruction. Production instrumentation never forces GC. Fixture-only `--expose-gc` is used to test bounded observations; missing fixture collection is incomplete evidence, not proof of a leak. Resource observation currently requires an existing synchronous zero-argument void method. Failed/duplicate close and failed construction do not fabricate completions. Uncompleted lifetimes are retained as censored counts at process exit.

The source adapter still needs official HTTP integration, broader CJS/ESM/compiler semantic cases, public CLI/prepared manifests, equivalent benchmarks, Grafana proof, final review and required CI/Sonar gates. Browser execution, arbitrary VM contexts, inheritance, async/value-returning release methods, selected async generators and several source constructs remain unsupported. Do not present this development branch as a complete language release.
