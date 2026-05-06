<goal>
Build a standalone Codex Desktop performance workaround from PLAN.md.

The deliverables are:
- `scripts/fix-codex-perf.py`, a cross-platform Python 3.10+ standard-library-only repair, backup, restore, and measurement tool for Codex thread metadata.
- A standalone CDP wrapper renderer patch, or equivalent standalone app-process patch, that improves large-thread loading without user clicks and without deleting chat history.
- A safe autonomous live-repair/self-relaunch strategy for the case where the current Codex agent cannot mutate `~/.codex` while Codex is running.
- Before/after measurement artifacts proving title repair, reconciliation durability, restoreability, and large-thread UX improvement.
</goal>

<context>
Start by reading:
- `PLAN.md`
- existing repository files with `rg --files`
- any generated CDP wrapper or script conventions if they are added during the work

Key PLAN.md facts to preserve:
- The bug is title metadata bloat, not `first_user_message` itself.
- SQLite-only repair is temporary because reconciliation can rebuild title metadata from JSONL rollout files and `session_index.jsonl`.
- The repair must update both SQLite and append later `ThreadNameUpdated` events to affected rollout JSONL histories.
- Existing rollout JSONL lines must not be rewritten.
- `first_user_message` and chat history must not be removed, truncated, or archived.
- Large-thread loading remains slow after title repair because renderer hydration/reconciliation/allocation dominates the tested path.

Useful discovery commands:
- `git status --short --branch`
- `rg --files`
- `rg "summarize_for_label|ThreadNameUpdated|session_index|state_5.sqlite" -n . ~/.codex 2>/dev/null`
- `python scripts/fix-codex-perf.py --help`
- `python scripts/fix-codex-perf.py measure --phase all`
- `codex --help`
- `codex resume --help`
- `pgrep -afil codex` on macOS/Linux, with fallbacks implemented in the tool
</context>

<constraints>
Implement this as a standalone local workaround, not as an upstream Codex patch.

Use Python 3.10+ standard library only if feasible. The repair tool should rely on modules such as `sqlite3`, `json`, `pathlib`, `shutil`, `datetime`, `platform`, `subprocess`, `tempfile`, `argparse`, `hashlib`, `signal`, and `time`.

Default Codex home is `~/.codex` on all platforms unless `--codex-home` is supplied. Do not hard-code machine-specific paths such as `/Volumes`, `~/Library`, `%APPDATA%`, or Linux-only locations.

Every mutating command must create a timestamped backup before mutation. Mutations must stop before making any change if SQLite integrity, `session_index.jsonl` parsing, or affected rollout JSONL parsing fails.

Do not mutate SQLite or JSONL while Codex Desktop, Codex CLI, or Codex app-server is running unless the user explicitly allows the tool to terminate matching Codex processes. The prompt default must be `N`, and pressing Enter must exit non-zero before mutation.

Do not solve the live-repair problem by weakening the process-safety rule or by requiring a human handoff. The goal must run end-to-end without questions and without asking the user to run follow-up commands.

If a temporary custom `--codex-home` is used for implementation or verification, it must start as a faithful copy of the current `~/.codex`, including `state_5.sqlite`, SQLite sidecars when present, `session_index.jsonl`, rollout JSONL files, relevant config, and any CDP wrapper renderer patch files needed to reproduce current behavior. It may then be isolated and mutated for tests. Synthetic fixtures can still be added for edge-case coverage, but they cannot replace the copied-current-state verification path.

If the current agent is running inside a Codex process that must be terminated for real `~/.codex` repair, design and prove an autonomous detached runner before live mutation. The runner must record the current session/goal resume command if the installed Codex supports it, schedule the repair after the current Codex process exits, and then relaunch/resume the session automatically. If non-interactive resume is not supported, the runner must continue autonomously by writing complete logs/artifacts and launching a new Codex process or equivalent continuation path that can finish verification without user input. It must not degrade into a human-run handoff.

A cron, launchd, `nohup`, `at`, background shell supervisor, or equivalent one-shot runner is acceptable only if it is explicit, auditable, self-cleaning, and logs command, PID, timestamps, exit status, backup path, and resume attempt. It must be tested against the copied-current-state custom Codex home before it is allowed to operate on real `~/.codex`.

The repair command must append JSONL title events rather than rewriting history. It must be idempotent and must not append duplicate title events on a second run.

The Python title-generation function must be a literal semantic port of the Rust `summarize_for_label` and `truncate` behavior described in PLAN.md: first line only, trim leading/trailing whitespace, truncate to 120 characters using 117 characters plus `...`, and do not collapse internal whitespace.

