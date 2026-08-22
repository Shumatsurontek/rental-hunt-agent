"use strict";

const API_TIMEOUT_MS = 15_000;
const BROWSER_SCAN_ALARM = "rental-hunt-browser-scan";
const BROWSER_PAGE_LOAD_TIMEOUT_MS = 30_000;
const BROWSER_SCAN_PAGES_MAX = 3;
const EXTENSION_RUNTIME_VERSION = "0.3.0";
const MAX_NOTIFICATION_METADATA = 100;
const NOTIFICATION_ALARM = "rental-hunt-notifications";
const NOTIFICATION_ID_PREFIX = "rental-hunt:";
const NOTIFICATION_PULL_MAX = 20;
const POLL_INTERVAL_MINUTES = 1;

let pollInFlight = false;
let browserScanInFlight = false;

chrome.runtime.onInstalled.addListener(() => {
  initializeBackground().catch(reportBackgroundError);
});

chrome.runtime.onStartup.addListener(() => {
  initializeBackground().catch(reportBackgroundError);
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === NOTIFICATION_ALARM) {
    pollNotifications().catch(reportBackgroundError);
  }
  if (alarm.name === BROWSER_SCAN_ALARM) {
    pollScheduledBrowserScan().catch(reportBackgroundError);
  }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (
    areaName === "local" &&
    (Object.hasOwn(changes, "apiBaseUrl") || Object.hasOwn(changes, "adminToken"))
  ) {
    pollNotifications().catch(reportBackgroundError);
  }
});

chrome.notifications.onClicked.addListener((notificationId) => {
  openNotificationTarget(notificationId).catch(reportBackgroundError);
});

chrome.notifications.onButtonClicked.addListener((notificationId, buttonIndex) => {
  submitNotificationFeedback(notificationId, buttonIndex).catch(reportBackgroundError);
});

chrome.notifications.onClosed.addListener((notificationId) => {
  removeNotificationMetadata(notificationId).catch(reportBackgroundError);
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "get-runtime-info") {
    sendResponse({ extensionRuntimeVersion: EXTENSION_RUNTIME_VERSION });
    return false;
  }
  if (message?.type === "poll-notifications") {
    pollNotifications()
      .then(sendResponse)
      .catch((error) => sendResponse({ error: errorMessage(error), processed: 0 }));
    return true;
  }
  if (message?.type === "scan-watch-with-browser") {
    scanWatchWithBrowser(message.watch)
      .then(sendResponse)
      .catch((error) => sendResponse({ error: errorMessage(error) }));
    return true;
  }
  if (message?.type === "get-current-seloger-search") {
    findMostRecentSelogerSearchUrl()
      .then((url) => sendResponse({ url }))
      .catch((error) => sendResponse({ error: errorMessage(error) }));
    return true;
  }
  return false;
});

async function initializeBackground() {
  await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  const alarm = await chrome.alarms.get(NOTIFICATION_ALARM);
  if (!alarm) {
    await chrome.alarms.create(NOTIFICATION_ALARM, {
      periodInMinutes: POLL_INTERVAL_MINUTES,
    });
  }
  const browserScanAlarm = await chrome.alarms.get(BROWSER_SCAN_ALARM);
  if (!browserScanAlarm) {
    await chrome.alarms.create(BROWSER_SCAN_ALARM, {
      periodInMinutes: POLL_INTERVAL_MINUTES,
    });
  }
  await pollNotifications();
  await pollScheduledBrowserScan();
}

