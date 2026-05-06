#!/usr/bin/env python3
"""Launch Codex Desktop with CDP and inject the standalone fast-loader patch."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import platform
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_CDP_PORT = 17373
PATCH_ID = "codex-perf-fast-thread-loader"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def default_renderer_path() -> Path:
    return Path(__file__).resolve().parents[1] / "renderer" / "fast-thread-loader.js"


def default_output_dir() -> Path:
    return Path.cwd() / "artifacts" / f"codex-perf-cdp-{timestamp_slug()}"


def windows_app_path_candidates(environ: dict[str, str] | None = None) -> list[Path]:
    env = environ if environ is not None else os.environ
    candidates: list[Path] = []
    explicit = env.get("CODEX_DESKTOP_PATH")
    if explicit:
        candidates.append(Path(explicit))
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        local_root = Path(local_app_data)
        candidates.extend([
            local_root / "Programs" / "Codex" / "Codex.exe",
            local_root / "Programs" / "codex" / "Codex.exe",
            local_root / "Programs" / "OpenAI Codex" / "Codex.exe",
            local_root / "Codex" / "Codex.exe",
        ])
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        value = env.get(key)
        if value:
            root = Path(value)
            candidates.extend([
                root / "Codex" / "Codex.exe",
                root / "OpenAI Codex" / "Codex.exe",
            ])
            candidates.extend(windows_store_app_path_candidates(root))
    return candidates


def windows_store_app_path_candidates(program_files: Path) -> list[Path]:
    windows_apps = program_files / "WindowsApps"
    candidates: list[Path] = []
    if not windows_apps.exists():
        return [
            windows_apps / "OpenAI.Codex_*" / "app" / "Codex.exe",
        ]
    try:
        packages = sorted(windows_apps.glob("OpenAI.Codex_*"))
    except OSError:
        return [
            windows_apps / "OpenAI.Codex_*" / "app" / "Codex.exe",
        ]
    for package in packages:
        candidates.extend([
            package / "app" / "Codex.exe",
            package / "app" / "codex.exe",
            package / "Codex.exe",
        ])
    if not candidates:
        candidates.append(windows_apps / "OpenAI.Codex_*" / "app" / "Codex.exe")
    return candidates


def default_app_path(system: str | None = None, environ: dict[str, str] | None = None) -> str:
    current_system = system or platform.system()
    if current_system == "Darwin":
        return "/Applications/Codex.app"
    if current_system == "Windows":
        candidates = windows_app_path_candidates(environ)
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        if candidates:
            return str(candidates[0])
        return "Codex.exe"
    return "codex"


def resolve_windows_app_executable(app_path: Path) -> Path:
    if app_path.is_dir():
        for name in ("Codex.exe", "codex.exe"):
            candidate = app_path / name
            if candidate.exists():
                return candidate
    return app_path


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch_json(url: str, timeout: float = 1.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_targets(port: int, timeout: float = 30.0) -> list[dict[str, Any]]:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            targets = fetch_json(f"http://127.0.0.1:{port}/json/list")
            if targets:
                return targets
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    raise TimeoutError(f"CDP target list unavailable on 127.0.0.1:{port}: {last_error}")


def select_codex_page_target(targets: list[dict[str, Any]]) -> dict[str, Any]:
    for target in targets:
        if target.get("type") == "page" and str(target.get("url", "")).startswith("app://-/index.html"):
            if target.get("webSocketDebuggerUrl"):
                return target
    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target
    raise RuntimeError("No debuggable Codex page target found")


def encode_ws_frame(payload: bytes, mask: bytes | None = None) -> bytes:
    mask = mask if mask is not None else os.urandom(4)
    if len(mask) != 4:
        raise ValueError("mask must be four bytes")
    header = bytearray([0x81])
    size = len(payload)
    if size < 126:
        header.append(0x80 | size)
    elif size < 65536:
        header.extend([0x80 | 126])
        header.extend(struct.pack("!H", size))
    else:
        header.extend([0x80 | 127])
        header.extend(struct.pack("!Q", size))
    header.extend(mask)
    return bytes(header) + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def decode_ws_frame_for_test(frame: bytes) -> bytes:
    b2 = frame[1]
    offset = 2
    size = b2 & 0x7F
    if size == 126:
        size = struct.unpack("!H", frame[offset : offset + 2])[0]
        offset += 2
    elif size == 127:
        size = struct.unpack("!Q", frame[offset : offset + 8])[0]
        offset += 8
    mask = frame[offset : offset + 4]
    offset += 4
    payload = frame[offset : offset + size]
    return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


class CdpClient:
    def __init__(self, websocket_url: str):
        parsed = urllib.parse.urlparse(websocket_url)
        if parsed.hostname is None or parsed.port is None:
            raise ValueError(f"Invalid websocket URL: {websocket_url}")
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = self.socket.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {response[:200]!r}")
        self.next_id = 0

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        payload = json.dumps({"id": request_id, "method": method, "params": params or {}}, separators=(",", ":")).encode("utf-8")
        self.socket.sendall(encode_ws_frame(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = self.recv(timeout=max(0.1, min(1.0, deadline - time.time())))
            except socket.timeout:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message
        raise TimeoutError(f"Timed out waiting for {method}")

    def recv(self, timeout: float = 1.0) -> dict[str, Any]:
        self.socket.settimeout(timeout)
        header = self._read_exact(2)
        first, second = header
        opcode = first & 0x0F
        if opcode == 8:
            raise EOFError("CDP websocket closed")
        masked = second & 0x80
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self._read_exact(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(size)
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return json.loads(payload.decode("utf-8"))

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.socket.recv(size - len(chunks))
            if not chunk:
                raise EOFError("CDP websocket closed")
            chunks.extend(chunk)
        return bytes(chunks)


def build_injection_source(
    renderer_path: Path,
) -> str:
    renderer_source = renderer_path.read_text(encoding="utf-8")
    return f"""
