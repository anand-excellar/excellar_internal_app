import { XYChart, sparkline, fmtTime, fmtFullTime } from './charts.js';

/* ---------------------------------------------------------------- format -- */
const nf = (dp, opts = {}) => new Intl.NumberFormat('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp, ...opts });
const fin = (v) => Number.isFinite(v);

const money = (v, dp = 0) => (fin(v) ? `${v < 0 ? '−' : ''}$${nf(dp).format(Math.abs(v))}` : '—');
const moneySigned = (v, dp = 0) => (fin(v) ? `${v < 0 ? '−' : '+'}$${nf(dp).format(Math.abs(v))}` : '—');
const units = (v, dp = 0) => (fin(v) ? nf(dp).format(v) : '—');
const pct = (v, dp = 1) => (fin(v) ? `${nf(dp).format(v * 100)}%` : '—');
const pctSigned = (v, dp = 2) => (fin(v) ? `${v < 0 ? '−' : '+'}${nf(dp).format(Math.abs(v) * 100)}%` : '—');
const dec = (v, dp = 4) => (fin(v) ? nf(dp).format(v) : '—');

/**
 * Raw per-contract greeks span many orders of magnitude on a $0.12 underlying
 * (gamma ~ 17, vega ~ 1e-4). A fixed decimal count destroys the small ones, so hold
 * significant digits instead.
 */
const SUP = { '-': '⁻', 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹' };
const sig = (v, digits = 3) => {
  if (!fin(v)) return '—';
  if (v === 0) return '0';
  const mag = Math.floor(Math.log10(Math.abs(v)));
  // Everything below 1e-3 goes exponential so a column never mixes notations.
  if (mag < -3) {
    const [mant, exp] = v.toExponential(2).split('e');
    return `${mant}×10${[...exp.replace('+', '')].map((c) => SUP[c] ?? c).join('')}`;
  }
  const dp = Math.max(0, Math.min(8, digits - 1 - mag));
  return nf(dp).format(v);
};
const spotFmt = (v) => (fin(v) ? nf(6).format(v) : '—');
const fmtDate = (iso) =>
  iso ? new Date(iso).toLocaleDateString([], { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' }) : '—';
const fmtDateTime = (iso) =>
  iso ? `${fmtDate(iso)} ${new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })} UTC` : '—';
const signClass = (v) => (!fin(v) || Math.abs(v) < 1e-9 ? '' : v > 0 ? 'pos' : 'neg');

const STATUS_COLOR = { good: '--good', warning: '--warning', serious: '--serious', critical: '--critical', unknown: '--text-muted' };
const ICONS = {
  good: '<path d="M13.5 4.5 6.5 12 2.5 8.2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
  warning: '<path d="M8 1.8 15 14H1L8 1.8Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" fill="none"/><path d="M8 6.2v3.4M8 11.7v.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  serious: '<circle cx="8" cy="8" r="6.4" stroke="currentColor" stroke-width="1.8" fill="none"/><path d="M8 4.8v4M8 11.1v.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  critical: '<circle cx="8" cy="8" r="6.4" stroke="currentColor" stroke-width="1.8" fill="none"/><path d="M5.7 5.7l4.6 4.6M10.3 5.7l-4.6 4.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  unknown: '<circle cx="8" cy="8" r="6.4" stroke="currentColor" stroke-width="1.8" fill="none"/>',
};

const $ = (id) => document.getElementById(id);
const h = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content; };

/* ------------------------------------------------------- calculation hints --
 * Every ⓘ shows the formula AND the same formula with this poll's numbers
 * substituted, so a figure can always be traced back to its inputs without
 * leaving the page.
 * -------------------------------------------------------------------------- */
const CALCS = new Map();
let calcSeq = 0;

function hint(label, calc) {
  if (!calc) return '';
  const key = `c${++calcSeq}`;
  CALCS.set(key, calc);
  return `<button type="button" class="hint" data-calc="${key}" aria-label="Show the calculation behind ${label}">ⓘ</button>`;
}

function renderCalc(c) {
  return (
    `<div class="c-title">${c.title}</div>` +
    (c.desc ? `<div class="c-desc">${c.desc}</div>` : '') +
    (c.formula ? `<div class="c-formula">${c.formula}</div>` : '') +
    (c.steps || []).map((s) => `<div class="c-step">${s}</div>`).join('') +
    (c.result ? `<div class="c-result">${c.result}</div>` : '') +
    (c.note ? `<div class="c-note">${c.note}</div>` : '')
  );
}

function initCalcPopover() {
  const pop = $('calcPop');
  let anchor = null;

  const place = () => {
    if (!anchor) return;
    const r = anchor.getBoundingClientRect();
    const pw = pop.offsetWidth;
    const ph = pop.offsetHeight;
    const vw = document.documentElement.clientWidth;
    let left = r.left + window.scrollX + r.width / 2 - pw / 2;
    left = Math.max(window.scrollX + 10, Math.min(left, window.scrollX + vw - pw - 10));
    // Flip above the anchor when there isn't room below.
    const below = r.bottom + ph + 14 <= window.innerHeight;
    pop.style.left = `${left}px`;
    pop.style.top = `${below ? r.bottom + window.scrollY + 8 : r.top + window.scrollY - ph - 8}px`;
  };

  const show = (btn) => {
    const c = CALCS.get(btn.dataset.calc);
    if (!c) return;
    anchor = btn;
    pop.innerHTML = renderCalc(c);
    pop.classList.add('on');
    pop.setAttribute('aria-hidden', 'false');
    place();
  };
  const hide = () => {
    anchor = null;
    pop.classList.remove('on');
    pop.setAttribute('aria-hidden', 'true');
  };

  // Hover and keyboard focus behave identically — the value is never hover-gated.
  document.addEventListener('pointerover', (e) => {
    const btn = e.target.closest('.hint[data-calc]');
    if (btn) show(btn);
    else if (anchor && !e.target.closest('.calcpop')) hide();
  });
  document.addEventListener('focusin', (e) => {
    const btn = e.target.closest('.hint[data-calc]');
    if (btn) show(btn);
    else if (anchor) hide();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', () => anchor && place(), { passive: true });
  window.addEventListener('resize', hide);
}

/* ------------------------------------------------------------------ state -- */
const state = { snap: null, history: [], rules: [], config: null, range: '24h', units: 'both', tables: false, mode: '…', lastPollAt: null, lastError: null };
const charts = new Map();

/* ------------------------------------------------------------------ fetch -- */
async function load({ range = state.range } = {}) {
  const res = await fetch(`/api/state?range=${range}`);
  const s = await res.json();
  state.snap = s.snapshot;
  state.history = s.history || [];
  state.rules = s.rules || [];
  state.config = s.config;
  state.mode = s.mode;
  state.lastPollAt = s.lastPollAt;
  state.lastError = s.lastError;
  state.pollSeconds = s.pollSeconds;
  renderAll();
}

/* ------------------------------------------------------------------- hero -- */
let heroAnim = null;
function tweenHero(node, to) {
  const had = node.dataset.v !== undefined;
  const from = Number(node.dataset.v ?? to);
  cancelAnimationFrame(heroAnim);

  if (!fin(to)) { node.dataset.v = ''; node.firstChild.nodeValue = '—'; return; }
  node.dataset.v = String(to);

  const settle = () => { node.firstChild.nodeValue = nf(2).format(to * 100); };
  // Never animate the first paint, and never leave a throttled rAF holding a
  // half-counted figure on screen — the true value is written first, then
  // tweened over it only when the browser is actually animating.
  settle();
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!had || reduce || Math.abs(to - from) < 1e-9) return;

  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 520);
    const e = 1 - Math.pow(1 - k, 3);
    node.firstChild.nodeValue = nf(2).format((from + (to - from) * e) * 100);
    if (k < 1) heroAnim = requestAnimationFrame(step);
    else settle();
  };
  heroAnim = requestAnimationFrame(step);
}

function renderHero() {
  const dm = state.snap.deltaMonitor;
  const c = state.snap.coverage;
  const accent = STATUS_COLOR[dm.status] || '--good';
  $('hero').style.setProperty('--hero-accent', `var(${accent})`);

  $('heroLabel').innerHTML =
    'Portfolio blended delta' +
    hint('portfolio blended delta', {
      title: 'Portfolio blended delta',
      desc: 'Net delta re-expressed in CC units, as a fraction of every CC you hold. This is the single number the rebalancing policy is written against.',
      formula: 'blended = (Delta $ ÷ spot) ÷ total CC held',
      steps: [
        `Delta $    = ${money(dm.totalDeltaUsd, 2)}`,
        `S          = ${spotFmt(state.snap.spot)}`,
        `net delta  = ${money(dm.totalDeltaUsd, 2)} ÷ ${spotFmt(state.snap.spot)}  =  ${units(dm.netDeltaCcUnits, 0)} CC`,
        `CC held    = ${units(c.marginPostedUnits, 0)} + ${units(c.custodyUnits, 0)} + ${units(c.pendingUnits, 0)}  =  ${units(dm.totalCcHeld, 0)} CC`,
        `           = ${units(dm.netDeltaCcUnits, 0)} ÷ ${units(dm.totalCcHeld, 0)}`,
      ],
      result: `blended = ${pct(dm.blendedDelta, 2)}`,
      note: `Hard floor ${pct(dm.hardFloor, 0)} · warning below ${pct(dm.hardFloor + dm.warningBuffer, 0)} · target ${pct(dm.target, 0)} · upper band ${pct(dm.upperBand, 0)}.`,
    });

  const val = $('heroValue');
  if (!val.firstChild || val.firstChild.nodeType !== 3) val.prepend(document.createTextNode('—'));
  tweenHero(val, dm.blendedDelta);

  $('heroSub').innerHTML =
    `${units(dm.netDeltaCcUnits, 0)} net delta ÷ ${units(dm.totalCcHeld, 0)} CC held &nbsp;·&nbsp; ` +
    `<b style="color:var(--text-primary)">${pctSigned(dm.distanceToFloor)}</b> vs hard floor`;

  $('statusIcon').innerHTML = ICONS[dm.status] || ICONS.unknown;
  $('statusText').textContent = dm.statusText;

  // Meter spans 50%→100% of blended delta.
  const toPct = (v) => `${Math.max(0, Math.min(100, ((v - 0.5) / 0.5) * 100))}%`;
  $('meterFill').style.width = toPct(dm.blendedDelta ?? 0.5);
  const floor = $('markFloor');
  floor.style.left = toPct(dm.hardFloor);
  floor.style.background = 'var(--critical)';
  floor.dataset.label = `floor ${pct(dm.hardFloor, 0)}`;
  const tgt = $('markTarget');
  tgt.style.left = toPct(dm.target);
  tgt.style.background = 'var(--text-muted)';
  tgt.dataset.label = `target ${pct(dm.target, 0)}`;

  const r = dm.rebalance;
  const rows = [];
  if (fin(r.addSpotCcUnits)) {
    const n = r.addSpotCcUnits;
    rows.push(n >= 0
      ? `<div class="rebal-line">Transfer in <b>${units(n, 0)} CC</b> spot to reach ${pct(dm.target, 0)}</div>`
      : `<div class="rebal-line">Room to deploy <b>${units(-n, 0)} CC</b> before dropping to ${pct(dm.target, 0)}</div>`);
  }
  if (fin(r.buyBackShortContracts)) {
    const n = r.buyBackShortContracts;
    rows.push(n >= 0
      ? `<div class="rebal-line">…or buy back <b>${units(n, 0)}</b> short calls</div>`
      : `<div class="rebal-line">…or write <b>${units(-n, 0)}</b> more calls at the same strike</div>`);
  }
  rows.push(`<div class="rebal-line" style="color:var(--text-muted)">Unencumbered CC available to write: <b>${units(state.snap.coverage.availableToWrite, 0)}</b></div>`);
  $('rebal').innerHTML = rows.join('');
}

/* ------------------------------------------------------------------ tiles -- */
function seriesFrom(key) {
  return state.history.map((p) => p[key]).filter(fin);
}

function tile({ label, calc, value, unit, delta, deltaGood, spark, color = '--series-1' }) {
  const node = h(`
    <div class="tile">
      <div class="t-label">${label}${hint(label, calc)}</div>
      <div class="t-value">${value}${unit ? `<span class="unit">${unit}</span>` : ''}</div>
      <div class="t-foot"><span class="t-delta"></span><span class="spark"></span></div>
    </div>`).firstElementChild;
  if (delta) {
    const d = node.querySelector('.t-delta');
    d.textContent = delta.text;
    if (deltaGood != null) d.classList.add(deltaGood ? 'up' : 'down');
  }
  if (spark && spark.length > 2) sparkline(node.querySelector('.spark'), spark.slice(-40), color);
  return node;
}

function change(key) {
  const s = seriesFrom(key);
  if (s.length < 2) return null;
  return { abs: s[s.length - 1] - s[0], rel: s[0] ? (s[s.length - 1] - s[0]) / Math.abs(s[0]) : null };
}

/** Sum of per-leg money greeks, written out term by term. */
const legTerms = (legs, key, dp) =>
  legs.map((l) => `${l.side === 'SHORT' ? '−' : '+'}${nf(dp).format(Math.abs(l.usd[key]))}`).join('  ');

function renderTiles() {
  const s = state.snap;
  const p = s.aggregation.portfolio;
  const c = s.coverage;
  const r = s.roll;
  const S = s.spot;
  const spotChg = change('spot');
  const box = $('tiles');
  box.replaceChildren();

  const optDelta = s.optionsUsd.delta;

  box.append(
    tile({
      label: `Spot ${s.baseCurrency}/${s.quoteCurrency}`,
      calc: {
        title: `Spot ${s.baseCurrency}/${s.quoteCurrency}`,
        desc: 'Reference spot as reported by STS on the CC option legs. Every dollar figure downstream is derived from this one number.',
        formula: 'S = spotPrice from GET /v1alpha/positions',
        steps: spotChg
          ? [
              `S           = ${spotFmt(S)}`,
              `range open  = ${spotFmt(S - spotChg.abs)}`,
              `change      = (${spotFmt(S)} − ${spotFmt(S - spotChg.abs)}) ÷ ${spotFmt(S - spotChg.abs)}`,
            ]
          : [`S = ${spotFmt(S)}`],
        result: spotChg ? `${spotFmt(S)}   (${pctSigned(spotChg.rel)} over range)` : `S = ${spotFmt(S)}`,
      },
      value: spotFmt(S),
      delta: spotChg ? { text: `${pctSigned(spotChg.rel)} over range` } : null,
      deltaGood: spotChg ? spotChg.abs >= 0 : null,
      spark: seriesFrom('spot'),
    }),
    tile({
      label: 'Portfolio Delta $',
      calc: {
        title: 'Portfolio dollar delta',
        desc: 'Options book plus every unit of CC you hold, plus premium not yet converted. This is the numerator of the blended-delta monitor.',
        formula: 'Delta $ = sum of leg Delta $  +  sum of (CC units × spot)  +  unconverted USDT',
        steps: [
          `options book  = ${legTerms(s.legs, 'delta', 1)}  =  ${moneySigned(optDelta, 1)}`,
          `margin at STS = ${units(c.marginPostedUnits, 0)} × ${spotFmt(S)}  =  ${money(c.marginPostedUnits * S, 2)}`,
          `custody CC    = ${units(c.custodyUnits, 0)} × ${spotFmt(S)}  =  ${money(c.custodyUnits * S, 2)}`,
          `premium       = ${money(c.unconvertedQuote, 2)}  (already USD)`,
        ],
        result: `Delta $ = ${money(p.delta, 2)}`,
      },
      value: money(p.delta, 0),
      delta: change('delta') ? { text: `${moneySigned(change('delta').abs, 0)} over range` } : null,
      deltaGood: change('delta') ? change('delta').abs >= 0 : null,
      spark: seriesFrom('delta'),
    }),
    tile({
      label: 'Portfolio Gamma $',
      calc: {
        title: 'Portfolio dollar gamma',
        desc: 'How much Delta $ changes for a 1% move in spot. Physical CC is linear, so only the options contribute.',
        formula: 'Gamma $ = sum of leg Gamma $      (spot CC has zero gamma)',
        steps: [
          `= ${legTerms(s.legs, 'gamma', 2)}`,
          `per-contract Gamma recovered as  Gamma = Gamma $ ÷ (qty × 0.01 × spot²)`,
        ],
        result: `Gamma $ = ${moneySigned(p.gamma, 2)} per 1% spot move`,
        note: `A 1% spot move (${spotFmt(S)} → ${spotFmt(S * 1.01)}) shifts Delta $ from ${money(p.delta, 0)} to about ${money(p.delta + p.gamma, 0)}.`,
      },
      value: money(p.gamma, 1),
      delta: { text: 'per 1% spot move' },
      spark: seriesFrom('gamma'),
      color: '--series-2',
    }),
    tile({
      label: 'Portfolio Vega $',
      calc: {
        title: 'Portfolio dollar vega',
        desc: 'P&L for a 1 volatility-point move (e.g. 52% → 53%) across the whole surface.',
        formula: 'Vega $ = sum of leg Vega $',
        steps: [`= ${legTerms(s.legs, 'vega', 3)}`, `per-contract Vega recovered as  Vega = Vega $ ÷ qty`],
        result: `Vega $ = ${moneySigned(p.vega, 3)} per vol point`,
        note: 'Net short vega — a volatility spike costs money, a crush pays.',
      },
      value: money(p.vega, 2),
      delta: { text: 'per 1 vol point' },
      spark: seriesFrom('vega'),
      color: '--series-3',
    }),
    tile({
      label: 'Portfolio Theta $',
      calc: {
        title: 'Portfolio dollar theta',
        desc: 'Time decay per calendar day, holding spot and vol still.',
        formula: 'Theta $ = sum of leg Theta $',
        steps: [`= ${legTerms(s.legs, 'theta', 3)}`, `per-contract Theta recovered as  Theta = Theta $ ÷ qty`],
        result: `Theta $ = ${moneySigned(p.theta, 3)} per day`,
        note: fin(r.daysToRoll) && r.daysToRoll > 0
          ? `≈ ${money(p.theta * r.daysToRoll, 2)} collected between now and the roll date if nothing moves.`
          : null,
      },
      value: money(p.theta, 2),
      delta: { text: 'per day' },
      deltaGood: p.theta >= 0,
      spark: seriesFrom('theta'),
      color: '--series-2',
    }),
    tile((() => {
      const u = s.pnl.underlying || NO_UNDERLYING_PNL;
      const hasUnderlying = fin(u.referenceSpot) && fin(u.total);
      const grandTotal = hasUnderlying ? s.pnl.portfolioTotal : s.pnl.total;
      return {
        label: 'Total P&L',
        calc: {
          title: 'Total P&L',
          desc: hasUnderlying
            ? 'Premium banked on the fills, plus mark-to-market on the open legs, plus P&L on the CC you hold. Full breakdown in the Profit & loss card.'
            : 'Premium already banked on the fills, plus mark-to-market on the open legs — underlying P&L is excluded until a reference spot is set in Settings. Full breakdown in the Profit & loss card.',
          formula: hasUnderlying ? 'total = premium collected + MtM + underlying P&L' : 'total = premium collected + MtM',
          steps: hasUnderlying
            ? [
                `premium collected = ${moneySigned(s.pnl.realized, 2)}   (cash exchanged)`,
                `MtM               = ${moneySigned(s.pnl.unrealized, 2)}   (mark to market)`,
                `underlying P&L    = ${moneySigned(u.total, 2)}   (vs entry spot ${spotFmt(u.referenceSpot)})`,
              ]
            : [
                `premium collected = ${moneySigned(s.pnl.realized, 2)}   (cash exchanged)`,
                `MtM               = ${moneySigned(s.pnl.unrealized, 2)}   (mark to market)`,
              ],
          result: `total = ${moneySigned(grandTotal, 2)}`,
          note: s.spread.marksInverted
            ? 'Marks are crossed, so the MtM half is indicative; premium collected is unaffected.'
            : hasUnderlying
              ? null
              : `Options total converges on the ${money(s.pnl.realized, 2)} premium collected if held to expiry below ${dec(s.spread.shortStrike, 3)}. Set a reference spot in Settings to fold underlying P&L in here.`,
        },
        value: moneySigned(grandTotal, 2),
        delta: {
          text: hasUnderlying
            ? `${moneySigned(s.pnl.realized, 2)} premium · ${moneySigned(s.pnl.unrealized, 2)} MtM · ${moneySigned(u.total, 2)} underlying`
            : `${moneySigned(s.pnl.realized, 2)} premium · ${moneySigned(s.pnl.unrealized, 2)} MtM`,
        },
        deltaGood: grandTotal >= 0,
        spark: seriesFrom(hasUnderlying ? 'portfolioPnl' : 'totalPnl').length > 2
          ? seriesFrom(hasUnderlying ? 'portfolioPnl' : 'totalPnl')
          : seriesFrom('pnl'),
      };
    })()),
    tile({
      label: 'Available CC to write',
      calc: {
        title: 'CC available to write new calls',
        desc: 'Unencumbered CC after reserving the full short-leg notional 1:1 — our own stipulation, stricter than STS margin.',
        formula: 'available = total CC held − CC reserved',
        steps: [
          `pending   = ${money(c.unconvertedQuote, 2)} ÷ ${spotFmt(S)}  =  ${units(c.pendingUnits, 0)} CC`,
          `held      = ${units(c.marginPostedUnits, 0)} margin + ${units(c.custodyUnits, 0)} custody + ${units(c.pendingUnits, 0)} pending  =  ${units(c.totalCcHeld, 0)}`,
          `reserved  = |${units(-c.ccReserved, 0)}| × ${nf(2).format(state.config.policy.coverageRatio)}  =  ${units(c.ccReserved, 0)}`,
        ],
        result: `available = ${units(c.availableToWrite, 0)} CC`,
      },
      value: units(c.availableToWrite, 0),
      unit: s.baseCurrency,
      delta: { text: `${units(c.ccReserved, 0)} reserved of ${units(c.totalCcHeld, 0)}` },
      deltaGood: c.availableToWrite >= 0,
      spark: seriesFrom('available'),
      color: '--series-3',
    }),
    tile({
      label: 'Days to roll',
      calc: {
        title: 'Days until the roll date',
        desc: `Policy rolls ${r.rollDaysBeforeExpiry} days before expiry, so the roll date — not the expiry — is the date this book is managed to.`,
        formula: 'days to roll = days to expiry − roll lead',
        steps: [
          `expiry       = ${r.expiry ? fmtDate(r.expiry) : '—'}`,
          `roll date    = expiry − ${r.rollDaysBeforeExpiry}d  =  ${r.rollDate ? fmtDate(r.rollDate) : '—'}`,
          `days to expiry = ${fin(r.daysToExpiry) ? nf(2).format(r.daysToExpiry) : '—'}`,
          `             = ${fin(r.daysToExpiry) ? nf(2).format(r.daysToExpiry) : '—'} − ${r.rollDaysBeforeExpiry}`,
        ],
        result: fin(r.daysToRoll)
          ? r.daysToRoll > 0
            ? `${nf(1).format(r.daysToRoll)} d to roll`
            : `${nf(1).format(Math.abs(r.daysToRoll))} d overdue`
          : '—',
        note: r.statusText,
      },
      value: fin(r.daysToRoll) ? nf(1).format(Math.abs(r.daysToRoll)) : '—',
      unit: fin(r.daysToRoll) && r.daysToRoll < 0 ? 'd over' : 'd',
      delta: { text: `${fin(r.daysToExpiry) ? nf(1).format(r.daysToExpiry) : '—'}d to expiry · roll ${r.rollDaysBeforeExpiry}d out` },
      deltaGood: r.status === 'good',
      spark: seriesFrom('spot'),
      color: '--series-2',
    }),
  );
}

/* --------------------------------------------------------------- coverage -- */
function kv(rows) {
  return rows
    .map((r) => (r === '-' ? '<div class="rule"></div>' : `<dt>${r[0]}</dt><dd class="${r[2] || ''}">${r[1]}</dd>`))
    .join('');
}

function renderCoverage() {
  const c = state.snap.coverage;
  const cfg = state.config;
  $('coverageKv').innerHTML = kv([
    ['Margin posted at STS', `${units(c.marginPostedUnits, 0)} CC`],
    [`Undeployed — ${cfg.externalHoldings.venue}`, `${units(c.custodyUnits, 0)} CC`],
    [`Pending conversion (${money(c.unconvertedQuote, 2)} unconverted premium)`, `${units(c.pendingUnits, 0)} CC`],
    '-',
    ['<b>Total CC held</b>', `<b>${units(c.totalCcHeld, 0)} CC</b>`],
    ['Value at spot', money(c.totalCcHeld * state.snap.spot, 0)],
  ]);

  const sp = state.snap.spread;
  const shortNotional = c.ccReserved * state.snap.spot;
  const maxLossBasis = fin(sp.width) ? sp.width * sp.contracts : null;

  $('coverKv').innerHTML = kv([
    [`CC reserved for written calls (${nf(2).format(cfg.policy.coverageRatio)}× notional)`, `${units(c.ccReserved, 0)} CC`],
    ['Total CC held', `${units(c.totalCcHeld, 0)} CC`],
    '-',
    ['<b>Available to write new calls</b>', `<b>${units(c.availableToWrite, 0)} CC</b>`, signClass(c.availableToWrite)],
    ['Coverage ratio', fin(c.coverageRatio) ? `${nf(3).format(c.coverageRatio)}×` : '—', c.covered ? 'pos' : 'neg'],
    '-',
    ['Reserved notional at spot', money(shortNotional, 0)],
    ['Spread max loss (STS margin basis)', money(maxLossBasis, 0)],
    [
      'Over-collateralisation vs max loss',
      fin(maxLossBasis) && maxLossBasis > 0 ? `${nf(1).format(shortNotional / maxLossBasis)}×` : '—',
      'accent',
    ],
  ]);

  // Single ratio against a limit → a meter, same-ramp track.
  $('coverMeter').innerHTML = `
    <div class="cov-bar" role="img" aria-label="${units(c.ccReserved, 0)} CC reserved, ${units(c.availableToWrite, 0)} CC free of ${units(c.totalCcHeld, 0)} held">
      <span class="cov-used" style="width:${Math.max(0, Math.min(100, (c.ccReserved / (c.totalCcHeld || 1)) * 100))}%"></span>
    </div>
    <div class="cov-legend">
      <span><i class="key" style="background:var(--series-1)"></i>${units(c.ccReserved, 0)} reserved</span>
      <span><i class="key" style="background:color-mix(in srgb, var(--series-1) 22%, var(--surface-2))"></i>${units(c.availableToWrite, 0)} free</span>
    </div>`;

  const chip = $('coverChip');
  chip.textContent = c.covered ? 'FULLY COVERED' : 'UNDER-COVERED';
  chip.className = `chip ${c.covered ? 'live' : 'err'}`;

  $('coverNote').innerHTML = c.covered
    ? ''
    : `<div class="note"><b>Short notional exceeds CC held.</b> ${units(-c.availableToWrite, 0)} CC short of the 1:1 stipulation — transfer in or buy back calls.</div>`;
}

function renderSpread() {
  const sp = state.snap.spread;
  $('spreadKv').innerHTML = kv([
    ['Structure', `${units(sp.contracts, 0)}× ${dec(sp.shortStrike, 3)} / ${dec(sp.longStrike, 3)} call spread`],
    ['Width', dec(sp.width, 3)],
    ['Net credit received', money(sp.netCredit, 2)],
    ['Credit per contract', dec(sp.creditPerContract, 5)],
    '-',
    ['Breakeven (spread)', dec(sp.breakeven, 5)],
    ['Max profit at expiry', money(sp.maxProfit, 2), 'pos'],
    ['Max loss at expiry', money(sp.maxLoss, 2), 'neg'],
    '-',
    ['Net option mark value', moneySigned(sp.netMarkValue, 2)],
    ['Total P&L (premium + MtM)', moneySigned(sp.totalPnl, 2), signClass(sp.totalPnl)],
  ]);

  $('spreadNote').innerHTML = sp.marksInverted
    ? `<div class="note"><b>STS marks look crossed.</b> The ${dec(sp.longStrike, 3)} leg is marking above the ${dec(sp.shortStrike, 3)} leg — impossible for a call spread. Treat mark-derived P&L as unreliable until the surface refreshes; the greeks themselves are self-consistent.</div>`
    : '';
}

/* -------------------------------------------------------------------- P&L -- */
const NO_UNDERLYING_PNL = { referenceSpot: null, units: null, spotChangePnl: null, acquisitionCost: 0, total: null };

function renderPnl() {
  const p = state.snap.pnl;
  // A snapshot cached before this field existed (stale latest.json) must not
  // crash the page — it just renders as "not configured" until the next poll.
  const u = p.underlying || NO_UNDERLYING_PNL;
  const sp = state.snap.spread;
  const fromTrades = p.realizedSource === 'trades';
  const hasUnderlying = fin(u.referenceSpot) && fin(u.total);
  const grandTotal = hasUnderlying ? p.portfolioTotal : p.total;

  const chip = $('pnlSourceChip');
  chip.textContent = fromTrades ? `${p.tradeCount} fills from /v1alpha/trades` : 'premium inferred from USDT balance';
  chip.className = `chip ${fromTrades ? 'live' : 'demo'}`;

  const figs = [
    {
      label: 'Premium collected',
      sub: 'cash already exchanged on the fills',
      value: p.realized,
      calc: {
        title: 'Premium collected',
        desc: 'Net premium that has actually changed hands: what you took in selling, less what you paid buying. It does not move unless a fill happens.',
        formula: 'premium collected = premium received on sells − premium paid on buys',
        steps: p.legs
          .filter((l) => fin(l.realized))
          .map((l) => `${l.side === 'SHORT' ? 'sold' : 'bought'} ${units(Math.abs(l.quantity), 0)} @ ${dec(l.entryPrice, 6)}  =  ${moneySigned(l.realized, 2)}`),
        result: `premium collected = ${moneySigned(p.realized, 2)}`,
        note: fromTrades
          ? 'Sourced from actual fills, so it stays correct after you convert premium to CC.'
          : 'No fills returned by the API — inferred from the unconverted USDT balance, which is only exact while none of the premium has been converted.',
      },
    },
    {
      label: 'MtM',
      sub: 'mark-to-market on the open legs',
      value: p.unrealized,
      calc: {
        title: 'MtM — mark to market',
        desc: "What the open legs are worth right now at STS's marks. This is the only part that moves with spot and vol.",
        formula: 'MtM = sum of current market value across open legs',
        steps: p.legs
          .filter((l) => l.open)
          .map((l) => `${l.side === 'SHORT' ? 'short' : 'long '} ${dec(l.strike, 3)}  ${units(Math.abs(l.quantity), 0)} @ ${dec(Math.abs(l.markPerContract), 6)}  =  ${moneySigned(l.unrealized, 2)}`),
        result: `MtM = ${moneySigned(p.unrealized, 2)}`,
        note: sp.marksInverted
          ? 'STS marks are crossed right now, so this figure is indicative — premium collected above is unaffected.'
          : null,
      },
    },
    {
      label: 'Underlying P&L',
      sub: hasUnderlying
        ? `${units(u.units, 0)} CC vs entry spot ${spotFmt(u.referenceSpot)}, net of acquisition cost`
        : 'set a reference spot in Settings to include this',
      value: u.total,
      calc: {
        title: 'Underlying P&L',
        desc: "P&L on the CC itself — the price move since the covered call book was opened, less what it cost to acquire that CC off-STS. Neither is visible to the STS API, so both come from the Settings panel.",
        formula: 'underlying P&L = (spot − reference spot) × CC units − acquisition cost',
        steps: hasUnderlying
          ? [
              `(spot − reference) = (${spotFmt(state.snap.spot)} − ${spotFmt(u.referenceSpot)})`,
              `price move  = ${moneySigned(u.spotChangePnl, 2)}  (${units(u.units, 0)} CC)`,
              `acquisition cost = ${money(u.acquisitionCost, 2)}`,
            ]
          : ['no reference spot set'],
        result: hasUnderlying ? `underlying P&L = ${moneySigned(u.total, 2)}` : '—',
        note: hasUnderlying
          ? null
          : 'Reference spot should be the spot price when this covered call book was opened — Settings → "Reference spot".',
      },
    },
  ];

  $('pnlFigs').innerHTML =
    figs
      .map(
        (f) => `
      <div class="pnl-fig">
        <div class="pf-label">${f.label}${hint(f.label, f.calc)}</div>
        <div class="pf-value ${signClass(f.value)}">${moneySigned(f.value, 2)}</div>
        <div class="pf-sub">${f.sub}</div>
      </div>`,
      )
      .join('') +
    `<div class="pnl-fig total">
       <div class="pf-label">Portfolio P&L${hint('portfolio P&L', {
         title: 'Portfolio P&L',
         desc: hasUnderlying
           ? 'Options book P&L plus the underlying P&L above.'
           : 'Options book P&L only — underlying P&L is excluded until a reference spot is set in Settings.',
         formula: hasUnderlying ? 'total = premium collected + MtM + underlying P&L' : 'total = premium collected + MtM',
         steps: hasUnderlying
           ? [
               `premium collected = ${moneySigned(p.realized, 2)}`,
               `MtM               = ${moneySigned(p.unrealized, 2)}`,
               `underlying P&L    = ${moneySigned(u.total, 2)}`,
             ]
           : [
               `premium collected = ${moneySigned(p.realized, 2)}`,
               `MtM               = ${moneySigned(p.unrealized, 2)}`,
             ],
         result: `total = ${moneySigned(grandTotal, 2)}`,
         note: hasUnderlying
           ? null
           : `Held to expiry below ${dec(sp.shortStrike, 3)}, MtM decays to zero and the options total converges on the ${money(p.realized, 2)} premium collected.`,
       })}</div>
       <div class="pf-value ${signClass(grandTotal)}">${moneySigned(grandTotal, 2)}</div>
     </div>`;

  $('pnlTable').innerHTML = `
    <thead><tr><th>Leg</th><th>Qty</th><th>Entry</th><th>Mark</th><th>Premium collected</th><th>MtM</th><th>Total</th></tr></thead>
    <tbody>
      ${state.snap.pnl.legs
        .map((l) => `
          <tr>
            <td><span class="leg-name"><span class="side-tag ${l.side}">${l.side}</span><span><div>${l.label}</div>${l.open ? '' : '<div class="code">closed</div>'}</span></span></td>
            <td class="${signClass(l.quantity)}">${units(l.quantity, 0)}</td>
            <td>${dec(l.entryPrice, 6)}</td>
            <td>${l.markPerContract == null ? '—' : dec(Math.abs(l.markPerContract), 6)}</td>
            <td class="${signClass(l.realized)}">${moneySigned(l.realized, 2)}</td>
            <td class="${signClass(l.unrealized)}">${moneySigned(l.unrealized, 2)}</td>
            <td class="${signClass(l.total)}">${moneySigned(l.total, 2)}</td>
          </tr>`)
        .join('')}
      <tr class="total">
        <td>Options book</td>
        <td colspan="3"></td>
        <td class="${signClass(p.realized)}">${moneySigned(p.realized, 2)}</td>
        <td class="${signClass(p.unrealized)}">${moneySigned(p.unrealized, 2)}</td>
        <td class="${signClass(p.total)}">${moneySigned(p.total, 2)}</td>
      </tr>
    </tbody>`;

  $('pnlNote').innerHTML = hasUnderlying
    ? `<div class="note" style="background:color-mix(in srgb, var(--series-1) 8%, var(--surface-1)); border-color:color-mix(in srgb, var(--series-1) 30%, transparent)">
        The <b>Options book</b> row above is the STS-visible half only. <b>Portfolio P&L</b> (top of this card) adds the
        ${units(u.units, 0)} CC you hold, valued off the reference spot ${spotFmt(u.referenceSpot)} and net of the
        ${money(u.acquisitionCost, 2)} acquisition cost set in Settings.
      </div>`
    : `<div class="note" style="background:color-mix(in srgb, var(--series-1) 8%, var(--surface-1)); border-color:color-mix(in srgb, var(--series-1) 30%, transparent)">
        <b>Options book only.</b> This excludes P&L on the ${units(state.snap.coverage.totalCcHeld, 0)} CC you hold —
        set a reference spot (and, if any, an acquisition cost) in Settings to include it. Premium collected is capped at the
        ${money(p.realized, 2)} credit; MtM carries the remaining options risk until expiry or roll.
      </div>`;
}

/* ------------------------------------------------------------------- roll -- */
function renderRoll() {
  const r = state.snap.roll;
  const sp = state.snap.spread;
  const accent = STATUS_COLOR[r.status] || '--good';
  $('rollCard').style.setProperty('--roll-accent', `var(${accent})`);

  const chip = $('rollChip');
  chip.textContent = r.status === 'good' ? 'ON SCHEDULE' : r.status === 'warning' ? 'ROLL APPROACHING' : r.status === 'unknown' ? 'NO EXPIRY' : 'ROLL DUE';
  chip.className = `chip ${r.status === 'good' ? 'live' : r.status === 'warning' ? 'demo' : 'err'}`;

  const overdue = fin(r.daysToRoll) && r.daysToRoll < 0;
  $('rollFigure').innerHTML = fin(r.daysToRoll)
    ? `${nf(1).format(Math.abs(r.daysToRoll))}<span class="unit">${overdue ? 'd overdue' : 'd to roll'}</span>`
    : '—';
  $('rollFigureSub').textContent = r.statusText;

  // Timeline runs today → expiry, with the roll date marked inside it.
  const frac = Math.max(0, Math.min(1, r.rollFraction));
  $('rtFill').style.width = `${frac * 100}%`;
  const mark = $('rtMark');
  mark.style.left = `${frac * 100}%`;
  mark.dataset.label = `roll by ${r.rollDate ? fmtDate(r.rollDate) : '—'}`;
  $('rtStart').textContent = `today · ${fmtDate(new Date().toISOString())}`;
  $('rtEnd').textContent = `expiry · ${r.expiry ? fmtDate(r.expiry) : '—'}`;

  $('rollKv').innerHTML = kv([
    [`Roll policy${hint('roll policy', {
      title: 'Roll policy',
      desc: 'The book is managed to the roll date, not to expiry — the position should never be held into the final two weeks.',
      formula: 'roll date = expiry − roll lead',
      steps: [
        `expiry     = ${fmtDateTime(r.expiry)}`,
        `roll lead  = ${r.rollDaysBeforeExpiry} days`,
        `roll date  = ${fmtDateTime(r.rollDate)}`,
      ],
      result: fin(r.daysToRoll)
        ? r.daysToRoll > 0 ? `${nf(2).format(r.daysToRoll)} d remaining` : `${nf(2).format(Math.abs(r.daysToRoll))} d overdue`
        : '—',
      note: `Warns ${r.approachDays}d before the roll date; escalates once the roll date passes.`,
    })}`, `${r.rollDaysBeforeExpiry}d before expiry`],
    ['Roll by', fmtDate(r.rollDate)],
    ['Expiry', fmtDate(r.expiry)],
    '-',
    ['Days to roll', fin(r.daysToRoll) ? `${nf(2).format(r.daysToRoll)} d` : '—', r.daysToRoll > 0 ? '' : 'neg'],
    ['Days to expiry', fin(r.daysToExpiry) ? `${nf(2).format(r.daysToExpiry)} d` : '—'],
    '-',
    [
      `Total P&L if closed now${hint('total P&L if closed now', {
        title: 'Total P&L if you closed today',
        desc: 'Unwinding both legs at the current marks turns MtM into cash, on top of the premium already banked. Same number as Total P&L while the marks hold.',
        formula: 'total = close proceeds + premium collected',
        steps: [
          ...r.legsToClose.map((l) => `${l.action.padEnd(14)} ${units(l.quantity, 0)} × ${dec(l.markPerContract, 5)}  =  ${moneySigned(l.cashflow, 2)}`),
          `close proceeds    = ${moneySigned(r.closeProceeds, 2)}`,
          `premium collected = ${moneySigned(sp.netCredit, 2)}`,
        ],
        result: `total = ${moneySigned(r.realisedIfClosedNow, 2)}`,
        note: r.marksReliable ? null : 'Marks are crossed right now — treat this number as indicative only.',
      })}`,
      moneySigned(r.realisedIfClosedNow, 2),
      signClass(r.realisedIfClosedNow),
    ],
  ]);

  $('rollTable').innerHTML = `
    <thead><tr><th>Action</th><th>Leg</th><th>Qty</th><th>Mark</th><th>Cash flow</th></tr></thead>
    <tbody>
      ${r.legsToClose
        .map((l) => `
          <tr>
            <td><span class="act-tag ${l.action.startsWith('BUY') ? 'buy' : 'sell'}">${l.action}</span></td>
            <td>${l.label}</td>
            <td>${units(l.quantity, 0)}</td>
            <td>${dec(l.markPerContract, 5)}</td>
            <td class="${signClass(l.cashflow)}">${moneySigned(l.cashflow, 2)}</td>
          </tr>`)
        .join('')}
      <tr class="total">
        <td colspan="4">Net proceeds from closing the spread</td>
        <td class="${signClass(r.closeProceeds)}">${moneySigned(r.closeProceeds, 2)}</td>
      </tr>
    </tbody>`;

  $('rollNote').innerHTML = r.marksReliable
    ? `<div class="note" style="background:color-mix(in srgb, var(--series-1) 8%, var(--surface-1)); border-color:color-mix(in srgb, var(--series-1) 30%, transparent)">
         <b>Rolling</b> means closing both legs above and reopening the same ${units(sp.contracts, 0)}× ${dec(sp.shortStrike, 3)}/${dec(sp.longStrike, 3)} structure in the next cycle.
         The ${units(state.snap.coverage.ccReserved, 0)} CC stays reserved throughout — coverage is unaffected by the roll itself.
       </div>`
    : `<div class="note"><b>Close figures are indicative.</b> STS marks are crossed, so the cash flows above will not match a real fill. The roll <em>schedule</em> is unaffected — it depends only on the expiry date.</div>`;
}

/* ------------------------------------------------------------------- legs -- */
const G = [
  { k: 'delta', sym: 'Delta', dp: 0 },
  { k: 'gamma', sym: 'Gamma', dp: 1 },
  { k: 'vega', sym: 'Vega', dp: 2 },
  { k: 'theta', sym: 'Theta', dp: 2 },
  { k: 'rho', sym: 'Rho', dp: 3 },
];

function renderLegs() {
  const s = state.snap;
  const showUsd = state.units !== 'raw';
  const showRaw = state.units !== 'usd';

  const grp = [];
  if (showUsd) grp.push(`<th class="grp sep" colspan="${G.length}">Money greeks (${s.quoteCurrency}, position)</th>`);
  if (showRaw) grp.push(`<th class="grp sep" colspan="${G.length}">Raw greeks (per contract)</th>`);

  const markHint = hint('theoretical mark', {
    title: 'Theoretical mark — derived, not quoted',
    desc: 'STS exposes no option price over REST. There is no bid, ask, mid or mark field anywhere in /v1alpha/positions or /v1alpha/instruments. The only price-bearing field is currentMarketValue — a position-level valuation from STS\'s own model (the portal\'s "Theoretical Value" column).',
    formula: 'mark per contract = currentMarketValue ÷ quantity',
    steps: s.legs.map(
      (l) => `${l.side === 'SHORT' ? 'short' : 'long '} ${dec(l.strike, 3)}  ${nf(4).format(l.markValue)} ÷ ${units(l.quantity, 0)}  =  ${dec(Math.abs(l.markPerContract), 6)}`,
    ),
    result: `net position value = ${moneySigned(s.spread.netMarkValue, 2)}`,
    note: 'Executable prices are FIX-only (Market Data Request, or an RFQ Quote) — and these CC strikes come back rfqActive: false, so no two-way market is being made on them. That is the most likely reason the two legs mark crossed.',
  });

  const head =
    `<tr><th class="grp" colspan="7"></th>${grp.join('')}</tr>` +
    `<tr>
      <th>Leg</th><th>Qty</th><th>Strike</th><th>DTE</th><th>Moneyness</th><th>Mark ${markHint}</th><th>Value</th>
      ${showUsd ? G.map((g, i) => `<th class="${i === 0 ? 'sep' : ''}">${g.sym} $</th>`).join('') : ''}
      ${showRaw ? G.map((g, i) => `<th class="${i === 0 ? 'sep' : ''}">${g.sym}</th>`).join('') : ''}
    </tr>`;

  const body = s.legs
    .map((l) => `
      <tr>
        <td><span class="leg-name"><span class="side-tag ${l.side}">${l.side}</span><span><div>${l.label}</div><div class="code">${l.isITM ? 'ITM' : 'OTM'} · ${l.accountName || ''}</div></span></span></td>
        <td class="${signClass(l.quantity)}">${units(l.quantity, 0)}</td>
        <td>${dec(l.strike, 3)}</td>
        <td>${fin(l.daysToExpiry) ? nf(1).format(l.daysToExpiry) : '—'}</td>
        <td class="${signClass(l.moneyness)}">${pctSigned(l.moneyness)}</td>
        <td>${dec(Math.abs(l.markPerContract), 5)}</td>
        <td class="${signClass(l.markValue)}">${moneySigned(l.markValue, 2)}</td>
        ${showUsd ? G.map((g, i) => `<td class="${i === 0 ? 'sep ' : ''}${signClass(l.usd[g.k])}">${moneySigned(l.usd[g.k], g.dp)}</td>`).join('') : ''}
        ${showRaw ? G.map((g, i) => `<td class="${i === 0 ? 'sep' : ''}">${sig(l.raw[g.k], 4)}</td>`).join('') : ''}
      </tr>`)
    .join('');

  const tot = s.optionsUsd;
  const totalRow = `
    <tr class="total">
      <td>Options book total</td>
      <td>${units(s.legs.reduce((a, l) => a + l.quantity, 0), 0)}</td>
      <td colspan="4"></td>
      <td class="${signClass(s.spread.netMarkValue)}">${moneySigned(s.spread.netMarkValue, 2)}</td>
      ${showUsd ? G.map((g, i) => `<td class="${i === 0 ? 'sep ' : ''}${signClass(tot[g.k])}">${moneySigned(tot[g.k], g.dp)}</td>`).join('') : ''}
      ${showRaw ? `<td class="sep" colspan="${G.length}" style="text-align:right;color:var(--text-muted);font-weight:500">raw greeks are per-contract — not additive across strikes</td>` : ''}
    </tr>`;

  $('legsTable').innerHTML = `<thead>${head}</thead><tbody>${body}${totalRow}</tbody>`;
}

/* ------------------------------------------------------------ aggregation -- */
function renderAgg() {
  const s = state.snap;
  const rows = s.aggregation.sources
    .map((src) => `
      <tr>
        <td>${src.label}</td>
        <td>${src.units == null ? '—' : units(src.units, 0)}</td>
        <td class="${signClass(src.delta)}">${moneySigned(src.delta, 0)}</td>
        <td class="${signClass(src.gamma)}">${src.gamma ? moneySigned(src.gamma, 1) : '—'}</td>
        <td class="${signClass(src.vega)}">${src.vega ? moneySigned(src.vega, 2) : '—'}</td>
        <td class="${signClass(src.theta)}">${src.theta ? moneySigned(src.theta, 2) : '—'}</td>
      </tr>`)
    .join('');
  const p = s.aggregation.portfolio;
  $('aggTable').innerHTML = `
    <thead><tr><th>Source</th><th>${s.baseCurrency} units</th><th>Delta $</th><th>Gamma $</th><th>Vega $</th><th>Theta $</th></tr></thead>
    <tbody>${rows}
      <tr class="total">
        <td>Total portfolio</td>
        <td>${units(s.coverage.totalCcHeld, 0)}</td>
        <td class="${signClass(p.delta)}">${moneySigned(p.delta, 0)}</td>
        <td class="${signClass(p.gamma)}">${moneySigned(p.gamma, 1)}</td>
        <td class="${signClass(p.vega)}">${moneySigned(p.vega, 2)}</td>
        <td class="${signClass(p.theta)}">${moneySigned(p.theta, 2)}</td>
      </tr>
    </tbody>`;
  $('spotChip').textContent = `spot ${spotFmt(s.spot)}`;
}

/* ---------------------------------------------------------------- payoff -- */
function renderPayoff() {
  const s = state.snap;
  const { shortStrike: Ks, longStrike: Kl, contracts, netCredit } = s.spread;
  const cc = s.coverage.totalCcHeld;
  if (!fin(Ks) || !fin(Kl) || !cc) return;

  const S0 = s.spot;
  const lo = Math.min(S0, Ks) * 0.55;
  const hi = Math.max(S0, Kl) * 1.45;
  const pts = [];
  for (let i = 0; i <= 160; i++) {
    const S = lo + ((hi - lo) * i) / 160;
    const spreadPnl = netCredit - contracts * Math.max(S - Ks, 0) + contracts * Math.max(S - Kl, 0);
    pts.push({ S, hedged: cc * (S - S0) + spreadPnl, naked: cc * (S - S0) });
  }

  $('payoffLegend').innerHTML = [
    ['--series-1', 'CC holding + call spread'],
    ['--series-2', `CC holding alone (${units(cc, 0)} units)`],
  ].map(([c, n]) => `<li><i class="key" style="background:var(${c})"></i>${n}</li>`).join('');

  const host = $('payoffChart');
  if (!charts.has('payoff')) {
    host.replaceChildren();
    charts.set('payoff', new XYChart(host, {
      height: 236,
      margin: { top: 20, right: 18, bottom: 28, left: 58 },
      zeroLine: true,
      series: [
        { key: 'hedged', name: 'CC + spread', color: '--series-1' },
        { key: 'naked', name: 'CC alone', color: '--series-2' },
      ],
      x: { accessor: (d) => d.S, format: (v) => nf(3).format(v), tooltip: (v) => `Spot at expiry ${nf(4).format(v)}`, type: 'linear' },
      yFormat: (v) => money(v, 0),
    }));
  }
  const ch = charts.get('payoff');
  // Strike / spot markers are annotations, not series — they stay neutral so a
  // hue never means two different things inside one chart.
  ch.o.vRefs = [
    { value: Ks, label: `short ${nf(3).format(Ks)}`, color: '--axis' },
    { value: Kl, label: `long ${nf(3).format(Kl)}`, color: '--axis' },
    { value: S0, label: `spot ${nf(4).format(S0)}`, color: '--text-muted' },
  ];
  ch.setData(pts);
}

/* ---------------------------------------------------------------- charts -- */
const CHART_DEFS = [
  {
    id: 'blended', span: true, title: 'Portfolio blended delta vs policy band',
    unit: 'net delta (CC units) ÷ total CC held',
    series: [{ key: 'blended', name: 'Blended delta', color: '--series-1' }],
    area: true,
    yFormat: (v) => pct(v, 0),
    refs: (cfg) => [
      { value: cfg.policy.hardFloor, label: `floor ${pct(cfg.policy.hardFloor, 0)}`, color: '--critical' },
      { value: cfg.policy.target, label: `target ${pct(cfg.policy.target, 0)}`, color: '--good' },
    ],
  },
  {
    id: 'deltaMix', span: true, title: 'Dollar delta decomposition',
    unit: 'Delta $ — options book vs physical CC',
    series: [
      { key: 'optionsDelta', name: 'STS options book', color: '--series-1' },
      { key: 'spotDelta', name: 'Physical CC + premium', color: '--series-2' },
      { key: 'delta', name: 'Total portfolio', color: '--series-3' },
    ],
    zeroLine: true,
    yFormat: (v) => money(v, 0),
  },
  { id: 'spot', title: 'Spot price', unit: 'CC/USDT', series: [{ key: 'spot', name: 'Spot', color: '--series-1' }], area: true, yFormat: (v) => nf(5).format(v) },
  { id: 'gamma', title: 'Portfolio gamma', unit: 'Gamma $ per 1% spot move', series: [{ key: 'gamma', name: 'Gamma $', color: '--series-2' }], area: true, zeroLine: true, yFormat: (v) => money(v, 1) },
  { id: 'vega', title: 'Portfolio vega', unit: 'Vega $ per 1 vol point', series: [{ key: 'vega', name: 'Vega $', color: '--series-3' }], area: true, zeroLine: true, yFormat: (v) => money(v, 2) },
  { id: 'theta', title: 'Portfolio theta', unit: 'Theta $ per day', series: [{ key: 'theta', name: 'Theta $', color: '--series-2' }], area: true, zeroLine: true, yFormat: (v) => money(v, 2) },
  {
    id: 'pnl', span: true, title: 'P&L split over time',
    unit: 'premium collected vs MtM',
    // Accessors reconstruct the split from pre-split history rows too, so the
    // series run continuously across the schema change.
    series: [
      { key: 'realizedPnl', name: 'Premium collected', color: '--series-1', accessor: (d) => (fin(d.realizedPnl) ? d.realizedPnl : fin(d.pnl) && fin(d.markValue) ? d.pnl - d.markValue : null) },
      { key: 'unrealizedPnl', name: 'MtM', color: '--series-2', accessor: (d) => (fin(d.unrealizedPnl) ? d.unrealizedPnl : d.markValue) },
      { key: 'totalPnl', name: 'Total', color: '--series-3', accessor: (d) => (fin(d.totalPnl) ? d.totalPnl : d.pnl) },
    ],
    zeroLine: true,
    yFormat: (v) => money(v, 2),
  },
  { id: 'coverage', title: 'CC available to write', unit: 'total held − reserved against short calls', series: [{ key: 'available', name: 'Available CC', color: '--series-3' }], area: true, zeroLine: true, yFormat: (v) => units(v, 0) },
];

function buildChartShells() {
  const grid = $('chartGrid');
  if (grid.childElementCount) return;
  for (const def of CHART_DEFS) {
    const legend = def.series.length > 1
      ? `<ul class="legend">${def.series.map((s) => `<li><i class="key" style="background:var(${s.color})"></i>${s.name}</li>`).join('')}</ul>`
      : '';
    grid.append(h(`
      <div class="card chart-card ${def.span ? 'span-2' : ''}">
        <header><h2>${def.title}</h2><span class="spacer"></span><span class="unit-note">${def.unit}</span></header>
        ${legend}
        <div class="chart-host" id="host-${def.id}"></div>
        <div class="table-view" id="tv-${def.id}"></div>
      </div>`));
  }
}

function renderCharts() {
  buildChartShells();
  const pts = state.history;
  const span = pts.length > 1 ? new Date(pts[pts.length - 1].t) - new Date(pts[0].t) : 0;

  for (const def of CHART_DEFS) {
    const host = $(`host-${def.id}`);
    if (!charts.has(def.id)) {
      charts.set(def.id, new XYChart(host, {
        height: def.span ? 224 : 176,
        series: def.series,
        area: def.area,
        zeroLine: def.zeroLine,
        yFormat: def.yFormat,
        refLines: def.refs ? def.refs(state.config) : [],
        x: { accessor: (d) => new Date(d.t).getTime(), format: (v) => fmtTime(v, span), tooltip: fmtFullTime, type: 'time' },
      }));
    }
    const ch = charts.get(def.id);
    ch.o.refLines = def.refs ? def.refs(state.config) : [];
    ch.o.x.format = (v) => fmtTime(v, span);
    ch.setData(pts);

    // Table-view twin — every value reachable without hovering.
    const tv = $(`tv-${def.id}`);
    if (state.tables) {
      // Read through the same accessor the chart uses, so the table twin is a
      // true equivalent even where a series is reconstructed from older fields.
      const val = (s, p) => (s.accessor ? s.accessor(p) : p[s.key]);
      const rows = pts.slice(-120).reverse().map((p) =>
        `<tr><td>${fmtFullTime(new Date(p.t).getTime())}</td>${def.series.map((s) => `<td>${fin(val(s, p)) ? def.yFormat(val(s, p)) : '—'}</td>`).join('')}</tr>`).join('');
      tv.innerHTML = `<table class="data"><thead><tr><th>Time</th>${def.series.map((s) => `<th>${s.name}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table>`;
    }
    tv.classList.toggle('on', state.tables);
  }

  $('pointsChip').textContent = `${pts.length} samples · every ${state.pollSeconds}s`;
}

/* ----------------------------------------------------------------- rules -- */
function renderRules() {
  // Rules are evaluated server-side (src/alerts.js) so the dashboard and the
  // future notifier can never disagree. Freshness is the one client-only rule.
  const ageMs = state.lastPollAt ? Date.now() - new Date(state.lastPollAt).getTime() : Infinity;
  const defs = [
    ...state.rules,
    {
      name: 'Data freshness',
      detail: `poll interval ${state.pollSeconds}s`,
      value: Number.isFinite(ageMs) ? `${Math.round(ageMs / 1000)}s ago` : '—',
      state: state.lastError || ageMs > state.pollSeconds * 3000 ? 'critical' : ageMs > state.pollSeconds * 1500 ? 'warning' : 'good',
    },
  ];

  $('rules').innerHTML = defs
    .map((r) => `
      <div class="rule-row" data-state="${r.state}">
        <span class="ico"><svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true">${ICONS[r.state]}</svg></span>
        <span class="desc"><b>${r.name}</b> — ${r.detail}</span>
        <span class="val">${r.value}</span>
      </div>`)
    .join('');
}

/* ------------------------------------------------------------------ chrome -- */
function renderChrome() {
  const s = state.snap;
  $('bookLabel').textContent = `${state.config.book.label} · ${s.legs.length} legs · ${s.baseCurrency}/${s.quoteCurrency}`;

  const chip = $('modeChip');
  const live = state.mode === 'live';
  chip.className = `chip ${state.lastError ? 'err' : live ? 'live' : 'demo'}`;
  $('modeText').textContent = state.lastError ? 'POLL ERROR' : live ? 'LIVE — STS' : 'DEMO DATA';
  chip.title = state.lastError ? state.lastError.message : live ? 'Polling tokyo.stsdigital.net' : 'No STS credentials — synthetic book priced with Black-Scholes';

  document.title = `${pct(s.deltaMonitor.blendedDelta, 1)} delta · Covered Call Spread Risk Monitor`;
}

function tickClock() {
  if (!state.lastPollAt) return;
  const secs = Math.round((Date.now() - new Date(state.lastPollAt).getTime()) / 1000);
  $('clockChip').textContent = `updated ${secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m`} ago`;
}

function renderAll() {
  if (!state.snap) return;
  // Calc definitions are rebuilt from the current snapshot each paint, so a
  // popover can never show numbers from a previous poll.
  CALCS.clear();
  calcSeq = 0;
  renderChrome();
  renderHero();
  renderTiles();
  renderCoverage();
  renderSpread();
  renderPnl();
  renderRoll();
  renderLegs();
  renderAgg();
  renderPayoff();
  renderCharts();
  renderRules();
  tickClock();
}

/* ------------------------------------------------------------------ events -- */
function segHandler(container, attr, onPick) {
  container.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-' + attr + ']');
    if (!btn) return;
    [...container.querySelectorAll('button')].forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
    onPick(btn.dataset[attr]);
  });
}

segHandler($('rangeSeg'), 'range', (r) => {
  state.range = r;
  document.querySelectorAll('.chart-host').forEach((n) => n.classList.add('stale'));
  load({ range: r }).finally(() => document.querySelectorAll('.chart-host').forEach((n) => n.classList.remove('stale')));
});

segHandler(document.querySelector('[aria-label="Greek units"]'), 'units', (u) => {
  state.units = u;
  renderLegs();
});

$('tablesBtn').addEventListener('click', () => {
  state.tables = !state.tables;
  $('tablesBtn').setAttribute('aria-pressed', String(state.tables));
  renderCharts();
});

$('refreshBtn').addEventListener('click', async () => {
  $('refreshBtn').disabled = true;
  await fetch('/api/refresh', { method: 'POST' });
  await load();
  $('refreshBtn').disabled = false;
});

$('themeBtn').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme;
  const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
  if (next) document.documentElement.dataset.theme = next;
  else delete document.documentElement.dataset.theme;
  localStorage.setItem('cst-theme', next);
  renderAll();
});
// ?theme=light|dark pins the theme for a shared link or a screenshot run.
const urlTheme = new URLSearchParams(location.search).get('theme');
const savedTheme = urlTheme || localStorage.getItem('cst-theme');
if (savedTheme === 'light' || savedTheme === 'dark') document.documentElement.dataset.theme = savedTheme;

