"use strict";

const DEFAULT_API_BASE = "http://127.0.0.1:8000";
const API_TIMEOUT_MS = 15_000;
const CHAT_TIMEOUT_MS = 65_000;
const EXTENSION_RUNTIME_VERSION = "0.3.0";
const JOB_POLL_ATTEMPTS = 70;
const JOB_POLL_INTERVAL_MS = 1_500;
const MAX_CHAT_MESSAGES = 20;
const MAX_POSTAL_CODES = 50;
const MAX_POSTAL_CODE_INPUT_CHARS = 299;
const MAX_RESPONSE_BYTES = 1_000_000;
const MAX_SOFT_PREFERENCES = 20;
const MAX_SOFT_PREFERENCE_CHARS = 200;
const MAX_STREAM_READS = 4_096;
const MAX_STREAM_EVENTS = 2_002;
const MAX_WATCH_URL_CHARS = 2_083;
const PIPELINE_POLL_ATTEMPTS = 140;
const POLL_INTERVAL_S_MIN = 120;
const POLL_INTERVAL_S_MAX = 3_600;

const elements = {
  apiBaseUrl: document.querySelector("#api-base-url"),
  adminToken: document.querySelector("#admin-token"),
  browserSnapshotList: document.querySelector("#browser-snapshot-list"),
  browserSnapshotPhase: document.querySelector("#browser-snapshot-phase"),
  browserSnapshotState: document.querySelector("#browser-snapshot-state"),
  chatForm: document.querySelector("#chat-form"),
  chatInput: document.querySelector("#chat-input"),
  chatLog: document.querySelector("#chat-log"),
  chatState: document.querySelector("#chat-state"),
  clearChat: document.querySelector("#clear-chat"),
  cancelWatchEditButton: document.querySelector("#cancel-watch-edit-button"),
  connectionPill: document.querySelector("#connection-pill"),
  connectionSettings: document.querySelector("#connection-settings"),
  createWatchButton: document.querySelector("#create-watch-button"),
  deliverButton: document.querySelector("#deliver-button"),
  editWatchButton: document.querySelector("#edit-watch-button"),
  listingCount: document.querySelector("#listing-count"),
  listings: document.querySelector("#listings"),
  modelValue: document.querySelector("#model-value"),
  notificationsValue: document.querySelector("#notifications-value"),
  queueValue: document.querySelector("#queue-value"),
  readyValue: document.querySelector("#ready-value"),
  refreshButton: document.querySelector("#refresh-button"),
  reloadExtensionButton: document.querySelector("#reload-extension-button"),
  runtimeError: document.querySelector("#runtime-error"),
  saveSettings: document.querySelector("#save-settings"),
  scanButton: document.querySelector("#scan-button"),
  sendChat: document.querySelector("#send-chat"),
  settingsForm: document.querySelector("#settings-form"),
  sourceValue: document.querySelector("#source-value"),
  tracingValue: document.querySelector("#tracing-value"),
  useCurrentSearchButton: document.querySelector("#use-current-search-button"),
  watchBudgetSummary: document.querySelector("#watch-budget-summary"),
  watchDetails: document.querySelector("#watch-details"),
  watchEmpty: document.querySelector("#watch-empty"),
  watchEmptyMessage: document.querySelector("#watch-empty-message"),
  watchFrequencySummary: document.querySelector("#watch-frequency-summary"),
  watchFurnished: document.querySelector("#watch-furnished"),
  watchFurnishedSummary: document.querySelector("#watch-furnished-summary"),
  watchMeta: document.querySelector("#watch-meta"),
  watchPollInterval: document.querySelector("#watch-poll-interval"),
  watchPostalCodes: document.querySelector("#watch-postal-codes"),
  watchPostalSummary: document.querySelector("#watch-postal-summary"),
  watchRentMax: document.querySelector("#watch-rent-max"),
  watchRoomsMin: document.querySelector("#watch-rooms-min"),
  watchRoomsSummary: document.querySelector("#watch-rooms-summary"),
  watchSearchUrl: document.querySelector("#watch-search-url"),
  watchSetupError: document.querySelector("#watch-setup-error"),
  watchSetupForm: document.querySelector("#watch-setup-form"),
  watchSoftPreferences: document.querySelector("#watch-soft-preferences"),
  watchSoftSummary: document.querySelector("#watch-soft-summary"),
  watchSurfaceMin: document.querySelector("#watch-surface-min"),
  watchSurfaceSummary: document.querySelector("#watch-surface-summary"),
  watchUrl: document.querySelector("#watch-url"),
  watchVersion: document.querySelector("#watch-version"),
  jobStatus: document.querySelector("#job-status"),
};

const state = {
  apiBaseUrl: DEFAULT_API_BASE,
  adminToken: "",
  chatMessages: [],
  connected: false,
  watch: null,
  preferences: null,
  editingWatch: false,
  runtimeCompatible: false,
};