(() => {{
  try {{
    if (typeof window.__codexPerfFastLoaderStop === "function") {{
      window.__codexPerfFastLoaderStop();
    }}
  }} catch (error) {{
    console.warn("[codex-perf] previous fast loader cleanup failed", error);
  }}
  const module = {{ exports: {{}} }};
  const exports = module.exports;
{renderer_source}
  const rendererPatch = module.exports.default || module.exports;
  const api = {{
    process: "renderer",
    log: {{
      debug: (...args) => console.debug("[codex-perf-fast-loader]", ...args),
      info: (...args) => console.info("[codex-perf-fast-loader]", ...args),
      warn: (...args) => console.warn("[codex-perf-fast-loader]", ...args),
      error: (...args) => console.error("[codex-perf-fast-loader]", ...args),
    }},
  }};
  let runtime = null;
  if (rendererPatch && typeof rendererPatch.start === "function") {{
    runtime = rendererPatch.start(api);
  }}
  window.__codexPerfFastLoaderStop = () => {{
    try {{ runtime && typeof runtime.stop === "function" && runtime.stop(); }} catch (error) {{ console.warn("[codex-perf] runtime stop failed", error); }}
    try {{ rendererPatch && typeof rendererPatch.stop === "function" && rendererPatch.stop(); }} catch (error) {{ console.warn("[codex-perf] renderer patch stop failed", error); }}
    delete window.__codexPerfFastLoaderStop;
  }};
  window.__codexPerfFastLoaderInjected = {{
    patchId: "{PATCH_ID}",
    injectedAt: Date.now(),
    source: "cdp-wrapper",
  }};
  return window.__codexPerfFastLoaderInjected;
}})();
"""


def launch_codex(app_path: Path, port: int, workspace: Path | None) -> None:
    current_system = platform.system()
    if current_system == "Darwin":
        launch_macos_codex(app_path, port)
    elif current_system == "Windows":
        launch_windows_codex(app_path, port)
    else:
        raise RuntimeError(f"Codex Desktop CDP launch is not implemented for {current_system}")
    if workspace is not None:
        time.sleep(1)
        subprocess.run(["codex", "app", str(workspace)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch_macos_codex(app_path: Path, port: int) -> None:
    subprocess.run(
        [
            "open",
            "-a",
            str(app_path),
            "--args",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
        ],
        check=True,
    )


def launch_windows_codex(app_path: Path, port: int) -> None:
    executable = resolve_windows_app_executable(app_path)
    if not executable.exists() or executable.is_dir():
        candidates = "\n  ".join(str(path) for path in windows_app_path_candidates())
        hint = f" Tried:\n  {candidates}" if candidates else ""
        raise RuntimeError(f"Codex Desktop executable not found: {app_path}.{hint}")
    subprocess.Popen(
        [
            str(executable),
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
        ],
        cwd=str(executable.parent),
        close_fds=True,
    )


def inject_renderer_patch(client: CdpClient, source: str) -> dict[str, Any]:
    client.call("Runtime.enable")
    client.call("Page.enable")
    client.call("Performance.enable")
    add_result = client.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})
    eval_result = client.call("Runtime.evaluate", {"expression": source, "returnByValue": True, "awaitPromise": True})
    return {"new_document_script": add_result.get("result", {}), "runtime_eval": eval_result.get("result", {})}


def stop_existing_renderer_patch(client: CdpClient) -> dict[str, Any]:
    client.call("Runtime.enable")
    expression = f"""
