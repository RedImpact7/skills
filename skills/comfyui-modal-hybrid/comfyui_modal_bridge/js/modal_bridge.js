import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ------------------------------------------------------------ helpers ---
const css = `
#modal-bridge-panel{position:fixed;right:16px;bottom:16px;z-index:9999;display:flex;gap:6px;align-items:center;
  font:13px system-ui,sans-serif;background:#1f1f24;border:1px solid #3a3a44;border-radius:10px;padding:6px;box-shadow:0 4px 16px #0006}
#modal-bridge-panel button{border:0;border-radius:7px;padding:7px 12px;cursor:pointer;color:#fff;background:#3a3a44;font:inherit}
#modal-bridge-panel button.primary{background:#16a34a;font-weight:600}
#modal-bridge-panel button.primary:disabled{background:#4b5563;cursor:wait}
#modal-bridge-status{color:#cbd5e1;max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 4px}
#modal-bridge-gallery{position:fixed;inset:0;z-index:10000;background:#000c;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:24px;gap:12px}
#modal-bridge-gallery .grid{display:flex;flex-wrap:wrap;gap:12px;justify-content:center;overflow:auto;max-height:80vh}
#modal-bridge-gallery img,#modal-bridge-gallery video{max-width:min(90vw,768px);max-height:75vh;border-radius:8px}
#modal-bridge-gallery .bar{color:#e5e7eb;font:14px system-ui}
#modal-bridge-gallery button{padding:8px 16px;border-radius:8px;border:0;cursor:pointer}
`;

let statusEl, runBtn, cancelBtn, currentJob = null;

function toast(severity, summary, detail) {
  try {
    app.extensionManager?.toast?.add({ severity, summary, detail, life: severity === "error" ? 12000 : 5000 });
  } catch (_) { /* old frontend */ }
}

function setStatus(text, busy) {
  statusEl.textContent = text;
  statusEl.title = text;
  runBtn.disabled = !!busy;
  cancelBtn.style.display = busy ? "" : "none";
}

function viewUrl(f) {
  const q = new URLSearchParams({ filename: f.filename, subfolder: f.subfolder, type: f.type });
  return api.apiURL(`/view?${q.toString()}`);
}

function showGallery(files, info) {
  document.getElementById("modal-bridge-gallery")?.remove();
  const wrap = document.createElement("div");
  wrap.id = "modal-bridge-gallery";
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const f of files) {
    const isVideo = /\.(mp4|webm|mov|mkv)$/i.test(f.filename);
    const isImage = /\.(png|jpe?g|webp|gif|bmp)$/i.test(f.filename);
    let el;
    if (isVideo) { el = document.createElement("video"); el.controls = true; el.loop = true; el.autoplay = true; el.muted = true; }
    else if (isImage) { el = document.createElement("img"); }
    else { el = document.createElement("a"); el.textContent = f.filename; el.style.color = "#93c5fd"; el.href = viewUrl(f); }
    if (isVideo || isImage) el.src = viewUrl(f);
    grid.appendChild(el);
  }
  const bar = document.createElement("div");
  bar.className = "bar";
  bar.textContent = `${files.length} file(s) saved to output/${files[0]?.subfolder ?? "modal"}  •  ${info}`;
  const close = document.createElement("button");
  close.textContent = "Close";
  close.onclick = () => wrap.remove();
  wrap.onclick = (e) => { if (e.target === wrap) wrap.remove(); };
  wrap.append(grid, bar, close);
  document.body.appendChild(wrap);
}

function showOnNodes(files) {
  // Best effort: show results as previews on the nodes that produced them.
  const byNode = {};
  for (const f of files) {
    if (!f.node_id || !/\.(png|jpe?g|webp|gif)$/i.test(f.filename)) continue;
    (byNode[f.node_id] ??= []).push({ filename: f.filename, subfolder: f.subfolder, type: f.type });
  }
  for (const [id, images] of Object.entries(byNode)) {
    try {
      api.dispatchCustomEvent("executed", { node: id, display_node: id, output: { images }, prompt_id: currentJob });
    } catch (_) { /* ignore */ }
  }
}

