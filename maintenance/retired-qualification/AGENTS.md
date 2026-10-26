# Qualification workflow

If `~/.agents/AGENTS.md` exists, read it and follow relevant shared guidance.

This directory is a local qualification workspace, not the implementation Git checkout. Follow [RETENTION.md](RETENTION.md).

- Store every new completed qualification run, successful or failed, through `make qualification` or `tools/qualification_record.py run`. Write generated evidence under `OTELC_QUALIFICATION_DIR`. Keep build caches and live work outside that directory.
- A completed record consists of a compressed ZIP and its small SHA-256 receipt. Run `qualification-verify` before accepting it. Do not retain a second expanded copy of a completed run.
- The runner must wait for all evidence writers and child processes before it exits. An interrupted runner or failed archive verification leaves its active evidence directory for recovery. Do not delete it until a valid archive exists and no live writer needs it.
- Use `pack` for existing terminal evidence. It verifies compressed payloads but deliberately preserves originals. Check live references and physical-file identity requirements before any separate retirement. Never rewrite historical manifests to conceal relocation or changed inode/ctime.
- Keep original downloaded ZIPs and other sealed inputs byte-for-byte inside the compressed record, or reuse their existing compressed copy by an explicit hash-bound reference. Do not unpack them permanently or discard a failed run.
- Verify and restore a record before a reader that requires paths. Restoring preserves bytes, mode and nanosecond mtime; it does not preserve inode, ctime, ownership, extended attributes or a previous live-process observation.
- Do not resume broader project implementation when the user has only authorised retention work.
- Reuse an existing checkout. Keep at most two normal development worktrees per implementation repository; additional frozen provider worktrees need a named consumer and a recorded reason. Do not create another worktree while that limit is exceeded.
- Retire a terminal development worktree after its source/branches and required evidence are retained, its consumers and open files are checked, and any required archive restoration passes. Historical source records do not by themselves require a permanent expanded checkout. Preserve current original-file identity contracts and record any retirement explicitly.
- Run `make retention-check` after terminal qualification work. Periodic maintenance follows the seven-day cache and fourteen-day expanded-copy policy in `RETENTION.md`; never delete by age or folder name alone.
- Admit only confirmed unused targets with a recorded reference-review reason. Preserve the pinned `work`, `toolchains` and `cache` trees. Do not remove or narrow a pin merely to make cleanup pass; changing a pin requires explicit review of all affected consumers.
- Every deletion run must use `make retention-clean ARCHIVE=...` so its results are compressed. Keep unsupported, changed, in-use and uniquely evidenced targets out of the cleanup queue. The maintenance schedule does not authorise resuming project implementation.