(() => {{
  const hadPatch = typeof window.__codexPerfFastLoaderStop === "function";
  if (hadPatch) {{
    window.__codexPerfFastLoaderStop();
  }}
  delete window.__codexPerfFastLoaderInjected;
  delete window.__codexPerfFastLoaderStats;
  return {{
    hadPatch,
    stillInjected: Boolean(window.__codexPerfFastLoaderInjected),
    patchId: "{PATCH_ID}",
  }};
}})();
"""
    result = client.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
    return result.get("result", {}).get("result", {}).get("value", {})


def get_performance_metric(client: CdpClient, name: str) -> float | None:
    perf = client.call("Performance.getMetrics").get("result", {}).get("metrics", [])
    for item in perf:
        if item.get("name") == name:
            return item.get("value")
    return None


def setup_measurement(client: CdpClient) -> None:
    client.call("Runtime.enable")
    client.call("Performance.enable")
    expression = """
(() => {
  try {
    if (window.__codexPerfCdpMetricsAbort) {
      window.__codexPerfCdpMetricsAbort.abort();
    }
  } catch {}
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  window.__codexPerfCdpMetricsAbort = controller;
  window.__codexPerfCdpMetrics = { events: [], longTasks: [], startedAt: performance.now() };
  ["navigation-start", "first-paint", "navigation-end"].forEach((name) => {
    window.addEventListener("codex-perf-thread-fastpath:" + name, (event) => {
      window.__codexPerfCdpMetrics.events.push({ name, t: performance.now(), detail: event.detail || {} });
    }, controller ? { signal: controller.signal } : undefined);
  });
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        window.__codexPerfCdpMetrics.longTasks.push({
          name: entry.name,
          start: entry.startTime,
          duration: entry.duration,
        });
      }
    }).observe({ entryTypes: ["longtask"] });
  } catch (error) {
    window.__codexPerfCdpMetrics.longTaskObserverError = String(error);
  }
  return true;
})();
"""
    client.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})


def click_thread_row(client: CdpClient, selector: str | None) -> dict[str, Any]:
    if selector:
        expression = f"""
(() => {{
  const target = document.querySelector({json.dumps(selector)});
  if (!target) return {{ clicked: false, reason: "selector-not-found", selector: {json.dumps(selector)} }};
  target.scrollIntoView({{ block: "center" }});
  target.click();
  return {{ clicked: true, selector: {json.dumps(selector)}, text: (target.innerText || "").slice(0, 200) }};
}})();
"""
    else:
        expression = """
(() => {
  const rows = Array.from(document.querySelectorAll("[role='button']"))
    .filter((node) => (node.innerText || "").length > 40);
  const target = rows[1] || rows[0];
  if (!target) return { clicked: false, reason: "no-thread-row", rowCount: rows.length };
  target.scrollIntoView({ block: "center" });
  target.click();
  return { clicked: true, rowCount: rows.length, text: (target.innerText || "").slice(0, 200) };
})();
"""
    result = client.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    return result["result"]["result"].get("value", {})


def wait_for_click_target(client: CdpClient, selector: str | None, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_state: dict[str, Any] = {}
    if selector:
        expression = f"""
(() => {{
  const target = document.querySelector({json.dumps(selector)});
  return {{ ready: Boolean(target), selector: {json.dumps(selector)}, text: target ? (target.innerText || "").slice(0, 120) : "" }};
}})();
"""
    else:
        expression = """
(() => {
  const rows = Array.from(document.querySelectorAll("[role='button']"))
    .filter((node) => (node.innerText || "").length > 40);
  return { ready: rows.length > 0, rowCount: rows.length, text: rows[0] ? (rows[0].innerText || "").slice(0, 120) : "" };
})();
"""
    while time.time() < deadline:
        try:
            value = client.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout=5)
            last_state = value["result"]["result"].get("value", {})
            if last_state.get("ready"):
                return last_state
        except Exception as exc:
            last_state = {"ready": False, "error": str(exc)}
        time.sleep(0.25)
    return {"ready": False, "timeout_seconds": timeout, "last_state": last_state}


def collect_renderer_metrics(
    client: CdpClient,
    click_result: dict[str, Any],
    port: int,
    injected: bool,
    js_heap_before_bytes: float | None,
) -> dict[str, Any]:
    js_heap_after_bytes = get_performance_metric(client, "JSHeapUsedSize")
    js_heap_post_gc_bytes = None
    try:
        client.call("HeapProfiler.enable")
        client.call("HeapProfiler.collectGarbage")
        js_heap_post_gc_bytes = get_performance_metric(client, "JSHeapUsedSize")
    except Exception:
        js_heap_post_gc_bytes = None
    expression = """
