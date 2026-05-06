import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "renderer" / "fast-thread-loader.js"


class RendererPatchTests(unittest.TestCase):
    def test_renderer_patch_has_guardrails_and_no_state_mutation(self):
        code = PATCH.read_text(encoding="utf-8")
        for required in [
            "KILL_SWITCH_KEY",
            "navigation-start",
            "first-paint",
            "navigation-end",
            "requestIdleCallback",
            "content-visibility: auto",
            "stop()",
            "removeEventListener",
            "MutationObserver",
            "patchElectronBridge",
            "subscribeToWorkerMessages",
            "thread/turns/list",
            "older-turns-loaded",
            "autoLoadOlderTurnControls",
            "data-app-action-sidebar-thread-row",
        ]:
            self.assertIn(required, code)
        for forbidden in ["state_5.sqlite", "session_index.jsonl", "ThreadNameUpdated"]:
            self.assertNotIn(forbidden, code)

    def test_renderer_patch_targets_current_codex_dom_and_thread_history_signals(self):
        code = PATCH.read_text(encoding="utf-8")
        for selector in [
            "[data-app-action-sidebar-thread-row]",
            "[data-app-action-sidebar-thread-title]",
            "[data-thread-find-composer]",
            "main article",
        ]:
            self.assertIn(selector, code)
        for signal in [
            "thread/read",
            "includeTurns",
            "thread/turns/list",
            "nextCursor",
            "olderCursor",
        ]:
            self.assertIn(signal, code)

    def test_thread_clicks_keep_native_navigation_and_only_prefetch(self):
        code = PATCH.read_text(encoding="utf-8")
        for required in [
            "requestAppAction",
            "prefetchNativeThreadPage",
            "debug-run-app-action-request",
            "debug-run-app-action-response",
            "threads.read",
            "threads.set_title",
            "native-thread-prefetch",
        ]:
            self.assertIn(required, code)
        for forbidden in [
            "startLightweightThreadView",
            "renderLightweightShell",
            "LIGHTWEIGHT_VIEW_ID",
            "preventDefault",
            "stopImmediatePropagation",
            "threadDataUrl",
            "fetch(",
            "lightweightEndpointAvailable",
            "127.0.0.1",
        ]:
            self.assertNotIn(forbidden, code)

    def test_title_repair_runs_periodic_guarded_app_action_loop(self):
        code = PATCH.read_text(encoding="utf-8")
        for required in [
            "TITLE_REPAIR_INTERVAL_MS",
            "TITLE_REPAIR_LIST_LIMIT",
            "schedulePeriodicTitleRepair",
            "runPeriodicTitleRepair",
            "repairTitleFromThreadSummary",
            'type: "threads.list"',
            'type: "threads.set_title"',
            "titleRepairTimer",
            "lastTitleRepairByThread",
        ]:
            self.assertIn(required, code)


if __name__ == "__main__":
    unittest.main()
