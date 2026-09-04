"use strict";

// Matches the real USB-2SP4T-852H: two SP4T groups, each COM selectable to 4 ports.
let CONFIG = { channels: ["A", "B"], ports: [1, 2, 3, 4] };

// Per-unit COM aliases. Only this specific unit gets channel-number labels:
// COM A = CH-1, COM B = CH-2. Other units render the plain "COM A/B" label.
const COM_ALIASES = {
  "12602090027": { A: "CH-1", B: "CH-2" },
  "12602090016": { A: "CH-3", B: "CH-4" },
};

const KNOWN = {};      // serial -> {serial, model, firmware} (every unit seen this session)
let RENDERED = "";     // sorted serial list currently drawn, to know when to rebuild
let POLL_MS = 450;     // live heartbeat — fast presence + state sync

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const postJSON = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

const $ = (id) => document.getElementById(id);

function setStatus(present, total) {
  const el = $("status");
  if (present > 0 && present === total) {
    el.textContent = `● CONNECTED · ${present} unit${present > 1 ? "s" : ""}`;
    el.className = "status connected";
  } else if (present > 0) {
    el.textContent = `● ${present}/${total} CONNECTED · ${total - present} offline`;
    el.className = "status partial";
  } else {
    el.textContent = "● DISCONNECTED";
    el.className = "status disconnected";
  }
}

function showBanner(msg) { $("banner-text").textContent = msg; $("banner").classList.remove("hidden"); }
function hideBanner() { $("banner").classList.add("hidden"); }
function showError(msg) { const o = $("scpi-out"); o.textContent = "Error: " + msg; o.classList.add("err"); }

// ---- faceplate builders ------------------------------------------------- //
function portConn(serial, ch, port, active) {
  return `<div class="sma-slot">
            <button class="conn port ${active ? "active" : ""}"
                    data-serial="${serial}" data-ch="${ch}" data-port="${port}"
                    title="Click to switch COM ${ch} → port ${port}">
              <span class="pin"></span>
            </button>
            <span class="sma-label">${port}</span>
          </div>`;
}
function comConn(serial, ch, current) {
  const alias = (COM_ALIASES[serial] || {})[ch];   // e.g. "CH-1" for SN 12602090027
  const aliasTag = alias ? `<span class="ch-alias">${alias}</span>` : "";
  return `<div class="sma-slot">
            <button class="conn com ${current ? "linked" : ""}"
                    data-serial="${serial}" data-ch="${ch}" data-com="1"
                    title="Common ${ch}${alias ? " (" + alias + ")" : ""}">
              <span class="pin"></span>
            </button>
            <span class="sma-label com-label">COM&nbsp;${ch}${aliasTag}</span>
          </div>`;
}
function sp4tGroup(serial, ch, current) {
  const p = CONFIG.ports;
  const cells = [
    portConn(serial, ch, p[0], current === p[0]),
    portConn(serial, ch, p[1], current === p[1]),
    comConn(serial, ch, current),
    portConn(serial, ch, p[2], current === p[2]),
    portConn(serial, ch, p[3], current === p[3]),
  ].join("");
  return `<div class="sp4t-group" data-ch="${ch}">
            <div class="sma-row">${cells}</div>
            <div class="group-tag">SWITCH ${ch}</div>
          </div>`;
}
function frontFace(u, st) {
  return `<div class="face front">
            <div class="top-edge">
              <span class="edge-spacer"></span>
              <span class="led pwr" title="power / connected"></span>
              <span class="edge-label">PWR</span>
              <span class="usb" title="USB Mini-B"></span>
            </div>
            <div class="plate-mid">
              <span class="brand">Mini-Circuits</span>
              <span class="subtitle">USB DUAL SOLID-STATE SP4T RF SWITCH</span>
              <span class="model">MODEL ${u.model || "USB-2SP4T-852H"}</span>
            </div>
            <div class="conn-deck">
              ${sp4tGroup(u.serial, "A", st.A)}
              <span class="group-divider"></span>
              ${sp4tGroup(u.serial, "B", st.B)}
            </div>
            <span class="facetag">FRONT · SN ${u.serial}</span>
          </div>`;
}
function rearFace(u, st) {
  const echo = CONFIG.channels
    .map((ch) => `<span>COM ${ch} → ${st[ch] ? "port " + st[ch] : "open"}</span>`).join("");
  return `<div class="face rear">
            <span class="screw tl"></span><span class="screw tr"></span>
            <span class="screw bl"></span><span class="screw br"></span>
            <div class="reg-mark">Regulatory Logo Marking</div>
            <div class="rear-echo">${echo}</div>
            <span class="facetag">REAR · ${u.firmware ? "fw " + u.firmware : ""}</span>
          </div>`;
}