class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function initialize() {
  bindEvents();
  const stored = await chrome.storage.local.get([
    "apiBaseUrl",
    "adminToken",
    "browserScanStatus",
  ]);
  state.apiBaseUrl = stored.apiBaseUrl || DEFAULT_API_BASE;
  state.adminToken = stored.adminToken || "";
  elements.apiBaseUrl.value = state.apiBaseUrl;
  elements.adminToken.value = state.adminToken;
  renderChat();
  renderBrowserScanStatus(stored.browserScanStatus);
  if (state.adminToken) {
    await refreshDashboard();
    if (state.connected) elements.connectionSettings.open = false;
  }
}

function bindEvents() {
  elements.settingsForm.addEventListener("submit", saveConnection);
  elements.refreshButton.addEventListener("click", refreshDashboard);
  elements.reloadExtensionButton.addEventListener("click", () => chrome.runtime.reload());
  elements.deliverButton.addEventListener("click", deliverNotifications);
  elements.scanButton.addEventListener("click", triggerScan);
  elements.watchSetupForm.addEventListener("submit", createWatch);
  elements.editWatchButton.addEventListener("click", beginWatchEdit);
  elements.cancelWatchEditButton.addEventListener("click", cancelWatchEdit);
  elements.useCurrentSearchButton.addEventListener("click", useCurrentSearch);
  elements.chatForm.addEventListener("submit", sendChat);
  elements.clearChat.addEventListener("click", () => {
    state.chatMessages = [];
    renderChat();
  });
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === "local" && Object.hasOwn(changes, "browserScanStatus")) {
      renderBrowserScanStatus(changes.browserScanStatus.newValue);
    }
  });
}

async function saveConnection(event) {
  event.preventDefault();
  setButtonBusy(elements.saveSettings, true, "Connecting…");
  try {
    const apiBaseUrl = normalizeApiBase(elements.apiBaseUrl.value);
    const adminToken = elements.adminToken.value.trim();
    if (adminToken.length < 24) {
      throw new ApiError("Admin token must contain at least 24 characters.");
    }
    const originPermission = `${new URL(apiBaseUrl).origin}/*`;
    const hasPermission = await chrome.permissions.contains({ origins: [originPermission] });
    if (!hasPermission) {
      const granted = await chrome.permissions.request({ origins: [originPermission] });
      if (!granted) {
        throw new ApiError("Chrome access to the configured API origin was not granted.");
      }
    }
    state.apiBaseUrl = apiBaseUrl;
    state.adminToken = adminToken;
    await chrome.storage.local.set({ apiBaseUrl, adminToken });
    await refreshDashboard();
    elements.connectionSettings.open = false;
  } catch (error) {
    showRuntimeError(error);
    setConnection("offline", "Connection failed");
  } finally {
    setButtonBusy(elements.saveSettings, false, "Save & connect");
  }
}

function normalizeApiBase(value) {
  const url = new URL(value.trim());
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new ApiError("API origin must use HTTP or HTTPS.");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new ApiError("API origin cannot contain credentials, a query, or a fragment.");
  }
  if (url.pathname !== "/") {
    throw new ApiError("Use the API origin only, without a path.");
  }
  return url.origin;
}

async function refreshDashboard() {
  if (!state.adminToken) {
    setConnection("offline", "Not connected");
    return;
  }
  setConnection("loading", "Checking…");
  elements.refreshButton.disabled = true;
  hideRuntimeError();
  try {
    await checkRuntimeCompatibility();
    const [readiness, agent, watch, preferences, listings, delivery] = await Promise.all([
      apiRequest("/readyz"),
      apiRequest("/v1/agent/status"),
      apiRequest("/v1/watches/current").catch((error) => {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }),
      apiRequest("/v1/preferences").catch((error) => {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }),
      apiRequest("/v1/listings?eligible_only=true&limit=100"),
      chrome.storage.local.get(["notificationDeliveryStatus", "browserScanStatus"]),
    ]);
    state.connected = true;
    state.watch = watch;
    state.preferences = preferences;
    renderRuntime(readiness, agent, delivery.notificationDeliveryStatus);
    renderWatch(watch, preferences);
    renderBrowserScanStatus(delivery.browserScanStatus);
    renderListings(listings.items || [], listings.next_cursor);
    setConnection("online", readiness.status === "ready" ? "Ready" : "Connected");
    elements.sendChat.disabled = false;
    if (!state.runtimeCompatible) showRuntimeCompatibilityError();
  } catch (error) {
    state.connected = false;
    state.watch = null;
    state.preferences = null;
    elements.scanButton.disabled = true;
    elements.createWatchButton.disabled = true;
    elements.sendChat.disabled = true;
    setConnection("offline", "Unavailable");
    showRuntimeError(error);
  } finally {
    elements.refreshButton.disabled = false;
  }
}

