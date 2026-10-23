# Measure object and resource lifetimes

<!-- markdownlint-disable MD013 -->

Select object types in the same policy used for function instrumentation. The application needs no telemetry imports, annotations or probes. The current rebuild implements the internal Java example; the remaining adapters follow the [delivery plan](rebuild-plan.md).

```toml
[lifetimes]
enabled = true
include = ["*WorkItem", "*work_item"]
exclude = []
release_methods = ["close"]
max_live = 1000000
```

`include` and `exclude` match resolved type names. Exclusions win. Source selection also applies. `max_live` bounds observer metadata, not the application's object population. A matching existing release method supplies the resource boundary; otelc never calls it. Enabling observation is a launch-time choice. Metric collection and emission can then change live through the normal controls.

## Boundaries and language support

| Boundary | Measurement | Current rebuild |
| --- | --- | --- |
| `observed_gc` | Successful construction to weak-reference collection notification | Java final concrete classes with one constructor, in the example's application classloader |
| `resource` | Successful construction to the first normal return from a selected instance release method | Java zero-argument synchronous `void` methods, such as existing `close()` |
| Destruction | Complete object construction to destruction, including exception unwinding | Planned C++/LLVM adapter |
| Drop | Completed construction to ownership-driven drop | Planned Rust adapter |
| Allocation/free | Selected allocation to matching free or resource release | Planned C/LLVM adapter |
| Other managed runtimes | Observed GC where the runtime permits it, plus explicit resource release | [Node](node-source.md) and [Python](python-native.md) candidates with declared source/runtime limits; Go planned |

GC notification has no exact destruction-time guarantee. A long observed interval can include time waiting for the collector and observer. It must not be compared as an equivalent of C++ destruction or Rust drop. See the [JDK reference queue contract](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/lang/ref/ReferenceQueue.html).

## Run and inspect

```sh
make setup java-build stack-up
bin/quux-otelc example java
```

Enter `10`, then `gc`. The first command allocates and closes ten ordinary `WorkItem` objects. The fixture's `gc` command requests GC for demonstration; production instrumentation does not force it. Enter `lifecycle`, then `gc`, to exercise a failed constructor, a failed close followed by retry, duplicate close and an abandoned object. Enter `quit` to exit.

Open [Grafana object lifetimes](http://localhost:3001/d/otelc-lifetimes). These metrics use seconds for duration and retain separate `type` and `boundary` labels:

| Metric | Meaning |
| --- | --- |
| `otelc.lifetime.admitted` | Observations started while collection was enabled |
| `otelc.lifetime.completed` | Observed boundary completions |
| `otelc.lifetime.duration` | Duration histogram for completed observations |
| `otelc.lifetime.live` | Last observed tracked objects and resources not yet released |
| `otelc.lifetime.incomplete` | Collected objects for which a selected resource release was not observed |
| `otelc.lifetime.lost` | New observations rejected at capacity |

With tracing enabled, each observed completion can also produce a sampled lifetime span. It has an explicit start/end time, the same boundary/type labels and, when available, a link to the active span at construction. It is a separate trace, so a long lifetime does not extend an already-completed request. Use Grafana Explore with the Tempo datasource to inspect traces.

Metric switches do not disable those spans. Objects created while metric collection is off do not become retroactive metric observations after collection is enabled. Completions while collection is off are suppressed by the SDK. Counts across an off interval therefore do not form a complete lifecycle census.

## Evidence and limits

`make check` launches the unchanged application under the official Java agent and decodes actual OTLP metrics and spans. It checks successful construction, failed construction, failed and duplicate release, missing release, observed GC, non-negative span durations, creation-span links and metric controls in one PID. Unit tests also check weak identity handling without application `hashCode`/`equals`, bounded capacity and incomplete resources.

The observer must not retain application objects strongly or change application cleanup. A missing release observation is evidence about the selected boundary, not proof of a leak. The last gauge value from a terminated process is historical. Objects still tracked at shutdown currently have no final duration or dedicated shutdown-censor counter. Arbitrary classloaders, inheritance and delegating constructors remain unsupported and are not claimed as qualified.

For overhead measurements, retain the plain baseline and all four live metric states with `make benchmark`. Tracing and lifecycle tracking can still have a cost when metrics are off. To isolate the lifecycle observer's added cost, run a second full benchmark with `lifetimes.enabled = false` and compare only matching runtime, workload, trace settings and live states. The driver retains this flag in every report and exported measurement.
