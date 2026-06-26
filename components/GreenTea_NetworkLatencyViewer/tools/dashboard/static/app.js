// GreenTea Network Latency Viewer — フロント制御。
// SSE で Live を購読し、Overview/Raw は fetch で取得。録画ボタンは
// /api/record/* を叩く (daemon socket 未実装なら graceful 表示)。

// charts.js が class TimeSeries / function renderNetDiagram を global に宣言済。
// classic script は字句スコープを共有するため destructure すると二重宣言になる →
// 名前空間オブジェクト経由で参照する。
const GUI = window.GTNLV;

// ---- タブ切替 ----
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tabpanel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "overview") loadRuns("ov-run").then(refreshOverview);
    if (btn.dataset.tab === "raw") loadRuns("raw-run").then(refreshRaw);
  });
});

// ---- Live: SSE ----
const overlayChart = new GUI.MultiSeries(
  document.getElementById("chart-overlay"),
  ["total(②+③)", "②AP滞留", "③air→HID"],
  ["#0a84ff", "#d29922", "#2ea043"],
  document.getElementById("overlay-key"));
const legHW = new GUI.TimeSeries(document.getElementById("chart-leg-hw"), "host+wire (ms)");
const ulChart = new GUI.MultiSeries(
  document.getElementById("chart-ul"),
  ["total(HID→wire)", "①HID→air", "②air→wire"],
  ["#0a84ff", "#d29922", "#2ea043"],
  document.getElementById("ul-key"),
  (r, t) => `time=${t}  hid_seq=${r.cyc}  total=${(r.total / 1000).toFixed(3)}ms  ` +
    `①=${r.hid_air != null ? (r.hid_air / 1000).toFixed(3) : "—"}  ` +
    `②=${r.air_wire != null ? (r.air_wire / 1000).toFixed(3) : "—"}`);
const svg = document.getElementById("netdiagram");
const connBadge = document.getElementById("conn");

function setConn(ok) {
  connBadge.textContent = ok ? "SSE: 接続中" : "SSE: 切断 (再接続中)";
  connBadge.className = "badge " + (ok ? "badge-on" : "badge-off");
}

function metricCard(label, value, sub) {
  return `<div class="metric"><div class="m-label">${label}</div>` +
    `<div class="m-value">${value}</div>` +
    `<div class="m-sub">${sub || ""}</div></div>`;
}

function renderTraffic(tr) {
  const box = document.getElementById("traffic-cards");
  if (!tr) { box.innerHTML = `<p class="info">集計に必要なデータがまだ揃いません。</p>`; return; }
  const air = tr.air_to_hid_rate_hz != null ? tr.air_to_hid_rate_hz.toFixed(1) + " Hz" : "—";
  const aloss = tr.air_hid_loss_pct != null ? tr.air_hid_loss_pct.toFixed(2) + " %" : "—";
  box.innerHTML =
    metricCard("TX rate (実効)", tr.tx_rate_hz.toFixed(1) + " Hz", "≈ HID rx_dl rate") +
    metricCard("累積送信数", tr.cumulative_tx.toLocaleString(), tr.seq_col) +
    metricCard("DL loss (window)", tr.loss_pct.toFixed(3) + " %",
      tr.missing > 0 ? tr.missing + " missed" : "all OK") +
    metricCard("air→HID rate", air, "sniffer dst=HID OUI") +
    metricCard("HID 内取りこぼし", aloss, "(air − rx_dl)/air");
}

function handleLive(msg) {
  const statusEl = document.getElementById("live-status");
  const nowEl = document.getElementById("live-now");
  if (!msg.active) {
    statusEl.textContent = "🛑 計測中の run が見つかりません";
    statusEl.className = "status status-idle";
    nowEl.textContent = "";
    GUI.renderNetDiagram(svg, null, null);
    document.getElementById("traffic-cards").innerHTML =
      `<p class="info">gtnlv-rpid を起動し pc_emulator を並走させてください。</p>`;
    ulChart.update([], ["total", "hid_air", "air_wire"]);
    renderUplink(null);
    return;
  }
  statusEl.textContent = "🟢 計測中: " + msg.active;
  statusEl.className = "status status-live";
  const live = msg.live || {};
  nowEl.textContent = live.now_text || "";
  const legs = live.legs || {};
  GUI.renderNetDiagram(svg, legs, live.total_bridge || legs.total);
  const ov = legs.overlay || [];
  if (ov.length) {
    overlayChart.update(ov, ["total", "wa", "ah"]);   // 3 系列重ね
  } else {
    // wire SPAN 無時は total 単線 (live.series)
    overlayChart.update((live.series || []).map((p) => ({ t: p.t, total: p.owd_us })), ["total"]);
  }
  legHW.update((legs.host_wire && legs.host_wire.series) || []);
  const ul = live.uplink || null;
  const ulLegs = (ul && ul.legs) || null;
  ulChart.update((ulLegs && ulLegs.series) || [], ["total", "hid_air", "air_wire"]);
  renderUplink(ul);
  renderTraffic(live.traffic);
}

