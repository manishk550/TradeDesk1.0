"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const signed = (v, d = 2) => (v === null || v === undefined ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(d));
const cls = (v) => (v > 0 ? "up" : v < 0 ? "dn" : "mute");
const big = (v) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
};

const S = {
  market: localStorage.getItem("fd.market") || "IN",
  scan: null,
  sel: null,
  detail: null,
  tab: "picks",
  ctab: "daily",
  range: 120,
  sub: "vol",
  show: { ema: true, sma: true, bb: false, st: false, sig: true, piv: false, lvl: true },
  sort: { key: "chg", dir: -1 },
  filter: "",
  intraday: null,
  poll: null,
  timers: [],
};
const riskKey = () => `fd.risk.${S.market}`;
const defaultRisk = () => (S.market === "IN" ? 10000 : 100);
const getRisk = () => Number(localStorage.getItem(riskKey())) || defaultRisk();

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

/* ------------------------------------------------------------------ boot */
function init() {
  $$("#market-toggle button").forEach((b) => {
    b.classList.toggle("on", b.dataset.m === S.market);
    b.onclick = () => switchMarket(b.dataset.m);
  });
  $$("#left-tabs button[data-t]").forEach((b) => (b.onclick = () => setTab(b.dataset.t)));
  setupResize();
  $("#refresh").onclick = () => load(true);
  const ri = $("#risk");
  ri.onchange = () => {
    localStorage.setItem(riskKey(), ri.value);
    load(true);
  };
  syncRiskInput();
  setupAsk();
  load();
  loadOrb();
  setInterval(() => { if (S.scan && S.scan.market_status.state === "open") load(true, true); }, 90000);
  setInterval(() => { if (S.scan && S.scan.market_status.state === "open") loadOrb(); }, 30000);
}
function syncRiskInput() {
  $("#risk").value = getRisk();
  $("#ccy").textContent = S.market === "IN" ? "₹" : "$";
}
function switchMarket(m) {
  if (m === S.market) return;
  S.market = m;
  localStorage.setItem("fd.market", m);
  S.scan = null; S.sel = null; S.detail = null; S.intraday = null;
  $$("#market-toggle button").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
  syncRiskInput();
  $("#detail").innerHTML = '<div class="empty big">Pick a name on the left to see its chart, setups, technicals, fundamentals and news.</div>';
  $("#today").innerHTML = "";
  S.orb = null;
  load();
  loadOrb();
}
const LEFT_COMPACT = 400;
function leftMax() { return Math.max(LEFT_COMPACT, window.innerWidth - 480); }
function setLeft(px, persist = true) {
  const w = Math.round(Math.min(leftMax(), Math.max(320, px)));
  document.documentElement.style.setProperty("--left", w + "px");
  if (persist) localStorage.setItem("fd.leftW", String(w));
  const wide = w > LEFT_COMPACT + 40;
  const b = $("#widen");
  b.textContent = wide ? "Compact" : "Expand";
  b.title = wide ? "Shrink the list back" : "Widen the list";
  b.setAttribute("aria-pressed", wide ? "true" : "false");
  requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
}
function setupResize() {
  const saved = Number(localStorage.getItem("fd.leftW"));
  setLeft(saved || LEFT_COMPACT, false);
  $("#widen").onclick = () => {
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--left"), 10) || LEFT_COMPACT;
    setLeft(cur > LEFT_COMPACT + 40 ? LEFT_COMPACT : Math.min(leftMax(), Math.max(780, Math.round(window.innerWidth * 0.58))));
  };
  const bar = $("#split");
  bar.onmousedown = (e) => {
    e.preventDefault();
    bar.classList.add("drag");
    const move = (ev) => {
      const desk = $(".desk").getBoundingClientRect();
      setLeft(ev.clientX - desk.left, false);
    };
    const up = (ev) => {
      bar.classList.remove("drag");
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.style.userSelect = "";
      const desk = $(".desk").getBoundingClientRect();
      setLeft(ev.clientX - desk.left);
    };
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  };
  bar.ondblclick = () => setLeft(LEFT_COMPACT);
}

function setTab(t) {
  S.tab = t;
  $$("#left-tabs button[data-t]").forEach((b) => b.classList.toggle("on", b.dataset.t === t));
  ["picks", "scan", "books"].forEach((x) => $("#tab-" + x).classList.toggle("hidden", x !== t));
}

async function load(force = false, quiet = false) {
  clearTimeout(S.poll);
  const m = S.market;
  if (!quiet) {
    $("#verdict").className = "verdict";
    $("#verdict").innerHTML = "<span>Scanning every contract, running the backtests and reading the news… the first scan of the day can take up to a minute.</span>";
  }
  try {
    const data = await api(`/api/scan?market=${m}&risk=${getRisk()}`);
    if (m !== S.market) return;
    S.scan = data;
    render();
    if (!data.ready || data.status.evaluating || (data.status.india_sync && data.status.india_sync.running) || (data.status.us_sync && data.status.us_sync.running)) {
      S.poll = setTimeout(() => load(true, true), 6000);
    }
  } catch (e) {
    $("#verdict").className = "verdict warn";
    $("#verdict").innerHTML = `<span>Could not load: ${esc(e.message)}</span>`;
  }
}

/* ------------------------------------------------------------------ left column */
function render() {
  const d = S.scan;
  const st = d.market_status;
  const chip = $("#mkt-state");
  chip.className = "chip " + st.state;
  chip.textContent = st.state === "open" ? `${S.market} OPEN · ${st.now}` : st.state === "pre" ? `${S.market} PRE-OPEN · ${st.now}` : `${S.market} CLOSED · opens ${st.next_open_ist}`;
  $("#asof").textContent = d.ready ? `data to ${d.as_of} · ${d.universe} contracts` : "loading data…";
  renderVerdict();
  renderToday();
  renderPicks();
  renderScan();
  renderBooks();
}

