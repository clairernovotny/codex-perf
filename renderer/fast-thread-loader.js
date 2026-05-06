/* global window, document, performance, requestAnimationFrame, requestIdleCallback, cancelIdleCallback */

const PATCH_ID = "codex-perf-fast-thread-loader";
const KILL_SWITCH_KEY = `${PATCH_ID}:disabled`;
const STYLE_ID = `${PATCH_ID}:style`;
const NAV_TIMEOUT_MS = 8000;
const IDLE_GRACE_MS = 250;
const OLDER_CONTROL_COOLDOWN_MS = 2000;
const APP_ACTION_TIMEOUT_MS = 5000;
const NATIVE_THREAD_PAGE_LIMIT = 10;
const TITLE_MAX_LEN = 120;
const TITLE_REPAIR_INTERVAL_MS = 30000;
const TITLE_REPAIR_COOLDOWN_MS = 60000;
const TITLE_REPAIR_LIST_LIMIT = 50;
const THREAD_ROW_SELECTOR = [
  "[data-app-action-sidebar-thread-row]",
  "[data-thread-id]",
  "[data-testid*='thread' i]"
].join(", ");
const THREAD_FIND_COMPOSER_SELECTOR = "[data-thread-find-composer]";
const THREAD_CONTENT_SELECTOR = [
  "[data-codex-thread-turn]",
  "[data-message-author-role]",
  THREAD_FIND_COMPOSER_SELECTOR,
  "main article"
].join(", ");
const OLDER_TURN_CONTROL_SELECTOR = [
  "[data-codex-load-older-turns]",
  "[data-app-action-load-older-turns]",
  "[data-testid*='load-older']",
  "button",
  "[role='button']",
].join(",");
let activeRuntime = null;

function nowMark(name) {
  if (typeof performance !== "undefined" && performance.mark) {
    performance.mark(`${PATCH_ID}:${name}`);
  }
}

function emit(name, detail = {}) {
  if (typeof window === "undefined" || typeof window.dispatchEvent !== "function") {
    return;
  }
  window.dispatchEvent(new CustomEvent(`codex-perf-thread-fastpath:${name}`, {
    detail: { patchId: PATCH_ID, timestamp: Date.now(), ...detail }
  }));
}

function storageDisabled() {
  try {
    return window.localStorage && window.localStorage.getItem(KILL_SWITCH_KEY) === "1";
  } catch {
    return true;
  }
}