function renderUplink(ul) {
  const box = document.getElementById("ul-cards");
  const lg = (ul && ul.legs) || {};
  if (!ul || (!ul.owd && !lg.total)) {
    box.innerHTML = `<p class="info">上り (tx_ul) データがまだありません。` +
      `HID が上り送信 (port 50000+id) すると 52000+id の tx_ul 報告から表示されます。</p>`;
    return;
  }
  const ms = (v) => (v == null ? "—" : (v / 1000).toFixed(3));
  const t = lg.total, ha = lg.hid_air, aw = lg.air_wire, o = ul.owd;
  box.innerHTML =
    metricCard("total (HID→wire) median", t ? ms(t.median) + " ms" : "—",
      t ? "p95 " + ms(t.p95) + " n=" + t.n : "区間 join 待ち") +
    metricCard("① HID→air median", ha ? ms(ha.median) + " ms" : "—",
      ha ? "p95 " + ms(ha.p95) + " (sniffer)" : "sniffer 待ち") +
    metricCard("② air→wire median", aw ? ms(aw.median) + " ms" : "—",
      aw ? "p95 " + ms(aw.p95) + " (SPAN)" : "wire 待ち") +
    metricCard("上り OWD (→RasPi app)", o ? ms(o.median) + " ms" : "—",
      o ? "p99 " + ms(o.p99) : "") +
    metricCard("上り損失 / rate",
      (ul.loss_pct != null ? ul.loss_pct.toFixed(2) + "%" : "—") + " / " +
      (ul.rate_hz != null ? ul.rate_hz.toFixed(1) + "Hz" : "—"),
      ul.missing > 0 ? ul.missing + " missed" : "all OK");
}

function connectSSE() {
  const es = new EventSource("/api/stream/live");
  es.onopen = () => setConn(true);
  es.onmessage = (ev) => {
    setConn(true);
    try { handleLive(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  };
  es.onerror = () => {
    setConn(false);
    es.close();
    setTimeout(connectSSE, 3000); // 自動再接続
  };
}

// ---- 録画制御 ----
async function refreshRecState() {
  const el = document.getElementById("rec-state");
  const note = document.getElementById("rec-note");
  const startBtn = document.getElementById("rec-start");
  const stopBtn = document.getElementById("rec-stop");
  try {
    const r = await (await fetch("/api/record/status")).json();
    if (!r.available) {
      el.textContent = "状態: daemon 制御 socket 未接続";
      el.className = "rec-state rec-unknown";
      note.textContent = r.error || "daemon A の制御 socket (P1 で実装予定) が無いため録画操作は無効。";
      startBtn.disabled = true; stopBtn.disabled = true;
      return;
    }
    const rec = !!r.recording;
    el.textContent = rec ? ("● 録画中: " + (r.tag || "")) : "○ 待機 (録画なし)";
    el.className = "rec-state " + (rec ? "rec-on" : "rec-off");
    if (r.detail) note.textContent = r.detail;  // detail 無い時は直近メッセージ(sniffer 状態等)を保持
    startBtn.disabled = rec; stopBtn.disabled = !rec;
  } catch (e) {
    el.textContent = "状態: 取得失敗"; el.className = "rec-state rec-unknown";
    note.textContent = String(e);
  }
}

document.getElementById("rec-start").addEventListener("click", async () => {
  const tag = document.getElementById("rec-tag").value.trim() || null;
  const ppsEl = document.getElementById("rec-pps");
  const pps = ppsEl ? ppsEl.checked : true;
  const note = document.getElementById("rec-note");
  const startBtn = document.getElementById("rec-start");
  startBtn.disabled = true;
  if (note) note.textContent = "sniffer 健全性チェック中…";
  try {
    const r = await (await fetch("/api/record/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag, pps }),
    })).json();
    // sniffer 健全性チェック結果を表示 (録画開始前に daemon が確認/復旧)
    const sn = r.sniffer || {};
    const msg = {
      ok: "✅ sniffer OK",
      recovered: "♻ sniffer 復旧 (RTS reboot)",
      failed: "⚠ sniffer 無音 — air/PPS 欠落の可能性",
      absent: "sniffer 未使用",
    }[sn.state] || "";
    if (note) note.textContent = (r.recording ? `録画中 ${r.tag || ""} — ` : "") + msg
      + (sn.n_recover ? ` (recover計${sn.n_recover}回)` : "");
  } catch (e) {
    if (note) note.textContent = "開始失敗: " + e;
  }
  refreshRecState();
});
document.getElementById("rec-stop").addEventListener("click", async () => {
  await fetch("/api/record/stop", { method: "POST" });
  refreshRecState();
});

// sniffer 対象 AP (SSID) 切替
document.getElementById("snf-set").addEventListener("click", async () => {
  const ssid = document.getElementById("snf-ssid").value.trim();
  const password = document.getElementById("snf-pass").value;
  const note = document.getElementById("snf-note");
  if (!ssid) { note.textContent = "SSID を入力してください"; return; }
  note.textContent = "送信中…";
  try {
    const r = await (await fetch("/api/sniffer/ssid", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid, password }),
    })).json();
    note.textContent = r.ok
      ? `切替要求送信: ${r.ssid} → sniffer 再 associate 中 (数秒～十数秒)`
      : `失敗: ${r.error || "不明"}`;
  } catch (e) {
    note.textContent = "送信エラー: " + e;
  }
});

