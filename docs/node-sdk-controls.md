# Node SDK pipeline and live controls

<!-- markdownlint-disable MD013 -->

This delivered foundation supplies the official SDK pipeline and live controls for the TypeScript/JavaScript rebuild. Source-free application launching, function/context/lifetime probes and compiler integration belong to the separate [Node source candidate](node-source.md), not this SDK component. The CLI keeps delivered languages separate from candidates while final benchmark and independent oteleq qualification remain open.

## Delivered building block

`OwnedSdkPipeline` consumes the relevant fields from the already validated common schema-3 policy. It uses the official JavaScript metric and trace SDKs and OTLP/HTTP protobuf exporters. The new code supplies live metric gates, exporter accounting and private local controls; it does not replace aggregation, sampling, queues, protobuf encoding or HTTP retries.

The public metric API wrapper gates counters, up-down counters, histograms, gauges and observable callbacks, including instruments cached before startup enable. Batch observables retain SDK registration/removal rules and instrument identities across meter scopes. A callback that crosses a collection transition cannot add a late observation to the next generation. Observations admitted before a transition retain normal cumulative SDK semantics.

Emission-off admits no new metric exports. Status reports in-flight requests; `effective_emission_off` is true only after those requests settle. Collection-off also prevents a cached meter snapshot from being emitted when emission alone is on. Cumulative measurements collected with emission off remain available when collection and emission are both enabled again. Traces have their launch-time sampler and exporter and remain independent of all metric switches.

SDK `forceFlush()` completion alone is insufficient: the periodic metric reader can absorb a collection or export failure. The pipeline also checks actual SDK collection results and exporter failures before reporting a successful flush or shutdown. Each signal has its own flush barrier. While one signal finishes, the other can start a new periodic export; status continues to report that pending work without treating it as a failed flush. Flush success is not a globally idle or emission-off barrier. Shutdown stops the producers and additionally requires pending operations to settle. These are producer acknowledgements, **not** proof of downstream receipt, queue-loss absence or per-invocation attribution. Shutdown bounds the caller's wait and records failure on timeout; SDK work can continue after a timed-out SDK operation. No completed drain is claimed in that case. SDK maintenance can still run while collection is off; the gates suppress instrument recording and application observable callbacks, not every SDK operation.

The pinned periodic reader requires its collection timeout not to exceed its interval. The adapter uses the smaller common interval/timeout as the reader budget, reports it as `reader_timeout_ms`, and retains the configured timeout on the HTTP exporters. This translates the SDK constraint without adding tool-specific user configuration.

## Shared local controls

The Node component publishes an authenticated IPv4 loopback endpoint in an owned mode-0600 `control.json` inside an owned mode-0700 directory. Publication is atomic and refuses existing files. The common Python control client accepts this descriptor as well as Java's Unix socket and retains the same commands: `status`, `on`, `off`, `collection on/off`, `emission on/off` and `flush`. The client opens the fixed descriptor basename relative to a verified private directory, refuses symlinks and non-regular replacements, and uses a literal IPv4 loopback destination. The descriptor binds the endpoint to the launched PID; authentication material is never included in status or diagnostics. The control directory must be kept private.

Node's Unix server implementation unlinks its bound path on close, including a replacement file. The descriptor transport avoids that behaviour. The listener is exclusive to its process and bounds concurrent peers to 32. Shutdown destroys idle control connections and removes only the unchanged descriptor it published; replaced or edited files remain intact. Malformed, oversized, incomplete or unauthenticated requests cannot change either metric flag. This does not yet provide the separate runtime-health or action-witness protocols.

## Verification

Use Node 24.11+; the default Makefile selects installed Homebrew Node 24 locally and the selected CI Node on GitHub. Override `NODE` with a full supported executable path when necessary. Microsoft TypeScript 7.0.2 checks the producer's TypeScript; this is not a claim that application compiler integration is finished. Versions are pinned in the root package and lockfile, with no lifecycle scripts required.

```sh
make setup
make node-check
make check
```

`make node-check` uses Node's built-in test runner and enforces at least 80% component line coverage, retaining LCOV for Sonar. `make check` also runs the Python integration fixture: the common control client drives a real Node process through all four live metric states and independently decodes protobuf metric/span requests. Cached counters, observable callbacks, metric failure/recovery, trace continuity, endpoint authentication and idle-peer cleanup have focused tests. The SDK fixture contains explicit API calls because it tests this producer boundary; it is not presented as a developer application or a source-free instrumentation example.
