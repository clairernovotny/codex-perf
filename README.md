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

The title problem is large enough to dominate thread-list work. A local
measurement pass on May 6, 2026 used 2,408 active thread rows and compared the
same title-list query before and after bounding oversized titles to 120
characters.

| Metric | Before bounded titles | After bounded titles |
| --- | ---: | ---: |
| Active titles over 120 chars | `93` | `0` |
| Total active title characters | `17,962,096` | `137,194` |
| Max active title length | `941,848` | `120` |
| Title-list payload | `18,970,749 bytes` | `45,676 bytes` |
| Title-list SQLite query median | `6.020 ms` | `0.180 ms` |
| Title-list SQLite query p95 | `6.751 ms` | `0.552 ms` |
| Title-list JSON encode median | `75.058 ms` | `0.162 ms` |
| Title-list JSON encode p95 | `79.421 ms` | `0.303 ms` |

That is a `99.8%` payload reduction for the title-list response and moves JSON
encoding from tens of milliseconds to sub-millisecond. The extreme case was one
thread title carrying `941,848` characters where the UI needed a short label.
In Electron terms, that removes about `18.9 MB` of avoidable string payload from
the thread-list path before the renderer starts parsing, allocating, reconciling,
and laying out sidebar rows.

The runtime fixer uses the same 120-character ceiling. A later drift check found
one new title at `137` characters; the same app-action repair path brought the
max active title length back to `120`.

Thread-click measurements are stored under `artifacts/`. The clean injected CDP
run in `artifacts/codex-perf-cdp-20260506-021020/` recorded:

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