The CDP wrapper renderer patch must never mutate `~/.codex/state_5.sqlite`, rollout JSONL, or `session_index.jsonl`. State repair belongs only in the backup-first Python maintenance tool.

The UX workaround must require no extra click to load older turns. Older history must load automatically in the background, and disabling the renderer patch must restore stock behavior.

The renderer patch must fail open on unknown Codex builds, selectors, or event shapes. Include a storage kill switch and a `stop()` cleanup path for listeners, timers, styles, and patches.

Do not widen scope into general Codex cleanup, archive active chats, delete history, or implement unrelated app features.
</constraints>

<done_when>
The work is complete only when all of the following are true:

`python scripts/fix-codex-perf.py backup` creates a timestamped backup package without mutating SQLite, `session_index.jsonl`, or rollout JSONL file hashes.

`python scripts/fix-codex-perf.py repair` automatically creates a timestamped backup before any mutation.

`python scripts/fix-codex-perf.py repair` detects running Codex Desktop, Codex CLI, and Codex app-server processes, prints matching PID/name/command-line rows, prompts `Kill all running Codex instances? [y/N]`, and exits non-zero before mutation when Enter is pressed.

`python scripts/fix-codex-perf.py repair -y` kills only matching Codex processes, records killed PIDs/commands in the manifest, and continues.

`python scripts/fix-codex-perf.py restore --backup <path>` restores `state_5.sqlite`, SQLite sidecars, `session_index.jsonl`, and affected rollout JSONL files from the selected backup, validates restored hashes, runs SQLite integrity check, and prints a concise restore summary.

Process detection has fixture tests for macOS `pgrep`/`ps`, Linux `pgrep`/`ps`, and Windows PowerShell/tasklist output, including negative fixtures for unrelated processes that merely contain `codex` in a path.

The script runs on macOS, Windows, and Linux with only documented dependencies.

`repair` creates a timestamped backup directory containing `state_5.sqlite`, SQLite sidecars when present, `session_index.jsonl`, every affected rollout JSONL file, metrics files, and a manifest.

The backup manifest records tool version, command, platform, Codex home, timestamp, killed Codex processes if any, SQLite integrity result before changes, affected thread ids, affected file paths, old/new title lengths, whether `title = first_user_message`, appended JSONL event ids/timestamps, hashes for every backed-up file, and restore instructions.

`repair` validates SQLite with `PRAGMA integrity_check` before and after mutation.

`repair` validates `session_index.jsonl` and every affected rollout JSONL file before appending any title event.

`repair` appends later title events instead of rewriting old rollout lines.

`repair` updates SQLite title values and appends or updates `session_index.jsonl` so the lightweight session index agrees with rollout history.

`repair` uses Rust-parity `summarize_for_label` semantics with tests for empty string, whitespace trim, multiline input, CRLF behavior, internal `\r`, 120-character unchanged title, 121-character truncation to 117 plus `...`, and Unicode character truncation.

`repair` does not remove or truncate `first_user_message`.

After `repair`, active `threads.title` max length is `<= 120`, active `threads.title` rows over 120 is `0`, and repaired rows no longer have `title = first_user_message`.

After forcing or waiting for reconciliation, active `threads.title` rows over 120 remains `0`.

Running `repair` a second time is idempotent: it reports no additional required title changes and does not append duplicate title events.

Restore from the backup manifest returns SQLite and JSONL files to their exact pre-repair hashes.

Measurement artifacts exist under the timestamped output directory:
- `metrics-before.json`
- `metrics-after-title-repair.json`
- `metrics-after-thread-loading-workaround.json`
- `metrics-summary.md`

Title metrics include active row count, total active title chars, max active title length, active titles over 120, active rows where `title = first_user_message`, title-list query payload bytes and median/p95 timing, and JSON encode median/p95 timing.

Thread-loading metrics include app-server read/list timings and payload sizes for selected large threads, CDP first-visible-content time, CDP settled-thread-shell time, long task count, total long-task duration, max long-task duration, JS heap before/after/post-GC, and a boolean or note confirming older turns loaded automatically without an extra user click.

The CDP wrapper injects the renderer patch by default, requires no user click to improve loading, emits performance marks for navigation start, first paint, and navigation end, coordinates through wrapper events to pause tab preview capture or similar expensive work during thread navigation, and does not mutate SQLite, rollout JSONL, or `session_index.jsonl`.

The CDP wrapper renderer patch fails open on unknown Codex builds/selectors/event shapes, has a storage kill switch, removes listeners/timers/styles/patches in `stop()`, and disabling it restores stock Codex behavior.

The live-repair/self-relaunch problem is explicitly resolved without user intervention: either all risky live mutation is avoided by completing the full repair, restore, metrics, renderer patch, and reconciliation verification against a custom `--codex-home` copied from the current `~/.codex`, or a detached self-cleaning runner proves it can run repair after Codex exits and automatically resume/relaunch/continue verification using installed Codex mechanisms. The chosen path is documented in `metrics-summary.md` or a dedicated autonomous-runner summary artifact with commands, logs, backup path, resume/continuation status, and fallback behavior. No completion path may depend on the user manually running commands.
</done_when>

<workflow>
1. Inspect the repo and current plan. Run `git status --short --branch`, list files with `rg --files`, and read `PLAN.md` before editing.

2. Establish copied-current-state verification before touching live state. Create a temporary custom `--codex-home` from the current `~/.codex`, preserving SQLite, SQLite sidecars when present, `session_index.jsonl`, rollout JSONL files, relevant config, and CDP wrapper renderer patch files needed for realistic verification. Record source and copied file hashes. Add synthetic edge-case fixtures only after the copied-current-state path exists.

3. Design the live-repair strategy. Inspect installed Codex resume capabilities with `codex --help` and `codex resume --help`. Decide whether live mutation will be avoided by completing the full run against the copied custom Codex home, or handled through a detached self-cleaning runner that can resume/relaunch/continue without user input. Document the autonomous path before any live `~/.codex` mutation.

4. Implement the CLI skeleton for `scripts/fix-codex-perf.py`: commands `backup`, `repair`, `restore`, and `measure`, plus `--codex-home`, `--backup-dir`, `-y|--yes`, and `--phase title|thread-loading|all`.

5. Implement Rust-parity title summarization and focused fixture tests first.

6. Implement process detection and termination. Keep matching rules narrow enough to catch Codex Desktop, Codex CLI, and app-server without killing unrelated processes. Add fixture tests for each platform output format.

7. Implement backup packaging. Include SQLite DB/sidecars, `session_index.jsonl`, affected rollout JSONL files, hashes, manifest, integrity result, and restore instructions. Prove `backup` is non-mutating by comparing pre/post hashes.

8. Implement inventory and validation. Query affected active rows, resolve rollout JSONL paths, detect existing latest `ThreadNameUpdated` events, parse files, and stop before mutation on validation failure.

9. Implement JSONL/session-index repair and SQLite repair. Append later valid `ThreadNameUpdated` events, update or append session index title data, then update SQLite in a transaction. Preserve `first_user_message`.

10. Implement restore. Restore all files from manifest paths using atomic replacement where appropriate, validate hashes, and run post-restore SQLite integrity checks.

11. Implement title measurement. Save before/after metrics for row counts, title lengths, payload bytes, query timing, JSON encode timing, and full list-item comparison.

12. Implement the CDP wrapper renderer patch. Verify selectors and event/request paths against the current Codex DOM/runtime. Prefer conservative renderer containment and navigation coordination first; intercept app-server/turn paging only when the path is stable and safely reversible.

13. Implement thread-loading measurement. Use CDP or an equivalent harness to measure first visible content, settled shell, long tasks, heap, app-server timing, and automatic older-turn hydration.

14. Validate first against the copied-current-state custom `--codex-home`, then against synthetic fixtures. Run backup, repair, second repair for idempotence, reconciliation simulation if available, restore, and hash comparison.

15. Use subagents when useful for independent workstreams such as process-detection fixtures, backup/restore hashing, JSONL reconciliation parsing, CDP wrapper runtime exploration, CDP metrics, or live-relaunch design. Give each subagent clear file/module ownership, tell it other agents may be editing the repo, and reconcile its findings before final verification.

16. Commit regularly if the implementation becomes long-running or spans multiple logical milestones. Good checkpoint commits include CLI skeleton/tests, backup/restore, repair/idempotence, metrics, CDP wrapper renderer patch, and autonomous runner. Each commit must be small enough to review, must not include unrelated user changes, and must only be made after focused verification for that milestone passes.

17. Only after the self-relaunch/live-repair strategy is proven and logged, run the live repair path for the real Codex home when it can be done autonomously. If non-interactive resume is not supported or cannot be proven safely, do not kill the active session from inside itself; instead complete the entire workflow against the copied-current-state custom Codex home and clearly report that live `~/.codex` was intentionally not mutated because an autonomous continuation path could not be proven.

18. Run final verification, inspect generated manifests/metrics, review the diff, and keep unrelated files untouched.
</workflow>