function renderVerdict() {
  const d = S.scan, el = $("#verdict");
  if (!d.ready) {
    const s = d.status.india_sync;
    const prog = s && s.running ? ` Downloading NSE history ${s.done}/${s.total}…` : "";
    el.className = "verdict warn";
    el.innerHTML = `<span>Preparing data.${esc(prog)} Hang on — this only happens on first start.</span>`;
    return;
  }
  const val = d.playbooks.filter((p) => p.verdict === "VALIDATED");
  const mar = d.playbooks.filter((p) => p.verdict === "MARGINAL");
  const tradable = d.picks.filter((p) => ["VALIDATED", "MARGINAL"].includes(p.verdict) && p.grade !== "SKIP");
  const buys = tradable.filter((p) => p.side === "long").length, sells = tradable.length - buys;
  const pend = d.status.evaluating ? " (backtests refreshing…)" : "";
  if (val.length) {
    el.className = "verdict good";
    el.innerHTML = `<span><b>${val.length} playbook${val.length > 1 ? "s" : ""} passed the out-of-sample test</b> on this market: ${val.map((p) => `${esc(p.name)} (${p.side})`).join(", ")}. ` +
      `Today: <b>${buys} BUY · ${sells} SELL</b> setups from tested rules${pend}.</span>` +
      `<div class="sub">Edge per trade is small (see Backtests): win rates and R-expectancy below are net of ${(d.cost * 100).toFixed(2)}% round-trip costs. ${d.playbooks.length} playbooks were tested, so treat even a pass as promising, not proven — risk small, keep stops.</div>`;
  } else {
    el.className = "verdict warn";
    el.innerHTML = `<span><b>No playbook has a validated edge on ${S.market === "IN" ? "NSE futures" : "these US names"} right now.</b> ${mar.length ? `${mar.length} are marginal (positive but thin).` : ""} ` +
      `The honest call is to trade small, paper-trade, or stay flat. Setups below are shown as ideas only${pend}.</span>` +
      `<div class="sub">${d.playbooks.length} rule sets were backtested over ${esc(d.history ? d.history.join(" → ") : "")} with the last 40% held out as an out-of-sample test.</div>`;
  }
}

function topSide(side) {
  return (S.scan.picks || []).filter((p) => p.side === side && p.grade !== "SKIP").slice(0, 5);
}
function todayCard(p, rank) {
  const lv = p.mode === "stop" ? `Trigger ${num(p.entry)}` : `Entry ${num(p.entry)}`;
  return `<button class="tcard ${p.side} ${p.symbol === S.sel ? "on" : ""}" data-sym="${esc(p.symbol)}">
    <div class="row"><span class="rank">${rank}</span><span class="sym">${esc(p.symbol)}</span><span class="act ${p.action}">${p.action}</span><span class="grade ${p.grade}">${p.grade}</span></div>
    <div class="rule" title="${esc(p.rule_name)}">${esc(p.rule_name)}</div>
    <div class="px">${lv}<br><span class="dn">SL ${num(p.stop)}</span> · <span class="up">T ${num(p.t1)}</span></div>
  </button>`;
}
function renderToday() {
  const el = $("#today");
  const d = S.scan;
  if (!d || !d.ready) { el.innerHTML = ""; return; }
  const buys = topSide("long"), sells = topSide("short");
  const shown = buys.concat(sells);
  const col = (title, cls, rows) => `<div class="today-col ${cls}"><h4>${title}</h4><div class="tcards">${
    rows.length ? rows.map((p, i) => todayCard(p, i + 1)).join("") : '<div class="empty">None today.</div>'
  }</div></div>`;
  const unproven = shown.length && shown.every((p) => !["VALIDATED", "MARGINAL"].includes(p.verdict));
  el.innerHTML = `<h3><span>Today's buy and sell</span><span class="sub">Top 5 each · ranked by backtest score · data to ${esc(d.as_of)}</span></h3>
    <div class="today-cols">${col("Buy", "buy", buys)}${col("Sell / short", "sell", sells)}</div>
    ${unproven ? '<div class="caveat">None of these passed the out-of-sample test. They are the highest-scoring ideas only — the list below has the full evidence.</div>' : ""}`;
  $$(".tcard", el).forEach((b) => (b.onclick = () => selectSymbol(b.dataset.sym)));
}

function pickCard(p, on) {
  const pb = p.playbook, o = pb ? pb.oos : null;
  const ev = o && o.n ? `${o.win}% win · ${signed(o.exp)}R/trade · PF ${o.pf} <span class="mute">(out-of-sample, ${o.n} trades)</span>` : "No backtest evidence yet";
  const unp = !["VALIDATED", "MARGINAL"].includes(p.verdict);
  const lv = p.mode === "stop" ? `${p.side === "long" ? "Buy-stop" : "Sell-stop"} ${num(p.entry)}` : `Entry ~${num(p.entry)}`;
  return `<button class="pick ${p.side} ${unp ? "unproven" : ""} ${on ? "on" : ""}" data-sym="${esc(p.symbol)}">
    <div class="row"><span class="sym">${esc(p.symbol)}</span><span class="act ${p.action}">${p.action}</span><span class="badge ${p.verdict}">${p.verdict === "TOO_FEW" ? "few trades" : p.verdict.toLowerCase()}</span><span class="grade ${p.grade}">${p.grade}</span></div>
    ${p.late ? '<div class="sub" style="color:var(--amber)">Mid-session: only if price is still near entry</div>' : ""}
    <div class="sub">${esc(p.rule_name)} · ${p.kind === "day" ? "day trade" : "swing " + p.hold + "d"}${p.cond ? " · " + esc(p.cond) : ""}</div>
    <div class="lv"><span>${lv}</span><span class="dn">SL ${num(p.stop)}</span><span class="up">T ${num(p.t1)}</span></div>
    <div class="ev">${ev}</div></button>`;
}

async function loadOrb() {
  const m = S.market;
  try {
    const r = await api(`/api/orb?market=${m}`);
    if (m !== S.market) return;
    S.orb = r;
    renderOrb();
  } catch (e) { S.orb = null; }
}
function renderOrb() {
  const el = $("#orb-board");
  if (!el || !S.orb) return;
  const rows = S.orb.rows, live = S.orb.market_status.state === "open";
  const stCls = { triggered: "up", waiting: "", forming: "", stopped: "dn", failed: "dn" };
  el.innerHTML = `<h3 class="sec"><span>Live ORB board — top relative volume</span><span>${live ? "live" : "last session"}</span></h3>
  <div class="note" style="margin-bottom:6px">Published rule (Zarattini–Barbon–Aziz): on the day's highest relative-volume names, trade the direction of the first 5-minute candle, enter on its break, stop at the other end, flat at the close. Forward-tested in the Backtests tab; not yet proven here.</div>
  <table class="t"><tr><th>Symbol</th><th>RVOL</th><th>Gap%</th><th>State</th><th>Entry / stop</th><th>R now</th></tr>${rows.map((r) => `<tr class="row" data-sym="${esc(r.symbol)}" data-intra="1"><td>${esc(r.symbol)} ${r.direction === "long" ? '<span class="up">▲</span>' : r.direction === "short" ? '<span class="dn">▼</span>' : ""}</td><td>${r.rvol}x</td><td class="${cls(r.gap)}">${r.gap == null ? "—" : signed(r.gap, 1)}</td><td class="${stCls[r.state] || "mute"}">${esc(r.state)}</td><td>${r.entry ? num(r.entry) + " / " + num(r.stop) : r.or_high ? "OR " + num(r.or_low) + "–" + num(r.or_high) : "—"}</td><td class="${cls(r.r_now)}">${r.r_now == null ? "—" : signed(r.r_now)}</td></tr>`).join("")}</table>`;
  $$("tr.row", el).forEach((tr) => (tr.onclick = () => selectSymbol(tr.dataset.sym, "intra")));
}

