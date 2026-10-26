# Retained Go benchmark and downstream data

<!-- markdownlint-disable MD013 -->

The public benchmark tests run real stock/woven Go applications, but their temporary timing and receiver records are removed afterwards. The separate retained qualification runner preserves a named comparison and checks actual Prometheus samples through the provisioned Grafana datasource. This is a one-host benchmark with lifetimes off and tracing fixed at the common policy's configured sample ratio. It does not complete Go language admission, source closure, tracing/lifetime acceptance or visual dashboard inspection.

Use a clean otelc checkout containing this component and an independently reviewed oteleq capture-forward checkout at an exact commit. Original application sources remain unchanged. The runner requires at least 8 GiB free before any compiler or application command. Its input is `examples/benchmark-metrics.toml`, retaining all eight language IDs, the same workload, selectors, trace sampling and lifetime-off policy. The generated common policy changes only the capture endpoint and sets both metric flags initially off. The same generated file is used for compilation and both benchmark lanes.

```sh
make stack-up
make go-benchmark-evidence-check
python -m ci.qualify_go_benchmark \
  --oteleq-root /absolute/path/to/reviewed-oteleq \
  --oteleq-sha REVIEWED_40_CHARACTER_COMMIT \
  --comparison-id go-fresh-comparison \
  --output "$PWD/build/go-retained-fresh"
```

The Python command must use the environment containing the pinned `requirements.txt` and `requirements-dev.txt` packages. Start with `make setup` if this environment is not prepared. The output directory must be new, either external to both source repositories or under otelc's ignored `build` directory. Comparison IDs in this qualification profile use lower-case letters, digits, hyphens and underscores so that the actual Grafana regex selector is unambiguous. This restriction does not change the public benchmark CLI.

The runner uses the real `bin/quux-otelc` build and benchmark commands. It compiles once, runs the original stock binary, then measures all four metric states in the same instrumented process. The existing driver validates final SDK drain, ownership and source inputs before publishing. Additional checks join every raw response, timing array, control transition and application/supervisor resource window. Zero CPU deltas remain possible because `ps` has limited resolution; no synthetic SDK process or RSS is added.

The shared policy also includes module-relative Go filenames. Before timing starts, the runner requires the build to select exactly the original Go workload function. After shutdown, it joins the runtime's function/selection identities and exact workload invocation count, including the first response, warmup and all measured states. It derives the runtime and report policy identities independently from the same common policy using each producer's existing encoding. It independently decodes the captured OTLP entities and requires the final cumulative workload call and duration counts from collection-enabled batches. Missing probes, benchmark-driver-only metrics, partial rejection and incomplete workload telemetry fail qualification even when the five timing series reach Grafana.

The pinned oteleq helper captures and forwards original OTLP/HTTP request and response entities to the real Collector. Capture completeness is checked after the application and benchmark driver drain. Forwarding HTTP 200 alone does not establish downstream storage or trace completeness. The additional checker requires exactly five current-run Prometheus histogram-count series with the expected sample count. A reused comparison ID with another run becomes an extra or stale series and fails. It then reads the running Grafana dashboard, checks the mean-panel expression against the current provisioned source and evaluates that expression through Grafana's real Prometheus datasource. Each of its five means must match the retained ns/operation samples within the declared binary64 tolerance.

This uses the [Prometheus instant-query API](https://prometheus.io/docs/prometheus/latest/querying/api/) and [Grafana datasource proxy](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/data_source/). The current pinned Grafana supports that proxy. Stack probes use literal loopback HTTP, bypass environment proxies and refuse redirects. Backend query acceptance is separate from checking the dashboard's rendered appearance.

Only `evidence/` is intended for the qualification artifact. It contains the common policy, immutable source revisions and original file facts, compressed source snapshots, build/selection metadata, compressed application images, declared private source trees, fixed completed launch/health records, raw samples/responses, original wire capture and downstream API responses. Compiler/module/weaver caches and live `control.json` authentication descriptors are excluded. Copied files acquire new filesystem identities: original inode/UID/time fields remain historical observations and must not be repaired to match the copies.

`qualification.json` is written only after the data joins and final source rechecks pass. Failed runs retain diagnostic records without this successful qualification marker. The first build/measurement failure remains primary if driver cleanup, command evidence or source verification also fails. A forced driver stop is diagnostic failure, never an accepted timing run.

Open [the comparison dashboard](http://localhost:3001/d/otelc-comparison), select the fresh comparison ID and include the run in the time range. Fresh execution, independent artifact review and visual acceptance must be recorded before a run is called qualified; the existence of this command or passing mock tests is not that evidence. Cross-language comparisons require the equivalent applications to use the same generated policy and comparison ID on the same host. Lifetime observer cost needs a separately labelled lifetime-enabled comparison and remains distinct from this metrics-only run.
