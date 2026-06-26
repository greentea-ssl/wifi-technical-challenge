// GreenTea Network Latency Viewer — 描画レイヤ。
//
// 時系列: uPlot が vendoring 済ならそれを使い、未取得なら canvas fallback。
// 網図:   inline SVG をハンドメイド更新 (Plotly 等の重依存を避ける)。

const HAS_UPLOT = (typeof uPlot !== "undefined");

// ---------------------------------------------------------------
// 時系列チャート (uPlot or canvas fallback)
// ---------------------------------------------------------------
class TimeSeries {
  constructor(el, label) {
    this.el = el;
    this.label = label || "value";
    this.uplot = null;
    if (HAS_UPLOT) {
      const opts = {
        width: el.clientWidth || 800,
        height: 260,
        scales: {
          x: { time: true },
          y: { range: (u, dataMin, dataMax) => [0, (dataMax == null || dataMax <= 0) ? 1 : dataMax] },
        },
        series: [
          {},
          { label: this.label, stroke: "#0a84ff", width: 1.5, points: { show: false } },
        ],
        axes: [
          { stroke: "#888" },
          { stroke: "#888", label: this.label },
        ],
      };
      this.uplot = new uPlot(opts, [[], []], el);
      window.addEventListener("resize", () => {
        this.uplot.setSize({ width: el.clientWidth || 800, height: 260 });
      });
    } else {
      this.canvas = document.createElement("canvas");
      this.canvas.height = 260;
      el.appendChild(this.canvas);
    }
  }

  // series: [{t: unix_sec, owd_us: ...}]
  update(series) {
    const xs = series.map((s) => s.t);
    const ys = series.map((s) => s.owd_us / 1000.0); // μs → ms
    if (this.uplot) {
      this.uplot.setData([xs, ys]);
      return;
    }
    this._canvasDraw(xs, ys);
  }

  _canvasDraw(xs, ys) {
    const c = this.canvas;
    c.width = c.parentElement.clientWidth || 800;
    const ctx = c.getContext("2d");
    const W = c.width, H = c.height, pad = 36;
    ctx.clearRect(0, 0, W, H);
    if (!xs.length) return;
    const xmin = xs[0], xmax = xs[xs.length - 1] || xs[0] + 1;
    let ymin = 0, ymax = Math.max(...ys);   // 0 基準
    if (ymax <= 0) ymax = 1;
    const px = (x) => pad + (W - 2 * pad) * (x - xmin) / (xmax - xmin || 1);
    const py = (y) => H - pad - (H - 2 * pad) * (y - ymin) / (ymax - ymin || 1);
    // axes
    ctx.strokeStyle = "#ccc"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad, pad); ctx.lineTo(pad, H - pad); ctx.lineTo(W - pad, H - pad); ctx.stroke();
    ctx.fillStyle = "#666"; ctx.font = "11px sans-serif";
    ctx.fillText(ymax.toFixed(2) + " ms", 2, pad + 4);
    ctx.fillText(ymin.toFixed(2) + " ms", 2, H - pad);
    // line
    ctx.strokeStyle = "#0a84ff"; ctx.lineWidth = 1.5; ctx.beginPath();
    xs.forEach((x, i) => { const X = px(x), Y = py(ys[i]); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
    ctx.stroke();
  }
}

// ---------------------------------------------------------------
// 多系列時系列 (total / ②AP滞留 / ③air→HID を同一時間軸に重ね描画)
// ---------------------------------------------------------------
class MultiSeries {
  constructor(el, labels, colors, keyEl, keyFmt) {
    this.el = el; this.labels = labels; this.colors = colors; this.uplot = null;
    this.keyEl = keyEl || null; this.keyFmt = keyFmt || null; this.rows = [];
    const self = this;
    if (HAS_UPLOT) {
      const series = [{}].concat(labels.map((lb, i) => ({
        label: lb, stroke: colors[i], width: 1.5, points: { show: false },
      })));
      const opts = {
        width: el.clientWidth || 800, height: 300,
        scales: { x: { time: true }, y: { range: (u, mn, mx) => [0, (mx == null || mx <= 0) ? 1 : mx] } },
        series, axes: [{ stroke: "#888" }, { stroke: "#888", label: "ms" }],
        legend: { show: true },
        hooks: {
          setCursor: [(u) => {
            if (!self.keyEl) return;
            const i = u.cursor.idx;
            if (i == null || !self.rows[i]) {
              self.keyEl.textContent = "(波形にカーソルを合わせると cycle_count / robot_id を表示)";
              return;
            }
            const r = self.rows[i];
            const d = new Date(r.t * 1000);
            const t = d.toTimeString().slice(0, 8) + "." + String(d.getMilliseconds()).padStart(3, "0");
            self.keyEl.textContent = self.keyFmt ? self.keyFmt(r, t) :
              `time=${t}  cycle_count=${r.cyc}  robot_id=${r.rid}  ` +
              `total=${(r.total / 1000).toFixed(3)}ms  ②=${(r.wa / 1000).toFixed(3)}  ③=${(r.ah / 1000).toFixed(3)}`;
          }],
        },
      };
      this.uplot = new uPlot(opts, [[]].concat(labels.map(() => [])), el);
      window.addEventListener("resize", () => this.uplot.setSize({ width: el.clientWidth || 800, height: 300 }));
    } else {
      this.canvas = document.createElement("canvas"); this.canvas.height = 300; el.appendChild(this.canvas);
    }
  }
  // rows: [{t, <key0>, <key1>, ...}] (μs)、keys: row のフィールド名 (labels と同順、欠損は null)
  update(rows, keys) {
    this.rows = rows;   // カーソル hook 用に保持 (cyc/rid 含む)
    const xs = rows.map((r) => r.t);
    const data = [xs].concat(keys.map((k) =>
      rows.map((r) => (r[k] == null ? null : r[k] / 1000))));  // μs→ms
    // labels より keys が少ない場合は残り系列を空に
    while (data.length < this.labels.length + 1) data.push(rows.map(() => null));
    if (this.uplot) { this.uplot.setData(data); return; }
    // canvas fallback: 全系列を簡易描画
    const c = this.canvas; c.width = c.parentElement.clientWidth || 800;
    const ctx = c.getContext("2d"), W = c.width, H = c.height, pad = 36;
    ctx.clearRect(0, 0, W, H);
    if (!xs.length) return;
    const xmin = xs[0], xmax = xs[xs.length - 1] || xs[0] + 1;
    let ymax = 0;
    for (let s = 1; s < data.length; s++) for (const v of data[s]) if (v != null && v > ymax) ymax = v;
    if (ymax <= 0) ymax = 1;
    const px = (x) => pad + (W - 2 * pad) * (x - xmin) / (xmax - xmin || 1);
    const py = (y) => H - pad - (H - 2 * pad) * (y - 0) / (ymax || 1);
    ctx.strokeStyle = "#ccc"; ctx.beginPath(); ctx.moveTo(pad, pad); ctx.lineTo(pad, H - pad); ctx.lineTo(W - pad, H - pad); ctx.stroke();
    for (let s = 1; s < data.length; s++) {
      ctx.strokeStyle = this.colors[s - 1] || "#0a84ff"; ctx.lineWidth = 1.3; ctx.beginPath();
      let started = false;
      data[s].forEach((y, i) => { if (y == null) return; const X = px(xs[i]), Y = py(y); started ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); started = true; });
      ctx.stroke();
    }
  }
}

