/* ==========================================================================
   Minimal SVG chart primitives.
   Mark specs are fixed: 2px lines w/ round joins, >=8px end markers carrying a
   2px surface ring, 10% area wash for single series, solid hairline grid, and
   selective direct labels only (never a value on every point).
   Every chart ships a legend for >=2 series, a crosshair tooltip, and a
   table-view twin rendered by app.js.
   ========================================================================== */

const NS = 'http://www.w3.org/2000/svg';
const el = (name, attrs = {}) => {
  const n = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, String(v));
  return n;
};
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Human-friendly axis ticks: 1 / 2 / 2.5 / 5 x 10^n. */
export function niceTicks(min, max, count = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0];
  if (min === max) {
    const pad = Math.abs(min) > 1e-12 ? Math.abs(min) * 0.1 : 1;
    min -= pad; max += pad;
  }
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.abs(raw) || 1)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) out.push(Number(v.toFixed(12)));
  return out.length ? out : [min, max];
}

export class XYChart {
  /**
   * @param {HTMLElement} host
   * @param {object} opts
   *   series      [{ key, name, color (css var name), accessor?, dashed? }]
   *   refLines    [{ value, label, color, style }]
   *   x           { accessor, format, type: 'time'|'linear' }
   *   yFormat     (v) => string
   *   area        boolean — wash under a single series
   *   height      number
   *   zeroLine    boolean — draw a rule at y = 0 when the domain straddles it
   */
  constructor(host, opts) {
    this.host = host;
    this.o = {
      height: 190,
      margin: { top: 14, right: 66, bottom: 26, left: 58 },
      yFormat: (v) => String(v),
      area: false,
      zeroLine: false,
      refLines: [],
      x: { accessor: (d) => new Date(d.t).getTime(), format: fmtTime, type: 'time' },
      ...opts,
    };
    this.data = [];
    this.svg = el('svg', { role: 'img' });
    this.tip = document.createElement('div');
    this.tip.className = 'tooltip';
    host.append(this.svg, this.tip);

    this._onMove = this._onMove.bind(this);
    this._onLeave = this._onLeave.bind(this);
    this.ro = new ResizeObserver(() => this.render());
    this.ro.observe(host);
  }

  setData(data) {
    this.data = Array.isArray(data) ? data : [];
    this.render();
  }

  _val(s, d) {
    return s.accessor ? s.accessor(d) : d[s.key];
  }