function renderRuntime(readiness, agent, delivery) {
  elements.readyValue.textContent = readiness.status || "unknown";
  elements.sourceValue.textContent = agent.source_mode || "unknown";
  elements.modelValue.textContent = `${agent.model_provider} / ${agent.model_name}`;
  elements.queueValue.textContent = `${agent.active_jobs} active`;
  const deliveryState = delivery?.state || "waiting";
  elements.notificationsValue.textContent = `${agent.pending_notifications} pending · ${deliveryState}`;
  elements.notificationsValue.title = delivery?.error || delivery?.polledAt || "Not polled yet";
  elements.tracingValue.textContent = agent.langsmith_tracing
    ? `on · ${agent.langsmith_project}`
    : "off";
  if ((agent.failed_background_tasks || []).length > 0) {
    throw new ApiError(`Stopped workers: ${agent.failed_background_tasks.join(", ")}`);
  }
}

async function deliverNotifications() {
  setButtonBusy(elements.deliverButton, true, "Delivering…");
  try {
    const result = await chrome.runtime.sendMessage({ type: "poll-notifications" });
    if (result?.error) throw new ApiError(result.error);
    await refreshDashboard();
  } catch (error) {
    showRuntimeError(error);
  } finally {
    setButtonBusy(elements.deliverButton, false, "Deliver alerts");
  }
}

function renderWatch(watch, preferences = state.preferences) {
  const hasWatch = watch !== null;
  const editing = hasWatch && state.editingWatch;
  elements.watchEmpty.hidden = hasWatch && !editing;
  elements.watchDetails.hidden = !hasWatch || editing;
  elements.editWatchButton.hidden = !hasWatch || editing;
  elements.cancelWatchEditButton.hidden = !editing;
  elements.scanButton.disabled =
    !hasWatch || !watch.enabled || editing || !state.runtimeCompatible;
  elements.scanButton.title = state.runtimeCompatible
    ? "Open the saved search, capture up to three pages, then run the agent"
    : "Reload the unpacked extension before exploring";
  elements.createWatchButton.disabled = !state.connected || (hasWatch && !editing);
  elements.createWatchButton.textContent = editing ? "Save changes" : "Create watcher";
  elements.watchEmptyMessage.textContent = editing
    ? "Edit the saved search and constraints. Save to create a clean baseline revision."
    : "No active watch. Configure the hard filters to start monitoring.";
  if (!hasWatch) {
    if (preferences && !elements.watchRentMax.value) {
      populateWatchForm(null, preferences);
    }
    return;
  }
  elements.watchUrl.href = watch.url;
  elements.watchUrl.title = watch.url;
  elements.watchVersion.textContent = `rev ${watch.configuration_version}`;
  elements.watchMeta.textContent = `${watch.enabled ? "enabled" : "disabled"} · baseline ${watch.baseline_complete ? "complete" : "pending"}`;
  elements.watchBudgetSummary.textContent = preferences
    ? `≤ €${Number(preferences.rent_eur_monthly_max).toLocaleString("en")}/month`
    : "not configured";
  elements.watchSurfaceSummary.textContent = preferences
    ? `≥ ${preferences.surface_m2_min} m²`
    : "not configured";
  elements.watchRoomsSummary.textContent = preferences?.rooms_min
    ? `≥ ${preferences.rooms_min}`
    : "any";
  elements.watchFurnishedSummary.textContent = furnishedLabel(preferences?.furnished);
  elements.watchPostalSummary.textContent = preferences?.postal_codes_allowed?.length
    ? preferences.postal_codes_allowed.join(", ")
    : "any";
  elements.watchFrequencySummary.textContent = formatPollInterval(watch.poll_interval_s);
  const softPreferences = preferences?.soft_preferences || [];
  elements.watchSoftSummary.hidden = softPreferences.length === 0;
  elements.watchSoftSummary.textContent = softPreferences.length
    ? `Soft preferences: ${softPreferences.join(" · ")}`
    : "";
}

async function createWatch(event) {
  event.preventDefault();
  const editing = state.editingWatch && state.watch !== null;
  if (!state.connected || (state.watch && !editing)) return;
  hideWatchSetupError();
  setButtonBusy(elements.createWatchButton, true, editing ? "Saving…" : "Creating…");
  try {
    const setup = parseWatchSetup();
    if (editing) {
      const configuration = await apiRequest(
        `/v1/watches/${state.watch.id}/configuration`,
        { method: "PUT", body: setup },
      );
      state.watch = configuration.watch;
      state.preferences = configuration.preferences;
      state.editingWatch = false;
    } else {
      await apiRequest("/v1/preferences", {
        method: "PUT",
        body: setup.preferences,
      });
      state.watch = await apiRequest("/v1/watches", {
        method: "POST",
        body: setup.watch,
      });
      state.preferences = setup.preferences;
    }
    renderWatch(state.watch, state.preferences);
    elements.jobStatus.hidden = false;
    elements.jobStatus.replaceChildren(
      document.createTextNode(
        editing
          ? `Watcher saved · revision ${state.watch.configuration_version} is due now.`
          : "Watcher created · the first baseline scan is due now.",
      ),
    );
    await refreshDashboard();
  } catch (error) {
    showWatchSetupError(error);
  } finally {
    const stillEditing = state.editingWatch && state.watch !== null;
    setButtonBusy(
      elements.createWatchButton,
      false,
      stillEditing ? "Save changes" : "Create watcher",
    );
    elements.createWatchButton.disabled =
      !state.connected || (state.watch !== null && !stillEditing);
  }
}