// ---- Overview ----
async function loadRuns(selectId) {
  const data = await (await fetch("/api/runs")).json();
  const sel = document.getElementById(selectId);
  const cur = sel.value;
  sel.innerHTML = "";
  data.runs.forEach((r) => {
    const o = document.createElement("option");
    o.value = r.name;
    o.textContent = r.name + (r.is_tmpfs ? " [tmpfs]" : "") + (r.active ? " 🟢" : "");
    sel.appendChild(o);
  });
  if (cur && data.runs.some((r) => r.name === cur)) sel.value = cur;
}

function fmtMs(v) { return (v == null || isNaN(v)) ? "—" : v.toFixed(3) + " ms"; }

async function refreshOverview() {
  const run = document.getElementById("ov-run").value;
  if (!run) return;
  const s = await (await fetch("/api/summary?run=" + encodeURIComponent(run))).json();
  const owd = s.owd, loss = s.loss, sn = s.sniffer;
  document.getElementById("kpi7").innerHTML =
    metricCard("① DL OWD median (raw)", owd ? fmtMs(owd.median) : "—", "owd_dl_approx_us") +
    metricCard("② DL Packet Loss", loss ? loss.loss_pct.toFixed(3) + " %" : "—",
      loss ? `${loss.delivered}/${loss.expected} (${loss.seq_col})` : "") +
    metricCard("③ Data Rate", s.data_rate_kbps != null ? s.data_rate_kbps.toFixed(1) + " kbps" : "—", "rx_dl × 64B") +
    metricCard("④ Interference (RSSI)", sn && sn.rssi_median_dbm != null ? sn.rssi_median_dbm.toFixed(0) + " dBm" : "—",
      sn ? `frame=${sn.n_frame}, dropped=${sn.dropped_total}` : "") +
    metricCard("⑤ Startup Time", "未計測", "task #5") +
    metricCard("⑥ Power", "未計測", "task #6") +
    metricCard("⑦ Cost (BOM)", "TBD", "");
  const m = s.m2k_dt, pb = s.pps_bridge;
  document.getElementById("kpi-sync").innerHTML =
    metricCard("PPS Δt median", m ? m.median.toFixed(1) + " μs" : "—", "ADALM2000") +
    metricCard("PPS Δt sd", m ? m.sd ? m.sd.toFixed(1) + " μs" : (m.max != null ? "—" : "—") : "—", "dispatch jitter") +
    metricCard("bridge offset range", pb ? pb.range_ms.toFixed(2) + " ms" : "—", "drift+jitter") +
    metricCard("per-sec 変動 sd", pb ? pb.adj_diff_sd_us.toFixed(1) + " μs" : "—", "") +
    metricCard("UART transport max", pb ? pb.uart_delay_max_ms.toFixed(2) + " ms" : "—",
      pb ? "median " + pb.uart_delay_median_ms.toFixed(2) + " ms" : "");
}
document.getElementById("ov-refresh").addEventListener("click", refreshOverview);
document.getElementById("ov-run").addEventListener("change", refreshOverview);

