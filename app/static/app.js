/* WhaleMirror console */

const $ = (id) => document.getElementById(id);
let settings = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 401) { showLogin(); throw new Error("unauthorized"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

/* ── Auth ──────────────────────────────────────────────────────────── */
function showLogin() { $("login").classList.remove("hidden"); $("app").classList.add("hidden"); }
function showApp()   { $("login").classList.add("hidden");   $("app").classList.remove("hidden"); }

$("login-btn").onclick = async () => {
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("password").value }) });
    $("login-error").classList.add("hidden");
    $("password").value = "";
    boot();
  } catch (e) {
    $("login-error").textContent = e.message;
    $("login-error").classList.remove("hidden");
  }
};
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") $("login-btn").click(); });
$("logout-btn").onclick = async () => { await api("/api/logout", { method: "POST" }); showLogin(); };

/* ── Tabs ──────────────────────────────────────────────────────────── */
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
    t.classList.add("active");
    $("tab-" + t.dataset.tab).classList.remove("hidden");
    if (t.dataset.tab === "activity") loadActivity();
    if (t.dataset.tab === "performance") loadPerformance();
    if (t.dataset.tab === "whales") loadWhales();
    if (t.dataset.tab === "settings") loadSettings();
  };
});

/* ── Filter controls ───────────────────────────────────────────────── */
$("f-search").addEventListener("input", (e) => { filters.search = e.target.value.toLowerCase(); if (lastData) renderSignals(lastData); });
$("f-sort").addEventListener("change", (e) => { filters.sort = e.target.value; if (lastData) renderSignals(lastData); });
[["f-new", "newOnly"], ["f-unmirrored", "unmirrored"], ["f-followed", "followedOnly"]].forEach(([id, key]) => {
  $(id).onclick = () => { filters[key] = !filters[key]; $(id).classList.toggle("on", filters[key]); if (lastData) renderSignals(lastData); };
});

/* ── Signals ───────────────────────────────────────────────────────── */
function fmtAgo(ts) {
  if (!ts) return "never";
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 1 ? "just now" : m < 60 ? `${m}m ago` : `${Math.round(m / 60)}h ago`;
}

function gaugeHTML(s) {
  const lo = Math.min(s.avg_whale_entry, s.current_price) * 100;
  const hi = Math.max(s.avg_whale_entry, s.current_price) * 100;
  const driftCls = s.entry_drift >= 0 ? "drift-up" : "drift-down";
  const driftTxt = (s.entry_drift >= 0 ? "+" : "") + (s.entry_drift * 100).toFixed(1) + "\u00A2";
  return `
    <div class="gauge">
      <div class="rail">
        <div class="span" style="left:${lo}%; width:${Math.max(hi - lo, 0.6)}%"></div>
        <div class="tick-entry" style="left:${s.avg_whale_entry * 100}%" title="Whale avg entry ${s.avg_whale_entry}"></div>
        <div class="tick-now" style="left:${s.current_price * 100}%" title="Current price ${s.current_price}"></div>
      </div>
      <div class="gauge-labels">
        <span>entry ${(s.avg_whale_entry * 100).toFixed(0)}\u00A2</span>
        <span class="${driftCls}">drift ${driftTxt}</span>
        <span>now ${(s.current_price * 100).toFixed(0)}\u00A2</span>
      </div>
      ${whaleChips(s)}
    </div>`;
}

function endsIn(endDate) {
  const d = Math.ceil((new Date(endDate) - Date.now()) / 86400000);
  return isFinite(d) ? ` <span class="held">(in ${d}d)</span>` : "";
}

let lastData = null;
let followedSet = new Set();
const filters = { search: "", sort: "score", newOnly: false, unmirrored: false, followedOnly: false };

function whaleChips(s) {
  const details = s.whale_details || (s.whales || []).map((n) => ({ name: n, address: null }));
  return `<div class="whale-chips">` + details.map((w) => {
    const on = w.address && followedSet.has(w.address);
    const attrs = w.address
      ? `data-follow="${esc(w.address)}" data-name="${esc(w.name)}" title="${on ? "Unfollow" : "Follow"} ${esc(w.name)}"`
      : `title="${esc(w.name)}"`;
    return `<span class="whale-chip ${on ? "followed" : ""}" ${attrs}><span class="star">${on ? "\u2605" : "\u2606"}</span>${esc(w.name)}</span>`;
  }).join("") + `</div>`;
}

