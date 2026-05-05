# Codex Thread Loading Performance Plan

Date: 2026-05-05

## Goal

Implement a standalone Codex Desktop performance workaround that fixes local title metadata, makes the fix durable across reconciliation, and improves large-thread loading UX without making the user click extra controls or lose chat history.

The workaround has three parts:

1. A cross-platform Python repair tool that updates both SQLite and JSONL, with mandatory timestamped backups.
2. A restore path that can put SQLite, JSONL, and index files back exactly from a chosen backup.
3. A no-click UX workaround for large-thread loading, implemented outside upstream Codex, with before/after measurements.

## Current Evidence

Title bloat was confirmed in `~/.codex/state_5.sqlite`.

Before repair, active title text duplicated almost the full first-message corpus:

| DB | Active rows | Active title chars | Active first_user_message chars | Max title chars | Active titles > 120 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | 134 | 14,610,549 | 14,614,033 | 675,773 | 94 |
| Same rows after title repair | 134 | 3,588 | 14,614,033 | 120 | 0 |
| Current repaired DB | 158 | 5,154 | 14,614,486 | 120 | 0 |

The thread-list style query was dramatically affected:

```sql
SELECT id, title, source, cwd, updated_at_ms
FROM threads
WHERE COALESCE(archived,0)=0
ORDER BY updated_at_ms DESC, id DESC
LIMIT 200;
```

Measured over 80 iterations:

| DB | Rows | Result payload bytes | SQLite query median | SQLite query p95 | JSON encode median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | 134 | 15,605,594 | 8.12 ms | 10.30 ms | 46.34 ms |
| Same rows after title repair | 134 | 25,074 | 0.15 ms | 0.19 ms | 0.14 ms |
| Current repaired DB | 158 | 34,343 | 0.17 ms | 0.19 ms | 0.17 ms |

Impact from title repair on the same rows:

- Result payload shrank about 622x.
- SQLite read median improved about 54x.
- JSON encode median improved about 331x.

A full list item that includes `first_user_message` showed that the title bug roughly doubled the heavy payload:

| DB | Rows | Result payload bytes | SQLite query median | SQLite query p95 | JSON encode median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | 134 | 31,196,414 | 14.70 ms | 17.75 ms | 90.73 ms |
| Same rows after title repair | 134 | 15,615,894 | 7.12 ms | 8.55 ms | 46.22 ms |

Separate CDP profiling showed a remaining large-thread loading problem after title repair:

- A tested thread switch took about 13.4s to settle in the UI.
- Direct app-server calls for the same scale of thread data were tens of ms.
- The slow path is therefore mostly renderer hydration/reconciliation/allocation, not SQLite or app-server I/O for the tested case.
- The same switch produced repeated long tasks around 1.8-2.0s and a transient heap spike of more than 200 MB.

Relevant upstream issues:

- Title root cause: https://github.com/openai/codex/issues/21154
- Perf-focused follow-up: https://github.com/openai/codex/issues/21211

## Where Repopulation Happens

The SQLite row is not the only source of truth. Codex can rebuild or reconcile thread metadata from JSONL rollout files and `session_index.jsonl`.

The critical behavior is:

- The state extractor reads rollout events.
- The first `EventMsg::UserMessage` can populate both `first_user_message` and fallback `title`.
- `ThreadNameUpdated` events can later overwrite the title.
- Reconciliation/upsert writes the resulting metadata back into SQLite.

That means a SQLite-only title trim is temporary. If the underlying JSONL still has no later good thread-name event, reconciliation can derive the bad title again from the first user message.

The safe repair is to update both layers:

- SQLite gets bounded `threads.title` values immediately.
- The affected JSONL histories get appended `ThreadNameUpdated` events after the bad fallback source event, so future reconciliation keeps the better title.

Do not rewrite existing JSONL lines. Append valid events after making backups. Later title events should win without altering original history.

## Local Repair Plan

### 0. Standalone Python Tool

The local repair should be implemented as a standalone Python tool, not as manual SQL snippets and not as an upstream Codex change.

Proposed tool name:

```text
scripts/fix_codex_thread_perf.py
```

Runtime requirements:

- Python 3.10 or newer.
- Standard library only if feasible: `sqlite3`, `json`, `pathlib`, `shutil`, `datetime`, `platform`, `subprocess`, `tempfile`, `argparse`.
- Works on macOS, Windows, and Linux.
- Writes a machine-readable manifest and a human-readable summary.
- Uses SQLite transactions for DB writes.
- Uses atomic file replacement for edited sidecar/index files where replacement is needed.
- Appends to rollout JSONL only after backups and parse validation pass.
- Does not remove or truncate `first_user_message`.
- Measures before/after numbers for both title repair and thread-loading UX.

Supported commands:

```text
python scripts/fix_codex_thread_perf.py repair [--codex-home <path>] [--backup-dir <path>] [-y|--yes]
python scripts/fix_codex_thread_perf.py backup [--codex-home <path>] [--backup-dir <path>]
python scripts/fix_codex_thread_perf.py restore --backup <path> [--codex-home <path>] [-y|--yes]
python scripts/fix_codex_thread_perf.py measure [--codex-home <path>] [--phase title|thread-loading|all]
```

There is no separate `--apply` flow in the plan. This is a one-shot repair/workaround, and every mutating command must create a backup first.

### 1. Running Codex Preflight

The script must not mutate SQLite or JSONL while Codex is running unless the user explicitly allows the script to terminate those processes.

Before `repair` and `restore`:

- Check for running Codex Desktop, Codex CLI, and Codex app-server processes before opening the DB for writes.
- On macOS/Linux, inspect process command lines with `pgrep -afil codex` or a `ps` fallback.
- On Windows, inspect process command lines with PowerShell `Get-CimInstance Win32_Process` or a `tasklist` fallback.
- Ignore the repair script's own Python process.
- Print matching PID/name/command-line rows.
- Prompt: `Kill all running Codex instances? [y/N]`.
- Default is `N`; pressing Enter must exit non-zero before any mutation.
- `-y` or `--yes` answers yes and kills matching Codex processes without prompting.
- Prefer graceful termination first, then force kill only if the process does not exit within a short timeout.
- Record killed process IDs and commands in the backup manifest.

Process termination behavior:

- macOS/Linux: send `SIGTERM`, wait, then `SIGKILL` for remaining matches.
- Windows: use PowerShell `Stop-Process -Id <pid>` or `taskkill /PID <pid> /T`, then force only if needed.
- Never kill unrelated processes that merely have `codex` in a path but are not Codex Desktop, Codex CLI, or Codex app-server. The matching rules need fixture tests.

Cross-platform path behavior:

- Default Codex home is `~/.codex` on all platforms unless overridden.
- Use `pathlib.Path.home()` and `Path.expanduser()`.
- Do not hard-code `/Volumes`, `~/Library`, `%APPDATA%`, or Linux-only paths.
- Normalize JSONL paths stored in manifests so restore works on the same machine even when path separators differ.

### 2. Mandatory Backup

Every mutating command must automatically create a timestamped backup before making any change.

Default backup directory:

```text
~/.codex/backups/thread-perf-fix-YYYYMMDD-HHMMSS/
```

The timestamped naming must allow multiple backups to coexist.

The explicit `backup` command creates the same backup package without mutating state. The `restore` command consumes one of these backup directories.

Backup contents:

- `~/.codex/state_5.sqlite`
- `~/.codex/state_5.sqlite-wal`, if present
- `~/.codex/state_5.sqlite-shm`, if present
- `~/.codex/session_index.jsonl`
- every affected rollout JSONL file
- generated metric files for before/after comparisons
- a manifest mapping original path to backup path

Manifest contents:

- tool version
- command
- platform
- Codex home
- timestamp
- killed Codex processes, if any
- SQLite integrity result before changes
- affected thread ids
- affected file paths
- per-thread old/new title lengths
- per-thread appended JSONL event id/timestamp
- hashes for every backed-up file
- restore instructions

Before any mutation:

- Run `PRAGMA integrity_check`.
- Parse `session_index.jsonl`.
- Parse every affected rollout JSONL line.
- Stop before mutating if any validation fails.

### 3. Inventory

Identify active rows that can affect navigation and title reconciliation:

```sql
SELECT id, title, first_user_message, source, cwd, updated_at_ms
FROM threads
WHERE COALESCE(archived, 0) = 0
  AND (
    length(title) > 120
    OR title = first_user_message
  )
ORDER BY updated_at_ms DESC, id DESC;
```

