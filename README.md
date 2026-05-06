# codex-perf

Local launcher and renderer patch for keeping a large Codex Desktop profile responsive.

`codex-perf` starts Codex Desktop with a Chrome DevTools Protocol port, injects
`renderer/fast-thread-loader.js`, and uses Codex's app-action API for ongoing
title repair and background thread prefetch. Thread navigation and chat
rendering stay on the native Codex thread page.

## The Performance Issue

Codex Desktop keeps thread metadata in local storage and sends that metadata
into an Electron renderer to build the sidebar and route into thread views. Large
local profiles can develop thread titles that are full prompt previews instead
of short labels.

Those long titles create UI lag in the Electron renderer:

- The app process has to read and JSON-encode much larger thread-list rows.
- Electron has to move that larger payload across the app-to-renderer boundary.
- The renderer has to parse, allocate, diff, and reconcile large strings on the
  JavaScript main thread.
- Sidebar rows with huge text force more text handling, truncation, and layout
  work during list updates.

The visible symptom is a sidebar or thread switch that appears stuck. The UI
thread is busy processing metadata that should have been a short label.

Changing into an old or large thread also has a cold path: Codex starts native
navigation first, then reads enough thread data to show the chat. When that read
starts only after the route transition, thread switching feels stuck even though
the app is still working.

`codex-perf` addresses those two hot spots at runtime:

- Keep thread titles bounded by calling Codex's `threads.set_title` app action.
- Start a lightweight `threads.read` prefetch as soon as a thread row is clicked.

## Observed Perf Data

