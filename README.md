# codex-perf

Local launcher and renderer patch for keeping a large Codex Desktop profile responsive.

`codex-perf` starts Codex Desktop with a Chrome DevTools Protocol port, injects
`renderer/fast-thread-loader.js`, and uses Codex's app-action API for ongoing
title repair and background thread prefetch. Thread navigation and chat
rendering stay on the native Codex thread page.

## The Performance Issue

Codex Desktop keeps thread metadata in local storage and uses that metadata to
build the sidebar and route into thread views. Large local profiles can develop
thread titles that are full prompt previews instead of short labels. Those long
titles make the sidebar heavier to render and make thread-list updates more
expensive.

Changing into an old or large thread also has a cold path: Codex starts native
navigation first, then reads enough thread data to show the chat. When that read
starts only after the route transition, thread switching feels stuck even though
the app is still working.

`codex-perf` addresses those two hot spots at runtime:

- Keep thread titles bounded by calling Codex's `threads.set_title` app action.
- Start a lightweight `threads.read` prefetch as soon as a thread row is clicked.

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
| Windows | `CODEX_DESKTOP_PATH`, then common Codex Desktop install locations |

Windows candidates include:

```text
%LOCALAPPDATA%\Programs\Codex\Codex.exe
%LOCALAPPDATA%\Programs\codex\Codex.exe
%LOCALAPPDATA%\Programs\OpenAI Codex\Codex.exe
%LOCALAPPDATA%\Codex\Codex.exe
%ProgramFiles%\Codex\Codex.exe
%ProgramFiles%\OpenAI Codex\Codex.exe
%ProgramFiles(x86)%\Codex\Codex.exe
%ProgramFiles(x86)%\OpenAI Codex\Codex.exe
```

Use an explicit executable path when needed:

```cmd
codex-perf.cmd --app-path "C:\path\to\Codex.exe"
```

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