For each affected row, resolve:

- thread id
- rollout JSONL path
- current title length
- whether `title = first_user_message`
- existing latest `ThreadNameUpdated` event, if any
- whether the thread is active/current and should not be archived

Do not select rows only because `first_user_message` is long. The tool must preserve `first_user_message`; the UX fix handles large previews/thread loading without deleting or truncating that field.

### 4. Title Generation

Use the same summary function semantics as the Rust Codex code. The Python tool should contain a literal port of `summarize_for_label` and `truncate` from:

```text
codex-rs/state/src/extract.rs
codex-rs/external-agent-sessions/src/lib.rs
```

Current Rust behavior:

```rust
fn summarize_for_label(text: &str) -> String {
    let first_line = text.lines().next().unwrap_or_default().trim();
    truncate(first_line, SESSION_TITLE_MAX_LEN)
}

fn truncate(text: &str, max_len: usize) -> String {
    if text.chars().count() <= max_len {
        return text.to_string();
    }
    let prefix = text
        .chars()
        .take(max_len.saturating_sub(3))
        .collect::<String>();
    format!("{prefix}...")
}
```

Python port contract:

```python
TITLE_MAX_LEN = 120

def summarize_for_label(text: str) -> str:
    first_line = text.split("\n", 1)[0]
    if first_line.endswith("\r"):
        first_line = first_line[:-1]
    first_line = first_line.strip()
    return truncate(first_line, TITLE_MAX_LEN)

def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(max_len - 3, 0)] + "..."
```

Behavioral requirements:

- `ThreadMetadata.title` must never be the raw full first user message.
- Use the first line only.
- Trim leading/trailing whitespace from that line.
- Do not invent a new summary, AI-generated title, or date-based title.
- Do not collapse internal whitespace unless the Rust function changes to do so.
- Truncate to 120 chars using the same 117 chars plus `...` behavior.
- Preserve `first_user_message`; do not solve this by truncating history/message data.

The tool should include fixture tests proving parity with the Rust behavior:

- empty string -> empty string
- leading/trailing whitespace is trimmed
- second and later lines are ignored
- CRLF is treated like Rust `str::lines`
- a lone internal `\r` is not treated as a line break beyond what Rust would do
- 120-char title is unchanged
- 121-char title becomes first 117 chars plus `...`
- Unicode input truncates by Python characters in the same practical way Rust truncates by `char`

### 5. JSONL Repair

For each affected rollout JSONL:

- Append a new valid `ThreadNameUpdated` event with the generated title.
- Place it at the end of the rollout file so it wins during reconciliation.
- Do not modify earlier events.
- Do not truncate message history.

Also append or update the corresponding `session_index.jsonl` thread-name entry so the lightweight session index agrees with the rollout history.

The key property is ordering: the good thread-name event must come after the first user message fallback event. That prevents reconcile from making the title worse again.

### 6. SQLite Repair

After JSONL backup and append succeeds, the Python script updates SQLite:

```sql
UPDATE threads
SET title = :generated_title
WHERE id = :thread_id;
```

Keep `first_user_message` intact. The performance bug being repaired here is title duplication and reconciliation repopulation, not the existence of full first-message history.

The script must also record per-row before/after values in the backup manifest:

- thread id
- old title length
- new title length
- whether `title = first_user_message` before repair
- rollout JSONL path
- appended event id/timestamp
- SQLite update status

### 7. Thread Loading UX Workaround

The standalone workaround should improve large-thread loading UX without deleting message data.

Preferred implementation shape:

- package a CDP wrapper renderer patch with the tool, or generate/install one as part of the workaround
- keep it default-enabled once installed
- require no user click to make thread loading faster
- keep older turns loading automatically in the background
- preserve scrollback, search, copy, active assistant output, and status updates

The UX workaround should focus on:

- staged initial load: show metadata plus newest visible turns first
- background hydration: load older turns automatically in small idle chunks
- renderer containment: reduce layout/reconciliation cost for large transcript containers
- coordination with other CDP wrapper renderer patchs: pause preview capture and other expensive work while a thread is switching/loading

Do not solve large-thread UX by removing `first_user_message` or deleting rollout history.

### 8. Measure Before/After

The tool must measure and write before/after numbers for two phases.

Phase 1: title repair metrics:

