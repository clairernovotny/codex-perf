# codex-perf

Local launcher and renderer patch for keeping a large Codex Desktop profile responsive.

The current path is CDP injection only. `codex-perf.sh` starts `Codex.app` with a
Chrome DevTools Protocol port, injects `renderer/fast-thread-loader.js`, and lets
that injected runtime use Codex's own app-action API for title repair and
background thread prefetch. The real Codex thread page remains responsible for
thread navigation and rendering. There is no loopback thread-data server in the
current path.

## Quick Start

Launch Codex with the current patch:

```bash
./codex-perf.sh
```

On Windows:

```cmd
codex-perf.cmd
```

Attach to an already CDP-enabled Codex app:

```bash
python3 scripts/codex-perf-launch.py --no-launch --no-measure
```

The launch scripts delegate directly to `scripts/codex-perf-launch.py`. They do
not print status, run the offline repair command, or check for or kill other
Codex processes before launch.

## Current Architecture

```text
codex-perf.sh / codex-perf.cmd
        |
        | launch Codex.app with --remote-debugging-port=17373
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
        | threads.set_title  -> durable Codex title repair
        | threads.list       -> periodic title drift detection
        | threads.read       -> background thread prefetch
        v
normal Codex storage and app behavior
```

## Title Repair

Codex can rewrite `~/.codex/state_5.sqlite` thread titles from rollout-derived
metadata. The injected runtime repairs that through Codex's own API instead of
editing storage directly during launch.

The renderer runs a guarded periodic fixer:

- Every `30s`, call `threads.list`.
- Detect titles longer than `120` characters or titles that are just a long
  preview fallback.
- Compute the bounded title from the thread preview.
- Call `threads.set_title` only when the bounded title differs.
- Apply a `60s` per-thread cooldown to avoid duplicate rename spam.

This means if another Codex component rewrites SQLite after startup, the injected
runtime can repair it again without a separate process and without modifying
rollout JSONL.

## Rendering Speed

Thread navigation stays native. The renderer patch does not prevent the sidebar
click, does not create a replacement thread page, and does not write a custom
conversation surface into the DOM.

On click, it makes a background `threads.read` request as a prefetch while Codex
continues with its normal route transition:

```text
click thread row
        |
        | native Codex click continues
        |
        | threads.read(limit=10, includeOutputs=false) in background
        v
native Codex thread page renders normally
```

If `threads.read` fails or the app-action bus is unavailable, the native click
still proceeds.

## Commands

### Launch Wrapper

```bash
python3 scripts/codex-perf-launch.py --help
```

Useful options:

| Option | Purpose |
| --- | --- |
| `--port 17373` | CDP port. Use `0` to pick a free port |
| `--app-path /Applications/Codex.app` | Codex Desktop app bundle path |
| `--workspace <path>` | Workspace to open |
| `--renderer-js <path>` | Renderer JavaScript file to inject |
| `--no-launch` | Attach to an existing CDP-enabled app |
| `--no-measure` | Launch and inject without collecting metrics |
| `--no-inject` | Stop an existing patch and measure baseline behavior |

### Offline Metadata Tool

`scripts/fix-codex-perf.py` remains available for explicit backup, restore, and
manual repair workflows. It is not part of the normal launch path.

```bash
python3 scripts/fix-codex-perf.py --help
```

| Command | Purpose |
| --- | --- |
| `backup` | Create a timestamped backup package |
| `status` | Check whether local title metadata needs repair |
| `repair` | Back up, update SQLite titles, append Codex-compatible session-index name records, and write metrics |
| `restore --backup <path>` | Restore files from a selected backup manifest and validate hashes |
| `stop` | Prompt before stopping matching Codex processes |
| `measure --phase all` | Write local measurement artifacts |

When the offline tool mutates the live default `~/.codex`, it checks for running
Codex Desktop, Codex CLI, and Codex app-server processes first. That process
safety path only belongs to explicit offline repair/restore commands.

## Safety Model

- The normal launch path does not edit Codex files directly.
- Periodic title repair is guarded by actual title/preview checks and a per-thread cooldown.
- Thread clicks keep native Codex navigation; the patch only prefetches with `threads.read`.
- The patch does not bind a loopback data server.
- The renderer patch has a localStorage kill switch:
  `codex-perf-fast-thread-loader:disabled=1`.
- The renderer patch cleans up listeners, timers, styles, and bridge patches through `stop()`.
- The offline repair tool creates backups before mutation and preserves
  `first_user_message`.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `CDP target list unavailable` | Relaunch through `./codex-perf.sh`, or attach with `--no-launch` only after starting Codex with the same CDP port |
| Patch should be disabled | Set localStorage key `codex-perf-fast-thread-loader:disabled` to `1` |
| Background prefetch fails | Native Codex navigation still proceeds; rerun with a current Codex build if you need prefetch metrics |
| Offline repair prompts about running Codex processes | Stop Codex yourself, or explicitly allow the offline tool to terminate matching processes |
| Restore fails hash validation | Use the unchanged backup directory containing the original `manifest.json` |

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
