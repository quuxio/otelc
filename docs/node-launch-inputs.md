# Node launch input evidence

<!-- markdownlint-disable MD013 -->

The source adapter retains candidate input evidence alongside each explicit-runtime launch. This records what the otelc loader/compiler read and returned. It is not an action witness, downstream receipt proof or protection from a hostile application that can write its own runtime directory. Independent consumer qualification and the complete prepared-launch contract remain open.

## Public recipe

Use the [public source launcher](node-source.md), selecting an original entry, project root, common policy and fresh private runtime directory. For example:

```sh
.venv/bin/python -m otelc --config /path/to/otelc.toml node \
  --language typescript --backend native \
  --root /path/to/project --entry /path/to/project/app.cts \
  --runtime /tmp/otelc-input-run -- 7
```

The Python producer selects a supported Node executable, pins argv to its resolved path, captures its content/version/module ABI/platform/architecture, generates its private plan/bootstrap and records the exact application argv, cwd and launcher PID. Bootstrap paths are absolute, including when the caller chooses a relative runtime directory. The source loader then reads original application files and records the bytes before transformation. The launcher waits for application exit and the separate SDK report before rechecking retained inputs. It returns failure for changed or unavailable evidence, preserving an existing non-zero application result. Direct callers of `otelc.node.command` must perform this final verification themselves; constructing argv is not successful qualification.

## Versioned files

The following records use `schema_version = 1`. Plain lanes use the `node-plain` prefix, so they can share a parent runtime directory with an instrumented lane without overwriting evidence. All files are producer-created with mode 0600 in the owned mode-0700 runtime directory.

| File | Meaning |
| --- | --- |
| `node-launch.json` | Fresh launch ID, language/backend/lane, original entry identity and nearest package context, Node identity, full installed adapter/npm tooling inventory, selected observer/HTTP product, resolved plan and its digest, generated file identities, argv/cwd/launcher PID and hashes of ambient `NODE_OPTIONS`/`NODE_PATH` values. |
| `node-sources.jsonl` | Synchronous read/emit journal. Each row keeps its physical origin; equal content at different paths/URLs does not merge identities. |
| `node-module-SHA256.txt` | Exact private compiler/hook output or source-map bytes, named by content digest. These are evidence files, not replacement application modules on disk. |
| `node-inputs.json` | Final `verified` or `failed` result. A verified instrumented result binds the launch manifest and final SDK report identities and records application/SDK PIDs. `action_delivery_verified` is always false. |

Input identities contain an absolute `path`, `resolved_path`, SHA-256 and byte `size`; producer symlink files also retain their link target. Read journals retain `requested_path` separately from the resolved physical origin. A configuration symlink retargeted to a different file with identical bytes still fails the final origin check. Producer directory symlinks and unreadable tooling trees are refused rather than omitted. Directory inventories include the complete installed files, including optional platform packages and any present development packages; they are not an assertion that every inventory file executed.

Journal kinds `source`, `compiler_config` and `package_context` bind the actual original bytes read. `absent_config` and `absent_package` retain absence where a later-created file could change configuration or module format. The TypeScript reader observes root and resolved `extends` configuration reads, including files outside the project root. Non-regular inputs are refused through non-blocking opens, so a substituted FIFO cannot hang before validation. An occupied final evidence filename is preserved and makes a successful application launch fail; an existing application failure remains the launcher result.

Kinds `compiled`, `instrumented` and `loaded` distinguish compiler emission, probe insertion and uninstrumented hook return. They retain original source hash/path, logical source name, original URL, format supplied by the compiler/loader (or `node-detected` when unspecified), output digest/size/file and optional source-map digest/file. The original file identity is separate from all emitted identities. A preceding loader that changes the original source bytes is refused. Known entry/package changes are refused before the application entry is loaded; final checks detect mutations during execution.

## Qualification boundary

Plain JavaScript runs Node directly, retaining entry/tool/runtime identities but no interposing source-load journal. Plain TypeScript records compiler/loader reads without installing probes or an SDK. Source observations currently cover the adapter's in-root JS/TS loading profile; outside-root imports, arbitrary custom loaders, native application addons, worker/VM contexts and general build-system inputs are not a complete closure. Package records describe the nearest package visible to our reader, not an independent observation of every Node resolver read. An unspecified loader format is reported rather than asserted as CommonJS or ESM. Runtime/compiler closure hashes do not substitute for an independent consumer validating the fixed allowed command/argv/bootstrap recipe.

The initial manifest can be large because it inventories the installed dependency files. This preparation and final verification occur outside application operation timing. They still contribute to overall launch/shutdown cost and must not be described as free. A verified input record says nothing about whether selected application actions were observed, whether spans were lost or whether the downstream Collector/Tempo received them. Check those independently using SDK accounting, decoded telemetry and oteleq qualification.