function beginWatchEdit() {
  if (!state.watch || !state.preferences) {
    showWatchSetupError(new ApiError("Watcher preferences are unavailable."));
    return;
  }
  state.editingWatch = true;
  populateWatchForm(state.watch, state.preferences);
  hideWatchSetupError();
  renderWatch(state.watch, state.preferences);
  elements.watchSearchUrl.focus();
}

function cancelWatchEdit() {
  state.editingWatch = false;
  hideWatchSetupError();
  renderWatch(state.watch, state.preferences);
}

async function useCurrentSearch() {
  setButtonBusy(elements.useCurrentSearchButton, true, "Reading…");
  hideWatchSetupError();
  try {
    const result = await chrome.runtime.sendMessage({ type: "get-current-seloger-search" });
    if (result?.error) throw new ApiError(result.error);
    if (typeof result?.url !== "string") {
      throw new ApiError("Chrome did not return a SeLoger results URL.");
    }
    elements.watchSearchUrl.value = parseWatchUrl(result.url);
  } catch (error) {
    showWatchSetupError(error);
  } finally {
    setButtonBusy(elements.useCurrentSearchButton, false, "Use open tab");
  }
}

function populateWatchForm(watch, preferences) {
  elements.watchSearchUrl.value = watch?.url || "";
  elements.watchRentMax.value = String(preferences.rent_eur_monthly_max);
  elements.watchSurfaceMin.value = String(preferences.surface_m2_min);
  elements.watchRoomsMin.value = preferences.rooms_min === null ? "" : String(preferences.rooms_min);
  elements.watchFurnished.value = preferences.furnished;
  elements.watchPollInterval.value = String(watch?.poll_interval_s || 600);
  elements.watchPostalCodes.value = (preferences.postal_codes_allowed || []).join(", ");
  elements.watchSoftPreferences.value = (preferences.soft_preferences || []).join("\n");
}

function furnishedLabel(value) {
  if (value === "required") return "required";
  if (value === "forbidden") return "forbidden";
  return "any";
}

function formatPollInterval(seconds) {
  if (seconds % 60 === 0) return `Every ${seconds / 60} min`;
  return `Every ${seconds}s`;
}

function parseWatchSetup() {
  const rentMax = parseInteger(elements.watchRentMax.value, "Maximum rent", 1, 20_000, false);
  const surfaceMin = parseSurface(elements.watchSurfaceMin.value);
  const roomsMin = parseInteger(elements.watchRoomsMin.value, "Minimum rooms", 1, 20, true);
  const furnished = parseFurnished(elements.watchFurnished.value);
  const postalCodes = parsePostalCodes(elements.watchPostalCodes.value);
  const softPreferences = parseSoftPreferences(elements.watchSoftPreferences.value);
  const pollInterval = parseInteger(
    elements.watchPollInterval.value,
    "Poll interval",
    POLL_INTERVAL_S_MIN,
    POLL_INTERVAL_S_MAX,
    false,
  );
  const preferences = {
      rent_eur_monthly_max: rentMax,
      surface_m2_min: surfaceMin,
      rooms_min: roomsMin,
      furnished,
      postal_codes_allowed: postalCodes,
      soft_preferences: softPreferences,
    };
  const sourceUrl = parseWatchUrl(elements.watchSearchUrl.value);
  return {
    preferences,
    watch: {
      url: mirrorHardConstraintsInSearchUrl(sourceUrl, preferences),
      poll_interval_s: pollInterval,
    },
  };
}

function mirrorHardConstraintsInSearchUrl(sourceUrl, preferences) {
  const url = new URL(sourceUrl);
  if (/\/(?:annonces|bien)\//i.test(url.pathname)) {
    throw new ApiError("Use a SeLoger results page, not an individual listing URL.");
  }
  url.searchParams.set("priceMax", String(preferences.rent_eur_monthly_max));
  url.searchParams.set("surfaceMin", String(Number(preferences.surface_m2_min)));
  url.searchParams.delete("spaceMin");
  if (preferences.rooms_min === null) {
    url.searchParams.delete("roomsMin");
  } else {
    url.searchParams.set("roomsMin", String(preferences.rooms_min));
  }
  url.searchParams.set("order", "DateDesc");
  return parseWatchUrl(url.href);
}