async function pollNotifications() {
  if (pollInFlight) return { processed: 0, skipped: true };
  pollInFlight = true;
  try {
    const connection = await loadConnection();
    if (!connection) {
      await storeDeliveryStatus({ delivered: 0, error: null, state: "not_configured" });
      return { processed: 0, skipped: false };
    }
    const permission = await chrome.notifications.getPermissionLevel();
    if (permission !== "granted") {
      throw new Error("Chrome notification permission is denied.");
    }
    const page = await apiRequest(connection, `/v1/notifications?limit=${NOTIFICATION_PULL_MAX}`);
    if (!Array.isArray(page.items) || page.items.length > NOTIFICATION_PULL_MAX) {
      throw new Error("Notification API returned an invalid bounded page.");
    }
    let processed = 0;
    for (const item of page.items) {
      await displayAndAcknowledge(connection, item);
      processed += 1;
    }
    await storeDeliveryStatus({ delivered: processed, error: null, state: "online" });
    await chrome.action.setBadgeText({ text: "" });
    return { processed, skipped: false };
  } catch (error) {
    await storeDeliveryStatus({ delivered: 0, error: errorMessage(error), state: "error" });
    await chrome.action.setBadgeBackgroundColor({ color: "#cd4239" });
    await chrome.action.setBadgeText({ text: "!" });
    throw error;
  } finally {
    pollInFlight = false;
  }
}

async function displayAndAcknowledge(connection, item) {
  validateNotification(item);
  const notificationId = `${NOTIFICATION_ID_PREFIX}${item.id}`;
  const metadata = await loadNotificationMetadata();
  let existing = metadata[notificationId];
  if (!existing) {
    const visibleNotifications = await chrome.notifications.getAll();
    existing = Object.hasOwn(visibleNotifications, notificationId)
      ? notificationMetadata(item)
      : null;
  }
  if (!existing) {
    await chrome.notifications.create(notificationId, notificationOptions(item));
    existing = notificationMetadata(item);
  }
  await saveNotificationMetadata(notificationId, existing);
  await apiRequest(connection, `/v1/notifications/${item.id}/ack`, { method: "POST" });
}

function notificationOptions(item) {
  const options = {
    type: "basic",
    iconUrl: "icons/rental-hunt-128.png",
    title: item.payload.title,
    message: item.payload.message,
    contextMessage: item.payload.context_message || "Rental Hunt Agent",
    priority: item.kind === "listing" ? 1 : 0,
    requireInteraction: item.kind === "listing",
  };
  if (item.kind === "listing") {
    options.buttons = [{ title: "Interested" }, { title: "Dismiss" }];
  }
  return options;
}

function notificationMetadata(item) {
  return {
    apiNotificationId: item.id,
    createdAt: Date.parse(item.created_at) || Date.now(),
    listingId: item.payload.listing_id || null,
    listingUrl: item.payload.listing_url || null,
  };
}

async function openNotificationTarget(notificationId) {
  const metadata = await loadNotificationMetadata();
  const target = metadata[notificationId];
  if (target?.listingUrl) {
    await chrome.tabs.create({ url: target.listingUrl });
  }
}

async function submitNotificationFeedback(notificationId, buttonIndex) {
  if (buttonIndex !== 0 && buttonIndex !== 1) return;
  const metadata = await loadNotificationMetadata();
  const target = metadata[notificationId];
  if (!target?.listingId) return;
  const connection = await loadConnection();
  if (!connection) throw new Error("Rental Hunt API connection is not configured.");
  const value = buttonIndex === 0 ? "interested" : "dismissed";
  await apiRequest(connection, `/v1/listings/${target.listingId}/feedback`, {
    method: "PUT",
    body: JSON.stringify({
      value,
      event_id: `chrome:${target.apiNotificationId}:${value}`,
    }),
  });
  await chrome.notifications.clear(notificationId);
  await removeNotificationMetadata(notificationId);
}