- active row count
- total active title chars
- max active title length
- active title rows over 120 chars
- active rows where `title = first_user_message`
- title-only list query payload bytes
- title-only list query median/p95
- title-only JSON encode median/p95
- full list-item query payload bytes, including `first_user_message`, for comparison only

Phase 2: thread-loading UX metrics:

- app-server `thread/read includeTurns=false` timing and response size for selected large threads
- app-server `thread/read includeTurns=true` timing and response size for selected large threads
- `thread/turns/list` timing and response size for initial page
- CDP-measured time from thread navigation to first visible content
- CDP-measured time to settled thread shell
- long task count, total long-task time, and max long-task duration
- JS heap before, after, and after forced GC
- whether older turns loaded automatically without user click

Metrics should be saved under the timestamped backup/workaround output directory:

```text
metrics-before.json
metrics-after-title-repair.json
metrics-after-thread-loading-workaround.json
metrics-summary.md
```

### 9. Verify

Run:

```sql
PRAGMA integrity_check;

SELECT COUNT(*), MAX(length(title))
FROM threads
WHERE COALESCE(archived, 0) = 0;

SELECT COUNT(*)
FROM threads
WHERE COALESCE(archived, 0) = 0
  AND length(title) > 120;
```

Expected:

- integrity check returns `ok`
- active max title length is <= 120
- active titles over 120 is 0
- active rows where `title = first_user_message` is 0 for affected repaired rows
- affected JSONL files still parse
- `session_index.jsonl` still parses
- a later title event exists in each affected rollout JSONL
- after reconciliation, the SQLite title checks still pass
- thread-loading metrics show improved first-visible-content time and reduced long-task cost

Then force or wait for reconciliation and repeat the SQLite checks. The counts should stay fixed.

### 10. Restore Command

The tool must include an explicit restore command:

```text
python scripts/fix_codex_thread_perf.py restore --backup ~/.codex/backups/thread-perf-fix-YYYYMMDD-HHMMSS
```

Restore behavior:

- Check for running Codex processes.
- Prompt to kill them with default `N`, or use `-y` to kill without prompting.
- Restore `state_5.sqlite` and sidecar files from the selected backup.
- Restore affected JSONL files and `session_index.jsonl` from the selected backup manifest.
- Validate restored file hashes against the manifest.
- Run SQLite integrity check after restore.
- Print a concise restore summary.

## Large Thread Loading Fix Plan

Even after title repair, loading large threads can still be slow because the renderer does too much work up front.

The app-server timing indicates the remaining slow path is mostly renderer-side:

- parsing/hydrating too much thread data
- React reconciliation for large transcripts
- long tasks during route/thread switch
- transient allocation and GC pressure
- background event/log handling interleaved with navigation

### Desired User Experience

Switching threads should feel immediate:

- first visible content appears fast
- previous turns continue loading automatically
- no "click to load more" requirement
- scrollback remains available once background hydration completes
- search/copy/history behavior remains correct

### Standalone UX Workaround

Implement the workaround as an installed CDP wrapper renderer patch or equivalent standalone app-process patch:

1. On thread switch, allow metadata plus the newest visible page of turns to render first.
2. Render the new thread shell and newest turns immediately.
3. Schedule older-turn hydration in small idle chunks.
4. Pause or coalesce noncritical stream/log/sidebar work during the initial switch.
5. Resume background loading after first paint.

Add transcript virtualization:

- mount only visible turns plus overscan
- preserve scroll anchoring
- support jumping to the newest turn and older scrollback
- keep collapsed/hidden turns out of the live DOM where possible

Reduce render/layout cost:

- use CSS containment on turn containers
- apply `content-visibility: auto` where safe
- avoid measuring the whole transcript on route switch
- batch state updates by frame

Reduce payload work:

- keep thread list payloads bounded
- avoid sending full title-sized message text through display-name fields
- page turns with explicit cursors and small initial limits
- avoid eager `includeTurns=true` for large histories unless the caller actually needs it

### Acceptance Targets

Use CDP measurements against known large local threads:

- first usable render under 2s for the previously slow case
- no initial-switch long task > 250ms
- total long-task time during initial switch reduced by at least 80 percent
- transient heap spike under 50 MB for the tested switch
- post-GC heap returns near baseline
- no loss of older history after background hydration completes

