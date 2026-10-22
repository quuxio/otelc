# Node launch input evidence

<!-- markdownlint-disable MD013 -->

The source adapter retains candidate input evidence alongside each explicit-runtime launch. This records the requested launch, its fixed generated recipe and what the otelc loader/compiler read and returned. It is not an action witness, downstream receipt proof or protection from a hostile application that can write its own runtime directory. Independent consumer qualification and complete preload/dependency closure remain open.

## Public recipe

Use the [public source launcher](node-source.md), selecting an original entry, project root, common policy and fresh private runtime directory. For example:

```sh
.venv/bin/python -m otelc --config /path/to/otelc.toml node \
  --language typescript --backend native \
  --root /path/to/project --entry /path/to/project/app.cts \
  --runtime /tmp/otelc-input-run -- 7
```

The Python producer selects a supported Node executable, pins argv to its resolved path, captures its content/version/module ABI/platform/architecture, generates its private plan/bootstrap and records the exact application argv, cwd and launcher PID. Bootstrap paths are absolute, including when the caller chooses a relative runtime directory. The original request keeps caller paths separately from their resolved origins, application arguments, common policy and selected compiler/lane. A policy loaded from TOML retains the exact stable read and text that produced the parsed policy; a programmatic `Policy.parse` request explicitly has no source file.

`otelc.node_recipe.verify(runtime, original_request, manifest, producer=approved_producer_root)` reconstructs the fixed argv, plan and bootstrap from original intent. It does not execute a command selected by evidence. A different entry, cwd, runtime, argument, compiler/lane, selection, generated file set or bootstrap fails even if its new content hash is internally consistent. Keep the original request independently when qualifying a provider; replacing both request and evidence with a different request is not proof of the original launch.

The source loader reads original application files and records the bytes before transformation. The launcher waits for application exit and the separate SDK report before rechecking retained inputs. It returns failure for changed or unavailable evidence, preserving an existing non-zero application result. Direct callers of `otelc.node.command` must perform this final verification themselves; constructing argv is not successful qualification.

## Versioned files

The launch and final input records use source evidence `schema_version = 3`. The separate request format is version 1. The common policy also remains schema 3; this is a distinct protocol. SDK status remains schema 1. Consumers must explicitly support source evidence version 3: version 1 lacked per-attempt/compiler binding, and version 2 lacked the original request, fixed recipe and startup receipt. Plain lanes use the `node-plain` prefix. All retained files are producer-created with mode 0600 in the owned mode-0700 runtime directory.

| File | Meaning |
| --- | --- |
| `node-request.json` | Original caller request, common policy and optional original TOML identity/text, requested/resolved paths, selected Node/compiler/lane, application arguments and private allowlisted environment records. |
| `node-launch.json` | Fresh launch ID, original request, language/backend/lane, entry/package context, Node identity, producer Python/adapter/npm source inventory, selected observer/HTTP product, resolved plan/digest, generated file identities, argv/cwd/launcher PID and the separate tooling environment. |
| `node-sources.jsonl` | Synchronous read/emit journal. Each row keeps its physical origin; equal content at different paths/URLs does not merge identities. |
| `node-module-SHA256.txt` | Exact private compiler/hook output or source-map bytes, named by content digest. These are evidence files, not replacement application modules on disk. |
| `node-inputs.json` | Final `verified` or `failed` result. A verified instrumented result binds the launch manifest and final SDK report identities and records application/SDK PIDs. `action_delivery_verified` is always false. |

Input identities contain an absolute `path`, `resolved_path`, SHA-256 and byte `size`; producer symlink files also retain their link target. Read journals retain `requested_path` separately from the resolved physical origin. A configuration symlink retargeted to a different file with identical bytes still fails the final origin check. Producer directory symlinks and unreadable tooling trees are refused rather than omitted. Directory inventories include the complete installed files, including optional platform packages and any present development packages; they are not an assertion that every inventory file executed.

The fixed environment allowlist is declared in `otelc/node_recipe.py` and mirrored by the startup reader. Each key has either `state = absent`, or `state = present` with its exact private value and SHA-256. Empty values are present; missing keys and wrong hashes fail validation. This is not a general environment dump. Keep these files private, since launch settings and TOML text can contain sensitive local information. Metadata probes remove `NODE_`, `OTEL_` and `OTELC_` variables while preserving OS settings. They therefore do not execute application preloads or apply application Node compile-cache settings during inspection. The real application keeps its inherited settings.