async function scanWatchWithBrowser(watch) {
  if (browserScanInFlight) throw new Error("A Chrome-assisted scan is already running.");
  validateWatch(watch);
  browserScanInFlight = true;
  await chrome.storage.local.set({
    browserScanAttempt: {
      attemptedAt: Date.now(),
      configurationVersion: watch.configuration_version,
      watchId: watch.id,
    },
  });
  await storeBrowserScanStatus({
    error: null,
    jobId: null,
    phase: "Opening saved SeLoger search",
    snapshots: [],
    state: "capturing",
  });
  try {
    const connection = await loadConnection();
    if (!connection) throw new Error("Rental Hunt API connection is not configured.");
    const sourceUrl = selectSearchUrl(watch.url);
    const capture = await captureBrowserPages(sourceUrl, watch.configuration_version);
    const accepted = await apiRequest(connection, `/v1/watches/${watch.id}/browser-scan`, {
      method: "POST",
      body: JSON.stringify(capture),
    });
    if (typeof accepted?.job_id !== "string") {
      throw new Error("Browser-scan API did not return a job ID.");
    }
    await storeBrowserScanStatus({
      error: null,
      jobId: accepted.job_id,
      phase: "Snapshots queued for parsing and agent analysis",
      snapshots: capture.pages.map(snapshotMetadata),
      state: "queued",
    });
    return { job_id: accepted.job_id };
  } catch (error) {
    const stored = await chrome.storage.local.get(["browserScanStatus"]);
    await storeBrowserScanStatus({
      error: errorMessage(error),
      jobId: null,
      phase: "Chrome exploration failed",
      snapshots: stored.browserScanStatus?.snapshots || [],
      state: "error",
    });
    throw error;
  } finally {
    browserScanInFlight = false;
  }
}

async function pollScheduledBrowserScan() {
  if (browserScanInFlight) return { skipped: "scan_in_flight" };
  const connection = await loadConnection();
  if (!connection) return { skipped: "not_configured" };
  const agent = await apiRequest(connection, "/v1/agent/status");
  if (agent.source_mode !== "chrome_extension") return { skipped: "playwright_mode" };
  let watch;
  try {
    watch = await apiRequest(connection, "/v1/watches/current");
  } catch (error) {
    if (error?.status === 404) return { skipped: "no_watch" };
    throw error;
  }
  validateWatch(watch);
  if (
    !Number.isInteger(watch.poll_interval_s) ||
    watch.poll_interval_s < 120 ||
    watch.poll_interval_s > 3_600
  ) {
    throw new Error("Active watch has an invalid poll interval.");
  }
  const dueAt = Date.parse(watch.next_scan_at);
  if (!Number.isFinite(dueAt)) throw new Error("Active watch has an invalid next-scan time.");
  const now = Date.now();
  if (dueAt > now) return { skipped: "not_due" };
  const stored = await chrome.storage.local.get(["browserScanAttempt"]);
  const attempt = stored.browserScanAttempt;
  if (
    attempt?.watchId === watch.id &&
    attempt?.configurationVersion === watch.configuration_version &&
    Number.isFinite(attempt.attemptedAt) &&
    now - attempt.attemptedAt < watch.poll_interval_s * 1_000
  ) {
    return { skipped: "attempt_throttled" };
  }
  return scanWatchWithBrowser(watch);
}

function validateWatch(watch) {
  if (
    !watch ||
    typeof watch.id !== "string" ||
    typeof watch.url !== "string" ||
    !Number.isInteger(watch.configuration_version) ||
    watch.configuration_version < 1 ||
    watch.configuration_version > 100
  ) {
    throw new Error("Active watch payload is invalid.");
  }
  validateSelogerUrl(watch.url);
}

function selectSearchUrl(watchUrl) {
  if (isLikelySearchUrl(watchUrl)) return validateSelogerUrl(watchUrl);
  throw new Error("Edit the watcher and save a filtered SeLoger results URL before scanning.");
}

async function findMostRecentSelogerSearchUrl() {
  const tabs = await chrome.tabs.query({ url: ["https://*.seloger.com/*"] });
  const candidates = tabs
    .filter((tab) => typeof tab.url === "string" && isLikelySearchUrl(tab.url))
    .sort((left, right) => (right.lastAccessed || 0) - (left.lastAccessed || 0));
  if (candidates[0]?.url) return validateSelogerUrl(candidates[0].url);
  throw new Error("Open a filtered SeLoger results page, then try again.");
}

function isLikelySearchUrl(value) {
  try {
    const url = new URL(value);
    validateSelogerUrl(url.href);
    return url.pathname !== "/" || url.search.length > 1;
  } catch (_error) {
    return false;
  }
}