function renderPicks() {
  const d = S.scan, el = $("#tab-picks");
  if (!d.ready) { el.innerHTML = '<div class="loading">Waiting for data…</div>'; return; }
  const good = d.picks.filter((p) => ["VALIDATED", "MARGINAL"].includes(p.verdict) && p.grade !== "SKIP");
  const skipped = d.picks.filter((p) => ["VALIDATED", "MARGINAL"].includes(p.verdict) && p.grade === "SKIP");
  const idea = d.picks.filter((p) => !["VALIDATED", "MARGINAL"].includes(p.verdict));
  const buys = good.filter((p) => p.side === "long"), sells = good.filter((p) => p.side === "short");
  let h = '<div id="orb-board"><div class="loading">Loading ORB board…</div></div>';
  h += `<h3 class="sec"><span>Buy</span><span>${buys.length}</span></h3>` + (buys.map((p) => pickCard(p, p.symbol === S.sel)).join("") || '<div class="empty">No buy setups from a tested playbook for the next session.</div>');
  h += `<h3 class="sec"><span>Sell / short</span><span>${sells.length}</span></h3>` + (sells.map((p) => pickCard(p, p.symbol === S.sel)).join("") || '<div class="empty">No sell setups from a tested playbook for the next session.</div>');
  if (d.conditional.length) {
    const pbs = Object.fromEntries(d.playbooks.map((p) => [p.key, p.verdict]));
    h += `<h3 class="sec"><span>Open-gap watch (in play)</span><span>${d.conditional.length}</span></h3><div class="note" style="margin-bottom:6px">Stocks that traded heavy volume. Decide at the open: a gap beyond the level plus confirmation → trade it with the gap, flat by the close.${["VALIDATED", "MARGINAL"].includes(pbs["gap_go:long"]) || ["VALIDATED", "MARGINAL"].includes(pbs["gap_go:short"]) ? "" : " <b>Gap & Go is not validated here, so treat these as a watchlist only.</b>"}</div>` +
      `<table class="t"><tr><th>Symbol</th><th>Buy above</th><th>Sell below</th><th>RVOL</th></tr>${d.conditional.slice(0, 15).map((c) => `<tr class="row" data-sym="${esc(c.symbol)}"><td>${esc(c.symbol)}</td><td class="up">${num(c.buy_above)}</td><td class="dn">${num(c.sell_below)}</td><td>${c.rvol}x</td></tr>`).join("")}</table>`;
  }
  if (d.hidden_setups) h += `<div class="note" style="margin-top:10px">${d.hidden_setups} more setups were hidden because their rule loses money out-of-sample.</div>`;
  if (skipped.length) h += `<details class="fold"><summary>Skipped by news / earnings (${skipped.length})</summary>${skipped.map((p) => pickCard(p, p.symbol === S.sel)).join("")}</details>`;
  if (idea.length) h += `<details class="fold" ${good.length ? "" : "open"}><summary>Unvalidated ideas — rules that lost money out-of-sample (${idea.length})</summary>${idea.map((p) => pickCard(p, p.symbol === S.sel)).join("")}</details>`;
  el.innerHTML = h;
  $$("[data-sym]", el).forEach((b) => { if (!b.dataset.intra) b.onclick = () => selectSymbol(b.dataset.sym); });
  renderOrb();
}