## CDP wrapper Patch Plan

CDP wrapper can run arbitrary patch code inside the Codex app process. The installed runtime supports `scope: "both"` patches, renderer preload execution, main-process execution, per-patch storage, IPC, and React fiber/DOM utilities.

Create a local standalone patch:

```text
id: codex-perf-fast-thread-loader
name: Keep Codex Fast Thread Loader
scope: both
main: index.js
```

It should be installed or generated by `scripts/fix_codex_thread_perf.py`, default-enabled after install, and require no extra user clicks.

### Renderer Fast Path

In the renderer half:

- detect thread navigation from sidebar clicks, route changes, and history changes
- mark `threadNavigationInProgress`
- record start/end performance marks
- wait for the first visible target thread content
- clear the navigation window after first paint plus a short idle grace period

During `threadNavigationInProgress`:

- defer noncritical DOM work
- coalesce repeated stream/state notifications to one per animation frame
- pause expensive CDP wrapper renderer patch work such as tab preview capture
- prevent background preview/metrics features from competing with the thread switch

Do not block or drop safety-critical events, active assistant output, errors, or user-visible status.

### Lazy Older-Turn Hydration

The patch should not make the user click "load older messages."

Instead:

- allow the first visible page of the target thread to render
- delay older-page hydration until after first paint/idle
- continue loading older pages automatically in idle chunks
- stop delaying if the user scrolls near the top or searches
- use caps and timeouts so the queue cannot grow forever

If the exact app-server request path is safely interceptable, defer repeated `thread/turns/list` calls for older pages during the initial switch. If the request path is not stable, fall back to DOM/render containment and coordination with other patches.

### DOM And Layout Mitigation

Apply conservative CSS to large transcript containers:

```css
[data-codex-thread-turn],
[data-message-author-role],
article {
  contain: layout paint style;
  content-visibility: auto;
  contain-intrinsic-size: auto 160px;
}
```

The actual selectors must be verified against the current Codex DOM. The patch should only apply rules to confirmed transcript/turn containers and should disable itself if selectors become ambiguous.

### Coordination With Other CDP wrapper Patches

Emit internal window events:

```text
codex-perf-thread-fastpath:navigation-start
codex-perf-thread-fastpath:first-paint
codex-perf-thread-fastpath:navigation-end
```

Existing or future patches can listen and pause expensive work. For the installed tab switcher, the plan is:

- skip capturePage during navigation
- delay preview refresh until after `navigation-end`
- do not take snapshots while the target thread is still hydrating

### Main-Process Half

The main half should stay minimal:

- provide a small IPC endpoint for version/build info
- optionally provide an app-server timing probe for diagnostics
- log performance summaries to the CDP wrapper log
- never mutate `~/.codex/state_5.sqlite`
- never mutate rollout JSONL

State repair remains a separate backup-first maintenance operation, not renderer patch behavior.

### Guardrails

The patch must:

- version-gate against Codex Desktop build/hash
- fail open if event shapes or selectors change
- include a storage kill switch
- default to enabled only for validated versions
- avoid patching prototypes globally unless there is no safer hook
- clean up all listeners, timers, styles, and patches in `stop()`

### Patch Validation

Use a CDP harness to measure:

- time from thread row click to target thread active
- time to first visible message
- time to settled thread shell
- long task count and max duration
- heap before, after, and after forced GC
- number of deferred/flushed background operations

Acceptance for the interim patch:

- tested slow switch improves from about 13.4s to under 2s first usable render
- no visible regression in transcript availability
- no extra user clicks
- no SQLite/JSONL mutation from the patch
- disabling the patch fully restores stock behavior

## Acceptance Criteria

### Standalone Python Tool

The local repair is accepted only when all of these pass:

- `python scripts/fix_codex_thread_perf.py backup` creates a timestamped backup without mutating SQLite, `session_index.jsonl`, or rollout JSONL file hashes.
- `python scripts/fix_codex_thread_perf.py repair` automatically creates a timestamped backup before any mutation.
- `python scripts/fix_codex_thread_perf.py repair` prompts to kill running Codex Desktop, Codex CLI, or Codex app-server processes with default `N`.
- Pressing Enter at the kill prompt exits non-zero before mutation.
- `python scripts/fix_codex_thread_perf.py repair -y` kills matching Codex processes without prompting, records them in the manifest, and then continues.
- `python scripts/fix_codex_thread_perf.py restore --backup <path>` restores from the selected backup and validates restored hashes.
- The no-running-Codex process detector has fixture tests for macOS `pgrep`/`ps`, Linux `pgrep`/`ps`, and Windows PowerShell/tasklist output.
- The script runs on macOS, Windows, and Linux with only documented Python dependencies.
- `repair` creates a timestamped backup directory containing `state_5.sqlite`, SQLite sidecars when present, `session_index.jsonl`, every affected rollout JSONL, metrics files, and a manifest.
- The backup manifest records every touched file and every changed thread id with old/new title lengths.
- `repair` validates SQLite with `PRAGMA integrity_check` before and after mutation.
- `repair` validates that every affected JSONL file parses before appending any title event.
- `repair` appends title events instead of rewriting old rollout lines.
- `repair` uses the same `summarize_for_label` semantics as the Rust Codex code: first line, trim, 120-char cap, 117 chars plus `...` when truncated.
- `repair` does not remove or truncate `first_user_message`.
- After `repair`, active `threads.title` max length is <= 120.
- After `repair`, active `threads.title` rows over 120 is 0.
- After forcing or waiting for reconciliation, active `threads.title` rows over 120 is still 0.
- Running `repair` a second time is idempotent: it reports no additional required title changes and does not append duplicate title events.
- Restore from the backup manifest returns SQLite and JSONL files to their exact pre-repair hashes.

### Measurement

The workaround is accepted only when measurement artifacts exist and show both phases:

- `metrics-before.json`
- `metrics-after-title-repair.json`
- `metrics-after-thread-loading-workaround.json`
- `metrics-summary.md`

Title phase must include:

- active row count
- total active title chars
- max active title length
- count of active titles over 120
- count of active rows where `title = first_user_message`
- title-list query payload bytes and median/p95 timing
- JSON encode median/p95 timing

Thread-loading phase must include:

- app-server read/list timings and payload sizes for selected large threads
- CDP first-visible-content time
- CDP settled-thread-shell time
- long task count, total duration, and max duration
- JS heap before/after/post-GC
- a boolean or note confirming older turns load automatically without an extra user click

### CDP wrapper Patch

The CDP wrapper UX patch is accepted only when all of these pass:

- It is default-enabled after install and requires no user click to improve loading.
- It emits performance marks for navigation start, first paint, and navigation end.
- It coordinates with installed CDP wrapper renderer patchs so tab preview capture and similar expensive work pause during thread navigation.
- It does not mutate SQLite, rollout JSONL, or `session_index.jsonl`.
- It fails open on unknown Codex builds, selectors, or event shapes.
- It has a storage kill switch.
- Its `stop()` handler removes listeners, timers, styles, and patches.
- Disabling it restores stock Codex behavior.

## Sequencing

1. Keep current local state stable.
2. Implement `scripts/fix_codex_thread_perf.py`.
3. Implement Rust-parity title summarization fixtures in Python.
4. Implement backup and restore commands.
5. Implement running-Codex detection and kill prompt with default `N` plus `-y`.
6. Implement SQLite plus JSONL repair.
7. Implement before/after metrics for title repair.
8. Implement or package the CDP wrapper thread-loading UX patch.
9. Implement before/after metrics for thread loading.
10. Run `backup` once explicitly to prove standalone backup works.
11. Run `repair`; let it create its own timestamped backup automatically.
12. Verify DB, JSONL, reconciliation durability, restoreability, and UX metrics.

## Non-Goals

- Do not delete chat history.
- Do not archive important active chats as part of this plan.
- Do not rewrite old JSONL lines when appending a title event is sufficient.
- Do not make the user click to load older messages.
- Do not treat RepoPrompt as the only source of the bug.
- Do not remove or truncate `first_user_message`.
- Do not implement this plan as an upstream Codex patch.

## Open Questions

- Which exact renderer request/event path should the CDP wrapper renderer patch intercept for older-turn paging?
- Does current Codex have a stable transcript container selector suitable for `content-visibility`?
- What is the safest process-matching rule that catches Codex Desktop, CLI, and app-server without catching unrelated paths?
- Which local large threads should be used as repeatable before/after UX benchmarks?