function applyFilters(signals, mirroredIds) {
  const mirrored = new Set(mirroredIds || []);
  const dayAgo = Date.now() / 1000 - 86400;
  let out = signals.filter((s) => {
    if (filters.search && !s.title.toLowerCase().includes(filters.search)) return false;
    if (filters.newOnly && (s.first_seen || 0) < dayAgo) return false;
    if (filters.unmirrored && mirrored.has(s.id)) return false;
    if (filters.followedOnly) {
      const addrs = (s.whale_details || []).map((w) => w.address);
      if (!addrs.some((a) => followedSet.has(a))) return false;
    }
    return true;
  });
  const key = filters.sort;
  out.sort((a, b) => {
    if (key === "end_date") return (a.end_date || "9999") < (b.end_date || "9999") ? -1 : 1;   // soonest first
    if (key === "entry_drift") return a.entry_drift - b.entry_drift;                             // best mirror price first
    return (b[key] || 0) - (a[key] || 0);                                                        // descending
  });
  return out;
}

function renderSignals(data) {
  lastData = data;
  followedSet = new Set(Object.keys(data.followed || {}));
  const list = $("signals-list");
  $("signal-count").textContent = data.signals.length;
  $("last-sweep").textContent = fmtAgo(data.last_refresh);

  const sonar = $("sonar");
  sonar.className = "sonar" + (data.refreshing ? " busy" : data.last_error ? " error" : "");
  $("progress").classList.toggle("hidden", !data.refreshing);
  $("progress").textContent = data.progress || "Sweeping\u2026";

  const banner = $("banner");
  if (data.last_error) {
    banner.className = "banner err"; banner.textContent = "Last sweep failed: " + data.last_error;
    banner.classList.remove("hidden");
  } else if (data.auto_results && data.auto_results.length) {
    banner.className = "banner ok";
    banner.textContent = `Auto-mirror: ${data.auto_results.length} signal(s) processed this sweep.`;
    banner.classList.remove("hidden");
  } else banner.classList.add("hidden");

  const mirrored = new Set(data.mirrored_ids || []);
  const shown = applyFilters(data.signals, data.mirrored_ids);
  $("f-count").textContent = shown.length === data.signals.length
    ? `${shown.length} signals` : `${shown.length} of ${data.signals.length}`;
  $("signals-empty").classList.toggle("hidden", shown.length > 0);

  list.innerHTML = shown.map((s) => `
    <div class="signal">
      <div class="signal-title">${s.url
          ? `<a class="market-link" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>`
          : esc(s.title)}<span class="side">\u2192 ${esc(s.outcome)}</span>
        ${s.signal_type === "followed" ? `<span class="tag tag-followed">FOLLOWED \u00B7 ${esc(s.followed_by || "")}</span>` : ""}
        ${mirrored.has(s.id) ? `<span class="tag tag-mirrored">\u2713 mirrored</span>` : ""}
      </div>
      <div class="signal-actions">
        <span class="score" title="Signal score">${s.score.toFixed(1)}</span>
        <button class="btn btn-mirror" data-mirror="${s.id}">Mirror</button>
      </div>
      <div class="signal-meta">
        <span><b>${s.whale_count}</b> whale${s.whale_count > 1 ? "s" : ""}</span>
        <span><b>$${s.whale_dollars.toLocaleString()}</b> behind it</span>
        <span><b>${(s.dominance * 100).toFixed(0)}%</b> dominance</span>
        ${s.category ? `<span>${esc(s.category)}</span>` : ""}
        ${s.end_date ? `<span>ends <b>${esc(String(s.end_date).slice(0, 10))}</b>${endsIn(s.end_date)}</span>` : ""}
      </div>
      ${s.opposing && s.opposing.whale_count ? `
      <div class="opposing">vs ${esc(s.opposing.outcome || "other side")}: <b>${s.opposing.whale_count}</b> whale${s.opposing.whale_count > 1 ? "s" : ""} \u00B7 <b>$${s.opposing.whale_dollars.toLocaleString()}</b>
        <span title="${esc((s.opposing.whale_details || []).map((w) => w.name).join(", "))}">(hover for names)</span></div>` : ""}
      ${gaugeHTML(s)}
    </div>`).join("");

  list.querySelectorAll("[data-mirror]").forEach((btn) => {
    btn.onclick = async () => {
      const usd = settings ? settings.per_trade_usd : 25;
      const live = settings && !settings.dry_run;
      const label = live ? `Place a LIVE $${usd} order on this signal?` : `Simulate a $${usd} mirror of this signal?`;
      if (!confirm(label)) return;
      btn.disabled = true;
      try {
        const r = await api(`/api/mirror/${btn.dataset.mirror}`, { method: "POST", body: JSON.stringify({}) });
        flash(r.status === "ok" ? "ok" : "err", r.detail);
      } catch (e) { flash("err", e.message); }
      btn.disabled = false;
    };
  });

  list.querySelectorAll("[data-follow]").forEach((chip) => {
    chip.onclick = async () => {
      const addr = chip.dataset.follow, name = chip.dataset.name;
      try {
        if (followedSet.has(addr)) {
          const r = await api(`/api/whales/follow/${addr}`, { method: "DELETE" });
          lastData.followed = r.followed;
          flash("ok", `Unfollowed ${name}.`);
        } else {
          const r = await api("/api/whales/follow", { method: "POST", body: JSON.stringify({ address: addr, name }) });
          lastData.followed = r.followed;
          flash("ok", `Following ${name} \u2014 their solo positions will appear as FOLLOWED signals after the next sweep.`);
        }
        renderSignals(lastData);
      } catch (e) { flash("err", e.message); }
    };
  });
}