// ---- Raw ----
async function refreshRaw() {
  const run = document.getElementById("raw-run").value;
  if (!run) return;
  const data = await (await fetch("/api/csv_files?run=" + encodeURIComponent(run))).json();
  const tb = document.querySelector("#raw-table tbody");
  tb.innerHTML = "";
  data.files.forEach((f) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${f.name}</td><td>${f.size_kb}</td><td>${f.lines}</td>`;
    tb.appendChild(tr);
  });
}
document.getElementById("raw-run").addEventListener("change", refreshRaw);

// ---- 起動 ----
connectSSE();
refreshRecState();
setInterval(refreshRecState, 5000);

// ---- Live: robot 個体毎の 下り OWD グラフ (per-robot) ----
// uPlot は生成コストが高いので robot_id ごとにチャートを cache し、
// robot 構成 (id 集合) が変わった時だけ DOM を作り直す。
const _perRobot = { charts: {}, ids: "" };
const _msf = (v) => (v == null ? "—" : v.toFixed(3));

async function renderPerRobot() {
  const box = document.getElementById("per-robot-box");
  if (!box) return;
  let d;
  try {
    d = await (await fetch("/api/live_per_robot")).json();
  } catch (e) { return; /* keep last */ }
  if (!d.active || !d.robots || !d.robots.length) {
    box.innerHTML = `<p class="info">robot 別データなし (rx_dl 待ち)</p>`;
    _perRobot.charts = {}; _perRobot.ids = "";
    return;
  }
  const ids = d.robots.map((r) => r.robot_id).join(",");
  if (ids !== _perRobot.ids) {
    // 構成変化 → DOM 再構築 + チャート再生成
    box.innerHTML = "";
    _perRobot.charts = {};
    const grid = document.createElement("div");
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = "repeat(auto-fill, minmax(340px, 1fr))";
    grid.style.gap = "12px";
    d.robots.forEach((r) => {
      const card = document.createElement("div");
      card.style.border = "1px solid var(--mut, #444)";
      card.style.borderRadius = "8px";
      card.style.padding = "8px";
      const head = document.createElement("div");
      head.id = `rh-${r.robot_id}`;
      head.style.fontSize = "13px";
      head.style.marginBottom = "4px";
      const chartEl = document.createElement("div");
      chartEl.id = `rc-${r.robot_id}`;
      card.appendChild(head);
      card.appendChild(chartEl);
      grid.appendChild(card);
      _perRobot.charts[r.robot_id] = new window.GTNLV.TimeSeries(chartEl, `r${r.robot_id} OWD (ms)`);
    });
    box.appendChild(grid);
    const note = document.createElement("p");
    note.id = "per-robot-note"; note.className = "note";
    box.appendChild(note);
    _perRobot.ids = ids;
    // grid 確定後に各チャート幅を再フィット
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }
  // 統計ヘッダ + 時系列を更新
  d.robots.forEach((r) => {
    const head = document.getElementById(`rh-${r.robot_id}`);
    if (head) {
      head.innerHTML = `<b>r${r.robot_id}</b> `
        + `<span style="color:var(--mut,#888)">median</span> ${_msf(r.bridge_median_ms ?? r.approx_median_ms)} ms`
        + ` / p95 ${_msf(r.bridge_p95_ms)} / max ${_msf(r.bridge_max_ms)}`
        + ` / loss ${r.loss_pct == null ? "—" : r.loss_pct.toFixed(2)}%`
        + ` / n ${r.n}`;
    }
    const ch = _perRobot.charts[r.robot_id];
    if (ch && r.series) ch.update(r.series);
  });
  const note = document.getElementById("per-robot-note");
  if (note) note.textContent = d.bridge_offset_ok
    ? "縦軸=PPS bridge 下り OWD (ms)、直近 60s。"
    : "⚠ PPS bridge offset 未確立 → グラフは approx (host clock 近似)。";
}
setInterval(renderPerRobot, 2000);
renderPerRobot();