function renderScan() {
  const d = S.scan, el = $("#tab-scan");
  if (!d.ready) { el.innerHTML = ""; return; }
  const inr = S.market === "IN";
  const cols = [["symbol", "Symbol"], ["close", "Last"], ["chg", "Chg%"], ["rvol", "RVOL"], ["rsi14", "RSI"], ["trend", "Trend"], ["rating", "Rating"]];
  if (inr) cols.push(["oi_chg", "OI%"]);
  let rows = d.table.filter((r) => !S.filter || r.symbol.toLowerCase().includes(S.filter) || (r.name || "").toLowerCase().includes(S.filter));
  const k = S.sort.key, dir = S.sort.dir;
  rows = [...rows].sort((a, b) => {
    const x = a[k], y = b[k];
    if (x == null) return 1;
    if (y == null) return -1;
    return (x > y ? 1 : x < y ? -1 : 0) * dir;
  });
  el.innerHTML = `<input class="filter" id="flt" placeholder="Filter ${d.table.length} contracts…" value="${esc(S.filter)}" />
  <table class="t"><tr>${cols.map(([key, label]) => `<th data-k="${key}">${label}${S.sort.key === key ? (S.sort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr>
  ${rows.map((r) => `<tr class="row ${r.symbol === S.sel ? "on" : ""}" data-sym="${esc(r.symbol)}"><td>${esc(r.symbol)}</td><td>${num(r.close)}</td><td class="${cls(r.chg)}">${signed(r.chg)}</td><td>${r.rvol ?? "—"}</td><td class="${r.rsi14 >= 70 ? "dn" : r.rsi14 <= 30 ? "up" : ""}">${r.rsi14 ?? "—"}</td><td class="${r.trend === "up" ? "up" : r.trend === "down" ? "dn" : ""}">${r.trend}</td><td class="${cls(r.rating)}">${esc(r.rating_label)}</td>${inr ? `<td class="${cls(r.oi_chg)}">${r.oi_chg == null ? "—" : signed(r.oi_chg, 1)}</td>` : ""}</tr>`).join("")}</table>`;
  $("#flt").oninput = (e) => { S.filter = e.target.value.toLowerCase(); renderScan(); const f = $("#flt"); f.focus(); f.setSelectionRange(f.value.length, f.value.length); };
  $$("th[data-k]", el).forEach((th) => (th.onclick = () => { const key = th.dataset.k; S.sort = { key, dir: S.sort.key === key ? -S.sort.dir : -1 }; renderScan(); }));
  $$("tr.row", el).forEach((tr) => (tr.onclick = () => selectSymbol(tr.dataset.sym)));
}

function spark(curve) {
  if (!curve || curve.length < 2) return "";
  const ys = curve.map((c) => c[1]);
  const lo = Math.min(...ys, 0), hi = Math.max(...ys, 0.01), W = 300, H = 34;
  const pts = ys.map((y, i) => `${(i / (ys.length - 1)) * W},${H - ((y - lo) / (hi - lo)) * (H - 4) - 2}`).join(" ");
  const zero = H - ((0 - lo) / (hi - lo)) * (H - 4) - 2;
  const col = ys[ys.length - 1] >= 0 ? "#2fd18b" : "#ff6464";
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="0" x2="${W}" y1="${zero}" y2="${zero}" stroke="#2c3646" stroke-dasharray="3 3"/><polyline fill="none" stroke="${col}" stroke-width="1.6" points="${pts}"/></svg>`;
}

function renderBooks() {
  const d = S.scan, el = $("#tab-books");
  if (!d.ready) { el.innerHTML = ""; return; }
  const note = `<div class="note" style="margin-bottom:10px">Every rule is fixed in advance (nothing tuned to this data) and run on all ${d.universe} contracts, ${esc(d.history ? d.history.join(" → ") : "")}. First 60% of days = in-sample, last 40% (from ${esc(d.split)}) = <b>out-of-sample</b>. R = profit in multiples of the stop distance, net of ${(d.cost * 100).toFixed(2)}% costs. VALIDATED = positive in both halves, out-of-sample PF ≥ 1.2 and day-clustered t ≥ 1.5. With ${d.playbooks.length} playbooks tested, expect a lucky pass now and then.</div>
  <div class="note" style="margin-bottom:12px"><b>Research basis:</b> the one large-sample published day-trading edge is the 5-minute Opening Range Breakout on high-relative-volume “Stocks in Play” (Zarattini–Barbon–Aziz 2024: 48% win rate, Sharpe 2.8, 7,000+ US stocks 2016–23). Free sources have no multi-year intraday history, so it is shown live on each chart (Intraday tab) but <b>not</b> backtested here.</div>`;
  const of = d.orb_forward;
  const orbCard = of ? `<div class="book"><div class="hd"><span class="nm">Opening Range Breakout (5-min) — forward test</span><span class="badge ${of.days >= of.need_days ? "MARGINAL" : "TOO_FEW"}">${of.days} / ${of.need_days} sessions</span></div>
    <div class="note" style="margin-top:6px">The desk records every contract's 5-minute bars after each session and scores the published ORB rule on them (trade the first candle's direction, enter on its break, stop at the other end, flat by the close). No look-ahead, nothing fitted — the evidence builds one real session at a time${of.first ? `, ${esc(of.first)} → ${esc(of.last)}` : ""}.</div>
    <div class="stats"><div>All trades<b>${of.all.n}</b></div><div>Win rate<b>${of.all.n ? of.all.win + "%" : "—"}</b></div><div>R/trade<b class="${cls(of.all.exp)}">${of.all.n ? signed(of.all.exp) : "—"}</b></div><div>PF<b>${of.all.n ? of.all.pf : "—"}</b></div></div>
    <div class="stats"><div>Gap ≥0.5 ATR<b>${of.in_play.n}</b></div><div>Win rate<b>${of.in_play.n ? of.in_play.win + "%" : "—"}</b></div><div>R/trade<b class="${cls(of.in_play.exp)}">${of.in_play.n ? signed(of.in_play.exp) : "—"}</b></div><div>PF<b>${of.in_play.n ? of.in_play.pf : "—"}</b></div></div>
    <div class="why">${of.days < of.need_days ? `Too early to trust: need about ${of.need_days} sessions. Use it as a live tracker on the Intraday tab, paper-trade it first.` : "Enough sessions recorded to judge."}</div></div>` : "";
  el.innerHTML = note + orbCard + d.playbooks.map((p) => {
    const a = p.all, i = p.is, o = p.oos;
    return `<div class="book"><div class="hd"><span class="nm">${esc(p.name)}</span><span class="act ${p.side === "long" ? "BUY" : "SELL"}">${p.side === "long" ? "LONG" : "SHORT"}</span><span class="badge ${p.verdict}">${p.verdict === "TOO_FEW" ? "few trades" : p.verdict.toLowerCase()}</span></div>
    <div class="stats"><div>Trades<b>${a.n}</b></div><div>Win rate<b>${o.n ? o.win : "—"}%</b></div><div>OOS R/trade<b class="${cls(o.exp)}">${signed(o.exp)}</b></div><div>OOS PF<b>${o.pf}</b></div></div>
    <div class="stats"><div>In-sample R<b class="${cls(i.exp)}">${signed(i.exp)}</b></div><div>OOS trades<b>${o.n}</b></div><div>Avg win / loss<b>${o.avg_win} / ${o.avg_loss}</b></div><div>t-stat<b>${o.t}</b></div></div>
    ${spark(p.curve)}<div class="why">${p.kind === "day" ? "Day trade" : "Swing"} · ${esc(p.why)}</div></div>`;
  }).join("");
}

/* ------------------------------------------------------------------ detail */
async function selectSymbol(sym, tab) {
  S.sel = sym;
  S.ctab = tab || "daily";
  S.intraday = null;
  renderToday(); renderPicks(); renderScan();
  $("#detail").innerHTML = '<div class="loading">Loading ' + esc(sym) + "…</div>";
  try {
    S.detail = await api(`/api/detail?market=${S.market}&symbol=${encodeURIComponent(sym)}&risk=${getRisk()}`);
    renderDetail();
  } catch (e) {
    $("#detail").innerHTML = `<div class="empty big">Could not load ${esc(sym)}: ${esc(e.message)}</div>`;
  }
}

function ticket(p, ccy) {
  const sz = p.sizing;
  let sizing = "";
  if (sz) {
    sizing = S.market === "IN"
      ? (sz.fits ? `<div><span>Size for your risk</span><b>${sz.lots} lot${sz.lots > 1 ? "s" : ""} · ${sz.qty} qty</b></div>` : `<div><span>Size for your risk</span><b class="dn">0 lots — 1 lot risks ${ccy}${num(sz.risk_per_lot, 0)}</b></div>`) +
        `<div><span>1 lot value / risk</span><b>${ccy}${big(sz.lot_value)} / ${ccy}${num(sz.risk_per_lot, 0)}</b></div>`
      : `<div><span>Size for your risk</span><b>${sz.qty} units</b></div><div><span>Notional</span><b>${ccy}${num(sz.notional, 0)}</b></div>`;
  }
  const pb = p.playbook, o = pb && pb.oos;
  return `<div class="ticket ${p.side}">
    <div class="top2"><span class="big">${p.action} ${esc(S.detail.symbol)}</span><span class="badge ${p.verdict}">${p.verdict === "TOO_FEW" ? "few trades" : p.verdict.toLowerCase()}</span><span class="grade ${p.grade}">${p.grade}</span><span class="mute">${esc(p.rule_name)} · ${p.kind === "day" ? "day trade" : "swing " + p.hold + "d"}</span></div>
    ${p.cond ? `<div class="note">${esc(p.cond)}</div>` : ""}
    <div class="levels"><div><span>${p.mode === "stop" ? "Trigger" : "Entry"}</span><b>${num(p.entry)}</b></div><div><span>Stop-loss</span><b class="dn">${num(p.stop)}</b></div><div><span>${p.target != null ? "Target" : "Target 1R"}</span><b class="up">${num(p.t1)}</b></div><div><span>Risk / unit</span><b>${num(p.risk)} (${p.risk_pct}%)</b></div>${sizing}</div>
    <ol>${p.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
    <ul>${p.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
    ${o && o.n ? `<div class="ev">Backtest (out-of-sample, ${o.n} trades, all ${S.scan ? S.scan.universe : ""} contracts): <b>${o.win}% win · ${signed(o.exp)}R per trade · PF ${o.pf}</b>. Avg win ${o.avg_win}R, avg loss ${o.avg_loss}R.</div>` : ""}
  </div>`;
}

function renderDetail() {
  const d = S.detail, ccy = d.ccy;
  const last = d.bars[d.bars.length - 1], prev = d.bars[d.bars.length - 2];
  const chg = prev ? (last.close / prev.close - 1) * 100 : 0;
  const sn = d.snapshot, t = d.tech, f = d.fundamentals, x = d.extra;
  const lp = sn.price ?? last.close;
  const tvSym = S.market === "IN" ? `NSE:${d.symbol.replace(/&/g, "_").replace(/-/g, "_")}1!` : d.tv_symbol || `NASDAQ:${d.symbol}`;
  const tvUrl = `https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(tvSym)}&interval=5&theme=dark&style=1&timezone=${S.market === "IN" ? "Asia%2FKolkata" : "America%2FNew_York"}&withdateranges=1&hide_side_toolbar=0&allow_symbol_change=0`;
  let html = `<div class="dhead"><h2>${esc(d.symbol)}</h2><span class="px">${ccy}${num(lp)}</span><span class="${cls(sn.change ?? chg)} mono">${signed(sn.change ?? chg)}%</span><span class="meta">${esc(d.name !== d.symbol ? d.name : "")} ${x.expiry ? "· exp " + esc(x.expiry) : ""} ${x.lot ? "· lot " + x.lot : ""} · daily to ${esc(d.as_of)}</span></div>`;
  html += `<div class="card"><div class="ctools">
    <div class="grp" id="ctab"><button data-c="daily" class="on">Daily</button><button data-c="intra">Intraday 5m + ORB</button><button data-c="tv">Live (TradingView)</button></div>
    <div class="grp" id="rng"><button data-r="60">3M</button><button data-r="120">6M</button><button data-r="260">1Y</button></div>
    <div class="grp" id="sub"><button data-s="vol">Volume</button>${S.market === "IN" ? '<button data-s="oi">Open int.</button>' : ""}<button data-s="rsi">RSI</button></div>
    <div class="grp" id="ovl"><button data-o="ema">EMA20</button><button data-o="sma">SMA50/100</button><button data-o="bb">Bollinger</button><button data-o="st">Supertrend</button><button data-o="sig">Signals</button><button data-o="lvl">Trade levels</button><button data-o="piv">Pivots</button></div></div>
    <div id="chart-area"></div></div>`;
  html += `<div class="card"><h4>Today's setups for ${esc(d.symbol)}</h4>${d.picks.length ? d.picks.map((p) => ticket(p, ccy)).join("") : `<div class="empty">None of the rules fire for the next session. No trade is a position — wait for a setup rather than forcing one.</div>`}</div>`;
  html += `<div class="grid2"><div class="card"><h4>Technical analysis</h4><div class="kv">
    <span>Trend (100d / 50d)</span><span class="${t.sma100 && t.close > t.sma100 ? "up" : "dn"}">${t.sma100 && t.close > t.sma100 ? "above" : "below"} / ${t.sma50 && t.close > t.sma50 ? "above" : "below"}</span>
    <span>EMA20 · SMA50 · SMA100</span><span>${num(t.ema20)} · ${num(t.sma50)} · ${num(t.sma100)}</span>
    <span>RSI(2) · RSI(14)</span><span class="${t.rsi2 <= 10 ? "up" : t.rsi2 >= 90 ? "dn" : ""}">${t.rsi2 ?? "—"} · ${t.rsi14 ?? "—"}</span>
    <span>MACD histogram</span><span class="${cls(t.macd_hist)}">${signed(t.macd_hist, 3)}</span>
    <span>Supertrend (10,3)</span><span class="${t.supertrend === "up" ? "up" : "dn"}">${t.supertrend}</span>
    <span>ATR(14)</span><span>${num(t.atr)} (${t.atr_pct}%)</span>
    <span>ADX</span><span>${t.adx ?? "—"}</span>
    <span>Relative volume</span><span>${t.rvol}x</span>
    <span>20-day high / low</span><span>${num(t.hh20)} / ${num(t.ll20)}</span>
    <span>Bollinger position</span><span>${t.bb_pos ?? "—"}%</span>
    <span>TradingView rating</span><span class="${cls(t.rating)}">${esc(t.rating_label)} (MA ${esc(t.rating_ma)} · Osc ${esc(t.rating_osc)})</span></div>
    <h4 style="margin-top:14px">Pivot levels (next session)</h4><div class="kv">${["R2", "R1", "P", "S1", "S2"].map((k) => `<span>${k}</span><span>${num(d.pivots[k])}</span>`).join("")}<span>Prev high / low</span><span>${num(d.pivots.prevH)} / ${num(d.pivots.prevL)}</span></div>
    ${S.market === "IN" && x.oi != null ? `<h4 style="margin-top:14px">Open interest</h4><div class="kv"><span>OI</span><span>${big(x.oi)}</span><span>Change today</span><span class="${cls(x.oi_chg_pct)}">${signed(x.oi_chg_pct, 1)}%</span><span>Read</span><span>${esc(x.oi_read)}</span><span>Lot value</span><span>${ccy}${big(x.lot_value)}</span></div>` : ""}
    </div>
    <div class="card"><h4>Fundamentals</h4>${fundBlock(f, ccy)}
    <h4 style="margin-top:14px">News (last 3 days) · <span class="tag ${d.news_summary.label}">${d.news_summary.label}</span></h4>
    <ul class="news">${d.news.length ? d.news.map((n) => `<li><span class="tag ${n.sentiment}">${n.sentiment}</span><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a><div class="m">${esc(n.source)}${n.ts ? " · " + new Date(n.ts * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : ""}</div></li>`).join("") : '<li class="mute">No recent headlines found.</li>'}</ul></div></div>`;
  const ps = Object.entries(d.per_symbol || {});
  if (ps.length) html += `<div class="card"><h4>How each rule has behaved on ${esc(d.symbol)} alone</h4><div class="note">Small samples — context only, never a reason to trade by itself.</div><table class="t" style="margin-top:8px"><tr><th>Playbook</th><th>Trades</th><th>Win%</th><th>R/trade</th></tr>${ps.map(([k, v]) => `<tr><td>${esc(k.replace(":", " · "))}</td><td>${v.n}</td><td>${v.win}%</td><td class="${cls(v.exp)}">${signed(v.exp)}</td></tr>`).join("")}</table></div>`;
  $("#detail").innerHTML = html;
  bindChartTools(tvUrl);
  drawActive();
}

function fundBlock(f, ccy) {
  if (!f.available) return '<div class="empty">No fundamentals for this contract (index futures or data unavailable).</div>';
  const rows = [
    ["Sector / industry", `${f.sector || "—"} · ${f.industry || "—"}`],
    ["Market cap", f.market_cap ? big(f.market_cap) : "—"],
    ["P/E · P/B", `${f.pe ? f.pe.toFixed(1) : "—"} · ${f.pb ? f.pb.toFixed(1) : "—"}`],
    ["EPS (TTM) · growth", `${f.eps != null ? f.eps.toFixed(2) : "—"} · ${f.eps_growth != null ? signed(f.eps_growth, 0) + "%" : "—"}`],
    ["Revenue growth y/y", f.revenue_growth != null ? signed(f.revenue_growth, 1) + "%" : "—"],
    ["Net margin · ROE", `${f.net_margin != null ? f.net_margin.toFixed(1) + "%" : "—"} · ${f.roe != null ? f.roe.toFixed(1) + "%" : "—"}`],
    ["Debt / equity · beta", `${f.debt_equity != null ? f.debt_equity.toFixed(2) : "—"} · ${f.beta != null ? f.beta.toFixed(2) : "—"}`],
    ["Dividend yield", f.div_yield != null ? f.div_yield.toFixed(2) + "%" : "—"],
    ["52-week range position", f.range_pos != null ? `${f.range_pos}% (${num(f.low_52w)} – ${num(f.high_52w)})` : "—"],
    ["Performance 1W / 1M / 3M", [f.perf["1W"], f.perf["1M"], f.perf["3M"]].map((v) => (v == null ? "—" : signed(v, 1) + "%")).join(" / ")],
    ["Next earnings", f.next_earnings ? new Date(f.next_earnings * 1000).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "—"],
    ["Quality read", `${f.quality}${f.notes.length ? " — " + f.notes.join(", ") : ""}`],
  ];
  return `<div class="kv">${rows.map(([a, b]) => `<span>${a}</span><span>${esc(b)}</span>`).join("")}</div>`;
}

/* ------------------------------------------------------------------ chart tools */
function bindChartTools(tvUrl) {
  const mark = () => {
    $$("#ctab button").forEach((b) => b.classList.toggle("on", b.dataset.c === S.ctab));
    $$("#rng button").forEach((b) => b.classList.toggle("on", Number(b.dataset.r) === S.range));
    $$("#sub button").forEach((b) => b.classList.toggle("on", b.dataset.s === S.sub));
    $$("#ovl button").forEach((b) => b.classList.toggle("on", !!S.show[b.dataset.o]));
    const daily = S.ctab === "daily";
    $("#rng").style.display = daily ? "" : "none";
    $("#sub").style.display = daily ? "" : "none";
    $("#ovl").style.display = daily ? "" : "none";
  };
  $$("#ctab button").forEach((b) => (b.onclick = () => { S.ctab = b.dataset.c; mark(); drawActive(tvUrl); }));
  $$("#rng button").forEach((b) => (b.onclick = () => { S.range = Number(b.dataset.r); mark(); drawActive(tvUrl); }));
  $$("#sub button").forEach((b) => (b.onclick = () => { S.sub = b.dataset.s; mark(); drawActive(tvUrl); }));
  $$("#ovl button").forEach((b) => (b.onclick = () => { S.show[b.dataset.o] = !S.show[b.dataset.o]; mark(); drawActive(tvUrl); }));
  S._tvUrl = tvUrl;
  mark();
}

function legendHtml() {
  return `<div class="legend"><span><i style="background:#6fb0ff"></i>EMA20</span><span><i style="background:#e9be5d"></i>SMA50</span><span><i style="background:#b784ff"></i>SMA100</span><span style="color:#2fd18b">▲ long signal</span><span style="color:#ff6464">▼ short signal</span><span>● exit (green win / red loss)</span><span>Signals shown only for validated or marginal playbooks.</span></div>`;
}

let intraTimer = null;
async function drawActive() {
  clearInterval(intraTimer);
  const area = $("#chart-area");
  if (!area) return;
  if (S.ctab === "tv") {
    area.innerHTML = `<iframe class="tv" src="${S._tvUrl}" title="TradingView live chart" allowfullscreen></iframe><div class="note" style="margin-top:6px">TradingView free widget: delayed quotes, and some exchanges may show a limited chart. The Daily and Intraday tabs use the desk's own data.</div>`;
    return;
  }
  if (S.ctab === "daily") {
    area.innerHTML = `<div class="chartwrap"><canvas class="chart" id="cv"></canvas><div class="hud" id="hud"></div></div>${legendHtml()}`;
    drawDaily();
    return;
  }
  area.innerHTML = `<div class="chartwrap"><canvas class="chart short" id="cv"></canvas><div class="hud" id="hud"></div></div><div id="orb" class="orb">Loading intraday…</div><div class="note" id="inote" style="margin-top:6px"></div>`;
  const run = async () => {
    try {
      const r = await api(`/api/intraday?market=${S.market}&symbol=${encodeURIComponent(S.sel)}`);
      S.intraday = r;
      $("#orb").className = "orb " + (r.orb.state || "");
      $("#orb").innerHTML = `<b>5-minute Opening Range Breakout</b> — ${esc(r.orb.text)}<div class="note" style="margin-top:4px">Rule (Zarattini–Barbon–Aziz): trade in the direction of the first 5-minute candle, enter on a break of its high/low, stop at the other end, exit at the close. Best on the day's highest relative-volume names.</div>`;
      $("#inote").textContent = r.note || "";
      drawIntraday();
    } catch (e) {
      $("#orb").textContent = "Intraday data unavailable: " + e.message;
    }
  };
  await run();
  if (S.detail.market_status.state === "open") intraTimer = setInterval(run, 20000);
}

/* ------------------------------------------------------------------ canvas candles */
function drawDaily() {
  const d = S.detail;
  const lines = [];
  if (S.show.lvl) d.picks.forEach((p) => {
    const col = p.side === "long" ? "#2fd18b" : "#ff6464";
    lines.push({ v: p.entry, label: `${p.action} ${p.mode === "stop" ? "trigger" : "entry"} ${num(p.entry)}`, color: col, dash: [6, 4] });
    lines.push({ v: p.stop, label: `SL ${num(p.stop)}`, color: "#ff6464", dash: [2, 3] });
    lines.push({ v: p.t1, label: `T ${num(p.t1)}`, color: "#2fd18b", dash: [2, 3] });
  });
  if (S.show.piv) Object.entries(d.pivots).forEach(([k, v]) => { if (["R1", "P", "S1"].includes(k)) lines.push({ v, label: k, color: "#6d7a90", dash: [1, 4] }); });
  const markers = S.show.sig ? d.markers.filter((m) => ["VALIDATED", "MARGINAL"].includes(m.verdict)) : [];
  const cfg = { canvas: $("#cv"), bars: d.bars, series: d.series, range: S.range, lines, markers, sub: S.sub, show: S.show, hud: $("#hud"), oi: S.market === "IN", fmtX: (b) => b.date.slice(5), ccy: d.ccy };
  renderCandles(cfg);
  window.onresize = () => renderCandles(cfg);
}

function drawIntraday() {
  const r = S.intraday;
  if (!r || !$("#cv")) return;
  const o = r.orb, lines = [];
  if (o.or_high) {
    lines.push({ v: o.or_high, label: `OR high ${num(o.or_high)}`, color: "#6fb0ff", dash: [5, 4] });
    lines.push({ v: o.or_low, label: `OR low ${num(o.or_low)}`, color: "#6fb0ff", dash: [5, 4] });
  }
  const tz = S.market === "IN" ? "Asia/Kolkata" : "America/New_York";
  const f = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: tz });
  const cfg = { canvas: $("#cv"), bars: r.bars.length ? r.bars : [], series: {}, range: 200, lines, markers: [], sub: r.bars.some((b) => b.volume) ? "vol" : null, show: {}, hud: $("#hud"), fmtX: (b) => f.format(new Date(b.time * 1000)), ccy: S.detail.ccy };
  if (!cfg.bars.length) { $("#hud").textContent = "No intraday bars yet (market closed or feed empty)."; const c = $("#cv"); c.getContext("2d").clearRect(0, 0, c.width, c.height); return; }
  renderCandles(cfg);
  window.onresize = () => renderCandles(cfg);
}

function renderCandles(cfg) {
  const cv = cfg.canvas;
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = cv.getBoundingClientRect();
  cv.width = Math.floor(rect.width * dpr);
  cv.height = Math.floor(rect.height * dpr);
  const c = cv.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = rect.width, H = rect.height;
  const padL = 8, padR = 70, padT = 10, padB = 22, subH = cfg.sub ? Math.round(H * 0.2) : 0, gap = cfg.sub ? 10 : 0;
  const mainH = H - padT - padB - subH - gap;
  const n = cfg.bars.length, vis = Math.min(cfg.range || n, n), start = n - vis;
  const plotW = W - padL - padR, step = plotW / vis, bw = Math.max(1, Math.min(14, step * 0.66));
  const X = (i) => padL + (i - start + 0.5) * step;
  let lo = Infinity, hi = -Infinity;
  for (let i = start; i < n; i++) { lo = Math.min(lo, cfg.bars[i].low); hi = Math.max(hi, cfg.bars[i].high); }
  const lastClose = cfg.bars[n - 1].close;
  cfg.lines.forEach((l) => { if (Math.abs(l.v / lastClose - 1) < 0.12) { lo = Math.min(lo, l.v); hi = Math.max(hi, l.v); } });
  const padPx = (hi - lo) * 0.06 || 1;
  lo -= padPx; hi += padPx;
  const Y = (v) => padT + (hi - v) / (hi - lo) * mainH;
  c.clearRect(0, 0, W, H);
  c.font = '11px "IBM Plex Mono", monospace';
  c.textBaseline = "middle";
  // grid + price axis
  c.strokeStyle = "#1f2733"; c.lineWidth = 1; c.fillStyle = "#7f8ba0";
  for (let g = 0; g <= 5; g++) {
    const v = lo + ((hi - lo) * g) / 5, y = Y(v);
    c.beginPath(); c.moveTo(padL, y); c.lineTo(W - padR, y); c.stroke();
    c.textAlign = "left"; c.fillText(num(v, v > 1000 ? 0 : 2), W - padR + 6, y);
  }
  // x labels
  c.textAlign = "center"; c.textBaseline = "alphabetic";
  const every = Math.max(1, Math.round(vis / 6));
  for (let i = start; i < n; i += every) c.fillText(cfg.fmtX(cfg.bars[i]), X(i), H - 6);
  c.textBaseline = "middle";
  // bollinger
  const sr = cfg.series || {};
  const line = (arr, color, w = 1.4, dash = []) => {
    if (!arr) return;
    c.beginPath(); c.strokeStyle = color; c.lineWidth = w; c.setLineDash(dash);
    let pen = false;
    for (let i = start; i < n; i++) {
      const v = arr[i];
      if (v == null) { pen = false; continue; }
      const x = X(i), y = Y(v);
      if (!pen) { c.moveTo(x, y); pen = true; } else c.lineTo(x, y);
    }
    c.stroke(); c.setLineDash([]);
  };
  if (cfg.show.bb) { line(sr.bb_up, "rgba(111,176,255,.45)", 1, [3, 3]); line(sr.bb_lo, "rgba(111,176,255,.45)", 1, [3, 3]); }
  // candles
  for (let i = start; i < n; i++) {
    const b = cfg.bars[i], up = b.close >= b.open, col = up ? "#2fd18b" : "#ff6464", x = X(i);
    c.strokeStyle = col; c.fillStyle = col; c.lineWidth = 1;
    c.beginPath(); c.moveTo(x, Y(b.high)); c.lineTo(x, Y(b.low)); c.stroke();
    const y1 = Y(Math.max(b.open, b.close)), y2 = Y(Math.min(b.open, b.close));
    c.fillRect(x - bw / 2, y1, bw, Math.max(1, y2 - y1));
  }
  if (cfg.show.ema) line(sr.ema20, "#6fb0ff");
  if (cfg.show.sma) { line(sr.sma50, "#e9be5d"); line(sr.sma100, "#b784ff"); }
  if (cfg.show.st && sr.supertrend) {
    for (let i = start + 1; i < n; i++) {
      const a = sr.supertrend[i - 1], b2 = sr.supertrend[i];
      if (a == null || b2 == null) continue;
      c.beginPath(); c.strokeStyle = b2 < cfg.bars[i].close ? "#2fd18b" : "#ff6464"; c.lineWidth = 1.6;
      c.moveTo(X(i - 1), Y(a)); c.lineTo(X(i), Y(b2)); c.stroke();
    }
  }
  // signals
  (cfg.markers || []).forEach((m) => {
    if (m.j < start) return;
    const x = X(m.j), long = m.side === "long", b = cfg.bars[m.j];
    c.fillStyle = long ? "#2fd18b" : "#ff6464";
    c.beginPath();
    if (long) { const y = Y(b.low) + 9; c.moveTo(x, y - 7); c.lineTo(x - 5, y + 2); c.lineTo(x + 5, y + 2); }
    else { const y = Y(b.high) - 9; c.moveTo(x, y + 7); c.lineTo(x - 5, y - 2); c.lineTo(x + 5, y - 2); }
    c.closePath(); c.fill();
    if (m.k >= start && m.k < n) {
      c.beginPath(); c.fillStyle = m.r > 0 ? "#2fd18b" : "#ff6464"; c.arc(X(m.k) + 3, Y(m.exit), 3, 0, 7); c.fill();
    }
  });
  // horizontal levels
  cfg.lines.forEach((l) => {
    if (l.v < lo || l.v > hi) return;
    const y = Y(l.v);
    c.strokeStyle = l.color; c.lineWidth = 1; c.setLineDash(l.dash || []);
    c.beginPath(); c.moveTo(padL, y); c.lineTo(W - padR, y); c.stroke(); c.setLineDash([]);
    c.fillStyle = l.color; c.textAlign = "right";
    const tw = c.measureText(l.label).width + 8;
    c.fillText(l.label, W - padR - 4, y - 7);
  });
  // last price tag
  { const y = Y(lastClose), up = lastClose >= cfg.bars[n - 1].open; c.fillStyle = up ? "#2fd18b" : "#ff6464"; c.fillRect(W - padR + 1, y - 9, padR - 2, 18); c.fillStyle = "#04120b"; c.textAlign = "left"; c.fillText(num(lastClose, lastClose > 1000 ? 0 : 2), W - padR + 5, y); }
  // sub panel
  if (cfg.sub) {
    const y0 = padT + mainH + gap, h = subH;
    c.strokeStyle = "#252d3a"; c.strokeRect(padL, y0, plotW, h);
    if (cfg.sub === "vol" || cfg.sub === "oi") {
      let vmax = 0;
      for (let i = start; i < n; i++) vmax = Math.max(vmax, cfg.bars[i].volume || 0);
      for (let i = start; i < n; i++) {
        const b = cfg.bars[i], vh = vmax ? ((b.volume || 0) / vmax) * (h - 4) : 0;
        c.fillStyle = (b.close >= b.open ? "rgba(47,209,139," : "rgba(255,100,100,") + (cfg.sub === "oi" ? ".25)" : ".6)");
        c.fillRect(X(i) - bw / 2, y0 + h - vh, bw, vh);
      }
      if (cfg.sub === "oi") {
        let omin = Infinity, omax = -Infinity;
        for (let i = start; i < n; i++) { const o = cfg.bars[i].oi; if (o != null) { omin = Math.min(omin, o); omax = Math.max(omax, o); } }
        c.beginPath(); c.strokeStyle = "#6fb0ff"; c.lineWidth = 1.5;
        for (let i = start; i < n; i++) { const o = cfg.bars[i].oi; if (o == null) continue; const y = y0 + h - 3 - ((o - omin) / (omax - omin || 1)) * (h - 8); i === start ? c.moveTo(X(i), y) : c.lineTo(X(i), y); }
        c.stroke(); c.fillStyle = "#6fb0ff"; c.textAlign = "left"; c.fillText("Open interest " + big(cfg.bars[n - 1].oi), padL + 6, y0 + 10);
      } else { c.fillStyle = "#7f8ba0"; c.textAlign = "left"; c.fillText("Volume", padL + 6, y0 + 10); }
    } else if (cfg.sub === "rsi") {
      const Yr = (v) => y0 + h - 3 - (v / 100) * (h - 6);
      [30, 70].forEach((lv) => { c.strokeStyle = "#2c3646"; c.setLineDash([3, 3]); c.beginPath(); c.moveTo(padL, Yr(lv)); c.lineTo(padL + plotW, Yr(lv)); c.stroke(); c.setLineDash([]); });
      const rl = (arr, col) => { c.beginPath(); c.strokeStyle = col; c.lineWidth = 1.4; let pen = false; for (let i = start; i < n; i++) { const v = arr[i]; if (v == null) { pen = false; continue; } pen ? c.lineTo(X(i), Yr(v)) : c.moveTo(X(i), Yr(v)); pen = true; } c.stroke(); };
      rl(sr.rsi14, "#6fb0ff"); rl(sr.rsi2, "#e9be5d");
      c.fillStyle = "#7f8ba0"; c.textAlign = "left"; c.fillText("RSI 14 (blue) · RSI 2 (amber)", padL + 6, y0 + 10);
    }
  }
  // hover
  cv.onmousemove = (ev) => {
    const r = cv.getBoundingClientRect(), x = ev.clientX - r.left;
    const i = Math.max(start, Math.min(n - 1, Math.floor((x - padL) / step) + start));
    const b = cfg.bars[i];
    const extra = [];
    if (sr.ema20 && sr.ema20[i] != null) extra.push("EMA20 " + num(sr.ema20[i]));
    if (sr.rsi2 && sr.rsi2[i] != null) extra.push("RSI2 " + Math.round(sr.rsi2[i]));
    if (b.oi != null) extra.push("OI " + big(b.oi));
    cfg.hud.textContent = `${cfg.fmtX(b)}  O ${num(b.open)}  H ${num(b.high)}  L ${num(b.low)}  C ${num(b.close)}${b.volume ? "  V " + big(b.volume) : ""}  ${extra.join("  ")}`;
  };
  cv.onmouseleave = () => { cfg.hud.textContent = ""; };
}

const ASK_PROMPTS = [
  "What should I buy today?",
  "What should I sell today?",
  "Which strategy actually passed the test?",
  "Is the market open?",
];
function setupAsk() {
  const box = $("#ask"), log = $("#ask-log");
  $("#ask-open").onclick = () => { box.classList.remove("hidden"); $("#ask-q").focus(); };
  $("#ask-close").onclick = () => box.classList.add("hidden");
  const suggest = document.createElement("div");
  suggest.className = "ask-suggest";
  suggest.innerHTML = ASK_PROMPTS.map((p) => `<button type="button">${esc(p)}</button>`).join("");
  log.appendChild(suggest);
  suggest.onclick = (ev) => { if (ev.target.tagName === "BUTTON") sendAsk(ev.target.textContent); };
  $("#ask-form").onsubmit = (ev) => { ev.preventDefault(); sendAsk($("#ask-q").value); };
}
function askBubble(role, text, chips) {
  const log = $("#ask-log");
  const el = document.createElement("div");
  el.className = "bubble " + role;
  el.textContent = text;
  log.appendChild(el);
  if (chips && chips.length) {
    const row = document.createElement("div");
    row.className = "ask-chips";
    row.innerHTML = chips.map((c) => `<button type="button" data-sym="${esc(c.symbol)}">${esc(c.label)}</button>`).join("");
    row.onclick = (ev) => { const b = ev.target.closest("button"); if (b) selectSymbol(b.dataset.sym); };
    log.appendChild(row);
  }
  log.scrollTop = log.scrollHeight;
}
async function sendAsk(raw) {
  const q = (raw || "").trim();
  if (!q || S.asking) return;
  const suggest = $(".ask-suggest");
  if (suggest) suggest.remove();
  $("#ask-q").value = "";
  $("#ask").classList.remove("hidden");
  askBubble("user", q);
  S.asking = true;
  askBubble("bot", "Looking at the scan…");
  const pending = $("#ask-log").lastElementChild;
  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ market: S.market, question: q, risk: getRisk() }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    pending.remove();
    askBubble("bot", data.text, data.chips);
  } catch (e) {
    pending.textContent = "Could not answer: " + e.message;
  } finally {
    S.asking = false;
  }
}

init();