  render() {
    const { o } = this;
    const w = this.host.clientWidth || 600;
    const h = o.height;
    const m = o.margin;
    const pw = Math.max(10, w - m.left - m.right);
    const ph = Math.max(10, h - m.top - m.bottom);

    this.svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    this.svg.setAttribute('height', h);
    this.svg.replaceChildren();

    const pts = this.data.filter((d) => Number.isFinite(o.x.accessor(d)));
    if (pts.length < 2) {
      const t = el('text', { x: w / 2, y: h / 2, 'text-anchor': 'middle', fill: cssVar('--text-muted'), 'font-size': 12 });
      t.textContent = 'Collecting data…';
      this.svg.append(t);
      return;
    }

    // ---- scales -----------------------------------------------------------
    const xs = pts.map(o.x.accessor);
    const x0 = Math.min(...xs);
    const x1 = Math.max(...xs);
    const X = (v) => m.left + (x1 === x0 ? pw / 2 : ((v - x0) / (x1 - x0)) * pw);

    let lo = Infinity;
    let hi = -Infinity;
    for (const s of o.series) {
      for (const d of pts) {
        const v = this._val(s, d);
        if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      }
    }
    for (const r of o.refLines) { lo = Math.min(lo, r.value); hi = Math.max(hi, r.value); }
    if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
    if (o.zeroLine) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    const pad = (hi - lo) * 0.12 || Math.abs(hi || 1) * 0.12;
    lo -= pad; hi += pad;
    const ticks = niceTicks(lo, hi, ph > 190 ? 5 : 4);
    lo = Math.min(lo, ticks[0]);
    hi = Math.max(hi, ticks[ticks.length - 1]);
    const Y = (v) => m.top + ph - ((v - lo) / (hi - lo)) * ph;
    this._X = X; this._Y = Y; this._pts = pts; this._geom = { m, pw, ph, w, h };

    const grid = cssVar('--grid');
    const axis = cssVar('--axis');
    const muted = cssVar('--text-muted');
    const surface = cssVar('--surface-1');

    // ---- grid + y axis ----------------------------------------------------
    const gGrid = el('g');
    for (const t of ticks) {
      const y = Y(t);
      if (y < m.top - 1 || y > m.top + ph + 1) continue;
      gGrid.append(el('line', { x1: m.left, x2: m.left + pw, y1: y, y2: y, stroke: grid, 'stroke-width': 1, 'shape-rendering': 'crispEdges' }));
      const lab = el('text', { x: m.left - 9, y: y + 3.5, 'text-anchor': 'end', fill: muted, 'font-size': 10.5, 'font-variant-numeric': 'tabular-nums' });
      lab.textContent = o.yFormat(t);
      gGrid.append(lab);
    }
    this.svg.append(gGrid);

    // ---- x axis ticks ------------------------------------------------------
    const gX = el('g');
    gX.append(el('line', { x1: m.left, x2: m.left + pw, y1: m.top + ph, y2: m.top + ph, stroke: axis, 'stroke-width': 1, 'shape-rendering': 'crispEdges' }));
    // Budget real width per tick or the labels collide — the widest label the
    // active format can produce drives the count, not a fixed divisor.
    const sampleLabel = o.x.format(x0 + (x1 - x0) / 2, x1 - x0);
    const perTick = Math.max(64, sampleLabel.length * 7.2 + 26);
    const xTickCount = Math.max(2, Math.min(6, Math.floor(pw / perTick)));
    for (let i = 0; i <= xTickCount; i++) {
      const v = x0 + ((x1 - x0) * i) / xTickCount;
      const t = el('text', {
        x: X(v), y: m.top + ph + 15,
        'text-anchor': i === 0 ? 'start' : i === xTickCount ? 'end' : 'middle',
        fill: muted, 'font-size': 10.5, 'font-variant-numeric': 'tabular-nums',
      });
      t.textContent = o.x.format(v, x1 - x0);
      gX.append(t);
    }
    this.svg.append(gX);

    // ---- reference lines (status-coloured, always labelled) ----------------
    for (const r of o.refLines) {
      const y = Y(r.value);
      if (y < m.top - 1 || y > m.top + ph + 1) continue;
      const c = cssVar(r.color) || r.color;
      this.svg.append(el('line', { x1: m.left, x2: m.left + pw, y1: y, y2: y, stroke: c, 'stroke-width': 1.5, opacity: 0.85, 'shape-rendering': 'crispEdges' }));
      const t = el('text', { x: m.left + pw + 7, y: y + 3.5, fill: muted, 'font-size': 10, 'font-weight': 600 });
      t.textContent = r.label;
      this.svg.append(t);
    }

    // Vertical references (strikes on a payoff curve).
    for (const r of o.vRefs || []) {
      const vx = X(r.value);
      if (vx < m.left - 1 || vx > m.left + pw + 1) continue;
      const c = cssVar(r.color) || r.color;
      this.svg.append(el('line', { x1: vx, x2: vx, y1: m.top, y2: m.top + ph, stroke: c, 'stroke-width': 1.5, opacity: 0.55, 'shape-rendering': 'crispEdges' }));
      const t = el('text', { x: vx, y: m.top - 3, 'text-anchor': 'middle', fill: muted, 'font-size': 10, 'font-weight': 600 });
      t.textContent = r.label;
      this.svg.append(t);
    }

    if (o.zeroLine && lo < 0 && hi > 0) {
      this.svg.append(el('line', { x1: m.left, x2: m.left + pw, y1: Y(0), y2: Y(0), stroke: axis, 'stroke-width': 1, 'shape-rendering': 'crispEdges' }));
    }

    // ---- series ------------------------------------------------------------
    const ends = [];
    for (const s of o.series) {
      const color = cssVar(s.color) || s.color;
      const coords = [];
      for (const d of pts) {
        const v = this._val(s, d);
        if (Number.isFinite(v)) coords.push([X(o.x.accessor(d)), Y(v), v]);
      }
      if (!coords.length) continue;

      if (o.area && o.series.length === 1) {
        const base = Y(Math.max(lo, Math.min(hi, o.zeroLine ? 0 : lo)));
        const dAttr = `M${coords[0][0]},${base}` + coords.map((c) => `L${c[0]},${c[1]}`).join('') + `L${coords[coords.length - 1][0]},${base}Z`;
        this.svg.append(el('path', { d: dAttr, fill: color, opacity: 0.10 }));
      }

      this.svg.append(el('path', {
        d: 'M' + coords.map((c) => `${c[0]},${c[1]}`).join('L'),
        fill: 'none', stroke: color, 'stroke-width': 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round',
        'stroke-dasharray': s.dashed ? '5 4' : null,
      }));

      const last = coords[coords.length - 1];
      // 2px surface ring so overlapping end-dots stay legible.
      this.svg.append(el('circle', { cx: last[0], cy: last[1], r: 4, fill: color, stroke: surface, 'stroke-width': 2 }));
      ends.push({ s, x: last[0], y: last[1], v: last[2], color });
    }

    // Direct end labels — only when they don't collide (never stack them).
    const sorted = [...ends].sort((a, b) => a.y - b.y);
    const collide = sorted.some((e, i) => i > 0 && Math.abs(e.y - sorted[i - 1].y) < 13);
    if (!collide && ends.length && m.right >= 50) {
      for (const e of ends) {
        // Identity rides the coloured dot; the text itself stays in ink.
        this.svg.append(el('circle', { cx: m.left + pw + 9, cy: e.y, r: 3, fill: e.color }));
        const t = el('text', { x: m.left + pw + 15, y: e.y + 3.5, fill: cssVar('--text-secondary'), 'font-size': 10.5, 'font-weight': 600, 'font-variant-numeric': 'tabular-nums' });
        t.textContent = o.yFormat(e.v);
        this.svg.append(t);
      }
    }

    // ---- hover layer -------------------------------------------------------
    this.cross = el('line', { y1: m.top, y2: m.top + ph, stroke: axis, 'stroke-width': 1, opacity: 0, 'shape-rendering': 'crispEdges' });
    this.hoverDots = el('g', { opacity: 0 });
    this.svg.append(this.cross, this.hoverDots);

    const overlay = el('rect', { x: m.left, y: m.top, width: pw, height: ph, fill: 'transparent', style: 'cursor:crosshair' });
    overlay.addEventListener('pointermove', this._onMove);
    overlay.addEventListener('pointerleave', this._onLeave);
    this.svg.append(overlay);
  }