// ---------------------------------------------------------------
// 網図 (Mermaid)  AIPC → AP(sniffer 並走) → HID、edge に区間中央値
// ---------------------------------------------------------------
const HAS_MERMAID = (typeof mermaid !== "undefined");
if (HAS_MERMAID) {
  mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "loose",
                       flowchart: { curve: "linear" } });
}

function legLabel(leg, fallback) {
  if (!leg) return fallback;
  return (leg.median / 1000).toFixed(2) + " ms (p95 " + (leg.p95 / 1000).toFixed(2) + ")";
}

let _ngSeq = 0;
function renderNetDiagram(el, legs, total) {
  if (!HAS_MERMAID) { el.textContent = "(mermaid 未読込)"; return; }
  legs = legs || {};
  const ah = legLabel(legs.air_hid, "air→HID");
  const tot = total ? "total(②+③, ①除く) " + (total.median / 1000).toFixed(2) + " ms" : "total —";
  let def;
  if (legs.host_wire && legs.wire_air) {
    // 3 区間 (wire SPAN 並走時): host+有線 / AP滞留 / air→HID。
    // ②AP滞留 = t_air(sniffer) − t_wire(SPAN)。frame は SPAN受信→AP→sniffer観測 を通る
    // ので、その 3 ノードを subgraph で囲み「SPAN-sniff で取得」を明示する。
    def =
      "graph LR\n" +
      '  AIPC["AIPC<br/>AI送出 (t_tx)"]\n' +
      '  subgraph QUEUE["② AP滞留 = t_air − t_wire ' + legLabel(legs.wire_air, "-") + '"]\n' +
      '    direction LR\n' +
      '    SPAN["SPAN受信<br/>RasPi eth0<br/>(t_wire)"]\n' +
      '    AP["AP / LN6001<br/>queue"]\n' +
      '    SNF["sniffer 並走<br/>air 観測<br/>(t_air)"]\n' +
      '    SPAN -->|有線| AP -->|air| SNF\n' +
      '  end\n' +
      '  HID["HID (t_hid)"]\n' +
      '  AIPC -- "①host+有線<br/>' + legLabel(legs.host_wire, "-") + '" --> SPAN\n' +
      '  SNF -- "③air→HID<br/>' + ah + '" --> HID\n' +
      '  AIPC -. "' + tot + '" .-> HID\n';
  } else {
    // 2 区間 (wire 無し): 送信→air / air→HID
    def =
      "graph LR\n" +
      '  AIPC["AIPC<br/>AI送出 (有線→SPAN)"]\n' +
      '  AP["AP / LN6001<br/>(sniffer 並走)"]\n' +
      '  HID["HID (ESP32-C5)"]\n' +
      '  AIPC -- "①送信→air<br/>' + legLabel(legs.send_air, "送信→air") + '" --> AP\n' +
      '  AP -- "②air→HID<br/>' + ah + '" --> HID\n' +
      '  AIPC -. "' + tot + '" .-> HID\n';
  }
  const id = "netg" + (++_ngSeq);
  try {
    mermaid.render(id, def).then(({ svg }) => { el.innerHTML = svg; })
      .catch((e) => { console.error("mermaid", e); });
  } catch (e) {
    console.error("mermaid", e);
  }
}

window.GTNLV = { TimeSeries, MultiSeries, renderNetDiagram, HAS_UPLOT, HAS_MERMAID };
