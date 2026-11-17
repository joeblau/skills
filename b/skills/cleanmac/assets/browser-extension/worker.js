"use strict";
// This companion never evaluates page JavaScript, reads page contents, or accepts
// HTTP requests. Its command vocabulary is deliberately closed.
let nativePort = null;
let connectedProfile = null;
let connectionMessage = "Disconnected. Register the native host, then connect this profile.";
const observedAt = new Map();
const tabVersions = new Map();
const privacyTypes = new Set(["history", "downloads", "cookies", "cache", "cacheStorage", "formData", "indexedDB", "localStorage", "serviceWorkers", "fileSystems"]);
const originTypes = new Set(["cookies", "cache", "cacheStorage", "indexedDB", "localStorage", "serviceWorkers", "fileSystems"]);
const fail = (code) => { const error = new Error(code); error.code = code; throw error; };

function versionFor(id) {
  let version = tabVersions.get(id);
  if (!version) { version = {identity: crypto.randomUUID(), revision: 0}; tabVersions.set(id, version); }
  return version;
}

function assertVersion(expected) {
  const version = versionFor(expected.tab_id);
  if (version.identity !== expected.identity || version.revision !== expected.revision) fail("browser_target_changed");
}

async function hash(value) {
  const bytes = new TextEncoder().encode(value);
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), n => n.toString(16).padStart(2, "0")).join("");
}

function safeReopen(url) {
  try {
    const parsed = new URL(url);
    return ["https:", "http:"].includes(parsed.protocol) && !parsed.username && !parsed.password && !parsed.search && !parsed.hash;
  } catch (_) { return false; }
}

async function tabView(tab) {
  if (tab.incognito) fail("browser_private_scope");
  if (!Number.isInteger(tab.id) || !Number.isInteger(tab.windowId) || typeof tab.url !== "string") fail("browser_bridge_protocol");
  let observed = observedAt.get(tab.id);
  if (!observed) { observed = {at: Date.now(), source: "first-observed"}; observedAt.set(tab.id, observed); }
  versionFor(tab.id).window_id = tab.windowId;
  const version = {...versionFor(tab.id)}, fingerprint = await hash(tab.url);
  if (versionFor(tab.id).revision !== version.revision) fail("browser_target_changed");
  return {tab_id: tab.id, window_id: tab.windowId, url: tab.url, url_digest: fingerprint, ...version,
    title: tab.title || "", active: !!tab.active, pinned: !!tab.pinned, audible: !!tab.audible,
    discarded: !!tab.discarded, auto_discardable: tab.autoDiscardable !== false,
    observed_at: observed.at, age_source: observed.source, form_state: "unknown", incognito: false};
}

async function checkedTab(expected, decisions, operation) {
  if (!expected || !Number.isInteger(expected.tab_id) || !Number.isInteger(expected.window_id)
      || typeof expected.url_digest !== "string" || typeof expected.identity !== "string"
      || !Number.isInteger(expected.revision)) fail("browser_bridge_protocol");
  const tab = await chrome.tabs.get(expected.tab_id);
  if (tab.incognito || tab.windowId !== expected.window_id || await hash(tab.url || "") !== expected.url_digest
      || tab.status === "loading" || tab.pendingUrl) fail("browser_target_changed");
  assertVersion(expected);
  // Navigation never turns an approved tab ID into approval for a new URL.
  for (const [field, decision] of [["active", "allow_active"], ["pinned", "allow_pinned"], ["audible", "allow_audible"]]) {
    if (tab[field] && decisions[decision] !== true) fail("browser_state_decision_required");
  }
  if (decisions.allow_form_loss !== true) fail("browser_state_decision_required");
  if (operation === "tab-discard" && (tab.active || tab.discarded || tab.autoDiscardable === false)) fail("browser_action_unavailable");
  return tab;
}

async function handleCommand(command, args, profile) {
  if (!args || typeof args !== "object" || Array.isArray(args)) fail("browser_bridge_protocol");
  if (command === "tabs-inspect") {
    const max = args.max_tabs === undefined ? 1000 : args.max_tabs;
    if (!Number.isInteger(max) || max < 1 || max > 2500) fail("browser_bridge_protocol");
    const tabs = (await chrome.tabs.query({})).filter(tab => !tab.incognito);
    const items = await Promise.all(tabs.slice(0, max).map(tabView));
    return {...profile, items, complete: tabs.length <= max, total: tabs.length, private_tabs: "excluded"};
  }
  if (command === "privacy-inspect") {
    const settings = await chrome.browsingData.settings();
    return {...profile, supported_types: [...privacyTypes], removal_permitted: settings.dataRemovalPermitted || {},
      passwords: "manual", previous_tabs: "use-selected-tab-actions", protected_web: "excluded", extension_data: "excluded"};
  }
  if (args.profile_id !== profile.profile_id || args.session_id !== profile.session_id) fail("browser_target_changed");
  if (["tab-close", "tab-discard", "tab-resume"].includes(command)) {
    const tab = await checkedTab(args.tab, args.decisions || {}, command);
    assertVersion(args.tab);
    const id = tab.id;
    if (command === "tab-close") {
      await chrome.tabs.remove(id);
      const remains = (await chrome.tabs.query({})).some(current => current.id === id);
      if (remains) fail("browser_action_unverified");
      return {status: "completed", tab_id: id, operation: "closed", restore_state: "URL only; unsaved forms and in-page state are not recoverable"};
    }
    if (command === "tab-discard") {
      const discarded = await chrome.tabs.discard(id);
      if (!discarded || discarded.id !== id || !discarded.discarded) fail("browser_action_unverified");
      return {status: "completed", tab_id: id, operation: "discarded", reload_on_resume: true};
    }
    if (!tab.discarded) fail("browser_action_unavailable");
    // Activating is Chrome's supported way to resume a discarded tab. Reloading
    // a background discarded tab is not silently substituted.
    await chrome.tabs.update(id, {active: true});
    const resumed = await chrome.tabs.get(id);
    if (resumed.discarded) fail("browser_action_unverified");
    return {status: "completed", tab_id: id, operation: "resumed", activated: true};
  }
  if (command === "tab-reopen") {
    if (!safeReopen(args.url) || !Number.isInteger(args.window_id)) fail("browser_action_unavailable");
    const window = await chrome.windows.get(args.window_id);
    if (window.incognito) fail("browser_private_scope");
    const tab = await chrome.tabs.create({windowId: args.window_id, url: args.url, active: false});
    if (!Number.isInteger(tab.id) || tab.incognito) fail("browser_action_unverified");
    return {status: "completed", tab_id: tab.id, operation: "reopened", restore_state: "URL only"};
  }
  if (command === "privacy-clear") {
    const types = args.types;
    if (!Array.isArray(types) || types.length !== 1 || !privacyTypes.has(types[0])) fail("browser_action_unavailable");
    if (!Number.isFinite(args.since_ms) || args.since_ms < 0 || args.since_ms > Date.now()) fail("browser_bridge_protocol");
    const origins = args.origins || [];
    if (!Array.isArray(origins) || origins.length > 100 || (origins.length && !originTypes.has(types[0]))) fail("browser_action_unavailable");
    for (const origin of origins) {
      let url; try { url = new URL(origin); } catch (_) { fail("browser_bridge_protocol"); }
      if (!["https:", "http:"].includes(url.protocol) || url.origin !== origin || url.username || url.password) fail("browser_bridge_protocol");
    }
    const settings = await chrome.browsingData.settings();
    if (settings.dataRemovalPermitted && settings.dataRemovalPermitted[types[0]] === false) fail("browser_action_unavailable");
    const options = {since: args.since_ms, originTypes: {unprotectedWeb: true, protectedWeb: false, extension: false}};
    if (origins.length) options.origins = origins;
    await chrome.browsingData.remove(options, {[types[0]]: true});
    return {status: "completed", operation: "privacy-cleared", types, verification: "browser API completion; no item-level enumeration or reclaimed-byte claim", downloaded_files_changed: false};
  }
  fail("browser_action_unavailable");
}

async function connect(label) {
  if (nativePort) nativePort.disconnect();
  const saved = await chrome.storage.local.get(["profileId"]);
  const profileId = saved.profileId || crypto.randomUUID();
  await chrome.storage.local.set({profileId, profileLabel: label});
  const port = chrome.runtime.connectNative("com.joeblau.cleanmac");
  nativePort = port; connectedProfile = null; connectionMessage = "Connecting…";
  port.onMessage.addListener(async message => {
    if (message.type === "connected") {
      connectedProfile = message.profile;
      connectionMessage = `Connected: ${label}. Return to CleanMac to review this profile.`;
      return;
    }
    if (!connectedProfile || typeof message.id !== "string") return;
    try {
      if (message.profile_id !== connectedProfile.profile_id || message.session_id !== connectedProfile.session_id) fail("browser_target_changed");
      const result = await handleCommand(message.command, message.arguments, connectedProfile);
      port.postMessage({id: message.id, ok: true, result});
    } catch (error) {
      // Chrome API error text can contain private URLs: expose only our codes.
      port.postMessage({id: message.id, ok: false, error: {code: /^browser_[a-z_]+$/.test(error.code || "") ? error.code : "browser_action_unavailable"}});
    }
  });
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    if (nativePort === port) { nativePort = null; connectedProfile = null; connectionMessage = "Disconnected. Check native-host registration, then connect again."; }
  });
  port.postMessage({type: "hello", protocol_version: 1, profile_id: profileId, label, extension_id: chrome.runtime.id});
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (message.type === "connect") {
    if (typeof message.label !== "string" || !message.label.trim() || message.label.length > 80 || /[\u0000-\u001f]/.test(message.label)) { reply({message: "Enter a valid profile label."}); return false; }
    connect(message.label.trim()).then(() => reply({message: connectionMessage})); return true;
  }
  if (message.type === "disconnect") { if (nativePort) nativePort.disconnect(); nativePort = null; connectedProfile = null; connectionMessage = "Disconnected."; }
  reply({message: connectionMessage}); return false;
});
chrome.tabs.onCreated.addListener(tab => { if (!tab.incognito) observedAt.set(tab.id, {at: Date.now(), source: "creation-observed"}); });
chrome.tabs.onRemoved.addListener(id => { observedAt.delete(id); tabVersions.delete(id); });
chrome.tabs.onUpdated.addListener((id, change) => {
  if (["url", "pinned", "audible", "discarded", "autoDiscardable"].some(key => change[key] !== undefined)
      || change.status === "loading") versionFor(id).revision++;
});
chrome.tabs.onReplaced.addListener((added, removed) => { tabVersions.delete(removed); observedAt.delete(removed); versionFor(added); });
chrome.tabs.onActivated.addListener(info => {
  for (const [id, version] of tabVersions) {
    if (id === info.tabId || version.window_id === info.windowId) version.revision++;
  }
});
chrome.tabs.onAttached.addListener(id => { versionFor(id).revision++; });
chrome.tabs.onDetached.addListener(id => { versionFor(id).revision++; });
chrome.tabs.onMoved.addListener(id => { versionFor(id).revision++; });
