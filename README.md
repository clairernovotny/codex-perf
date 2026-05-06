# codex-perf

Local launcher and renderer patch for making large Codex Desktop profiles usable.

`codex-perf` starts Codex Desktop with a Chrome DevTools Protocol port, injects
`renderer/fast-thread-loader.js`, and uses Codex's app-action API to keep thread
titles bounded and prefetch thread data on click. The chat view remains the
native Codex thread page.

## TL;DR

| Area | What happens |
| --- | --- |
| Problem | Oversized thread metadata and eager large-history hydration make Electron thread switching feel stuck. |
| Runtime fix | Inject a renderer patch over CDP, repair titles with `threads.set_title`, and prefetch with `threads.read`. |
| macOS launch | `./codex-perf.sh` opens `/Applications/Codex.app` with CDP on `127.0.0.1:17373`. |
| Windows launch | `codex-perf.cmd` finds Codex Desktop in common install locations, including MSIX `WindowsApps` packages. |
| Evidence | The issue data shows title-list payload dropping from `15.6 MB` to `25 KB`, and JSON encode median from `46.34 ms` to `0.14 ms`. |

## Why This Exists

Codex Desktop keeps thread metadata locally and sends it into an Electron
renderer to build the sidebar and open thread views. In affected profiles,
`threads.title` can become a full prompt-sized preview instead of a short label.

That matters because the UI path pays for the oversized title repeatedly:

- SQLite reads larger thread-list rows.
- The app process JSON-encodes much larger payloads.
- Electron moves the payload into the renderer.
- The renderer parses, allocates, diffs, reconciles, truncates, and lays out huge
  strings on the JavaScript main thread.

Large threads add a second hot path: opening a thread can eagerly hydrate and
render too much history before the view becomes usable.

`codex-perf` targets both paths at runtime:

- Bound display titles through Codex's `threads.set_title` app action.
- Start a lightweight `threads.read(limit=10, includeOutputs=false)` prefetch as
  soon as a thread row is clicked.

## Evidence

The baseline data below comes from
[openai/codex#21211](https://github.com/openai/codex/issues/21211), which
isolated title length by benchmarking the same SQLite row set before and after
shortening only pathological active titles.

### Title Metadata

| DB snapshot | Active rows | Active title chars | Active first_user_message chars | Max title chars | Titles > 120 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | `134` | `14,610,549` | `14,614,033` | `675,773` | `94` |
| Same rows after title repair | `134` | `3,588` | `14,614,033` | `120` | `0` |
| Current repaired DB | `158` | `5,154` | `14,614,486` | `120` | `0` |

### Thread-List Query

```sql
SELECT id, title, source, cwd, updated_at_ms
FROM threads
WHERE COALESCE(archived,0)=0
ORDER BY updated_at_ms DESC, id DESC
LIMIT 200;
```

Measured over 80 iterations:

| DB snapshot | Rows | Result payload | SQLite median | SQLite p95 | JSON encode median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | `134` | `15,605,594 bytes` | `8.12 ms` | `10.30 ms` | `46.34 ms` |
| Same rows after title repair | `134` | `25,074 bytes` | `0.15 ms` | `0.19 ms` | `0.14 ms` |
| Current repaired DB | `158` | `34,343 bytes` | `0.17 ms` | `0.19 ms` | `0.17 ms` |

Impact on the same rows:

| Metric | Change |
| --- | ---: |
| Result payload | `15.6 MB` -> `25 KB`, about `622x` smaller |
| SQLite read median | `8.12 ms` -> `0.15 ms`, about `54x` faster |
| JSON encode median | `46.34 ms` -> `0.14 ms`, about `331x` faster |

Those numbers are before Electron IPC and before renderer-side parse,
allocation, reconciliation, and layout work.

### Full List Item With Preview

When the list item includes both `title` and `first_user_message`, a bad title
duplicates the full prompt-sized preview:

```sql
SELECT id, title, first_user_message, source, cwd, updated_at_ms
FROM threads
WHERE COALESCE(archived,0)=0
ORDER BY updated_at_ms DESC, id DESC
LIMIT 200;
```

| DB snapshot | Rows | Result payload | SQLite median | SQLite p95 | JSON encode median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bad backup | `134` | `31,196,414 bytes` | `14.70 ms` | `17.75 ms` | `90.73 ms` |
| Same rows after title repair | `134` | `15,615,894 bytes` | `7.12 ms` | `8.55 ms` | `46.22 ms` |
| Current repaired DB | `158` | `15,626,192 bytes` | `7.08 ms` | `8.50 ms` | `45.40 ms` |

### Thread Read Hydration

Direct app-server probes show thread-list payload size and large-thread
hydration as separate hot paths:

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

The repo also contains a clean injected CDP measurement in
`artifacts/codex-perf-cdp-20260506-021020/`:

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

## How It Works

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

The renderer runs a guarded periodic title fixer:

| Setting | Value |
| --- | ---: |
| Scan interval | `30 s` |
| Title ceiling | `120 chars` |
| Per-thread cooldown | `60 s` |
| App action | `threads.set_title` |

Thread row clicks keep the native Codex route transition and start a background
prefetch:

```text
click thread row
        |
        | native Codex click continues
        |
        | threads.read(limit=10, includeOutputs=false)
        v
native Codex thread page renders
```

## Platform Details

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

Use an explicit executable or MSIX `app` directory path when needed:

```cmd
codex-perf.cmd --app-path "C:\path\to\Codex.exe"
codex-perf.cmd --app-path "C:\Program Files\WindowsApps\OpenAI.Codex_26.429.8261.0_x64__2p2nqsd0c76g0\app"
```

`CODEX_DESKTOP_PATH` is also honored when set.

## Command Reference

```bash
python3 scripts/codex-perf-launch.py --help
```

| Option | Purpose |
| --- | --- |
| `--port 17373` | CDP port. Use `0` to pick a free port. |
| `--app-path <path>` | Codex Desktop app bundle, executable, or MSIX `app` directory. |
| `--workspace <path>` | Workspace to open. |
| `--renderer-js <path>` | Renderer JavaScript file to inject. |
| `--no-launch` | Attach to an existing CDP-enabled app. |
| `--no-measure` | Launch and inject with metrics collection skipped. |
| `--no-inject` | Stop an existing patch and measure baseline behavior. |
| `--output-dir <path>` | Directory for CDP measurement artifacts. |

Repeated measurement runs should attach to one CDP-enabled app:

```bash
python3 scripts/codex-perf-launch.py --no-launch --output-dir artifacts/perf-injected
python3 scripts/codex-perf-launch.py --no-launch --no-inject --output-dir artifacts/perf-baseline
```

For useful thread-switching numbers, capture a clicked thread row, a rendered
chat view, long-task data, heap data, and the output artifact path from the same
CDP-enabled app instance.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `CDP target list unavailable` | Relaunch through the platform wrapper, or attach with `--no-launch` after starting Codex with the same CDP port. |
| Windows custom install path | Pass `--app-path "C:\path\to\Codex.exe"` or set `CODEX_DESKTOP_PATH`. |
| Repeated measurements | Start one CDP-enabled app and rerun measurements with `--no-launch`. |
| Patch kill switch is enabled | Clear localStorage key `codex-perf-fast-thread-loader:disabled`. |

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

Check syntax:

```bash
python3 -m py_compile scripts/fix-codex-perf.py scripts/codex-perf-launch.py
node --check renderer/fast-thread-loader.js
sh -n codex-perf.sh
```

## License

MIT. See `LICENSE`.