function validateSelogerUrl(value) {
  const url = new URL(value);
  const hostAllowed = url.hostname === "seloger.com" || url.hostname.endsWith(".seloger.com");
  if (url.protocol !== "https:" || !hostAllowed || url.username || url.password) {
    throw new Error("Browser scan URL must be a safe HTTPS SeLoger URL.");
  }
  if (url.port && url.port !== "443") {
    throw new Error("Browser scan URL must use the default HTTPS port.");
  }
  return url.href;
}

async function captureBrowserPages(sourceUrl, watchConfigurationVersion) {
  const tab = await chrome.tabs.create({ active: false, url: sourceUrl });
  if (typeof tab.id !== "number") throw new Error("Chrome did not return a capture tab ID.");
  const pages = [];
  try {
    await waitForTabComplete(tab.id);
    for (let pageIndex = 0; pageIndex < BROWSER_SCAN_PAGES_MAX; pageIndex += 1) {
      await storeBrowserScanStatus({
        error: null,
        jobId: null,
        phase: `Taking snapshot ${pageIndex + 1}/${BROWSER_SCAN_PAGES_MAX}`,
        snapshots: pages.map(snapshotMetadata),
        state: "capturing",
      });
      const response = await chrome.tabs.sendMessage(tab.id, { type: "capture-seloger-page" });
      if (response?.error) throw new Error(response.error);
      if (!response?.page || typeof response.page !== "object") {
        throw new Error("SeLoger content capture returned an invalid page.");
      }
      pages.push(response.page);
      await storeBrowserScanStatus({
        error: null,
        jobId: null,
        phase: `Snapshot ${pageIndex + 1} captured`,
        snapshots: pages.map(snapshotMetadata),
        state: "capturing",
      });
      if (!response.page.next_url || pageIndex + 1 === BROWSER_SCAN_PAGES_MAX) break;
      const nextUrl = validateSelogerUrl(response.page.next_url);
      await storeBrowserScanStatus({
        error: null,
        jobId: null,
        phase: `Opening result page ${pageIndex + 2}`,
        snapshots: pages.map(snapshotMetadata),
        state: "capturing",
      });
      await chrome.tabs.update(tab.id, { url: nextUrl });
      await waitForTabComplete(tab.id);
    }
  } finally {
    try {
      await chrome.tabs.remove(tab.id);
    } catch (error) {
      console.warn("Rental Hunt could not close its capture tab", error);
    }
  }
  if (pages.length === 0 || pages.length > BROWSER_SCAN_PAGES_MAX) {
    throw new Error("Chrome capture violated the page-count bound.");
  }
  return {
    capture_id: crypto.randomUUID(),
    watch_configuration_version: watchConfigurationVersion,
    pages,
  };
}

function snapshotMetadata(page, pageIndex) {
  const documents = Array.isArray(page?.json_documents) ? page.json_documents : [];
  const candidates = Array.isArray(page?.dom_candidates) ? page.dom_candidates : [];
  return {
    bodyChars: typeof page?.body_text === "string" ? page.body_text.length : 0,
    capturedDomCandidates: candidates.length,
    domCandidateCount: Number.isInteger(page?.dom_candidate_count)
      ? page.dom_candidate_count
      : 0,
    jsonDocumentChars: documents.reduce(
      (total, document) => total + (typeof document === "string" ? document.length : 0),
      0,
    ),
    jsonDocumentCount: documents.length,
    page: pageIndex + 1,
    url: typeof page?.url === "string" ? page.url : null,
  };
}

async function waitForTabComplete(tabId) {
  const initial = await chrome.tabs.get(tabId);
  if (initial.status === "complete") return;
  await new Promise((resolve, reject) => {
    const timeoutId = setTimeout(() => {
      cleanup();
      reject(new Error("SeLoger tab navigation exceeded 30 seconds."));
    }, BROWSER_PAGE_LOAD_TIMEOUT_MS);
    const updated = (updatedTabId, changeInfo) => {
      if (updatedTabId !== tabId || changeInfo.status !== "complete") return;
      cleanup();
      resolve();
    };
    const removed = (removedTabId) => {
      if (removedTabId !== tabId) return;
      cleanup();
      reject(new Error("SeLoger capture tab closed before navigation completed."));
    };
    function cleanup() {
      clearTimeout(timeoutId);
      chrome.tabs.onUpdated.removeListener(updated);
      chrome.tabs.onRemoved.removeListener(removed);
    }
    chrome.tabs.onUpdated.addListener(updated);
    chrome.tabs.onRemoved.addListener(removed);
    chrome.tabs.get(tabId).then((current) => {
      if (current.status === "complete") updated(tabId, { status: "complete" });
    }, (error) => {
      cleanup();
      reject(error);
    });
  });
}

