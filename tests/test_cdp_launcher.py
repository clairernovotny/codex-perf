import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-perf-launch.py"
PATCH = ROOT / "renderer" / "fast-thread-loader.js"


def load_launcher():
    spec = importlib.util.spec_from_file_location("codex_perf_launch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CdpLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = load_launcher()

    def test_selects_codex_page_target(self):
        targets = [
            {"type": "worker", "url": "", "webSocketDebuggerUrl": "ws://worker"},
            {"type": "page", "url": "https://example.test", "webSocketDebuggerUrl": "ws://wrong"},
            {"type": "page", "url": "app://-/index.html?hostId=local", "webSocketDebuggerUrl": "ws://codex"},
        ]
        self.assertEqual(self.launcher.select_codex_page_target(targets)["webSocketDebuggerUrl"], "ws://codex")

    def test_default_cdp_port_avoids_chrome_default(self):
        self.assertEqual(self.launcher.DEFAULT_CDP_PORT, 17373)
        self.assertNotEqual(self.launcher.DEFAULT_CDP_PORT, 9222)

    def test_websocket_frame_masking_roundtrip(self):
        payload = b'{"id":1,"method":"Runtime.enable"}'
        frame = self.launcher.encode_ws_frame(payload, mask=b"\x01\x02\x03\x04")
        self.assertEqual(frame[0], 0x81)
        self.assertTrue(frame[1] & 0x80)
        self.assertEqual(self.launcher.decode_ws_frame_for_test(frame), payload)

    def test_injection_source_wraps_renderer_patch_without_state_mutation(self):
        source = self.launcher.build_injection_source(PATCH)
        self.assertIn("window.__codexPerfFastLoaderStop", source)
        self.assertIn("module.exports", source)
        self.assertIn("start(api)", source)
        self.assertIn('source: "cdp-wrapper"', source)
        self.assertIn("navigation-start", source)
        for forbidden in ["state_5.sqlite", "session_index.jsonl", "ThreadNameUpdated"]:
            self.assertNotIn(forbidden, source)

    def test_no_inject_path_stops_existing_renderer_patch(self):
        outer = self

        class FakeClient:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None, timeout=30.0):
                self.calls.append((method, params or {}))
                if method == "Runtime.evaluate":
                    expression = params["expression"]
                    outer.assertIn("__codexPerfFastLoaderStop", expression)
                    return {"result": {"result": {"value": {"hadPatch": True, "stillInjected": False}}}}
                return {"result": {}}

        client = FakeClient()
        result = self.launcher.stop_existing_renderer_patch(client)
        self.assertTrue(result["hadPatch"])
        self.assertEqual([method for method, _ in client.calls], ["Runtime.enable", "Runtime.evaluate"])

    def test_preloaded_thread_data_reads_recent_rollout_without_state_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_dir = home / "sessions" / "2026" / "01" / "02"
            session_dir.mkdir(parents=True)
            thread_id = "019abc00-0000-7000-8000-000000000002"
            rollout = session_dir / f"rollout-test-{thread_id}.jsonl"
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps({"timestamp": "2026-01-02T00:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello"}}),
                        json.dumps({"timestamp": "2026-01-02T00:00:01Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "world" * 20}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            con = sqlite3.connect(home / "state_5.sqlite")
            con.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    first_user_message TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    rollout_path TEXT NOT NULL,
                    updated_at_ms INTEGER,
                    archived INTEGER DEFAULT 0
                )
                """
            )
            con.execute(
                "INSERT INTO threads (id,title,first_user_message,cwd,rollout_path,updated_at_ms,archived) VALUES (?,?,?,?,?,?,0)",
                (thread_id, "fixture title", "hello", str(ROOT), str(rollout), 2),
            )
            con.commit()
            con.close()

            data = self.launcher.ThreadDataStore(home).preloaded_recent_threads(5, 10, 16)
            self.assertIn(thread_id, data["threads"])
            page = data["threads"][thread_id]
            self.assertEqual(page["thread"]["title"], "fixture title")
            self.assertEqual(len(page["turns"]), 2)
            self.assertTrue(page["turns"][1]["text"].endswith("[truncated view]"))


if __name__ == "__main__":
    unittest.main()