A first `startup` journal row records the actual source-loader PID, Node executable, cwd, application argv, Node exec argv and allowlisted environment before the application entry loads. Missing, duplicate, out-of-order or mismatched startup evidence fails final verification; its PID must agree with the independent SDK checkpoint. A changed generated plan is refused before installing probes. Plain JavaScript uses no loader, so it retains requested launch evidence without claiming an in-process startup receipt. Application preloads run before this observation boundary; their own behaviour, inputs and dependency closure need separate evidence.

Journal kinds `source`, `compiler_config` and `package_context` bind the actual original bytes read. `absent_config` and `absent_package` retain absence where a later-created file could change configuration or module format. The TypeScript reader observes root and resolved `extends` configuration reads, including files outside the project root. Non-regular inputs are refused through non-blocking opens, so a substituted FIFO cannot hang before validation. An occupied final evidence filename is preserved and makes a successful application launch fail; an existing application failure remains the launcher result.

Each `module_requested` starts a distinct `load_id`, retaining its original source path/URL, logical name and JavaScript/TypeScript kind. Kinds `compiled`, `instrumented` and `loaded` distinguish compiler emission, probe insertion and uninstrumented hook return. They retain that load identity, original source hash/path, logical source name, original URL, format supplied by the compiler/loader (or `node-detected` when unspecified), output digest/size/file and optional source-map digest/file. Repeated imports with distinct URLs or repeated attempts do not collapse. Every attempted in-scope load needs a completed hook result. An application that catches a compiler/probe refusal can continue its own work, but the CLI reports failed input verification instead of treating missing instrumentation as success.

TypeScript attempts also require a `compilation` record. Its `compiler` object identifies the fixed compiler path/version/backend, cwd, native argv or API method, transformer profile and content-addressed `files`. The `input`, `options`, raw `output` and raw `map` artifacts are separate from the original source and remapped/probed output. Native evidence retains the actual augmented input, generated config, raw emitted bytes and preparation map before deleting its private workspace. The generated input/config must remain byte-identical across the actual compiler call. CJS `.ts` inputs use a private `.cts` name so an unrelated private/ancestor package type cannot change the requested format. API evidence retains the exact source string and numeric compiler-option object passed to `transpileModule`; the pinned transformer implementation belongs to the adapter closure. The native preparation map binds the original source path and decoded text; UTF-8 file bytes and decoded input text have separate hashes. Final checks validate the fixed compiler recipe, every artifact and the raw-output-to-compiled-output link.

The original file identity is separate from all emitted identities. A preceding loader that changes the original source bytes is refused. Known entry/package changes are refused before the application entry is loaded; final checks detect mutations during execution.

## Qualification boundary

Plain JavaScript runs Node directly, retaining entry/tool/runtime identities but no interposing source-load journal. Plain TypeScript records compiler/loader reads without installing probes or an SDK. Source observations currently cover the adapter's in-root JS/TS loading profile; outside-root imports, ambient preload dependency closure, arbitrary custom loaders, native application addons, worker/VM contexts and general build-system inputs remain incomplete. Package records describe the nearest package visible to our reader, not an independent observation of every Node resolver read. An unspecified loader format is reported rather than asserted as CommonJS or ESM. The fixed recipe and startup receipt do not substitute for independent consumer validation.

Node interprets `NODE_OPTIONS` before explicit CLI options, and environment `--require` entries precede the producer's CLI bootstrap. The original option string is retained without changing or re-executing its application preloads during planning. See [Node's option-order rules](https://nodejs.org/api/cli.html#node_optionsoptions). The existing preload fixtures verify exactly-once application effects; complete resolved preload input identities and replay qualification are still required before claiming that broader closure.

The initial manifest can be large because it inventories the installed dependency files. This preparation and final verification occur outside application operation timing. They still contribute to overall launch/shutdown cost and must not be described as free. A verified input record says nothing about whether selected application actions were observed, whether spans were lost or whether the downstream Collector/Tempo received them. Check those independently using SDK accounting, decoded telemetry and oteleq qualification.
