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
    if (t.dataset.tab === "settings") loadSettings();
  };
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
  const driftTxt = (s.entry_drift >= 0 ? "+" : "") + (s.entry_drift * 100).toFixed(1) + "¢";
  const pips = s.whales.slice(0, 12).map(() => "<i></i>").join("");
  return `
    <div class="gauge">
      <div class="rail">
        <div class="span" style="left:${lo}%; width:${Math.max(hi - lo, 0.6)}%"></div>
        <div class="tick-entry" style="left:${s.avg_whale_entry * 100}%" title="Whale avg entry ${s.avg_whale_entry}"></div>
        <div class="tick-now" style="left:${s.current_price * 100}%" title="Current price ${s.current_price}"></div>
      </div>
      <div class="gauge-labels">
        <span>entry ${(s.avg_whale_entry * 100).toFixed(0)}¢</span>
        <span class="${driftCls}">drift ${driftTxt}</span>
        <span>now ${(s.current_price * 100).toFixed(0)}¢</span>
      </div>
      <div class="whale-pips" title="${s.whale_count} whales">${pips}</div>
      <div class="whale-names">${s.whales.join(" · ")}</div>
    </div>`;
}

function renderSignals(data) {
  const list = $("signals-list");
  $("signal-count").textContent = data.signals.length;
  $("last-sweep").textContent = fmtAgo(data.last_refresh);

  const sonar = $("sonar");
  sonar.className = "sonar" + (data.refreshing ? " busy" : data.last_error ? " error" : "");
  $("progress").classList.toggle("hidden", !data.refreshing);
  $("progress").textContent = data.progress || "Sweeping…";

  const banner = $("banner");
  if (data.last_error) {
    banner.className = "banner err"; banner.textContent = "Last sweep failed: " + data.last_error;
    banner.classList.remove("hidden");
  } else if (data.auto_results && data.auto_results.length) {
    banner.className = "banner ok";
    banner.textContent = `Auto-mirror: ${data.auto_results.length} signal(s) processed this sweep.`;
    banner.classList.remove("hidden");
  } else banner.classList.add("hidden");

  $("signals-empty").classList.toggle("hidden", data.signals.length > 0);
  list.innerHTML = data.signals.map((s) => `
    <div class="signal">
      <div class="signal-title">${esc(s.title)}<span class="side">→ ${esc(s.outcome)}</span></div>
      <div class="signal-actions">
        <span class="score" title="Consensus score">${s.score.toFixed(1)}</span>
        <button class="btn btn-mirror" data-mirror="${s.id}">Mirror</button>
      </div>
      <div class="signal-meta">
        <span><b>${s.whale_count}</b> whales</span>
        <span><b>$${s.whale_dollars.toLocaleString()}</b> behind it</span>
        <span><b>${(s.dominance * 100).toFixed(0)}%</b> dominance</span>
        ${s.end_date ? `<span>ends <b>${esc(String(s.end_date).slice(0, 10))}</b></span>` : ""}
      </div>
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

/* ── Settings ──────────────────────────────────────────────────────── */
async function loadSettings() {
  const data = await api("/api/settings");
  settings = data.settings;
  $("s-dry-run").checked = settings.dry_run;
  $("s-auto-mirror").checked = settings.auto_mirror;
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
async function boot() {
  showApp();
  await loadSettings().catch(() => {});
  await pollSignals();
  setInterval(pollSignals, 10000);
}

(async () => {
  try { await api("/api/settings"); boot(); } catch (_) { showLogin(); }
})();