async function apiRequest(connection, path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  try {
    const response = await fetch(`${connection.apiBaseUrl}${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${connection.adminToken}`,
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      signal: controller.signal,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.detail || `Rental Hunt API returned HTTP ${response.status}.`);
      error.status = response.status;
      throw error;
    }
    return body;
  } finally {
    clearTimeout(timeout);
  }
}

async function loadConnection() {
  const stored = await chrome.storage.local.get(["apiBaseUrl", "adminToken"]);
  if (!stored.apiBaseUrl || !stored.adminToken) return null;
  const url = new URL(stored.apiBaseUrl);
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/"
  ) {
    throw new Error("Stored API origin is invalid.");
  }
  const originPermission = `${url.origin}/*`;
  const allowed = await chrome.permissions.contains({ origins: [originPermission] });
  if (!allowed) throw new Error("Chrome access to the configured API origin is missing.");
  return { apiBaseUrl: url.origin, adminToken: stored.adminToken };
}

function validateNotification(item) {
  if (
    !item ||
    typeof item.id !== "string" ||
    (item.kind !== "listing" && item.kind !== "bootstrap_digest") ||
    typeof item.created_at !== "string" ||
    typeof item.payload?.title !== "string" ||
    typeof item.payload?.message !== "string"
  ) {
    throw new Error("Notification API returned an invalid item.");
  }
  if (item.kind === "listing" && (!item.payload.listing_id || !item.payload.listing_url)) {
    throw new Error("Listing notification is missing its action target.");
  }
}

async function loadNotificationMetadata() {
  const stored = await chrome.storage.local.get(["notificationMetadata"]);
  return stored.notificationMetadata && typeof stored.notificationMetadata === "object"
    ? stored.notificationMetadata
    : {};
}

async function saveNotificationMetadata(notificationId, value) {
  const metadata = await loadNotificationMetadata();
  metadata[notificationId] = value;
  const ordered = Object.entries(metadata).sort(
    ([idA, a], [idB, b]) => (a.createdAt - b.createdAt) || idA.localeCompare(idB),
  );
  const excess = Math.max(ordered.length - MAX_NOTIFICATION_METADATA, 0);
  for (const [oldId] of ordered.slice(0, excess)) {
    delete metadata[oldId];
    await chrome.notifications.clear(oldId);
  }
  await chrome.storage.local.set({ notificationMetadata: metadata });
}

async function removeNotificationMetadata(notificationId) {
  const metadata = await loadNotificationMetadata();
  if (!Object.hasOwn(metadata, notificationId)) return;
  delete metadata[notificationId];
  await chrome.storage.local.set({ notificationMetadata: metadata });
}

async function storeDeliveryStatus({ delivered, error, state }) {
  await chrome.storage.local.set({
    notificationDeliveryStatus: {
      delivered,
      error,
      polledAt: new Date().toISOString(),
      state,
    },
  });
}

async function storeBrowserScanStatus({ error, jobId, phase, snapshots, state }) {
  if (!Array.isArray(snapshots) || snapshots.length > BROWSER_SCAN_PAGES_MAX) {
    throw new Error("Browser scan status violated the snapshot-count bound.");
  }
  await chrome.storage.local.set({
    browserScanStatus: {
      error,
      jobId,
      phase,
      snapshots,
      state,
      updatedAt: new Date().toISOString(),
    },
  });
}

function reportBackgroundError(error) {
  console.error("Rental Hunt background operation failed", error);
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

initializeBackground().catch(reportBackgroundError);