function parseWatchUrl(rawValue) {
  const value = rawValue.trim();
  if (value.length === 0 || value.length > MAX_WATCH_URL_CHARS) {
    throw new ApiError(`SeLoger URL must contain 1–${MAX_WATCH_URL_CHARS} characters.`);
  }
  let url;
  try {
    url = new URL(value);
  } catch (_error) {
    throw new ApiError("Enter a complete SeLoger results URL.");
  }
  if (url.protocol !== "https:") throw new ApiError("SeLoger URL must use HTTPS.");
  if (url.username || url.password) throw new ApiError("SeLoger URL cannot contain credentials.");
  const hostAllowed = url.hostname === "seloger.com" || url.hostname.endsWith(".seloger.com");
  if (!hostAllowed) throw new ApiError("Search URL host must be seloger.com.");
  if (url.port && url.port !== "443") throw new ApiError("SeLoger URL must use the default port.");
  return url.href;
}

function parseInteger(rawValue, label, minimum, maximum, optional) {
  const value = rawValue.trim();
  if (optional && value.length === 0) return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new ApiError(`${label} must be a whole number from ${minimum} to ${maximum}.`);
  }
  return parsed;
}

function parseSurface(rawValue) {
  const value = rawValue.trim();
  if (!/^\d+(?:\.\d{1,2})?$/.test(value)) {
    throw new ApiError("Minimum surface must have at most two decimal places.");
  }
  const parsed = Number(value);
  if (parsed <= 0 || parsed > 500) {
    throw new ApiError("Minimum surface must be greater than 0 and at most 500 m².");
  }
  return parsed.toFixed(2);
}

function parseFurnished(value) {
  if (value !== "required" && value !== "forbidden" && value !== "any") {
    throw new ApiError("Furnished preference is invalid.");
  }
  return value;
}

function parsePostalCodes(rawValue) {
  const value = rawValue.trim();
  if (value.length > MAX_POSTAL_CODE_INPUT_CHARS) {
    throw new ApiError("Postal-code input is too long.");
  }
  if (!value) return [];
  const postalCodes = [...new Set(value.split(/[\s,;]+/).filter(Boolean))];
  if (postalCodes.length > MAX_POSTAL_CODES) {
    throw new ApiError(`At most ${MAX_POSTAL_CODES} postal codes are allowed.`);
  }
  const invalid = postalCodes.find((postalCode) => !/^\d{5}$/.test(postalCode));
  if (invalid) throw new ApiError(`Invalid French postal code: ${invalid}.`);
  return postalCodes;
}

function parseSoftPreferences(rawValue) {
  const preferences = [
    ...new Set(
      rawValue
        .split(/\r?\n/)
        .map((value) => value.trim())
        .filter(Boolean),
    ),
  ];
  if (preferences.length > MAX_SOFT_PREFERENCES) {
    throw new ApiError(`At most ${MAX_SOFT_PREFERENCES} soft preferences are allowed.`);
  }
  const tooLong = preferences.find((value) => value.length > MAX_SOFT_PREFERENCE_CHARS);
  if (tooLong) {
    throw new ApiError(
      `Each soft preference is limited to ${MAX_SOFT_PREFERENCE_CHARS} characters.`,
    );
  }
  return preferences;
}

function showWatchSetupError(error) {
  elements.watchSetupError.hidden = false;
  elements.watchSetupError.textContent = errorMessage(error);
}

function hideWatchSetupError() {
  elements.watchSetupError.hidden = true;
  elements.watchSetupError.textContent = "";
}

function renderListings(items, nextCursor = null) {
  elements.listings.replaceChildren();
  elements.listingCount.textContent = `${items.length}${nextCursor ? "+" : ""}`;
  if (items.length === 0) {
    elements.listings.append(emptyState("No hard matches in the current watcher revision yet."));
    return;
  }
  const ranked = [...items].sort(
    (left, right) => (right.assessment?.score ?? -1) - (left.assessment?.score ?? -1),
  );
  for (const item of ranked.slice(0, 10)) {
    const listing = item.listing;
    const link = document.createElement("a");
    link.className = "listing";
    link.href = listing.canonical_url;
    link.target = "_blank";
    link.rel = "noreferrer";

    const top = document.createElement("div");
    top.className = "listing-topline";
    const title = document.createElement("span");
    title.className = "listing-title";
    title.textContent = listing.title;
    const score = document.createElement("span");
    score.className = "listing-score";
    score.textContent = item.assessment ? `${item.assessment.score}/100` : "unscored";
    top.append(title, score);

    const meta = document.createElement("p");
    meta.className = "listing-meta";
    const rent = listing.rent_eur_monthly ? `€${listing.rent_eur_monthly}/mo` : "rent unknown";
    const surface = listing.surface_m2 ? `${listing.surface_m2} m²` : "surface unknown";
    const place = listing.postal_code || listing.city || "location unknown";
    meta.textContent = `${rent} · ${surface} · ${place}${item.feedback ? ` · ${item.feedback}` : ""}`;
    link.append(top, meta);
    elements.listings.append(link);
  }
}

async function triggerScan() {
  if (!state.watch) return;
  if (!state.runtimeCompatible) {
    showRuntimeCompatibilityError();
    return;
  }
  setButtonBusy(elements.scanButton, true, "Exploring…");
  try {
    const accepted = await chrome.runtime.sendMessage({
      type: "scan-watch-with-browser",
      watch: {
        id: state.watch.id,
        url: state.watch.url,
        configuration_version: state.watch.configuration_version,
      },
    });
    if (accepted?.error) throw new ApiError(accepted.error);
    if (typeof accepted?.job_id !== "string") {
      throw new ApiError("Chrome capture did not return a scan job ID.");
    }
    renderJob({ id: accepted.job_id, status: "pending", attempts: 0 });
    const scanJob = await pollJob(accepted.job_id);
    if (scanJob.status !== "succeeded") {
      throw new ApiError(scanJob.last_error || `Scan job ${accepted.job_id} failed.`);
    }
    await pollAgentPipeline();
    const delivery = await chrome.runtime.sendMessage({ type: "poll-notifications" });
    if (delivery?.error) throw new ApiError(delivery.error);
    await refreshDashboard();
  } catch (error) {
    renderJobError(error);
  } finally {
    elements.scanButton.textContent = "Explore with agent";
    elements.scanButton.disabled =
      !state.watch || !state.watch.enabled || !state.runtimeCompatible || state.editingWatch;
  }
}

async function pollJob(jobId) {
  for (let attempt = 0; attempt < JOB_POLL_ATTEMPTS; attempt += 1) {
    await delay(JOB_POLL_INTERVAL_MS);
    const job = await apiRequest(`/v1/jobs/${jobId}`);
    renderJob(job);
    if (job.status === "succeeded" || job.status === "dead") return job;
  }
  throw new ApiError(`Job ${jobId} is still running after the bounded polling window.`);
}

async function pollAgentPipeline() {
  for (let attempt = 0; attempt < PIPELINE_POLL_ATTEMPTS; attempt += 1) {
    const agent = await apiRequest("/v1/agent/status");
    if (!Number.isInteger(agent.active_jobs) || agent.active_jobs < 0) {
      throw new ApiError("Agent status returned an invalid active-job count.");
    }
    if (agent.active_jobs === 0) return;
    elements.scanButton.textContent = `Agent analyzing · ${agent.active_jobs}`;
    elements.jobStatus.hidden = false;
    elements.jobStatus.replaceChildren(
      document.createTextNode(
        `Chrome snapshots parsed · ${agent.active_jobs} bounded agent job${agent.active_jobs === 1 ? "" : "s"} remaining`,
      ),
    );
    await delay(JOB_POLL_INTERVAL_MS);
  }
  throw new ApiError("Agent analysis is still running after the 210-second UI wait bound.");
}

async function checkRuntimeCompatibility() {
  try {
    const runtime = await chrome.runtime.sendMessage({ type: "get-runtime-info" });
    state.runtimeCompatible = runtime?.extensionRuntimeVersion === EXTENSION_RUNTIME_VERSION;
  } catch (_error) {
    state.runtimeCompatible = false;
  }
  elements.reloadExtensionButton.hidden = state.runtimeCompatible;
}

function showRuntimeCompatibilityError() {
  showRuntimeError(
    new ApiError(
      `Chrome is still running an older extension worker. Reload Rental Hunt Agent ${EXTENSION_RUNTIME_VERSION} in chrome://extensions, then reopen this panel.`,
    ),
  );
}

function renderBrowserScanStatus(status) {
  const snapshots = Array.isArray(status?.snapshots) ? status.snapshots.slice(0, 3) : [];
  elements.browserSnapshotState.textContent = status?.state || "idle";
  elements.browserSnapshotPhase.textContent =
    status?.error || status?.phase || "No exploration captured yet.";
  elements.browserSnapshotList.replaceChildren();
  if (snapshots.length === 0) {
    elements.browserSnapshotList.append(
      emptyState("The agent will show each bounded SeLoger page snapshot here."),
    );
    return;
  }
  for (const snapshot of snapshots) {
    const item = document.createElement("li");
    item.className = "snapshot-item";
    const summary = document.createElement("strong");
    const page = Number.isInteger(snapshot.page) ? snapshot.page : "?";
    const candidates = Number.isInteger(snapshot.domCandidateCount)
      ? snapshot.domCandidateCount
      : 0;
    const captured = Number.isInteger(snapshot.capturedDomCandidates)
      ? snapshot.capturedDomCandidates
      : 0;
    const documents = Number.isInteger(snapshot.jsonDocumentCount)
      ? snapshot.jsonDocumentCount
      : 0;
    summary.textContent = `Page ${page} · ${candidates} listing links (${captured} captured) · ${documents} structured blocks`;
    const url = document.createElement("span");
    url.className = "snapshot-url";
    url.textContent = typeof snapshot.url === "string" ? snapshot.url : "URL unavailable";
    item.append(summary, url);
    elements.browserSnapshotList.append(item);
  }
}

function renderJob(job) {
  elements.jobStatus.hidden = false;
  const suffix = job.last_error ? ` · ${job.last_error}` : "";
  const label = document.createElement("span");
  label.textContent = `Job ${shortId(job.id)} · ${job.status} · attempt ${job.attempts}${suffix}`;
  elements.jobStatus.replaceChildren(label);
  if (job.trace_id) {
    const copy = document.createElement("button");
    copy.className = "trace-button";
    copy.type = "button";
    copy.textContent = `copy trace ${shortId(job.trace_id)}`;
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(job.trace_id);
      copy.textContent = "trace copied";
    });
    elements.jobStatus.append(" · ", copy);
  }
}

function renderJobError(error) {
  elements.jobStatus.hidden = false;
  elements.jobStatus.replaceChildren(document.createTextNode(errorMessage(error)));
}

async function sendChat(event) {
  event.preventDefault();
  const content = elements.chatInput.value.trim();
  if (!content || !state.connected) return;
  const history = state.chatMessages.slice(-(MAX_CHAT_MESSAGES - 1));
  state.chatMessages = [...history, { role: "user", content }];
  const requestMessages = state.chatMessages.map(({ role, content: messageContent }) => ({
    role,
    content: messageContent,
  }));
  const assistant = { role: "assistant", content: "", streaming: true };
  state.chatMessages.push(assistant);
  state.chatMessages = state.chatMessages.slice(-MAX_CHAT_MESSAGES);
  elements.chatInput.value = "";
  renderChat();
  setButtonBusy(elements.sendChat, true, "Thinking…");
  elements.chatLog.setAttribute("aria-busy", "true");
  elements.chatState.textContent = "Streaming traced model call…";
  try {
    const response = await streamChat(requestMessages, (delta) => {
      assistant.content += delta;
      updateStreamingMessage(assistant.content);
    });
    assistant.content = response.message;
    assistant.model = `${response.model_provider} / ${response.model_name}`;
    assistant.traceId = response.trace_id;
    assistant.streaming = false;
    elements.chatState.textContent = response.trace_id
      ? `Trace ${shortId(response.trace_id)}`
      : "Tracing disabled";
  } catch (error) {
    assistant.streaming = false;
    const interruption = `[Stream interrupted: ${errorMessage(error)}]`;
    assistant.content = assistant.content ? `${assistant.content}\n\n${interruption}` : interruption;
    elements.chatState.textContent = errorMessage(error);
  } finally {
    elements.chatLog.setAttribute("aria-busy", "false");
    setButtonBusy(elements.sendChat, false, "Send");
    renderChat();
  }
}

function renderChat() {
  elements.chatLog.replaceChildren();
  if (state.chatMessages.length === 0) {
    elements.chatLog.append(emptyState("Ask about deployment state, matches, or failures."));
    return;
  }
  for (const message of state.chatMessages) {
    const bubble = document.createElement("div");
    bubble.className = `message message-${message.role}`;
    const text = document.createElement("div");
    text.className = "message-content";
    text.textContent = message.content;
    bubble.append(text);
    if (message.role === "assistant" && (message.model || message.traceId)) {
      const meta = document.createElement("div");
      meta.className = "message-meta";
      const model = document.createElement("span");
      model.textContent = message.model || "agent";
      meta.append(model);
      if (message.traceId) {
        const copy = document.createElement("button");
        copy.className = "trace-button";
        copy.type = "button";
        copy.textContent = `copy trace ${shortId(message.traceId)}`;
        copy.addEventListener("click", async () => {
          await navigator.clipboard.writeText(message.traceId);
          copy.textContent = "trace copied";
        });
        const open = document.createElement("button");
        open.className = "trace-button";
        open.type = "button";
        open.textContent = "open LangSmith ↗";
        open.addEventListener("click", () => {
          chrome.tabs.create({ url: "https://smith.langchain.com/" });
        });
        meta.append(copy, open);
      }
      bubble.append(meta);
    }
    elements.chatLog.append(bubble);
  }
  elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
}

function updateStreamingMessage(content) {
  const lastMessage = elements.chatLog.lastElementChild;
  const text = lastMessage?.querySelector(".message-content");
  if (text) text.textContent = content;
  elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
}