// ---- full render (only when the set of known units changes) -------------- //
function renderUnits(unitList, states, present) {
  const rack = $("rack");
  rack.innerHTML = "";
  if (!unitList.length) {
    rack.innerHTML = '<p class="empty">No switches connected. Plug one in — it will appear here automatically.</p>';
    return;
  }
  unitList.forEach((u) => {
    const st = states[u.serial] || {};
    const online = present.has(u.serial);
    const row = document.createElement("div");
    row.className = "rack-unit" + (online ? "" : " offline");
    row.dataset.serial = u.serial;
    row.innerHTML = `
      <div class="rack-scene">
        <div class="rack-bar" data-face="front">
          ${frontFace(u, st)}
          ${rearFace(u, st)}
        </div>
        <span class="offline-tag">DISCONNECTED</span>
      </div>
      <div class="rack-meta">
        <span class="led status-led ${online ? "on" : ""}" title="${online ? "connected" : "disconnected"}"></span>
        <span class="meta-serial">${u.serial}</span>
        <span class="meta-model">${u.model || ""}${u.firmware ? " · fw " + u.firmware : ""}</span>
        <span class="meta-state" data-stats>${stateLine(st)}</span>
        <button class="flip ghost btn-sm" type="button">⟲ Flip</button>
      </div>`;
    rack.appendChild(row);
  });
  wireRow(rack);
}

function stateLine(st) {
  return `COM A → ${st.A ? "port " + st.A : "—"}  ·  COM B → ${st.B ? "port " + st.B : "—"}`;
}

function wireRow(rack) {
  rack.querySelectorAll(".flip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const bar = btn.closest(".rack-unit").querySelector(".rack-bar");
      bar.dataset.face = bar.dataset.face === "rear" ? "front" : "rear";
    });
  });
  // Left-click a port = select it (switch COM there). Optimistic glow, then confirm.
  rack.querySelectorAll(".conn.port").forEach((btn) => {
    btn.addEventListener("click", () => selectPort(btn));
  });
}

// ---- selection (left-click) ---------------------------------------------- //
async function selectPort(btn) {
  const { serial, ch, port } = btn.dataset;
  const row = btn.closest(".rack-unit");
  if (row.classList.contains("offline")) return;   // can't switch an unplugged unit
  const p = Number(port);
  // optimistic: move the glow immediately for a snappy feel
  optimisticGlow(row, ch, p);
  try {
    await postJSON(`/api/units/${encodeURIComponent(serial)}/state`, { [ch]: p });
    const live = await api(`/api/units/${encodeURIComponent(serial)}/state`);
    applyUnitState(serial, live);                   // confirm against hardware readback
    const r = [...$("rack").children].find((x) => x.dataset.serial === serial);
    if (r) r.querySelector("[data-stats]").textContent = `COM ${ch} → port ${live[ch]}`;
  } catch (e) { showError(e.message); }
}

function optimisticGlow(row, ch, port) {
  row.querySelectorAll(`.conn.port[data-ch="${ch}"]`).forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.port) === port);
  });
  const com = row.querySelector(`.conn.com[data-ch="${ch}"]`);
  if (com) com.classList.add("linked");
}

// ---- in-place updates (no rebuild → flips & glow preserved) -------------- //
function applyUnitState(serial, live) {
  const row = [...$("rack").children].find((r) => r.dataset.serial === serial);
  if (!row) return;
  CONFIG.channels.forEach((ch) => {
    row.querySelectorAll(`.conn.port[data-ch="${ch}"]`).forEach((b) => {
      b.classList.toggle("active", Number(b.dataset.port) === live[ch]);
    });
    const com = row.querySelector(`.conn.com[data-ch="${ch}"]`);
    if (com) com.classList.toggle("linked", !!live[ch]);
  });
  const stats = row.querySelector("[data-stats]");
  if (stats) stats.textContent = stateLine(live);
}

function applyStates(states) {
  Object.entries(states).forEach(([serial, live]) => applyUnitState(serial, live));
}

function applyPresence(present) {
  [...$("rack").children].forEach((row) => {
    if (!row.dataset.serial) return;
    const online = present.has(row.dataset.serial);
    row.classList.toggle("offline", !online);
    const led = row.querySelector(".status-led");
    if (led) { led.classList.toggle("on", online); led.title = online ? "connected" : "disconnected"; }
  });
}

// ---- config / dropdowns (refreshed when topology changes) --------------- //
async function loadConfig() {
  try {
    const data = await api("/api/units");
    CONFIG = { channels: data.channels, ports: data.ports };
  } catch (_) { /* keep defaults */ }
  const sel = $("scpi-serial");
  sel.innerHTML = "";
  Object.keys(KNOWN).forEach((s) => {
    const o = document.createElement("option");
    o.value = s; o.textContent = s;
    sel.appendChild(o);
  });
}

// ---- presets ------------------------------------------------------------- //
function presetSummary(states) {
  return Object.entries(states || {})
    .map(([sn, st]) => `${sn.slice(-4)}: A${st.A ?? "–"}/B${st.B ?? "–"}`)
    .join("  ·  ");
}

