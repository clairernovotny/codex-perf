/* global window, document, performance, requestAnimationFrame, requestIdleCallback, cancelIdleCallback */

const PATCH_ID = "codex-perf-fast-thread-loader";
const KILL_SWITCH_KEY = `${PATCH_ID}:disabled`;
const STYLE_ID = `${PATCH_ID}:style`;
const LIGHTWEIGHT_VIEW_ID = `${PATCH_ID}:lightweight-view`;
const NAV_TIMEOUT_MS = 8000;
const IDLE_GRACE_MS = 250;
const OLDER_CONTROL_COOLDOWN_MS = 2000;
const THREAD_ROW_SELECTOR = [
  "[data-app-action-sidebar-thread-row]",
  "[data-thread-id]",
  "[data-testid*='thread' i]"
].join(", ");
const THREAD_TITLE_SELECTOR = "[data-app-action-sidebar-thread-title]";
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

    #${LIGHTWEIGHT_VIEW_ID} {
      position: fixed;
      inset: 0 0 0 min(360px, 32vw);
      z-index: 2147483000;
      overflow: auto;
      background: var(--color-token-main-surface-primary, var(--vscode-editor-background, Canvas));
      color: var(--color-token-text-primary, var(--vscode-editor-foreground, CanvasText));
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      contain: strict;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-shell {
      max-width: 860px;
      margin: 0 auto;
      padding: 18px 28px 72px;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-header {
      padding: 4px 0 22px;
    }

    #${LIGHTWEIGHT_VIEW_ID} h1 {
      margin: 0;
      font-size: 17px;
      line-height: 1.3;
      font-weight: 650;
      letter-spacing: 0;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-meta {
      margin-top: 6px;
      opacity: .7;
      font-size: 12px;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-turn {
      margin: 0 0 18px;
      padding: 0;
      border: 0;
      border-radius: 0;
      contain: layout paint style;
      content-visibility: auto;
      contain-intrinsic-size: auto 120px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-turn[data-role="user"] {
      display: flex;
      justify-content: flex-end;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-bubble {
      max-width: min(720px, 88%);
      padding: 10px 13px;
      border-radius: 14px;
      background: transparent;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-turn[data-role="user"] .codex-perf-thread-bubble {
      background: var(--color-token-message-surface, color-mix(in oklab, currentColor 8%, transparent));
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-turn[data-role="tool"] .codex-perf-thread-bubble {
      max-width: 100%;
      font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
      font-size: 12px;
      color: var(--color-token-text-secondary, currentColor);
      background: color-mix(in oklab, currentColor 6%, transparent);
      border: 1px solid color-mix(in oklab, currentColor 10%, transparent);
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-role {
      display: inline-block;
      margin-bottom: 8px;
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
      opacity: .62;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-turn[data-role="assistant"] .codex-perf-thread-role,
    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-turn[data-role="user"] .codex-perf-thread-role {
      display: none;
    }

    #${LIGHTWEIGHT_VIEW_ID} .codex-perf-thread-note {
      margin: 18px 0 0;
      opacity: .72;
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
  let lightweightAbort = null;
  const stats = {
    bridgeRequests: 0,
    bridgeResponses: 0,
    includeTurnsReads: 0,
    turnsListRequests: 0,
    olderTurnPagesObserved: 0,
    olderTurnControlClicks: 0,
    lastOlderTurnSignalAt: null,
    lightweightViews: 0,
    lightweightPageLoads: 0,
    lightweightBackgroundLoads: 0,
    lightweightPreloadedHits: 0,
    lightweightPreloadedTurnCount: 0,
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

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
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
    if (row && startLightweightThreadView(row, event)) {
      return;
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

  function getThreadTitleFromRow(row) {
    return row.getAttribute("data-app-action-sidebar-thread-title") ||
      row.querySelector(THREAD_TITLE_SELECTOR)?.textContent ||
      row.textContent ||
      "Thread";
  }

  function getPreloadedThread(threadId) {
    const preloaded = activeRuntime && activeRuntime.api && activeRuntime.api.preloadedThreads;
    if (!preloaded || !preloaded.threads) {
      return null;
    }
    return preloaded.threads[threadId] || preloaded.threads[`local:${threadId}`] || null;
  }

  function startLightweightThreadView(row, event) {
    const endpoint = activeRuntime && activeRuntime.api && activeRuntime.api.threadDataUrl;
    if (!endpoint || storageDisabled()) {
      return false;
    }
    const threadId = getThreadIdFromRow(row);
    if (!threadId) {
      return false;
    }
    event.preventDefault();
    event.stopPropagation();
    if (typeof event.stopImmediatePropagation === "function") {
      event.stopImmediatePropagation();
    }
    beginNavigation("lightweight-thread-view");
    renderLightweightShell({
      threadId,
      title: getThreadTitleFromRow(row),
      cwd: "",
      turns: [],
      note: "Loading local thread view...",
    });
    stats.lightweightViews += 1;
    const preloaded = getPreloadedThread(threadId);
    if (preloaded) {
      renderLightweightPreloaded(threadId, preloaded);
      return true;
    }
    loadLightweightThreadPage(threadId, 0, true);
    return true;
  }

  function getLightweightView() {
    let view = document.getElementById(LIGHTWEIGHT_VIEW_ID);
    if (!view) {
      view = document.createElement("section");
      view.id = LIGHTWEIGHT_VIEW_ID;
      view.setAttribute("aria-live", "polite");
      view.setAttribute("aria-label", "Codex thread");
      document.body.appendChild(view);
      cleanup.push(() => view.remove());
    }
    return view;
  }

  function renderLightweightShell({ threadId, title, cwd, turns, note, page }) {
    const view = getLightweightView();
    const body = turns.map((turn) => `
      <article class="codex-perf-thread-turn" data-role="${escapeHtml(turn.role || turn.type || "turn")}" data-codex-perf-lightweight-turn="${escapeHtml(turn.line)}">
        <div class="codex-perf-thread-bubble">
          <div class="codex-perf-thread-role">${escapeHtml(turn.role || turn.type || "turn")}</div>
          <div>${escapeHtml(turn.text || "")}</div>
        </div>
      </article>
    `).join("");
    view.innerHTML = `
      <div class="codex-perf-thread-shell" data-codex-perf-lightweight-thread="${escapeHtml(threadId)}">
        <header class="codex-perf-thread-header">
          <h1>${escapeHtml(title)}</h1>
          <div class="codex-perf-thread-meta">${escapeHtml(cwd || "")}</div>
        </header>
        ${body || `<p class="codex-perf-thread-note">${escapeHtml(note || "")}</p>`}
      </div>
    `;
  }

  async function loadLightweightThreadPage(threadId, cursor, foreground) {
    const endpoint = activeRuntime && activeRuntime.api && activeRuntime.api.threadDataUrl;
    if (!endpoint || stopped) {
      return;
    }
    if (foreground && lightweightAbort) {
      lightweightAbort.abort();
    }
    const controller = new AbortController();
    if (foreground) {
      lightweightAbort = controller;
    }
    const separator = endpoint.includes("?") ? "&" : "?";
    const url = `${endpoint}${separator}thread_id=${encodeURIComponent(threadId)}&cursor=${encodeURIComponent(cursor)}&limit=20`;
    try {
      const response = await fetch(url, { signal: controller.signal });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const data = await response.json();
      stats.lightweightPageLoads += 1;
      if (foreground) {
        renderLightweightShell({
          threadId,
          title: data.thread?.title || threadId,
          cwd: data.thread?.cwd || "",
          turns: data.turns || [],
          page: data.page || null,
        });
        firstPaint();
      } else {
        appendLightweightTurns(data.turns || []);
        stats.lightweightBackgroundLoads += 1;
      }
      if (data.page && data.page.has_more && data.page.next_cursor != null) {
        stats.lastOlderTurnSignalAt = Date.now();
        nowMark("older-turns-loaded");
        emit("older-turns-loaded", { method: "lightweight-local-page", cursor: data.page.next_cursor });
        const run = () => loadLightweightThreadPage(threadId, data.page.next_cursor, false);
        if (typeof requestIdleCallback === "function") {
          requestIdleCallback(run, { timeout: 1000 });
        } else {
          window.setTimeout(run, 250);
        }
      }
    } catch (error) {
      if (error && error.name === "AbortError") {
        return;
      }
      renderLightweightShell({
        threadId,
        title: threadId,
        cwd: "",
        turns: [],
        note: `Local thread view failed open: ${String(error)}`,
      });
      endNavigation("lightweight-error");
    }
  }

  function renderLightweightPreloaded(threadId, data) {
    const turns = Array.isArray(data.turns) ? data.turns : [];
    const newest = turns.slice(-20);
    stats.lightweightPreloadedHits += 1;
    stats.lightweightPreloadedTurnCount = turns.length;
    stats.lightweightPageLoads += 1;
    renderLightweightShell({
      threadId,
      title: data.thread?.title || threadId,
      cwd: data.thread?.cwd || "",
      turns: newest,
      page: { has_more: turns.length > newest.length },
    });
    firstPaint();
    if (turns.length > newest.length) {
      stats.lastOlderTurnSignalAt = Date.now();
      nowMark("older-turns-loaded");
      emit("older-turns-loaded", { method: "preloaded-local-page", count: turns.length - newest.length });
      let cursor = turns.length - newest.length;
      const appendChunk = () => {
        if (stopped || cursor <= 0) {
          return;
        }
        const start = Math.max(0, cursor - 20);
        appendLightweightTurns(turns.slice(start, cursor));
        cursor = start;
        stats.lightweightBackgroundLoads += 1;
        if (cursor > 0) {
          if (typeof requestIdleCallback === "function") {
            requestIdleCallback(appendChunk, { timeout: 1000 });
          } else {
            window.setTimeout(appendChunk, 250);
          }
        }
      };
      appendChunk();
    }
  }

  function appendLightweightTurns(turns) {
    const shell = document.getElementById(LIGHTWEIGHT_VIEW_ID)?.querySelector(".codex-perf-thread-shell");
    if (!shell || !turns.length) {
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const turn of turns) {
      const article = document.createElement("article");
      article.className = "codex-perf-thread-turn";
      article.setAttribute("data-role", String(turn.role || turn.type || "turn"));
      article.setAttribute("data-codex-perf-lightweight-turn", String(turn.line || ""));
      article.innerHTML = `<div class="codex-perf-thread-bubble"><div class="codex-perf-thread-role">${escapeHtml(turn.role || turn.type || "turn")}</div><div>${escapeHtml(turn.text || "")}</div></div>`;
      fragment.insertBefore(article, fragment.firstChild);
    }
    const header = shell.querySelector(".codex-perf-thread-header");
    shell.insertBefore(fragment, header ? header.nextSibling : shell.firstChild);
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

  return {
    api: null,
    stop() {
      stopped = true;
      active = false;
      clearTimers();
      if (lightweightAbort) {
        lightweightAbort.abort();
        lightweightAbort = null;
      }
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