The baseline data is from
[openai/codex#21211](https://github.com/openai/codex/issues/21211), which
isolated the title-length issue by benchmarking the same affected SQLite row set
before and after shortening only pathological active titles.

### Title Metadata

| DB snapshot | Active rows | Active title chars | Active first_user_message chars | Max title chars | Active titles > 120 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | `134` | `14,610,549` | `14,614,033` | `675,773` | `94` |
| Same rows after title repair | `134` | `3,588` | `14,614,033` | `120` | `0` |
| Current repaired DB | `158` | `5,154` | `14,614,486` | `120` | `0` |

### Thread-List Query

This query approximates the active thread navigation/list path that needs titles:

```sql
SELECT id, title, source, cwd, updated_at_ms
FROM threads
WHERE COALESCE(archived,0)=0
ORDER BY updated_at_ms DESC, id DESC
LIMIT 200;
```

Measured over 80 iterations:

| DB snapshot | Rows | Result payload bytes | SQLite query median | SQLite query p95 | JSON encode median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | `134` | `15,605,594` | `8.12 ms` | `10.30 ms` | `46.34 ms` |
| Same rows after title repair | `134` | `25,074` | `0.15 ms` | `0.19 ms` | `0.14 ms` |
| Current repaired DB | `158` | `34,343` | `0.17 ms` | `0.19 ms` | `0.17 ms` |

The same rows went from `15.6 MB` to `25 KB`, about `622x` smaller. SQLite read
median went from `8.12 ms` to `0.15 ms`, about `54x` faster. JSON encode median
went from `46.34 ms` to `0.14 ms`, about `331x` faster.

Electron pays after these numbers: the payload still has to cross the app-to-
renderer boundary, then the renderer parses, allocates, reconciles, and lays out
sidebar rows on the JavaScript main thread.

### Full List Item With Preview

When the list path includes both `title` and `first_user_message`, the bad title
duplicates the full prompt-sized preview:

```sql
SELECT id, title, first_user_message, source, cwd, updated_at_ms
FROM threads
WHERE COALESCE(archived,0)=0
ORDER BY updated_at_ms DESC, id DESC
LIMIT 200;
```

| DB snapshot | Rows | Result payload bytes | SQLite query median | SQLite query p95 | JSON encode median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | `134` | `31,196,414` | `14.70 ms` | `17.75 ms` | `90.73 ms` |
| Same rows after title repair | `134` | `15,615,894` | `7.12 ms` | `8.55 ms` | `46.22 ms` |
| Current repaired DB | `158` | `15,626,192` | `7.08 ms` | `8.50 ms` | `45.40 ms` |

### Thread List And Thread Read

Direct app-server probes showed list payload size and large-thread hydration as
separate hot paths:

| Operation | Timing | Response size |
| --- | ---: | ---: |
| `thread/list archived=false useStateDbOnly=true limit=20` | `2.56 s` | `4,505,704 bytes` |
| `thread/list archived=false useStateDbOnly=true limit=100` | `7.95 s` | `14,628,806 bytes` |
| `thread/list archived=false useStateDbOnly=false limit=100` | `8.26 s` | `14,627,363 bytes` |
| `thread/read includeTurns=false`, 48.7 MB image-heavy rollout | `62.8 ms` | `850 bytes` |
| `thread/read includeTurns=true`, 48.7 MB image-heavy rollout | `11.65 s` | `20,654,619 bytes` |
| `thread/read includeTurns=false`, 45.9 MB compaction-heavy rollout | `27.8 ms` | `875 bytes` |
| `thread/read includeTurns=true`, 45.9 MB compaction-heavy rollout | `3.39 s` | `6,363,354 bytes` |
| `thread/read includeTurns=false`, 1.4 MB giant first-message rollout | `415.9 ms` | `704,374 bytes` |
| `thread/read includeTurns=true`, 1.4 MB giant first-message rollout | `814.0 ms` | `1,414,448 bytes` |

CDP profiling inside Codex Desktop tied those payloads to visible Electron lag:

| UI observation | Value |
| --- | ---: |
| Tested thread switch settle time | about `13.4 s` |
| Renderer heap spike during switch | `>200 MB` transient |
| Repeated renderer long tasks | about `1.8-2.0 s` each |

Thread-click measurements in this repo are stored under `artifacts/`. The clean
injected CDP run in `artifacts/codex-perf-cdp-20260506-021020/` recorded:

| Metric | Value |
| --- | ---: |
| First visible content | `27.900 ms` |
| Settled thread shell | `144.600 ms` |
| Long task count | `0` |
| Total long task duration | `0 ms` |
| Transient heap spike | `91,844,356 bytes` |

## Quick Start

macOS:

```bash
./codex-perf.sh
```

Windows:

```cmd
codex-perf.cmd
```

Attach to an already CDP-enabled Codex app:

```bash
python3 scripts/codex-perf-launch.py --no-launch --no-measure
```

The root launch scripts delegate directly to `scripts/codex-perf-launch.py`.
macOS uses `open -a` so an existing `Codex.app` instance can receive the launch
request. Windows starts the Codex Desktop `.exe` directly with the same CDP
arguments.

## Current Architecture

```text
codex-perf.sh / codex-perf.cmd
        |
        | launch Codex Desktop with --remote-debugging-port=17373
        v
scripts/codex-perf-launch.py
        |
        | inject renderer/fast-thread-loader.js through CDP
        v
Codex renderer process
        |
        | debug-run-app-action-request
        v
Codex app-action API
        |
        | threads.list       -> title drift detection
        | threads.set_title  -> durable title repair
        | threads.read       -> background thread prefetch
        v
normal Codex storage and native rendering
```

## Platform Launch

Default app path resolution:

| Platform | Default |
| --- | --- |
| macOS | `/Applications/Codex.app` |
| Windows | Common Codex Desktop install locations, including MSIX packages under `WindowsApps` |

Windows candidates include:

```text
%ProgramFiles%\WindowsApps\OpenAI.Codex_*\app\Codex.exe
%LOCALAPPDATA%\Programs\Codex\Codex.exe
%LOCALAPPDATA%\Programs\codex\Codex.exe
%LOCALAPPDATA%\Programs\OpenAI Codex\Codex.exe
%LOCALAPPDATA%\Codex\Codex.exe
%ProgramFiles%\Codex\Codex.exe
%ProgramFiles%\OpenAI Codex\Codex.exe
%ProgramFiles(x86)%\Codex\Codex.exe
%ProgramFiles(x86)%\OpenAI Codex\Codex.exe
```

Use an explicit executable or `app` directory path when needed:

```cmd
codex-perf.cmd --app-path "C:\path\to\Codex.exe"
codex-perf.cmd --app-path "C:\Program Files\WindowsApps\OpenAI.Codex_26.429.8261.0_x64__2p2nqsd0c76g0\app"
```

`CODEX_DESKTOP_PATH` is also honored when set.

## Title Repair

Codex can rewrite thread titles from rollout-derived metadata. The injected
runtime repairs those titles through Codex's own app-action API.

The renderer runs a guarded periodic fixer:

- Every `30s`, call `threads.list`.
- Detect titles longer than `120` characters and titles that match a long
  preview fallback.
- Compute a bounded title from the thread preview.
- Call `threads.set_title` when the bounded title differs.
- Apply a `60s` per-thread cooldown.

## Thread Prefetch

Thread row clicks keep the native Codex route transition. The renderer patch
starts a background prefetch beside that native transition:

```text
click thread row
        |
        | native Codex click continues
        |
        | threads.read(limit=10, includeOutputs=false) runs in background
        v
native Codex thread page renders
```

## Commands

### Launch Wrapper

```bash
python3 scripts/codex-perf-launch.py --help
```

Useful options:

| Option | Purpose |
| --- | --- |
| `--port 17373` | CDP port. Use `0` to pick a free port |
| `--app-path <path>` | Codex Desktop app bundle or executable path |
| `--workspace <path>` | Workspace to open |
| `--renderer-js <path>` | Renderer JavaScript file to inject |
| `--no-launch` | Attach to an existing CDP-enabled app |
| `--no-measure` | Launch and inject with metrics collection skipped |
| `--no-inject` | Stop an existing patch and measure baseline behavior |

For repeated measurement runs, start one CDP-enabled Codex app and attach to it:

```bash
python3 scripts/codex-perf-launch.py --no-launch --output-dir artifacts/perf-injected
python3 scripts/codex-perf-launch.py --no-launch --no-inject --output-dir artifacts/perf-baseline
```

For reliable thread-switching numbers, capture a clicked thread row, a rendered
chat view, long-task data, heap data, and the output artifact path from the same
CDP-enabled app instance.

## Safety Model

- The normal launch path edits titles through Codex app actions.
- Periodic title repair uses actual title/preview checks and a per-thread cooldown.
- Thread clicks use native Codex navigation with a background `threads.read` prefetch.
- The renderer patch has a localStorage kill switch:
  `codex-perf-fast-thread-loader:disabled=1`.
- The renderer patch cleans up listeners, timers, styles, and bridge patches through `stop()`.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `CDP target list unavailable` | Relaunch through the platform wrapper, or attach with `--no-launch` after starting Codex with the same CDP port |
| Windows custom install path | Pass `--app-path "C:\path\to\Codex.exe"` or set `CODEX_DESKTOP_PATH` |
| Repeated measurements | Start one CDP-enabled app and rerun measurements with `--no-launch` |
| Patch kill switch is enabled | Clear localStorage key `codex-perf-fast-thread-loader:disabled` |

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

Check syntax:

```bash
python3 -m py_compile scripts/codex-perf-launch.py
node --check renderer/fast-thread-loader.js
sh -n codex-perf.sh
```

## License

MIT. See `LICENSE`.