function installStyle() {
  if (!document || document.getElementById(STYLE_ID)) {
    return null;
  }
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    [data-codex-thread-turn],
    [data-message-author-role],
    [data-thread-find-composer],
    main article {
      contain: layout paint style;
      content-visibility: auto;
      contain-intrinsic-size: auto 160px;
    }

    [data-app-action-sidebar-thread-row],
    [data-app-action-sidebar-thread-title] {
      contain: layout style;
      content-visibility: auto;
      contain-intrinsic-size: auto 36px;
    }

    html[data-codex-perf-thread-fastpath="1"] [data-codex-preview-capture],
    html[data-codex-perf-thread-fastpath="1"] [data-codex-perf-preview-capture] {
      content-visibility: hidden;
    }
  `;
  document.head.appendChild(style);
  return style;
}

function createRuntime() {
  const cleanup = [];
  let navigationTimer = null;
  let idleTimer = null;
  let olderTurnTimer = null;
  let active = false;
  let stopped = false;
  let bridgePatched = false;
  let bridgePatchAttempts = 0;
  let appActionRequestSeq = 0;
  let titleRepairTimer = null;
  let titleRepairInFlight = false;
  const lastTitleRepairByThread = new Map();
  const stats = {
    bridgeRequests: 0,
    bridgeResponses: 0,
    includeTurnsReads: 0,
    turnsListRequests: 0,
    olderTurnPagesObserved: 0,
    olderTurnControlClicks: 0,
    lastOlderTurnSignalAt: null,
    nativeThreadPrefetches: 0,
    nativeThreadPrefetchFailures: 0,
    nativeAppActionRequests: 0,
    nativeAppActionResponses: 0,
    nativeAppActionFailures: 0,
    titleRepairQueued: 0,
    titleRepairSucceeded: 0,
    titleRepairFailed: 0,
    titleRepairPeriodicRuns: 0,
  };

  try {
    window.__codexPerfFastLoaderStats = stats;
  } catch {
    // Metrics are best-effort only.
  }

  function clearTimers() {
    if (navigationTimer !== null) {
      window.clearTimeout(navigationTimer);
      navigationTimer = null;
    }
    if (idleTimer !== null) {
      if (typeof cancelIdleCallback === "function") {
        cancelIdleCallback(idleTimer);
      } else {
        window.clearTimeout(idleTimer);
      }
      idleTimer = null;
    }
    if (olderTurnTimer !== null) {
      if (typeof cancelIdleCallback === "function") {
        cancelIdleCallback(olderTurnTimer);
      } else {
        window.clearTimeout(olderTurnTimer);
      }
      olderTurnTimer = null;
    }
  }

  function clearTitleRepairTimer() {
    if (titleRepairTimer !== null) {
      window.clearTimeout(titleRepairTimer);
      titleRepairTimer = null;
    }
  }

  function log(level, message, detail) {
    const logger = activeRuntime && activeRuntime.api && activeRuntime.api.log && activeRuntime.api.log[level];
    if (typeof logger === "function") {
      logger(message, detail || {});
    }
  }

  function endNavigation(reason) {
    if (!active || stopped) {
      return;
    }
    active = false;
    clearTimers();
    document.documentElement.removeAttribute("data-codex-perf-thread-fastpath");
    nowMark("navigation-end");
    emit("navigation-end", { reason });
    log("info", "navigation-end", { reason });
  }

  function firstPaint() {
    if (!active || stopped) {
      return;
    }
    nowMark("first-paint");
    emit("first-paint");
    log("info", "first-paint");
    const finish = () => endNavigation("idle-grace");
    if (typeof requestIdleCallback === "function") {
      idleTimer = requestIdleCallback(finish, { timeout: IDLE_GRACE_MS });
    } else {
      idleTimer = window.setTimeout(finish, IDLE_GRACE_MS);
    }
  }

  function beginNavigation(reason) {
    if (stopped || storageDisabled()) {
      return;
    }
    active = true;
    clearTimers();
    document.documentElement.setAttribute("data-codex-perf-thread-fastpath", "1");
    nowMark("navigation-start");
    emit("navigation-start", { reason });
    log("info", "navigation-start", { reason });
    requestAnimationFrame(() => requestAnimationFrame(firstPaint));
    scheduleOlderTurnLoad();
    navigationTimer = window.setTimeout(() => endNavigation("timeout"), NAV_TIMEOUT_MS);
  }

  function onClick(event) {
    const row = event.target && event.target.closest
      ? event.target.closest("[data-app-action-sidebar-thread-row]")
      : null;
    if (row) {
      prefetchNativeThreadPage(row);
    }
    const target = event.target && event.target.closest
      ? event.target.closest(`a,button,[role='button'],[data-thread-id],[data-testid*='thread'],${THREAD_ROW_SELECTOR}`)
      : null;
    if (target) {
      beginNavigation("activation");
    }
  }

  function getThreadIdFromRow(row) {
    const raw = row.getAttribute("data-app-action-sidebar-thread-id") || row.getAttribute("data-thread-id") || "";
    return raw.startsWith("local:") ? raw.slice("local:".length) : raw;
  }

  function requestAppAction(action, timeoutMs = APP_ACTION_TIMEOUT_MS) {
    if (stopped || typeof window.postMessage !== "function") {
      return Promise.reject(new Error("app action bus is unavailable"));
    }
    stats.nativeAppActionRequests += 1;
    const requestId = `${PATCH_ID}:${Date.now()}:${++appActionRequestSeq}`;
    const targetOrigin = window.location.origin && window.location.origin !== "null"
      ? window.location.origin
      : "*";
    const payload = {
      type: "debug-run-app-action-request",
      requestId,
      action,
    };
    return new Promise((resolve, reject) => {
      let settled = false;
      const cleanupListeners = () => {
        window.removeEventListener("message", onMessage, true);
        window.removeEventListener("codex-message-from-view", onCustomEvent, true);
        window.clearTimeout(timer);
      };
      const settle = (fn, value) => {
        if (settled) {
          return;
        }
        settled = true;
        cleanupListeners();
        fn(value);
      };
      const handleResponse = (message) => {
        if (!message || message.type !== "debug-run-app-action-response" || message.requestId !== requestId) {
          return;
        }
        stats.nativeAppActionResponses += 1;
        if (message.ok) {
          settle(resolve, message.result);
        } else {
          stats.nativeAppActionFailures += 1;
          settle(reject, new Error(message.errorMessage || "app action failed"));
        }
      };
      const onMessage = (event) => handleResponse(event.data);
      const onCustomEvent = (event) => handleResponse(event.detail);
      const timer = window.setTimeout(() => {
        stats.nativeAppActionFailures += 1;
        settle(reject, new Error(`app action timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      window.addEventListener("message", onMessage, true);
      window.addEventListener("codex-message-from-view", onCustomEvent, true);
      window.postMessage(payload, targetOrigin);
    });
  }

  async function prefetchNativeThreadPage(row) {
    const threadId = getThreadIdFromRow(row);
    if (!threadId || stopped || storageDisabled()) {
      return;
    }
    try {
      await requestAppAction({
        type: "threads.read",
        threadId,
        limit: NATIVE_THREAD_PAGE_LIMIT,
        includeOutputs: false,
        maxOutputChars: 2000,
      }, 8000);
      stats.nativeThreadPrefetches += 1;
      emit("native-thread-prefetch", { threadId });
    } catch (error) {
      stats.nativeThreadPrefetchFailures += 1;
      log("debug", "native-thread-prefetch-failed", { threadId, error: String(error && error.message || error) });
    }
  }

  function truncateTitle(text) {
    return text.length > TITLE_MAX_LEN ? `${text.slice(0, Math.max(TITLE_MAX_LEN - 3, 0))}...` : text;
  }

  function summarizeTitle(text) {
    let firstLine = String(text == null ? "" : text).split("\n", 1)[0];
    if (firstLine.endsWith("\r")) {
      firstLine = firstLine.slice(0, -1);
    }
    return truncateTitle(firstLine.trim());
  }

  function repairTitleForThreadSummary(thread) {
    if (!thread || typeof thread !== "object") {
      return null;
    }
    const currentTitle = String(thread.title || "").trim();
    const preview = String(thread.preview || "").trim();
    const source = preview || currentTitle;
    const repairedTitle = summarizeTitle(source);
    if (!thread.id || !repairedTitle || currentTitle === repairedTitle) {
      return null;
    }
    const titleIsLong = currentTitle.length > TITLE_MAX_LEN;
    const titleIsPreviewFallback = preview.length > TITLE_MAX_LEN && currentTitle === preview;
    return titleIsLong || titleIsPreviewFallback ? { threadId: thread.id, title: repairedTitle } : null;
  }

  async function repairTitleFromThreadSummary(thread) {
    const item = repairTitleForThreadSummary(thread);
    if (!item) {
      return false;
    }
    const last = lastTitleRepairByThread.get(item.threadId);
    const now = Date.now();
    if (last && last.title === item.title && now - last.at < TITLE_REPAIR_COOLDOWN_MS) {
      return false;
    }
    lastTitleRepairByThread.set(item.threadId, { title: item.title, at: now });
    try {
      await requestAppAction({
        type: "threads.set_title",
        threadId: item.threadId,
        title: item.title,
      }, 8000);
      stats.titleRepairSucceeded += 1;
      return true;
    } catch (error) {
      stats.titleRepairFailed += 1;
      log("warn", "periodic-title-repair-failed", { threadId: item.threadId, error: String(error && error.message || error) });
      return false;
    }
  }

  async function runPeriodicTitleRepair() {
    if (stopped || storageDisabled() || titleRepairInFlight) {
      return;
    }
    titleRepairInFlight = true;
    stats.titleRepairPeriodicRuns += 1;
    try {
      const data = await requestAppAction({
        type: "threads.list",
        limit: TITLE_REPAIR_LIST_LIMIT,
      }, 8000);
      const threads = Array.isArray(data?.threads) ? data.threads : [];
      for (const thread of threads) {
        if (stopped) {
          break;
        }
        await repairTitleFromThreadSummary(thread);
      }
    } catch (error) {
      stats.titleRepairFailed += 1;
      log("debug", "periodic-title-repair-list-failed", { error: String(error && error.message || error) });
    } finally {
      titleRepairInFlight = false;
    }
  }

  function schedulePeriodicTitleRepair(initialDelayMs = TITLE_REPAIR_INTERVAL_MS) {
    clearTitleRepairTimer();
    if (stopped || storageDisabled()) {
      return;
    }
    titleRepairTimer = window.setTimeout(async () => {
      titleRepairTimer = null;
      await runPeriodicTitleRepair();
      schedulePeriodicTitleRepair(TITLE_REPAIR_INTERVAL_MS);
    }, initialDelayMs);
  }

  function onPopState() {
    beginNavigation("history");
  }

  function patchHistory(method) {
    const original = window.history && window.history[method];
    if (typeof original !== "function") {
      return;
    }
    window.history[method] = function patchedHistory() {
      const result = original.apply(this, arguments);
      beginNavigation(method);
      return result;
    };
    cleanup.push(() => {
      window.history[method] = original;
    });
  }

  function threadSignalFromMessage(message) {
    const found = {
      method: null,
      includeTurns: false,
      hasCursor: false,
      hasNextCursor: false,
      hasOlderCursor: false,
      itemCount: null,
    };
    const seen = new Set();

    function visit(value, depth) {
      if (!value || depth > 5) {
        return;
      }
      if (typeof value === "string") {
        if (value.includes("thread/turns/list")) {
          found.method = "thread/turns/list";
        } else if (value.includes("thread/read")) {
          found.method = found.method || "thread/read";
        }
        return;
      }
      if (typeof value !== "object" || seen.has(value)) {
        return;
      }
      seen.add(value);

      if (value.method === "thread/turns/list" || value.id?.startsWith?.("thread/turns/list:")) {
        found.method = "thread/turns/list";
      }
      if (value.method === "thread/read" || value.id?.startsWith?.("thread/read:")) {
        found.method = found.method || "thread/read";
      }
      if (value.includeTurns === true || value.params?.includeTurns === true) {
        found.includeTurns = true;
      }
      if (value.cursor != null || value.params?.cursor != null) {
        found.hasCursor = true;
      }
      if (value.nextCursor != null || value.result?.nextCursor != null) {
        found.hasNextCursor = true;
      }
      if (value.olderCursor != null || value.turnsPagination?.olderCursor != null) {
        found.hasOlderCursor = true;
      }

      const data = Array.isArray(value.data) ? value.data : Array.isArray(value.result?.data) ? value.result.data : null;
      if (data) {
        found.itemCount = data.length;
      }
      for (const key of Object.keys(value).slice(0, 24)) {
        visit(value[key], depth + 1);
      }
    }

    visit(message, 0);
    return found.method || found.includeTurns || found.hasNextCursor || found.hasOlderCursor ? found : null;
  }

  function observeThreadSignal(message, direction) {
    const signal = threadSignalFromMessage(message);
    if (!signal) {
      return;
    }
    if (direction === "request") {
      stats.bridgeRequests += 1;
    } else {
      stats.bridgeResponses += 1;
    }
    if (signal.method === "thread/turns/list") {
      if (direction === "request") {
        stats.turnsListRequests += 1;
      } else {
        stats.olderTurnPagesObserved += 1;
      }
    }
    if (signal.method === "thread/read" && signal.includeTurns) {
      stats.includeTurnsReads += 1;
    }
    if (signal.method === "thread/turns/list" || signal.hasNextCursor || signal.hasOlderCursor) {
      stats.lastOlderTurnSignalAt = Date.now();
      nowMark("older-turns-loaded");
      emit("older-turns-loaded", {
        direction,
        method: signal.method,
        hasCursor: signal.hasCursor,
        hasNextCursor: signal.hasNextCursor,
        hasOlderCursor: signal.hasOlderCursor,
        itemCount: signal.itemCount,
      });
    }
  }

  function patchElectronBridge() {
    const bridge = window.electronBridge;
    if (!bridge || typeof bridge !== "object" || bridgePatched) {
      return bridgePatched;
    }
    bridgePatched = true;

    patchBridgeSender(bridge, "sendMessageFromView");
    patchBridgeSender(bridge, "sendWorkerMessageFromView");
    patchBridgeSubscriber(bridge, "subscribeToWorkerMessages");
    emit("bridge-patched", {
      hasSendMessageFromView: typeof bridge.sendMessageFromView === "function",
      hasSendWorkerMessageFromView: typeof bridge.sendWorkerMessageFromView === "function",
      hasSubscribeToWorkerMessages: typeof bridge.subscribeToWorkerMessages === "function",
    });
    return true;
  }

  function patchBridgeSender(bridge, method) {
    const descriptor = Object.getOwnPropertyDescriptor(bridge, method);
    if (descriptor && descriptor.writable === false || typeof bridge[method] !== "function") {
      return;
    }
    const original = bridge[method];
    bridge[method] = function patchedBridgeSender(message) {
      observeThreadSignal(message, "request");
      return original.apply(this, arguments);
    };
    cleanup.push(() => {
      bridge[method] = original;
    });
  }

  function patchBridgeSubscriber(bridge, method) {
    const descriptor = Object.getOwnPropertyDescriptor(bridge, method);
    if (descriptor && descriptor.writable === false || typeof bridge[method] !== "function") {
      return;
    }
    const originalSubscribe = bridge[method];
    bridge[method] = function patchedSubscribeToWorkerMessages() {
      const args = Array.from(arguments).map((arg) => {
        if (typeof arg !== "function") {
          return arg;
        }
        return function patchedWorkerMessage(message) {
          observeThreadSignal(message, "response");
          return arg.apply(this, arguments);
        };
      });
      return originalSubscribe.apply(this, args);
    };
    cleanup.push(() => {
      bridge[method] = originalSubscribe;
    });
  }

  function retryPatchElectronBridge() {
    if (patchElectronBridge() || stopped || bridgePatchAttempts >= 40) {
      return;
    }
    bridgePatchAttempts += 1;
    const timer = window.setTimeout(retryPatchElectronBridge, 100);
    cleanup.push(() => {
      window.clearTimeout(timer);
    });
  }

  function isOlderTurnControl(node) {
    if (!node || node.disabled || node.getAttribute("aria-disabled") === "true") {
      return false;
    }
    const last = Number(node.getAttribute("data-codex-perf-older-autoload-at") || "0");
    if (Number.isFinite(last) && Date.now() - last < OLDER_CONTROL_COOLDOWN_MS) {
      return false;
    }
    const label = [
      node.getAttribute("aria-label"),
      node.getAttribute("title"),
      node.getAttribute("data-app-action"),
      node.textContent,
    ].filter(Boolean).join(" ").toLowerCase();
    if (!/(load|show|fetch).{0,32}(older|previous|earlier|history|turns|messages)|(older|previous|earlier).{0,32}(turns|messages|history)/i.test(label)) {
      return false;
    }
    return !!node.closest(`main,${THREAD_CONTENT_SELECTOR}`);
  }

  function autoLoadOlderTurnControls() {
    if (stopped || storageDisabled()) {
      return 0;
    }
    const controls = Array.from(document.querySelectorAll(OLDER_TURN_CONTROL_SELECTOR));
    let clicked = 0;
    for (const target of controls) {
      if (!isOlderTurnControl(target)) {
        continue;
      }
      try {
        target.setAttribute("data-codex-perf-older-autoload-at", String(Date.now()));
        target.click();
        clicked += 1;
        stats.olderTurnControlClicks += 1;
        stats.lastOlderTurnSignalAt = Date.now();
        nowMark("older-turns-control-clicked");
        emit("older-turns-control-clicked", { clicked });
      } catch (error) {
        log("debug", "older-turn-control-click-failed", { error: String(error) });
      }
      if (clicked >= 2) {
        break;
      }
    }
    return clicked;
  }

  function scheduleOlderTurnLoad() {
    if (olderTurnTimer !== null) {
      return;
    }
    const run = () => {
      olderTurnTimer = null;
      autoLoadOlderTurnControls();
    };
    if (typeof requestIdleCallback === "function") {
      olderTurnTimer = requestIdleCallback(run, { timeout: 1000 });
    } else {
      olderTurnTimer = window.setTimeout(run, 500);
    }
  }

  function observeThreadContent() {
    if (typeof MutationObserver === "undefined") {
      return;
    }
    const observer = new MutationObserver(() => {
      scheduleOlderTurnLoad();
      if (!active) {
        return;
      }
      const visibleTurn = document.querySelector(THREAD_CONTENT_SELECTOR);
      if (visibleTurn) {
        firstPaint();
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    cleanup.push(() => observer.disconnect());
  }

  const style = installStyle();
  if (style) {
    cleanup.push(() => style.remove());
  }

  document.addEventListener("click", onClick, true);
  window.addEventListener("popstate", onPopState, true);
  cleanup.push(() => document.removeEventListener("click", onClick, true));
  cleanup.push(() => window.removeEventListener("popstate", onPopState, true));
  patchHistory("pushState");
  patchHistory("replaceState");
  retryPatchElectronBridge();
  observeThreadContent();
  scheduleOlderTurnLoad();
  schedulePeriodicTitleRepair(5000);
  cleanup.push(clearTitleRepairTimer);

  return {
    api: null,
    stop() {
      stopped = true;
      active = false;
      clearTimers();
      document.documentElement.removeAttribute("data-codex-perf-thread-fastpath");
      while (cleanup.length) {
        const fn = cleanup.pop();
        try {
          fn();
        } catch {
          // Fail open: cleanup best-effort only.
        }
      }
    },
    beginNavigation,
  };
}

function start(api) {
  if (activeRuntime) {
    activeRuntime.stop();
    activeRuntime = null;
  }
  if (typeof window === "undefined" || typeof document === "undefined") {
    return { stop() {} };
  }
  if (storageDisabled()) {
    return { stop() {} };
  }
  activeRuntime = createRuntime();
  activeRuntime.api = api || null;
  return activeRuntime;
}

function stop() {
  if (!activeRuntime) {
    return;
  }
  activeRuntime.stop();
  activeRuntime = null;
}

module.exports = { start, stop };
