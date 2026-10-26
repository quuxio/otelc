# Qualification data retention

Keep the smallest set that can independently support each current qualification claim. Build output is not qualification evidence by default.

## Keep

- One source archive per exact qualified or reproduced-failure commit, with its tracked-file hash manifest and parent/base identity. Reuse that archive across runs of the same commit.
- Policy, toolchain/compiler/runtime and provider identities; commands, environment profile and original input identities.
- Raw application outcomes, decoded and original telemetry, process/control receipts, run manifests and complete loss/health evidence required by the verifier.
- The latest accepted corpus per supported profile and the smallest actual reproductions of unresolved or fixed defects. Retire superseded large corpora only through an explicit record describing which claims can still be verified.
- Final coverage summaries and the raw inputs needed to reproduce their counts. Inactive historical raw profiles may be stored losslessly with an original-path/content-hash index and tested restore command.
- Reviews, required-check results, delivery receipts, failures and explicit limitations. A later passing run does not erase an earlier failure.

## Remove after a run is terminal

Compiler object/dependency/incremental directories, build caches, temporary links and generated executables that are not explicitly required as retained application/provider inputs. Do not copy these into a directory named archive or cleanup. Keep an existing unique qualified binary if a consumer binds its bytes and cannot regenerate it independently.

Never clean an active worktree's build output, its shared dependencies, a referenced frozen provider, uncommitted source, or a live container volume. Confirm tracked-file boundaries and open references first. Record paths and actual available-space changes; `du` totals and APFS copy-on-write extents do not equal unique reclaimable bytes.

## Reduce duplicated evidence

Hash-identical, inactive output files may share APFS copy-on-write storage only if the consumer binds bytes rather than inode/ctime. Preserve original paths, bytes, mode and mtime, verify all content hashes before/after, and test that later writes remain isolated. Do not use writable hardlinks.

Where compression changes a file's normal path, supply an explicit archive/restore index. Preserve original-content hashes and required metadata. Verify every archived payload before deleting an original and test restoration. Do not silently change a live consumer's input contract or rewrite a sealed manifest to pretend an old run produced a new format.

For the 23 October historical coverage compaction, use:

```sh
/opt/homebrew/opt/python@3.12/bin/python3.12 \
  /Users/sclarke/github/otelc-qualification/2026-10-23/disk-investigation/compact-coverage.py \
  --restore /Users/sclarke/github/otelc-qualification/2026-10-23/disk-investigation/coverage-archives/INDEX.json
```

The restore command uses only the Python standard library (Python 3.11 or newer); no project virtual environment is required. Each index names its original target directory, archive hash, original file hashes and metadata. Restore refuses conflicting existing files and symlink paths. Restoring needs space for the uncompressed profiles. The files remain historical coverage evidence; deleted compiler products must be rebuilt before rerunning a compiler/coverage workflow.

## Worktree limits

Reuse existing source checkouts. The limit is two normal development worktrees per implementation repository, including its canonical checkout. A frozen provider exception must name its consumer and explain why Git objects or a compressed source record are insufficient. No new worktrees may be created while this limit is exceeded. Keep shared Git common stores and branches; retirement of a directory does not authorise deleting its commits.

Review source changes, ignored outputs, current consumers and open files before retirement. Preserve the complete required contents in a verified compressed record and test restoration before removing an old mixed tree. Keep a receipt at the evidence location identifying the original path, commit, archive and restoration limits. Retaining a historical source manifest does not require keeping every old development checkout expanded indefinitely; an active original-file identity contract remains protected until its reader is migrated explicitly.

## New runs

Start from `git archive` or tracked source, not a recursive copy of a dirty working directory. Keep reproducible compiler caches separate from retained evidence and reuse them only with compatible toolchain/profile keys. Give each run its own small evidence directory. Check free space before a large corpus and stop producing new large data when the budget would be exceeded. Do not make another full snapshot merely to collect a status update.

## Compressed record workflow

Every new completed run must be retained as a compressed record. This applies to successful runs, reproduced failures, reviews, logs, telemetry, manifests, source bundles and any retained compiler/runtime inputs. The only uncompressed completion file is the small archive receipt. Use the Python standard-library runner (Python 3.11 or newer), directly or through Make:

```sh
cd /Users/sclarke/github/otelc-qualification
make qualification ARCHIVE=2026-10-26/example.qualification.zip \
  COMMAND='python3 -B /absolute/path/to/qualification_driver.py'
```

The driver must write its evidence under the injected `OTELC_QUALIFICATION_DIR` environment variable and wait for every child and evidence writer. The names `stdout.log`, `stderr.log` and `command.json` are reserved for the wrapper, which captures output, command, current directory, times and exit status. It does not record the environment. Keep secrets out of arguments and logs. Build caches and shared/live source must remain outside the evidence directory. Downloads that are already compressed can remain unchanged inside the record, or be referenced by exact archive hash from another retained compressed record.