  _nearest(px) {
    const { o } = this;
    let best = 0;
    let bestD = Infinity;
    this._pts.forEach((d, i) => {
      const dist = Math.abs(this._X(o.x.accessor(d)) - px);
      if (dist < bestD) { bestD = dist; best = i; }
    });
    return best;
  }

  _onMove(ev) {
    const rect = this.svg.getBoundingClientRect();
    const scale = (this._geom.w || rect.width) / rect.width;
    const px = (ev.clientX - rect.left) * scale;
    const i = this._nearest(px);
    const d = this._pts[i];
    if (!d) return;

    const { o } = this;
    const x = this._X(o.x.accessor(d));
    this.cross.setAttribute('x1', x);
    this.cross.setAttribute('x2', x);
    this.cross.setAttribute('opacity', 1);

    this.hoverDots.replaceChildren();
    const rows = [];
    for (const s of o.series) {
      const v = this._val(s, d);
      if (!Number.isFinite(v)) continue;
      const color = cssVar(s.color) || s.color;
      this.hoverDots.append(el('circle', { cx: x, cy: this._Y(v), r: 4.5, fill: color, stroke: cssVar('--surface-1'), 'stroke-width': 2 }));
      rows.push(`<div class="tt-row"><span class="tt-name"><i class="tt-key" style="background:${color}"></i>${s.name}</span><span class="tt-val">${o.yFormat(v)}</span></div>`);
    }
    this.hoverDots.setAttribute('opacity', 1);

    this.tip.innerHTML = `<div class="tt-time">${o.x.tooltip ? o.x.tooltip(o.x.accessor(d)) : o.x.format(o.x.accessor(d), Infinity)}</div>${rows.join('')}`;
    this.tip.classList.add('on');
    const hostW = this.host.clientWidth;
    const left = Math.min(Math.max((x / this._geom.w) * hostW, 80), hostW - 80);
    this.tip.style.left = `${left}px`;
    this.tip.style.top = `${Math.max(this._geom.m.top + 4, ev.offsetY - 14)}px`;
  }

  _onLeave() {
    this.cross.setAttribute('opacity', 0);
    this.hoverDots.setAttribute('opacity', 0);
    this.tip.classList.remove('on');
  }

  destroy() {
    this.ro.disconnect();
  }
}

/** 12-point sparkline for stat tiles. Current value gets the accent dot. */
export function sparkline(host, values, colorVar = '--series-1', { width = 76, height = 26 } = {}) {
  host.replaceChildren();
  const vals = values.filter(Number.isFinite);
  if (vals.length < 2) return;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const span = hi - lo || Math.abs(hi) * 0.1 || 1;
  const svg = el('svg', { viewBox: `0 0 ${width} ${height}`, width, height, 'aria-hidden': 'true' });
  const pt = (v, i) => [
    (i / (vals.length - 1)) * (width - 5) + 2.5,
    height - 3 - ((v - lo) / span) * (height - 6),
  ];
  const coords = vals.map(pt);
  const color = cssVar(colorVar) || colorVar;
  svg.append(el('path', {
    d: `M${coords[0][0]},${height} ` + coords.map((c) => `L${c[0]},${c[1]}`).join('') + `L${coords[coords.length - 1][0]},${height}Z`,
    fill: color, opacity: 0.10,
  }));
  svg.append(el('path', { d: 'M' + coords.map((c) => `${c[0]},${c[1]}`).join('L'), fill: 'none', stroke: color, 'stroke-width': 1.75, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  const last = coords[coords.length - 1];
  svg.append(el('circle', { cx: last[0], cy: last[1], r: 2.4, fill: color, stroke: cssVar('--surface-1'), 'stroke-width': 1.5 }));
  host.append(svg);
}

export function fmtTime(ms, span = 86_400_000) {
  const d = new Date(ms);
  // Keep tick labels as narrow as the span allows — wide labels collide.
  if (span <= 36 * 3.6e6) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (span <= 7 * 86_400_000) {
    return `${d.toLocaleDateString([], { weekday: 'short' })} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  }
  return d.toLocaleDateString([], { day: '2-digit', month: 'short' });
}

export const fmtFullTime = (ms) => new Date(ms).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });