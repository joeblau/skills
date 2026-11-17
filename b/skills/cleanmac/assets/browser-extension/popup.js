"use strict";
document.getElementById("extension").textContent = chrome.runtime.id;
const status = document.getElementById("status");
chrome.storage.local.get(["profileLabel"]).then(value => { document.getElementById("label").value = value.profileLabel || ""; });
document.getElementById("connect").addEventListener("click", async () => {
  const label = document.getElementById("label").value.trim();
  if (!label || /[\u0000-\u001f]/.test(label)) { status.textContent = "Enter a profile label."; return; }
  const reply = await chrome.runtime.sendMessage({type: "connect", label});
  status.textContent = reply.message;
});
document.getElementById("disconnect").addEventListener("click", async () => {
  const reply = await chrome.runtime.sendMessage({type: "disconnect"}); status.textContent = reply.message;
});
async function refreshStatus() {
  try {
    const reply = await chrome.runtime.sendMessage({type: "status"});
    status.textContent = reply.message;
  } catch (_) {
    status.textContent = "The companion restarted. Reopen this popup and reconnect.";
  }
}
refreshStatus();
// The native handshake finishes after Connect returns. Poll only while this
// short-lived popup is open so its status reflects success or registration errors.
setInterval(refreshStatus, 1000);