(() => {
  const data = window.__codexPerfCdpMetrics || { events: [], longTasks: [] };
  const marks = performance.getEntriesByType("mark")
    .filter((entry) => entry.name.includes("codex-perf-fast-thread-loader"))
    .map((entry) => ({ name: entry.name, startTime: entry.startTime }));
  const navStart = data.events.find((event) => event.name === "navigation-start");
  const firstVisible = navStart
    ? data.events.find((event) => event.name === "first-paint" && event.t >= navStart.t)
    : null;
  const navEnd = navStart
    ? data.events.find((event) => event.name === "navigation-end" && event.t >= navStart.t)
    : null;
  const longTasks = data.longTasks || [];
  const patchStats = window.__codexPerfFastLoaderStats || null;
  const olderTurnsLoadedAutomatically = patchStats
    ? Boolean(
        patchStats.olderTurnPagesObserved > 0 ||
        patchStats.olderTurnControlClicks > 0 ||
        patchStats.lastOlderTurnSignalAt
      )
    : null;
  return {
    events: data.events || [],
    marks,
    longTaskCount: longTasks.length,
    totalLongTaskDurationMs: longTasks.reduce((sum, entry) => sum + entry.duration, 0),
    maxLongTaskDurationMs: longTasks.reduce((max, entry) => Math.max(max, entry.duration), 0),
    firstVisibleContentMs: navStart && firstVisible ? firstVisible.t - navStart.t : null,
    settledThreadShellMs: navStart && navEnd ? navEnd.t - navStart.t : null,
    articleCount: document.querySelectorAll("article,[data-codex-thread-turn],[data-message-author-role]").length,
    fastpathAttribute: document.documentElement.getAttribute("data-codex-perf-thread-fastpath"),
    injected: window.__codexPerfFastLoaderInjected || null,
    patchStats,
    olderTurnsLoadedAutomatically,
    bodyPreview: document.body.innerText.slice(0, 500),
  };
})();
"""
    value = client.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})["result"]["result"]["value"]
    return {
        "phase": "thread-loading",
        "timestamp": utc_now(),
        "patch": "cdp-wrapper",
        "cdp_port": port,
        "injected": injected,
        "description": (
            "CDP wrapper injected fast-loader script into Codex Desktop"
            if injected
            else "CDP wrapper attached without injection for baseline measurement"
        ),
        "click_result": click_result,
        "cdp_first_visible_content_ms": value.get("firstVisibleContentMs"),
        "cdp_settled_thread_shell_ms": value.get("settledThreadShellMs"),
        "long_task_count": value.get("longTaskCount"),
        "total_long_task_duration_ms": round(value.get("totalLongTaskDurationMs") or 0, 3),
        "max_long_task_duration_ms": round(value.get("maxLongTaskDurationMs") or 0, 3),
        "js_heap_after_bytes": js_heap_after_bytes,
        "js_heap_before_bytes": js_heap_before_bytes,
        "js_heap_post_gc_bytes": js_heap_post_gc_bytes,
        "transient_heap_spike_bytes": (
            js_heap_after_bytes - js_heap_before_bytes
            if js_heap_after_bytes is not None and js_heap_before_bytes is not None
            else None
        ),
        "older_turns_loaded_automatically": value.get("olderTurnsLoadedAutomatically"),
        "patch_stats": value.get("patchStats"),
        "article_count": value.get("articleCount"),
        "performance_marks": value.get("marks", []),
        "fastpath_events": value.get("events", []),
        "injection_state": value.get("injected"),
        "note": "Measured through a localhost CDP wrapper attached to Codex Desktop.",
    }


def write_outputs(output_dir: Path, metrics: dict[str, Any], inject_result: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics = {**metrics, "inject_result": inject_result}
    (output_dir / "metrics-after-thread-loading-workaround.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = [
        "# Codex Perf CDP Wrapper Summary",
        "",
        f"Generated: {utc_now()}",
        "",
        f"- Patch: `{metrics.get('patch')}`",
        f"- CDP port: `{metrics.get('cdp_port')}`",
        f"- Injected: `{metrics.get('injected')}`",
        f"- First visible content: `{metrics.get('cdp_first_visible_content_ms')}` ms",
        f"- Settled thread shell: `{metrics.get('cdp_settled_thread_shell_ms')}` ms",
        f"- Long task count: `{metrics.get('long_task_count')}`",
        f"- Total long task duration: `{metrics.get('total_long_task_duration_ms')}` ms",
        f"- Max long task duration: `{metrics.get('max_long_task_duration_ms')}` ms",
        f"- JS heap before: `{metrics.get('js_heap_before_bytes')}` bytes",
        f"- JS heap after: `{metrics.get('js_heap_after_bytes')}` bytes",
        f"- JS heap post-GC: `{metrics.get('js_heap_post_gc_bytes')}` bytes",
        f"- Transient heap spike: `{metrics.get('transient_heap_spike_bytes')}` bytes",
        "",
        (
            "The wrapper does not mutate `~/.codex/state_5.sqlite`, rollout JSONL, or `session_index.jsonl`; "
            "it only launches Codex Desktop with a localhost CDP port and injects renderer JavaScript."
            if metrics.get("injected")
            else
            "The wrapper does not mutate `~/.codex/state_5.sqlite`, rollout JSONL, or `session_index.jsonl`; "
            "this baseline run launched Codex Desktop with a localhost CDP port without injecting renderer JavaScript."
        ),
        "",
    ]
    (output_dir / "metrics-summary.md").write_text("\n".join(summary), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    port = find_free_port() if args.port == 0 else args.port
    app_path = Path(args.app_path).expanduser()
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None
    if args.launch:
        launch_codex(app_path, port, workspace)
    targets = wait_for_targets(port, args.timeout)
    target = select_codex_page_target(targets)
    client = CdpClient(target["webSocketDebuggerUrl"])
    try:
        source = build_injection_source(Path(args.renderer_js).expanduser())
        inject_result = {}
        if args.inject:
            inject_result = inject_renderer_patch(client, source)
        else:
            inject_result = {"stopped_existing_patch": stop_existing_renderer_patch(client)}
        metrics = {}
        if args.measure:
            readiness = wait_for_click_target(client, args.click_selector, args.pre_click_timeout)
            setup_measurement(client)
            js_heap_before_bytes = get_performance_metric(client, "JSHeapUsedSize")
            click_result = click_thread_row(client, args.click_selector)
            click_result = {"readiness": readiness, **click_result}
            time.sleep(args.wait_seconds)
            metrics = collect_renderer_metrics(client, click_result, port, args.inject, js_heap_before_bytes)
            write_outputs(Path(args.output_dir).expanduser(), metrics, inject_result)
        if args.measure:
            print(json.dumps({
                "port": port,
                "target": {"id": target.get("id"), "url": target.get("url"), "title": target.get("title")},
                "injected": bool(args.inject),
                "measured": True,
                "output_dir": str(Path(args.output_dir).expanduser()),
            }, indent=2, sort_keys=True))
    finally:
        client.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_CDP_PORT, help=f"Local CDP port, or 0 for a free port. Default: {DEFAULT_CDP_PORT}")
    parser.add_argument("--app-path", default=default_app_path(), help="Path to Codex Desktop app bundle or executable")
    parser.add_argument("--workspace", default=str(Path.cwd()), help="Workspace to open with Codex Desktop")
    parser.add_argument("--renderer-js", default=str(default_renderer_path()), help="Renderer JavaScript file to inject")
    parser.add_argument("--output-dir", default=str(default_output_dir()), help="Directory for CDP metrics artifacts")
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for CDP target")
    parser.add_argument("--wait-seconds", type=float, default=5.0, help="Seconds to wait after triggering navigation")
    parser.add_argument("--pre-click-timeout", type=float, default=10.0, help="Seconds to wait for a clickable thread row before measuring")
    parser.add_argument("--click-selector", help="Optional CSS selector to click for measurement")
    parser.add_argument("--no-launch", dest="launch", action="store_false", help="Attach to an existing CDP-enabled Codex app")
    parser.add_argument("--no-inject", dest="inject", action="store_false", help="Measure without injecting the fast-loader script")
    parser.add_argument("--no-measure", dest="measure", action="store_false", help="Only launch and inject")
    parser.set_defaults(launch=True, inject=True, measure=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