const dlg = $('settingsDlg');
$('settingsBtn').addEventListener('click', () => {
  const cfg = state.config;
  $('fCustody').value = cfg.externalHoldings.custodyCcUnits;
  $('fVenue').value = cfg.externalHoldings.venue;
  $('fRefSpot').value = cfg.underlyingPnl?.referenceSpot ?? '';
  $('fAcqCost').value = cfg.underlyingPnl?.acquisitionCost ?? 0;
  $('fFloor').value = cfg.policy.hardFloor;
  $('fBuffer').value = cfg.policy.warningBuffer;
  $('fTarget').value = cfg.policy.target;
  $('fUpper').value = cfg.policy.upperBand;
  $('fCoverage').value = cfg.policy.coverageRatio;
  $('fRoll').value = cfg.policy.rollDaysBeforeExpiry;
  $('fRollWarn').value = cfg.policy.rollApproachDays;
  dlg.showModal();
});
dlg.addEventListener('close', async () => {
  if (dlg.returnValue !== 'save') return;
  await fetch('/api/config', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      custodyCcUnits: $('fCustody').value,
      venue: $('fVenue').value,
      referenceSpot: $('fRefSpot').value === '' ? null : $('fRefSpot').value,
      acquisitionCost: $('fAcqCost').value,
      hardFloor: $('fFloor').value,
      warningBuffer: $('fBuffer').value,
      target: $('fTarget').value,
      upperBand: $('fUpper').value,
      coverageRatio: $('fCoverage').value,
      rollDaysBeforeExpiry: $('fRoll').value,
      rollApproachDays: $('fRollWarn').value,
    }),
  });
  await load();
});

/* ------------------------------------------------------------------- boot -- */
initCalcPopover();
load();
setInterval(() => load(), 30_000);
setInterval(tickClock, 1000);
window.addEventListener('keydown', (e) => {
  if (e.key === 'r' && !e.metaKey && !e.ctrlKey && document.activeElement === document.body) $('refreshBtn').click();
});