Both success and failure are compressed with DEFLATE. Every file is read back from the ZIP and checked against its original SHA-256 and size before the wrapper removes its own temporary directory. Direct command use preserves the command's exit status; Make reports a failed target for any nonzero result. Interruption, unsupported links/special files, a changing source or an archive-write/verification failure leaves the expanded directory for recovery. No completion claim is valid without a verified archive and receipt. There must be enough free space for the active evidence and its compressed archive at the same time; this is not a streaming collector or a disk quota.

Verify and restore explicitly:

```sh
make qualification-verify ARCHIVE=2026-10-26/example.qualification.zip
make qualification-restore ARCHIVE=2026-10-26/example.qualification.zip \
  DESTINATION=/private/tmp/otelc-example-restored
make check
```

Restore refuses an existing destination and verifies all archive members before creating it. It restores file contents, empty directories, modes and nanosecond modification times. It cannot reproduce inode, ctime, ownership, extended attributes or historical process state. Readers which bind those facts must retain their original active inputs; do not substitute a restoration and claim the old physical identity is still verified. Treat receipts as integrity records, not signatures proving who produced an archive. Restore only trusted records into a private location.

For an existing terminal record:

```sh
make qualification-pack SOURCE=/absolute/path/to/completed-record \
  ARCHIVE=/absolute/path/outside-that-record/completed.qualification.zip
```

Packing preserves the existing record and never overwrites an archive or receipt. This is the first step of a migration, not automatic permission to delete old files. Before retiring an old expanded record, verify restoration, check its live readers and physical-identity contracts, and record the retirement separately. Do not archive the entire workspace, copy build caches into evidence, or recompress historical directories still used by active cross-project reviews. Existing compressed coverage archives remain valid. Historical uncompressed records have not been silently converted by this workflow change.

## Periodic maintenance

Run a daily maintenance check at 09:00 Europe/London and `make retention-check` after terminal qualification work. Maintenance may inspect, classify, compress and remove eligible qualification data; it must not resume paused instrumentation implementation. Notify only when space is reclaimed, a material storage risk appears, cleanup fails or a decision is needed. Stay quiet when nothing changes.

The small operational registry `qualification-retention.json` is separate from compressed evidence. It holds retention pins and explicit retirement admissions. It starts with no admitted targets; none of the mixed historical folders are silently approved. The entire `work`, `toolchains` and `cache` trees start pinned. A reference review must cover SDK and oteleq consumers, frozen source facts, retained compiler inputs, coverage maps, unresolved defects, unmerged work and live processes. A directory name or an old timestamp is insufficient evidence.

After that review confirms an unused disposable cache, register the exact leaf directory and record the review reason:

```sh
python3 -B tools/qualification_cleanup.py register \
  2026-10-26/example/build-cache --kind cache --confirmed-unused \
  --reason 'Terminal run; no retained compiler inputs or live/shared readers; review ARCHIVE#record'
```

Only recognised cache leaf names are accepted. A whole `build`, source tree, module cache, historical corpus or compressed archive cannot be registered as a disposable cache. A `go-cache/build` leaf still needs the compiler-input reference review; registration is not an automated proof that it is disposable. Do not narrow a pin just to pass admission.

An expanded record can instead be admitted with `--kind expanded --archive /absolute/path/to/verified.zip`. Its archive must describe the exact original root, full member set, bytes, modes and modification times, and must remain outside every retirement candidate. The archive and its receipt are kept. A verified restoration and review of path/inode-dependent consumers must precede admission.

Disposable caches wait at least seven days after admission and last modification; expanded copies wait fourteen days. Any later membership, inode, mode, size, mtime or ctime change blocks deletion and requires a new review. Pins and archive integrity are checked again at cleanup. The macOS `lsof` command must confirm there are no open files or working directories inside an eligible target. Failed, timed-out or incomplete inspection blocks deletion. Stop all writers before admission and keep registered targets out of subsequent runs; this is maintenance of trusted local work, not protection against a hostile concurrent filesystem writer.

```sh
make retention-check
make retention-clean ARCHIVE=2026-10-26/retention-cleanup-UNIQUE.zip
```

Cleanup uses a nonblocking lock and atomic registry writes. Dry runs do not delete. Apply runs report every waiting, eligible, blocked or removed target, plus available space before and after; the wrapper compresses that audit. APFS sharing and concurrent projects prevent treating file totals or free-space changes as exclusive reclaimed bytes. A partially failed deletion is reported and its admission is kept for investigation. Unique evidence and compressed archives never expire automatically. Discovery and admission remain explicit reviewed steps; the cleaner does not infer that arbitrary historical data is stale.

For a direct user request to clean up immediately, `make retention-clean` also accepts an explicit `RETENTION_COMMAND` for a reviewed, bounded one-off driver. Do not backdate periodic admissions or alter the scheduled waiting periods. An immediate archive retirement must preserve all target bytes in a verified archive, test full restoration, confirm terminal state and no live/shared readers, recheck unchanged original identities and record the exact retired paths. Keep archives and recovery instructions outside the retired targets. This does not authorise deleting unique evidence or changing pins. The periodic schedule continues to use the default guarded cleaner.