// ------------------------------------------------------------ actions ---
async function configure() {
  const cur = await (await api.fetchApi("/modal_bridge/config")).json();
  const endpoint = prompt(
    "Modal endpoint URL (from `modal deploy`, e.g. https://yourname--comfy-hybrid-api.modal.run):",
    cur.endpoint || "");
  if (endpoint === null) return;
  const token = prompt(cur.has_token ? "Bridge token (leave empty to keep current):" : "Bridge token (BRIDGE_TOKEN in Modal secret):", "");
  if (token === null) return;
  await api.fetchApi("/modal_bridge/config", { method: "POST", body: JSON.stringify({ endpoint, token }) });
  const h = await (await api.fetchApi("/modal_bridge/health")).json();
  if (h.ok) toast("success", "Modal connected", `GPU: ${h.gpu}`);
  else toast("error", "Modal not reachable", h.error || "check URL");
  setStatus(h.ok ? `Connected (${h.gpu})` : "Not connected", false);
}

async function runOnModal() {
  const cfg = await (await api.fetchApi("/modal_bridge/config")).json();
  if (!cfg.endpoint || !cfg.has_token) { await configure(); return; }
  setStatus("Preparing workflow...", true);
  try {
    const { output } = await app.graphToPrompt();
    const r = await api.fetchApi("/modal_bridge/run", { method: "POST", body: JSON.stringify({ prompt: output }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    currentJob = j.job_id;
  } catch (e) {
    setStatus("Error", false);
    toast("error", "Run on Modal failed", String(e.message || e));
  }
}

async function cancelJob() {
  if (currentJob) await api.fetchApi(`/modal_bridge/cancel/${currentJob}`, { method: "POST" });
}

async function syncModels() {
  setStatus("Syncing model list from Modal...", true);
  const r = await api.fetchApi("/modal_bridge/sync_models", { method: "POST" });
  const j = await r.json();
  setStatus(r.ok ? `Model list synced (${j.remote_total} in cloud)` : "Sync failed", false);
  if (r.ok) toast("success", "Model list synced",
    `${j.created} new placeholder(s). Refresh (press R) to see them in dropdowns. Placeholders are 0-byte: always use "Run on Modal".`);
  else toast("error", "Sync failed", j.error);
}

// ---------------------------------------------------------- extension ---
app.registerExtension({
  name: "modal.bridge",
  commands: [
    { id: "modal.bridge.run", label: "Run on Modal (cloud GPU)", icon: "pi pi-cloud-upload", function: runOnModal },
    { id: "modal.bridge.sync", label: "Sync model list from Modal", icon: "pi pi-sync", function: syncModels },
    { id: "modal.bridge.config", label: "Modal settings", icon: "pi pi-cog", function: configure },
  ],
  menuCommands: [{ path: ["Modal"], commands: ["modal.bridge.run", "modal.bridge.sync", "modal.bridge.config"] }],
  keybindings: [{ combo: { key: "Enter", ctrl: true, shift: true }, commandId: "modal.bridge.run" }],

  async setup() {
    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);

    const panel = document.createElement("div");
    panel.id = "modal-bridge-panel";
    runBtn = Object.assign(document.createElement("button"), { className: "primary", textContent: "☁ Run on Modal", title: "Ctrl+Shift+Enter" });
    runBtn.onclick = runOnModal;
    cancelBtn = Object.assign(document.createElement("button"), { textContent: "✕", title: "Cancel" });
    cancelBtn.onclick = cancelJob;
    const syncBtn = Object.assign(document.createElement("button"), { textContent: "⟳", title: "Sync model list from Modal" });
    syncBtn.onclick = syncModels;
    const cfgBtn = Object.assign(document.createElement("button"), { textContent: "⚙", title: "Modal settings" });
    cfgBtn.onclick = configure;
    statusEl = Object.assign(document.createElement("span"), { id: "modal-bridge-status" });
    panel.append(runBtn, cancelBtn, statusEl, syncBtn, cfgBtn);
    document.body.appendChild(panel);
    setStatus("Ready", false);

    api.addEventListener("modal_bridge.status", ({ detail }) => {
      if (detail.job_id === currentJob) setStatus(detail.message, true);
    });
    api.addEventListener("modal_bridge.done", ({ detail }) => {
      if (detail.job_id !== currentJob) return;
      const info = `GPU ${detail.gpu}: ${detail.gpu_seconds}s, total ${detail.total_seconds}s`;
      setStatus(`Done • ${info}`, false);
      toast("success", "Modal job done", info);
      if (detail.files.length) { showOnNodes(detail.files); showGallery(detail.files, info); }
      else toast("warn", "No outputs", "Add a Save Image / Video Combine node to your workflow.");
    });
    api.addEventListener("modal_bridge.error", ({ detail }) => {
      if (detail.job_id !== currentJob) return;
      setStatus("Error - see message", false);
      toast("error", "Modal job failed", detail.error);
      console.error("[modal-bridge]", detail.error);
    });
  },
});