function flash(kind, msg) {
  const banner = $("banner");
  banner.className = "banner " + kind;
  banner.textContent = msg;
  banner.classList.remove("hidden");
  setTimeout(() => banner.classList.add("hidden"), 8000);
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function pollSignals() {
  try { renderSignals(await api("/api/signals")); } catch (_) { /* login shown */ }
}

$("refresh-btn").onclick = async () => {
  await api("/api/refresh", { method: "POST" });
  pollSignals();
};

/* ── Activity ──────────────────────────────────────────────────────── */
async function loadActivity() {
  const { mirrors } = await api("/api/activity");
  $("activity-empty").classList.toggle("hidden", mirrors.length > 0);
  $("activity-body").innerHTML = mirrors.map((m) => `
    <tr>
      <td>${new Date(m.ts * 1000).toLocaleString()}</td>
      <td>${esc(m.title || "")}</td>
      <td>${esc(m.outcome || "")}</td>
      <td class="num">$${(m.usd || 0).toFixed(2)}</td>
      <td class="num">${(m.price || 0).toFixed(3)}</td>
      <td><span class="tag ${m.mode === "live" ? "tag-live" : "tag-dry"}">${m.mode}</span></td>
      <td><span class="tag ${m.status === "ok" ? "tag-ok" : m.status === "error" ? "tag-err" : "tag-skip"}">${m.status}</span></td>
      <td>${esc(m.detail || "")}</td>
    </tr>`).join("");
}

/* ── Performance ───────────────────────────────────────────────────── */
let pnlChart = null, posChart = null;
const CSS = getComputedStyle(document.documentElement);
const C = (v) => CSS.getPropertyValue(v).trim();
const money = (n) => (n >= 0 ? "+$" : "-$") + Math.abs(n).toFixed(2);
const pnlCls = (n) => (n >= 0 ? "pnl-pos" : "pnl-neg");

function tabVisible(name) { return !$("tab-" + name).classList.contains("hidden"); }

async function loadPerformance() {
  const data = await api("/api/performance");
  const chartsVisible = tabVisible("performance");
  const d = data.summary.dry_run, l = data.summary.live;
  const total = d.total + l.total;
  const realized = d.realized + l.realized;
  const unreal = d.unrealized + l.unrealized;
  const wins = d.wins + l.wins, losses = d.losses + l.losses;

  const set = (id, val, colored = true) => {
    const el = $(id);
    el.textContent = typeof val === "number" ? money(val) : val;
    if (colored && typeof val === "number") el.className = pnlCls(val);
  };
  set("p-total", total);
  set("p-realized", realized);
  set("p-unrealized", unreal);
  const costBasis = d.cost_basis + l.cost_basis;
  const roiEl = $("p-roi");
  roiEl.textContent = costBasis ? ((total / costBasis) * 100).toFixed(1) + "%" : "\u2014";
  roiEl.className = pnlCls(total);
  $("p-inplay").textContent = "$" + (d.open_cost + l.open_cost).toLocaleString(undefined, { maximumFractionDigits: 0 });
  set("p-24h-real", d.realized_24h + l.realized_24h);
  $("p-24h-wl").textContent = `${d.wins_24h + l.wins_24h} / ${d.losses_24h + l.losses_24h}`;
  $("p-24h-opened").textContent = d.opened_24h + l.opened_24h;
  $("p-24h-deployed").textContent = "$" + (d.deployed_24h + l.deployed_24h).toLocaleString(undefined, { maximumFractionDigits: 0 });
  $("p-winrate").textContent = wins + losses ? ((wins / (wins + losses)) * 100).toFixed(0) + "%" : "—";
  const openN = d.open_count + l.open_count;
  $("p-counts").textContent = `${openN} / ${wins + losses}`;

  $("performance-empty").classList.toggle("hidden", data.positions.length > 0);

  // Cumulative P&L line (dry vs live), from snapshots
  const cutoff = chartRange === "24h" ? Date.now() / 1000 - 86400 : 0;
  const mkSeries = (snaps) => snaps.filter((s) => s.ts >= cutoff).map((s) => ({
    x: new Date(s.ts * 1000).toLocaleString(),
    y: +(s.realized + (s.value - s.cost)).toFixed(2),
  }));
  const dry = mkSeries(data.snapshots.dry_run), live = mkSeries(data.snapshots.live);
  if (chartsVisible) {
  const labels = (dry.length >= live.length ? dry : live).map((p) => p.x);
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart($("pnl-chart"), {
    type: "line",
    data: { labels, datasets: [
      { label: "Dry run", data: dry.map((p) => p.y), borderColor: C("--amber"), backgroundColor: "transparent", tension: 0.25, pointRadius: 0 },
      { label: "Live", data: live.map((p) => p.y), borderColor: C("--sonar"), backgroundColor: "transparent", tension: 0.25, pointRadius: 0 },
    ]},
    options: chartOpts(),
  });

  // Per-position P&L bars, green/red
  const pos = [...data.positions].sort((a, b) => a.ts - b.ts);
  if (posChart) posChart.destroy();
  posChart = new Chart($("pos-chart"), {
    type: "bar",
    data: {
      labels: pos.map((p) => p.title.slice(0, 28) + (p.title.length > 28 ? "…" : "")),
      datasets: [{
        label: "P&L (USD)",
        data: pos.map((p) => p.pnl),
        backgroundColor: pos.map((p) => p.pnl >= 0 ? C("--kelp") : C("--coral")),
      }],
    },
    options: chartOpts(false),
  });
  }

  // Category breakdown
  $("category-body").innerHTML = (data.categories || []).map((c) => `
    <tr>
      <td>${esc(c.category)}</td>
      <td class="num">${c.positions}</td>
      <td class="num">$${c.invested.toFixed(0)}</td>
      <td class="num ${pnlCls(c.pnl)}">${money(c.pnl)}</td>
      <td class="num ${pnlCls(c.roi)}">${(c.roi * 100).toFixed(1)}%</td>
      <td class="num">${c.wins} / ${c.losses}</td>
    </tr>`).join("");

  // Ledger table
  const pf = positionFilters;
  const filtered = data.positions.filter((p) => {
    if (pf.open && p.status !== "open") return false;
    if (pf.settled && p.status === "open") return false;
    if (pf.wins && !(p.status === "won" || (p.status === "sold" && p.pnl > 0))) return false;
    if (pf.losses && !(p.status === "lost" || (p.status === "sold" && p.pnl <= 0))) return false;
    return true;
  });
  $("pf-count").textContent = filtered.length === data.positions.length
    ? `${filtered.length} positions` : `${filtered.length} of ${data.positions.length}`;
  $("positions-body").innerHTML = filtered.map((p) => {
    const open = p.status === "open";
    const lvl = (field, val) => open
      ? `<input class="level-input" type="number" min="0.01" max="0.99" step="0.01"
           data-level="${field}" data-pos="${p.id}" value="${val ?? ""}" placeholder="—">`
      : (val != null ? val.toFixed(2) : "—");
    return `
    <tr>
      <td>${new Date(p.ts * 1000).toLocaleDateString()}${open ? `<span class="held">held ${Math.floor((Date.now() / 1000 - p.ts) / 86400)}d</span>` : ""}</td>
      <td>${esc(p.title || "")}</td>
      <td>${esc(p.outcome || "")}</td>
      <td class="num">$${p.usd.toFixed(2)}</td>
      <td class="num">${p.entry_price.toFixed(3)}</td>
      <td class="num">${(p.last_price ?? p.entry_price).toFixed(3)}</td>
      <td class="num ${pnlCls(p.pnl)}">${money(p.pnl)}</td>
      <td class="num">${lvl("floor", p.floor)}</td>
      <td class="num">${lvl("ceiling", p.ceiling)}</td>
      <td><span class="tag ${p.mode === "live" ? "tag-live" : "tag-dry"}">${p.mode}</span></td>
      <td><span class="tag tag-${p.status}" title="${esc(p.exit_reason || "")}">${p.status}</span>${p.exit_reason ? `<span class="exit-reason">${esc(p.exit_reason)}</span>` : ""}</td>
      <td>${open ? `<button class="btn btn-sell" data-sell="${p.id}">Sell</button>` : ""}</td>
    </tr>`;
  }).join("");

  document.querySelectorAll("[data-sell]").forEach((btn) => {
    btn.onclick = async () => {
      const p = data.positions.find((x) => x.id == btn.dataset.sell);
      const live = p.mode === "live";
      if (!confirm(`${live ? "LIVE sell" : "Simulated sell"}: close "${p.title}" at market? Current P&L ${money(p.pnl)}.`)) return;
      btn.disabled = true;
      try {
        const r = await api(`/api/positions/${p.id}/sell`, { method: "POST" });
        flash(r.status === "ok" ? "ok" : "err", r.detail);
        loadPerformance();
      } catch (e) { flash("err", e.message); btn.disabled = false; }
    };
  });

  document.querySelectorAll(".level-input").forEach((inp) => {
    inp.onchange = async () => {
      const row = data.positions.find((x) => x.id == inp.dataset.pos);
      const floor = row && document.querySelector(`[data-level="floor"][data-pos="${inp.dataset.pos}"]`).value;
      const ceiling = row && document.querySelector(`[data-level="ceiling"][data-pos="${inp.dataset.pos}"]`).value;
      try {
        await api(`/api/positions/${inp.dataset.pos}/levels`, {
          method: "POST",
          body: JSON.stringify({ floor: floor ? +floor : null, ceiling: ceiling ? +ceiling : null }),
        });
        flash("ok", "Exit levels saved.");
      } catch (e) { flash("err", e.message); }
    };
  });
}

function chartOpts(legend = true) {
  const grid = { color: C("--line") }, ticks = { color: C("--muted"), font: { family: "IBM Plex Mono", size: 10 } };
  return {
    responsive: true,
    plugins: { legend: { display: legend, labels: { color: C("--ink"), font: { family: "Barlow" } } } },
    scales: { x: { grid, ticks: { ...ticks, maxTicksLimit: 8 } }, y: { grid, ticks } },
  };
}

/* ── Performance controls ──────────────────────────────────────────── */
let chartRange = "all";
const positionFilters = { open: false, settled: false, wins: false, losses: false };
$("r-all").onclick = () => { chartRange = "all"; $("r-all").classList.add("on"); $("r-24h").classList.remove("on"); loadPerformance(); };
$("r-24h").onclick = () => { chartRange = "24h"; $("r-24h").classList.add("on"); $("r-all").classList.remove("on"); loadPerformance(); };
document.querySelectorAll("[data-pf]").forEach((chip) => {
  chip.onclick = () => {
    const key = chip.dataset.pf;
    positionFilters[key] = !positionFilters[key];
    if (key === "open" && positionFilters.open) { positionFilters.settled = false; document.querySelector('[data-pf="settled"]').classList.remove("on"); }
    if (key === "settled" && positionFilters.settled) { positionFilters.open = false; document.querySelector('[data-pf="open"]').classList.remove("on"); }
    chip.classList.toggle("on", positionFilters[key]);
    loadPerformance();
  };
});

/* ── Whales tab ────────────────────────────────────────────────────── */
let whaleSort = { key: "pnl", dir: -1 };
let whaleData = null;
async function loadWhales() {
  whaleData = await api("/api/whales/leaderboard");
  renderWhales();
}
function renderWhales() {
  const rows = [...(whaleData.whales || [])].sort((a, b) => {
    const va = a[whaleSort.key], vb = b[whaleSort.key];
    if (typeof va === "string") return whaleSort.dir * va.localeCompare(vb);
    return whaleSort.dir * ((va ?? -Infinity) - (vb ?? -Infinity));
  });
  $("whales-empty").classList.toggle("hidden", rows.length > 0);
  $("whales-body").innerHTML = rows.map((w) => `
    <tr>
      <td>${esc(w.name)}</td>
      <td class="num">${w.positions}</td>
      <td class="num">${w.wins} / ${w.losses}</td>
      <td class="num">${w.win_rate != null ? (w.win_rate * 100).toFixed(0) + "%" : "\u2014"}</td>
      <td class="num ${pnlCls(w.pnl)}">${money(w.pnl)}</td>
      <td class="num">$${w.invested.toFixed(0)}</td>
      <td class="num ${pnlCls(w.roi)}">${(w.roi * 100).toFixed(1)}%</td>
      <td class="num">${w.open_count}</td>
      <td>${new Date(w.last_seen * 1000).toLocaleDateString()}</td>
      <td><button class="btn ${w.followed ? "" : "btn-mirror"}" data-wfollow="${esc(w.address)}" data-wname="${esc(w.name)}">${w.followed ? "Unfollow" : "Follow"}</button></td>
    </tr>`).join("");
  document.querySelectorAll("[data-wfollow]").forEach((btn) => {
    btn.onclick = async () => {
      const addr = btn.dataset.wfollow, name = btn.dataset.wname;
      const isFollowed = whaleData.whales.find((w) => w.address === addr)?.followed;
      if (isFollowed) await api(`/api/whales/follow/${addr}`, { method: "DELETE" });
      else await api("/api/whales/follow", { method: "POST", body: JSON.stringify({ address: addr, name }) });
      loadWhales();
    };
  });
}
document.querySelectorAll("#whales-table th[data-ws]").forEach((th) => {
  th.onclick = () => {
    const key = th.dataset.ws;
    whaleSort.dir = whaleSort.key === key ? -whaleSort.dir : -1;
    whaleSort.key = key;
    if (whaleData) renderWhales();
  };
});

/* ── Settings ──────────────────────────────────────────────────────── */
async function loadSettings() {
  const data = await api("/api/settings");
  settings = data.settings;
  $("s-dry-run").checked = settings.dry_run;
  $("s-auto-mirror").checked = settings.auto_mirror;
  $("s-auto-followed").checked = settings.auto_mirror_followed;
  $("s-exit-whales").checked = settings.exit_with_whales;
  $("s-floor-off").value = Math.round(settings.default_floor_offset * 100);
  $("s-ceiling-off").value = Math.round(settings.default_ceiling_offset * 100);
  $("s-stop-pct").value = settings.stop_loss_pct;
  $("s-max-hold").value = settings.max_hold_days;
  $("s-min-entry").value = Math.round(settings.min_entry_price * 100);
  $("s-max-entry").value = Math.round(settings.max_entry_price * 100);
  $("s-max-days").value = settings.max_days_to_resolution;
  $("s-per-trade").value = settings.per_trade_usd;
  $("s-daily-cap").value = settings.daily_cap_usd;
  $("s-slippage").value = settings.max_slippage * 100;
  $("s-score-floor").value = settings.min_score_to_mirror;
  $("s-min-whales").value = settings.min_whales;
  $("s-dominance").value = settings.dominance;
  $("s-refresh").value = settings.refresh_minutes;
  $("spent-today").textContent = "$" + data.spent_today.toFixed(0);

  const badge = $("mode-badge");
  badge.textContent = settings.dry_run ? "DRY RUN" : "LIVE";
  badge.className = "badge " + (settings.dry_run ? "badge-dry" : "badge-live");

  const c = data.credentials;
  $("creds-status").textContent = c.configured
    ? `Configured for ${c.funder_address} (signature type ${c.signature_type})${data.clob_available ? "" : " — py-clob-client missing from image!"}`
    : "Not configured — the console is view + dry-run only.";
}

$("save-settings").onclick = async () => {
  try {
    const patch = {
      dry_run: $("s-dry-run").checked,
      auto_mirror: $("s-auto-mirror").checked,
      auto_mirror_followed: $("s-auto-followed").checked,
      exit_with_whales: $("s-exit-whales").checked,
      default_floor_offset: +$("s-floor-off").value / 100,
      default_ceiling_offset: +$("s-ceiling-off").value / 100,
      stop_loss_pct: +$("s-stop-pct").value,
      max_hold_days: +$("s-max-hold").value,
      min_entry_price: +$("s-min-entry").value / 100,
      max_entry_price: +$("s-max-entry").value / 100,
      max_days_to_resolution: +$("s-max-days").value,
      per_trade_usd: +$("s-per-trade").value,
      daily_cap_usd: +$("s-daily-cap").value,
      max_slippage: +$("s-slippage").value / 100,
      min_score_to_mirror: +$("s-score-floor").value,
      min_whales: +$("s-min-whales").value,
      dominance: +$("s-dominance").value,
      refresh_minutes: +$("s-refresh").value,
    };
    if (patch.dry_run === false && !confirm("Turn OFF dry run? Mirrors will place real orders with real funds.")) {
      $("s-dry-run").checked = true;
      return;
    }
    await api("/api/settings", { method: "POST", body: JSON.stringify(patch) });
    $("settings-saved").classList.remove("hidden");
    setTimeout(() => $("settings-saved").classList.add("hidden"), 2500);
    loadSettings();
  } catch (e) { flash("err", e.message); }
};

$("save-creds").onclick = async () => {
  try {
    await api("/api/credentials", {
      method: "POST",
      body: JSON.stringify({
        private_key: $("c-key").value,
        funder_address: $("c-funder").value,
        signature_type: +$("c-sigtype").value,
      }),
    });
    $("c-key").value = "";
    flash("ok", "Credentials saved and encrypted.");
    loadSettings();
  } catch (e) { flash("err", e.message); }
};

$("clear-creds").onclick = async () => {
  if (!confirm("Remove trading credentials? Dry run will be re-enabled.")) return;
  await api("/api/credentials", { method: "DELETE" });
  flash("ok", "Credentials removed. Dry run re-enabled.");
  loadSettings();
};

/* ── Boot ──────────────────────────────────────────────────────────── */
async function refreshHeader() {
  // header-only stats (mode badge + spend) without touching settings form fields
  try {
    const data = await api("/api/settings");
    settings = data.settings;
    $("spent-today").textContent = "$" + data.spent_today.toFixed(0);
    const badge = $("mode-badge");
    badge.textContent = settings.dry_run ? "DRY RUN" : "LIVE";
    badge.className = "badge " + (settings.dry_run ? "badge-dry" : "badge-live");
  } catch (_) {}
}

let bgTick = 0;
async function tick() {
  await pollSignals();                     // always: signals + sweep status
  bgTick += 1;
  if (bgTick % 3 === 0) {                  // every ~30s: everything else
    refreshHeader();
    loadPerformance().catch(() => {});
    if (tabVisible("activity")) loadActivity().catch(() => {});
  }
}

async function boot() {
  showApp();
  await loadSettings().catch(() => {});
  await tick();
  setInterval(tick, 10000);
}

(async () => {
  try { await api("/api/settings"); boot(); } catch (_) { showLogin(); }
})();