<verification_loop>
Use focused checks first:
- `python scripts/fix-codex-perf.py --help`
- create a copied-current-state custom Codex home from `~/.codex` and record source/copy hashes
- `python scripts/fix-codex-perf.py backup --codex-home <copied-current-home> --backup-dir <tmp-backups>`
- `python scripts/fix-codex-perf.py repair --codex-home <copied-current-home> --backup-dir <tmp-backups>`
- `python scripts/fix-codex-perf.py repair --codex-home <copied-current-home> --backup-dir <tmp-backups>` for idempotence
- `python scripts/fix-codex-perf.py restore --backup <copied-current-backup> --codex-home <copied-current-home> -y`
- `python scripts/fix-codex-perf.py measure --codex-home <copied-current-home> --phase title`

Run Python unit/fixture tests using the repo's chosen test command. If no test framework exists, use `python -m unittest discover` or a focused script-based test harness and document the command.

Run SQLite verification after repair and restore:
```sql
PRAGMA integrity_check;

SELECT COUNT(*), MAX(length(title))
FROM threads
WHERE COALESCE(archived, 0)=0;

SELECT COUNT(*)
FROM threads
WHERE COALESCE(archived, 0)=0
  AND length(title)>120;
```

Expected SQLite results after repair:
- integrity check returns `ok`
- active max title length is `<= 120`
- active titles over 120 is `0`
- affected repaired rows no longer have `title = first_user_message`

Verify JSONL/session-index behavior:
- every affected rollout JSONL still parses
- `session_index.jsonl` still parses
- each affected rollout has a later good title event
- reconciliation or reconciliation simulation keeps active title checks passing
- second repair does not append duplicate title events

Verify backup/restore:
- backup command does not mutate source hashes
- repair-created backup contains all required files and manifest fields
- restore returns all backed-up files to manifest hashes

Verify CDP wrapper renderer patch behavior:
- renderer patch exists at the expected standalone location
- it is injected by default
- it emits navigation start, first-paint, and navigation-end marks
- it pauses/coalesces noncritical work during navigation without dropping active assistant output, errors, or status
- running the wrapper with `--no-inject` restores stock behavior
- `stop()` removes listeners, timers, styles, and patches

Verify UX metrics against selected large local threads:
- first usable render is under 2s for the previously slow case
- no initial-switch long task is over 250ms
- total long-task time during initial switch is reduced by at least 80 percent
- transient heap spike is under 50 MB for the tested switch
- post-GC heap returns near baseline
- older turns load automatically without an extra user click

If a check cannot run on the current host, state why, provide the closest fixture or manual evidence, and do not mark that item complete until equivalent evidence exists.
</verification_loop>

<execution_rules>
- Check git status before edits.
- Preserve unrelated user changes.
- Use subagents for independent research or implementation tracks when that reduces risk or keeps the goal moving; do not let subagents edit overlapping files without explicit ownership.
- Commit regularly for long-running work at verified logical milestones, preserving unrelated user changes and avoiding noisy checkpoint churn.
- Prefer `rg` over `grep` when available.
- Use the runtime's patch/edit tool for manual edits when available.
- Read context files before implementation.
- Batch independent file reads in parallel when the runtime supports it.
- Run focused tests before broad tests.
- Do not paper over failures.
- Do not widen scope.
- Keep the final answer concise.
- Keep development against a copied-current-state custom `--codex-home` until live-repair safety is solved.
- Do not ask the user questions or require user-run follow-up commands; make conservative autonomous decisions and document them.
- Do not kill or relaunch the current Codex session unless the self-relaunch runner has been explicitly proven and logged.
- Before any live `~/.codex` mutation, verify backup creation, process-safety behavior, idempotence, and restore on the copied-current-state custom Codex home.
- Treat metrics and manifests as required deliverables, not optional diagnostics.
</execution_rules>

<output_contract>
Final artifacts must include:
- `scripts/fix-codex-perf.py`
- test fixtures or tests for title summarization, process detection, backup/repair/restore, idempotence, and restore hashes
- CDP wrapper renderer patch files, if the renderer patch path is feasible
- timestamped backup/output directories from verification runs
- `metrics-before.json`
- `metrics-after-title-repair.json`
- `metrics-after-thread-loading-workaround.json`
- `metrics-summary.md`
- a documented autonomous self-relaunch/live-repair runner summary if real `~/.codex` mutation requires terminating the active session
- a copied-current-state custom Codex home verification summary with source/copy hashes when that path is used

The final response must summarize:
- what was implemented
- which commands/checks passed
- where the backup, manifest, metrics, and autonomous runner/continuation artifacts are
- whether live `~/.codex` was actually mutated or the full end-to-end workflow was completed against a copied-current-state custom Codex home
- any checks that could not be run and why

Completion signal: mark the goal complete only after the measurable `done_when` items are satisfied or explicitly report the remaining blockers without claiming completion.
</output_contract>