async function streamChat(messages, onDelta) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);
  let reader = null;
  try {
    const response = await fetch(`${state.apiBaseUrl}/v1/chat/stream`, {
      body: JSON.stringify({ messages }),
      headers: {
        Accept: "text/event-stream",
        Authorization: `Bearer ${state.adminToken}`,
        "Content-Type": "application/json",
      },
      method: "POST",
      signal: controller.signal,
    });
    if (!response.ok) {
      const text = await response.text();
      if (text.length > MAX_RESPONSE_BYTES) {
        throw new ApiError("API error response exceeded the 1 MB client bound.", response.status);
      }
      let detail = `Streaming request failed with HTTP ${response.status}.`;
      try {
        detail = JSON.parse(text)?.detail || detail;
      } catch (_error) {
        // Keep the bounded status-only error when the proxy does not return JSON.
      }
      throw new ApiError(detail, response.status);
    }
    if (!response.body) throw new ApiError("Browser did not expose the streaming response body.");

    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let bytesSeen = 0;
    let eventsSeen = 0;
    let completed = null;
    let streamEnded = false;

    for (let readCount = 0; readCount < MAX_STREAM_READS; readCount += 1) {
      const { done, value } = await reader.read();
      if (done) {
        streamEnded = true;
        break;
      }
      bytesSeen += value.byteLength;
      if (bytesSeen > MAX_RESPONSE_BYTES) {
        throw new ApiError("Streaming response exceeded the 1 MB client bound.");
      }
      buffer += decoder.decode(value, { stream: true });
      let separator = buffer.indexOf("\n\n");
      while (separator !== -1) {
        const block = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        separator = buffer.indexOf("\n\n");
        if (!block.trim()) continue;
        eventsSeen += 1;
        if (eventsSeen > MAX_STREAM_EVENTS) {
          throw new ApiError("Streaming response exceeded the event-count bound.");
        }
        const event = parseSseBlock(block);
        if (event.name === "delta") {
          if (event.payload.type !== "delta" || typeof event.payload.delta !== "string") {
            throw new ApiError("API returned an invalid delta event.");
          }
          onDelta(event.payload.delta);
        } else if (event.name === "done") {
          completed = event.payload;
        } else if (event.name === "error") {
          throw new ApiError(event.payload.detail || "Agent stream failed.");
        }
      }
    }
    if (!streamEnded) throw new ApiError("Streaming response exceeded the read-count bound.");
    if (!completed || completed.type !== "done" || typeof completed.message !== "string") {
      throw new ApiError("Agent stream ended without a completion event.");
    }
    return completed;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new ApiError(`Agent stream exceeded ${Math.round(CHAT_TIMEOUT_MS / 1000)} seconds.`);
    }
    if (error instanceof ApiError) throw error;
    throw new ApiError(errorMessage(error));
  } finally {
    clearTimeout(timeoutId);
    if (reader) await reader.cancel().catch(() => undefined);
  }
}

function parseSseBlock(block) {
  let name = "message";
  const data = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) throw new ApiError("API returned an SSE event without data.");
  try {
    return { name, payload: JSON.parse(data.join("\n")) };
  } catch (_error) {
    throw new ApiError("API returned invalid JSON in the agent stream.");
  }
}

async function apiRequest(path, options = {}) {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs || API_TIMEOUT_MS;
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  const headers = { Authorization: `Bearer ${state.adminToken}` };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  try {
    const response = await fetch(`${state.apiBaseUrl}${path}`, {
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      headers,
      method: options.method || "GET",
      signal: controller.signal,
    });
    const text = await response.text();
    if (text.length > MAX_RESPONSE_BYTES) {
      throw new ApiError("API response exceeded the 1 MB client bound.", response.status);
    }
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (_error) {
        throw new ApiError("API returned invalid JSON.", response.status);
      }
    }
    if (!response.ok) {
      throw new ApiError(payload?.detail || `API request failed with HTTP ${response.status}.`, response.status);
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new ApiError(`API request exceeded ${Math.round(timeoutMs / 1000)} seconds.`);
    }
    if (error instanceof ApiError) throw error;
    throw new ApiError(errorMessage(error));
  } finally {
    clearTimeout(timeoutId);
  }
}

function setConnection(status, label) {
  elements.connectionPill.className = `pill pill-${status}`;
  elements.connectionPill.textContent = label;
}

function setButtonBusy(button, busy, label) {
  button.disabled = busy;
  button.textContent = label;
}

function showRuntimeError(error) {
  elements.runtimeError.hidden = false;
  elements.runtimeError.textContent = errorMessage(error);
}

function hideRuntimeError() {
  elements.runtimeError.hidden = true;
  elements.runtimeError.textContent = "";
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

function emptyState(message) {
  const element = document.createElement("p");
  element.className = "empty-state";
  element.textContent = message;
  return element;
}

function shortId(value) {
  return String(value).slice(0, 8);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

initialize().catch((error) => {
  showRuntimeError(error);
  setConnection("offline", "Initialization failed");
});