function renderPresets(list) {
  const wrap = $("presets");
  wrap.innerHTML = "";
  if (!list.length) {
    wrap.innerHTML = '<span class="empty">No presets yet — set the routing you want, name it below, and save.</span>';
    return;
  }
  list.forEach((p) => {
    const chip = document.createElement("div");
    chip.className = "preset-chip";
    chip.innerHTML = `
      <button class="preset-apply" title="Apply — ${presetSummary(p.states)}">${p.name}</button>
      <button class="preset-del" title="Delete preset">×</button>`;
    chip.querySelector(".preset-apply").addEventListener("click", () => applyPreset(p.name));
    chip.querySelector(".preset-del").addEventListener("click", () => deletePreset(p.name));
    wrap.appendChild(chip);
  });
}

async function loadPresets() {
  try { renderPresets((await api("/api/presets")).presets || []); }
  catch (_) { /* ignore */ }
}
async function applyPreset(name) {
  try {
    const states = await postJSON(`/api/presets/${encodeURIComponent(name)}/apply`, {});
    applyStates(states);
  } catch (e) { showError(e.message); }
}
async function deletePreset(name) {
  try { renderPresets((await api(`/api/presets/${encodeURIComponent(name)}`, { method: "DELETE" })).presets || []); }
  catch (e) { showError(e.message); }
}

function stamp() { $("last-update").textContent = "updated " + new Date().toLocaleTimeString(); }

// ---- the live heartbeat -------------------------------------------------- //
async function poll() {
  let health;
  try {
    health = await api("/api/health");
  } catch (e) {
    setStatus(0, Object.keys(KNOWN).length);
    showBanner(e.message);
    applyPresence(new Set());
    return;
  }
  const units = health.units || [];
  const present = new Set(units.map((u) => u.serial));
  units.forEach((u) => { KNOWN[u.serial] = u; });

  const total = Object.keys(KNOWN).length;
  setStatus(present.size, total);
  if (present.size > 0) hideBanner(); else showBanner(health.error || "No switch connected.");

  // Rebuild only when the known set changes (new unit appears); else update in place.
  const knownSerials = Object.keys(KNOWN).sort().join(",");
  let states = {};
  try { states = await api("/api/states"); } catch (_) { states = {}; }

  if (knownSerials !== RENDERED) {
    await loadConfig();
    renderUnits(Object.values(KNOWN), states, present);
    RENDERED = knownSerials;
  } else {
    applyStates(states);
    applyPresence(present);
  }
  stamp();
}

function startPolling() {
  poll();
  setInterval(poll, POLL_MS);
}

// ---- controls ------------------------------------------------------------ //
$("scpi-send").addEventListener("click", async () => {
  const out = $("scpi-out");
  try {
    const data = await postJSON("/api/scpi", { serial: $("scpi-serial").value, command: $("scpi-cmd").value });
    out.classList.remove("err");
    out.textContent = `${$("scpi-cmd").value}  ->  ${data.reply === "" ? "(empty)" : data.reply}`;
  } catch (e) { showError(e.message); }
});
$("scpi-cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("scpi-send").click(); });

$("preset-save").addEventListener("click", async () => {
  const name = $("preset-name").value.trim();
  if (!name) { $("preset-name").focus(); return; }
  try {
    renderPresets((await postJSON("/api/presets", { name })).presets || []);
    $("preset-name").value = "";
  } catch (e) { showError(e.message); }
});
$("preset-name").addEventListener("keydown", (e) => { if (e.key === "Enter") $("preset-save").click(); });

$("refresh").addEventListener("click", () => poll());

$("retry").addEventListener("click", async () => {
  $("banner-text").textContent = "reconnecting…";
  try { await postJSON("/api/reconnect"); } catch (_) { /* surfaced by poll */ }
  poll();
});

// ---- command-reference filter ------------------------------------------- //
const refFilter = $("ref-filter");
if (refFilter) {
  refFilter.addEventListener("input", () => {
    const q = refFilter.value.trim().toLowerCase();
    let anyVisible = false;
    document.querySelectorAll(".ref-block").forEach((block) => {
      const rows = block.querySelectorAll(".ref-table tbody tr, .ref-notes li");
      if (!q) {
        // cleared: restore default — unhide everything, collapse to defaults
        block.classList.remove("hidden");
        rows.forEach((r) => r.classList.remove("hidden"));
        block.open = block.hasAttribute("data-default-open");
        anyVisible = true;
        return;
      }
      let blockVisible = false;
      rows.forEach((row) => {
        const hit = row.textContent.toLowerCase().includes(q);
        row.classList.toggle("hidden", !hit);
        if (hit) blockVisible = true;
      });
      block.classList.toggle("hidden", !blockVisible);
      block.open = blockVisible;          // auto-expand sections that have a match
      if (blockVisible) anyVisible = true;
    });
    $("ref-empty").classList.toggle("hidden", anyVisible);
  });
}

loadPresets();
startPolling();
