// SPDX-License-Identifier: GPL-3.0-or-later
// Canvas drawings: strip charts (always with grid lines), the sky dome and the world map.
// Colours come from the CSS theme variables, so drawings follow the Dark/Red switch.

const DEG = Math.PI / 180;

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function setup(canvas, aspect) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 300;
  const h = Math.round(w * aspect);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.height = h + 'px';
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

const MONO = '11px "JetBrains Mono", ui-monospace, Menlo, monospace';
const SANS = '11px Inter, system-ui, sans-serif';

export function niceTicks(lo, hi, n = 4) {
  const span = Math.max(hi - lo, 1e-9);
  const raw = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) ticks.push(+v.toFixed(10));
  return { ticks, step };
}

// series: [{ points: [[t_s, value], ...], color }], x from -span..0 s relative to now_s.
// signed: values either side of zero, a symmetric scale and a stronger zero line.
export function drawStrip(canvas, { series, now_s, span_s = 60, unit = '', minTop = 1, aspect = 0.38, signed = false }) {
  const { ctx, w, h } = setup(canvas, aspect);
  const L = 40, R = 8, T = 8, B = 20;
  const pw = w - L - R, ph = h - T - B;
  let top = minTop;
  for (const s of series) for (const [t, v] of s.points) if (t >= now_s - span_s && Number.isFinite(v)) top = Math.max(top, Math.abs(v));
  const { ticks: up, step } = niceTicks(0, top * 1.1, signed ? 2 : 4);
  if (up[up.length - 1] < top) up.push(+(up[up.length - 1] + step).toFixed(10)); // the scale covers the data
  const ymax = up[up.length - 1] || top;
  const ymin = signed ? -ymax : 0;
  const ticks = signed ? [...up.slice(1).reverse().map((v) => -v), ...up] : up;
  const X = (t) => L + pw * (1 - (now_s - t) / span_s);
  const Y = (v) => T + ph * (1 - (v - ymin) / (ymax - ymin));
  const grid = cssVar('--grid'), muted = cssVar('--muted');
  ctx.lineWidth = 1;
  ctx.strokeStyle = grid;
  ctx.fillStyle = muted;
  ctx.font = MONO;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const v of ticks) {
    ctx.beginPath(); ctx.moveTo(L, Y(v) + 0.5); ctx.lineTo(L + pw, Y(v) + 0.5); ctx.stroke();
    ctx.fillText(`${+v.toFixed(2)}${unit}`, L - 4, Y(v));
  }
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (let k = 0; k <= 6; k++) {
    const t = now_s - span_s + (span_s * k) / 6;
    const x = L + (pw * k) / 6;
    ctx.beginPath(); ctx.moveTo(x + 0.5, T); ctx.lineTo(x + 0.5, T + ph); ctx.stroke();
    ctx.fillText(k === 6 ? '0 s' : String(Math.round(t - now_s)), x, T + ph + 4);
  }
  if (signed) {
    ctx.strokeStyle = muted;
    ctx.beginPath(); ctx.moveTo(L, Y(0) + 0.5); ctx.lineTo(L + pw, Y(0) + 0.5); ctx.stroke();
  }
  ctx.lineWidth = 1.6;
  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.beginPath();
    let started = false;
    for (const [t, v] of s.points) {
      if (t < now_s - span_s || !Number.isFinite(v)) { started = false; continue; }
      const x = X(t), y = Y(Math.max(ymin, Math.min(v, ymax)));
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
}

// Sky dome: az/el polar projection, north up, east left (as seen looking up from below).
// preview: [[az, el, sunlit], ...] for a pass being looked at (solid where sunlit, dotted in shadow).
// preview2: a second object's path (conjunctions), thinner and in the accent colour.
// mark: [az, el] of an event (the time of closest approach), drawn as a cross.
// points: [{ az, el, label, sel }] (aircraft); labels: the preview's two end labels.
export function drawSky(canvas, { maskEl = 10, path = [], target = null, mount = null, sun = null, preview = null, preview2 = null, mark = null,
  points = null, labels = ['AOS', 'LOS'] }) {
  const { ctx, w, h } = setup(canvas, 1);
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 18;
  const P = (az, el) => {
    const r = (R * (90 - el)) / 90;
    return [cx - r * Math.sin(az * DEG), cy - r * Math.cos(az * DEG)];
  };
  ctx.fillStyle = cssVar('--ground'); ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = cssVar('--sky'); ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.fill();
  // Horizon mask band.
  ctx.fillStyle = cssVar('--mask');
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.arc(cx, cy, (R * (90 - maskEl)) / 90, 0, 2 * Math.PI, true); ctx.fill();
  // Grid: elevation rings every 15 deg, azimuth spokes every 30 deg.
  ctx.strokeStyle = cssVar('--grid'); ctx.lineWidth = 1;
  for (let el = 15; el < 90; el += 15) { ctx.beginPath(); ctx.arc(cx, cy, (R * (90 - el)) / 90, 0, 2 * Math.PI); ctx.stroke(); }
  for (let az = 0; az < 360; az += 30) { const [x, y] = P(az, 0); ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke(); }
  ctx.strokeStyle = cssVar('--horizon'); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.stroke();
  ctx.fillStyle = cssVar('--muted'); ctx.font = SANS; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  const names = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' };
  for (let az = 0; az < 360; az += 30) { const r = R + 10; ctx.fillText(names[az] || String(az), cx - r * Math.sin(az * DEG), cy - r * Math.cos(az * DEG)); }
  for (const el of [30, 60]) ctx.fillText(`${el}°`, cx + 12, cy - (R * (90 - el)) / 90 + 6);
  // Keyhole near the zenith.
  ctx.strokeStyle = cssVar('--warn'); ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.arc(cx, cy, (R * 5) / 90, 0, 2 * Math.PI); ctx.stroke(); ctx.setLineDash([]);
  // Planned path.
  if (path.length > 1) {
    ctx.strokeStyle = cssVar('--accent'); ctx.lineWidth = 2; ctx.setLineDash([6, 5]); ctx.beginPath();
    path.forEach(([az, el], i) => { const [x, y] = P(az, Math.max(el, -2)); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke(); ctx.setLineDash([]);
  }
  if (preview && preview.length > 1) {
    ctx.strokeStyle = cssVar('--accent2');
    for (let i = 1; i < preview.length; i++) {
      const lit = preview[i][2];
      ctx.lineWidth = lit ? 3 : 2; ctx.setLineDash(lit ? [] : [3, 4]);
      const [x0, y0] = P(preview[i - 1][0], preview[i - 1][1]), [x1, y1] = P(preview[i][0], preview[i][1]);
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
    }
    ctx.setLineDash([]);
    const vis = preview.filter((q) => q[1] >= -1);
    if (vis.length > 1) endLabels(ctx, P(...vis[0]), P(...vis[vis.length - 1]), cssVar('--accent2'), labels);
  }
  if (preview2 && preview2.length > 1) {
    ctx.strokeStyle = cssVar('--accent'); ctx.lineWidth = 1.5;
    ctx.beginPath();
    let up = false;
    for (const [az, el] of preview2) {
      if (el < -1) { up = false; continue; }
      const [x, y] = P(az, el);
      up ? ctx.lineTo(x, y) : ctx.moveTo(x, y); up = true;
    }
    ctx.stroke();
  }
  if (mark) cross(ctx, P(mark[0], mark[1]), cssVar('--text'));
  if (points) {
    ctx.font = SANS; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    for (const p of [...points].sort((a, b) => a.sel - b.sel)) {
      if (p.el < -1) continue;
      const [x, y] = P(p.az, p.el);
      ctx.fillStyle = p.sel ? cssVar('--accent2') : cssVar('--accent');
      ctx.beginPath(); ctx.arc(x, y, p.sel ? 5 : 3.5, 0, 2 * Math.PI); ctx.fill();
      ctx.fillStyle = p.sel ? cssVar('--text') : cssVar('--muted'); ctx.fillText(p.label, x + 7, y);
    }
  }
  if (sun && sun[1] > -2) {
    const [x, y] = P(sun[0], sun[1]); ctx.fillStyle = cssVar('--sun'); ctx.beginPath(); ctx.arc(x, y, 7, 0, 2 * Math.PI); ctx.fill();
  }
  if (target && target[1] > -5) {
    const [x, y] = P(target[0], target[1]); ctx.fillStyle = cssVar('--accent'); ctx.beginPath(); ctx.arc(x, y, 5, 0, 2 * Math.PI); ctx.fill();
  }
  if (mount) {
    const [x, y] = P(mount[0], Math.max(mount[1], -5));
    ctx.strokeStyle = cssVar('--text'); ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(x, y, 10, 0, 2 * Math.PI); ctx.stroke();
    for (const [dx, dy] of [[0, -1], [0, 1], [-1, 0], [1, 0]]) { ctx.beginPath(); ctx.moveTo(x + dx * 13, y + dy * 13); ctx.lineTo(x + dx * 19, y + dy * 19); ctx.stroke(); }
  }
}

// Aircraft layer: range rings around the site, the selected aircraft's trail (solid) and
// predicted path (dashed), each aircraft as an arrow along its track, and the site.
function drawAir(ctx, air, X, Y, w) {
  const [slon, slat] = air.site;
  const pxPerDeg = X(slon + 1) - X(slon);
  ctx.strokeStyle = cssVar('--grid'); ctx.fillStyle = cssVar('--muted'); ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
  ctx.font = SANS; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
  for (const r of [10, 25, 50, 100, 200].filter((r) => r <= air.radius_km * 1.4)) {
    const ry = (r / 111.2) * pxPerDeg, rx = ry / Math.cos(slat * DEG);
    ctx.beginPath(); ctx.ellipse(X(slon), Y(slat), rx, ry, 0, 0, 2 * Math.PI); ctx.stroke();
    ctx.fillText(`${r} km`, X(slon) + 3, Y(slat) - ry - 1);
  }
  ctx.setLineDash([]);
  const acc2 = cssVar('--accent2');
  const line = (pts, dash) => {
    if (!pts || pts.length < 2) return;
    ctx.strokeStyle = acc2; ctx.lineWidth = dash ? 1.5 : 2; ctx.setLineDash(dash ? [6, 5] : []);
    ctx.beginPath(); pts.forEach(([lo, la], i) => (i ? ctx.lineTo(X(lo), Y(la)) : ctx.moveTo(X(lo), Y(la)))); ctx.stroke();
    ctx.setLineDash([]);
  };
  line(air.trail, false); line(air.path, true);
  ctx.textBaseline = 'middle';
  for (const it of [...air.items].sort((a, b) => a.sel - b.sel)) {
    const x = X(it.lon), y = Y(it.lat);
    if (x < -20 || x > w + 20) continue;
    const c = it.sel ? acc2 : cssVar('--accent'), s = it.sel ? 8 : 6;
    ctx.save(); ctx.translate(x, y); ctx.rotate(it.track_deg * DEG);
    ctx.fillStyle = c; ctx.beginPath(); ctx.moveTo(0, -s); ctx.lineTo(s * 0.7, s); ctx.lineTo(0, s * 0.5); ctx.lineTo(-s * 0.7, s); ctx.closePath(); ctx.fill();
    ctx.restore();
    ctx.fillStyle = it.sel ? cssVar('--text') : cssVar('--muted'); ctx.fillText(it.label, x + 10, y);
  }
  ctx.fillStyle = cssVar('--warn'); const [sx, sy] = [X(slon), Y(slat)];
  ctx.beginPath(); ctx.moveTo(sx, sy - 6); ctx.lineTo(sx + 5, sy + 4); ctx.lineTo(sx - 5, sy + 4); ctx.closePath(); ctx.fill();
}

// An event marker: a cross with a ring, labelled TCA.
function cross(ctx, [x, y], color) {
  ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(x - 7, y - 7); ctx.lineTo(x + 7, y + 7); ctx.moveTo(x + 7, y - 7); ctx.lineTo(x - 7, y + 7); ctx.stroke();
  ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(x, y, 11, 0, 2 * Math.PI); ctx.stroke();
  ctx.font = SANS; ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText('TCA', x + 14, y);
}

// AOS (open circle) and LOS (filled circle) markers with labels.
function endLabels(ctx, [ax, ay], [lx, ly], color, labels = ['AOS', 'LOS']) {
  ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(ax, ay, 5, 0, 2 * Math.PI); ctx.stroke();
  ctx.beginPath(); ctx.arc(lx, ly, 5, 0, 2 * Math.PI); ctx.fill();
  ctx.font = SANS; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillStyle = cssVar('--text');
  if (labels[0]) ctx.fillText(labels[0], ax + 8, ay);
  if (labels[1]) ctx.fillText(labels[1], lx + 8, ly);
}

// Sun-altitude bands: day, civil (0 to -6), nautical (-6 to -12), astronomical (-12 to -18)
// twilight, night, with the band edges drawn as lines (the day/night line strongest) so they
// show even where shading barely changes a dark map. One cell per CSS pixel (scaled up
// smoothly on high-DPI screens); cached per canvas and minute (~10 ms to draw).
const BAND_ALPHA = [0, 0.22, 0.36, 0.5, 0.62];
const layerCache = new WeakMap();
function parseColor(str) {
  const m = str.match(/rgba?\(([^)]+)\)/);
  if (m) { const v = m[1].split(',').map(Number); return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1]; }
  const hex = str.replace('#', '');
  return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16)).concat(1);
}
function lightingLayer(canvas, w, h, sun, lonAt, latAt, now) {
  const day = cssVar('--day'), night = cssVar('--night-deep'), term = cssVar('--terminator'), twl = cssVar('--twilight-line');
  const key = [w, h, lonAt(0), lonAt(w), latAt(0), Math.floor(now.getTime() / 60000), day, night, term, twl].join('|');
  const hit = layerCache.get(canvas);
  if (hit && hit.key === key) return hit.img;
  const cw = Math.max(1, Math.ceil(w)), ch = Math.max(1, Math.ceil(h));
  const img = document.createElement('canvas'); img.width = cw; img.height = ch;
  const g = img.getContext('2d'), data = g.createImageData(cw, ch), px = data.data;
  const [dr, dg, db, da] = parseColor(day), [nr, ng, nb] = parseColor(night);
  const sd = Math.sin(sun.dec * DEG), cd = Math.cos(sun.dec * DEG);
  const bands = new Uint8Array(cw * ch);
  for (let j = 0; j < ch; j++) {
    const lat = latAt(((j + 0.5) / ch) * h) * DEG, sl = Math.sin(lat), cl = Math.cos(lat);
    for (let i = 0; i < cw; i++) {
      const H = (lonAt(((i + 0.5) / cw) * w) - sun.lon) * DEG;
      const alt = Math.asin(sl * sd + cl * cd * Math.cos(H)) / DEG;
      const band = alt >= 0 ? 0 : alt > -6 ? 1 : alt > -12 ? 2 : alt > -18 ? 3 : 4;
      bands[j * cw + i] = band;
      const o = 4 * (j * cw + i);
      if (band === 0) { px[o] = dr; px[o + 1] = dg; px[o + 2] = db; px[o + 3] = Math.round(da * 255); continue; }
      px[o] = nr; px[o + 1] = ng; px[o + 2] = nb; px[o + 3] = Math.round(BAND_ALPHA[band] * 255);
    }
  }
  const lines = [parseColor(term), parseColor(twl)];
  for (let j = 0; j < ch; j++) {
    for (let i = 0; i < cw; i++) {
      const b = bands[j * cw + i];
      const r = i + 1 < cw ? bands[j * cw + i + 1] : b, d = j + 1 < ch ? bands[(j + 1) * cw + i] : b;
      if (r === b && d === b) continue;
      const [lr, lg, lb, la] = lines[Math.min(b, r, d) === 0 ? 0 : 1];
      const o = 4 * (j * cw + i);
      px[o] = lr; px[o + 1] = lg; px[o + 2] = lb; px[o + 3] = Math.round(la * 255);
    }
  }
  g.putImageData(data, 0, 0);
  layerCache.set(canvas, { key, img });
  return img;
}

// Approximate subsolar point (good to ~0.1 deg; enough to shade the night side).
function subsolar(date) {
  const d = date.getTime() / 86400000 - 10957.5; // days since J2000.0
  const g = (357.529 + 0.98560028 * d) * DEG;
  const q = 280.459 + 0.98564736 * d;
  const L = (q + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * DEG;
  const e = (23.439 - 0.00000036 * d) * DEG;
  const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) / DEG / 15;
  const dec = Math.asin(Math.sin(e) * Math.sin(L)) / DEG;
  const gmst = (18.697374558 + 24.06570982441908 * d) % 24;
  let lon = (ra - gmst) * 15;
  lon = ((lon + 540) % 360) - 180;
  return { lon, dec };
}

const wrap180 = (d) => ((((d % 360) + 540) % 360) - 180);

// World map (plate carree). A pass preview (track.aos set) is unwrapped around the site and
// zoomed to fit the pass; a live target's track is shown on the whole world.
// air: aircraft mode, centred on the site: { site: [lon, lat], radius_km, items: [{ lon, lat,
// track_deg, label, sel }], trail: [[lon, lat]], path: [[lon, lat]] } (trail and predicted path
// of the selected aircraft).
export function drawMap(canvas, { land = null, track = null, now = new Date(), air = null }) {
  const { ctx, w, h } = setup(canvas, 0.5);
  const pv = !!track?.aos;
  let pts = track?.points || [];
  let lon0 = -180, span = 360, top = 90; // view: left edge, longitude span, top latitude (lat span = span / 2)
  let site = track?.site, aos, los, now0 = track?.now;
  if (pv) {
    const c = site[0];
    const near = (lo) => c + wrap180(lo - c);
    let prev = null;
    pts = pts.map(([lo, la, f]) => { prev = prev === null ? near(lo) : prev + wrap180(lo - prev); return [prev, la, f]; });
    [aos, los, now0] = [track.aos, track.los, track.now].map(([lo, la]) => [near(lo), la]);
    // Fit the pass (above the mask), the site and the visibility circle; the tails run off the edges.
    const vis = pts.filter((q) => q[2] === 1), v = track.visibility_deg;
    const lons = vis.map((q) => q[0]).concat(c, now0[0] - v / Math.cos(Math.min(80, Math.abs(now0[1])) * DEG), now0[0] + v / Math.cos(Math.min(80, Math.abs(now0[1])) * DEG));
    const lats = vis.map((q) => q[1]).concat(site[1], now0[1] - v, now0[1] + v);
    const pad = 4;
    const [l0, l1] = [Math.min(...lons) - pad, Math.max(...lons) + pad], [b0, b1] = [Math.min(...lats) - pad, Math.max(...lats) + pad];
    span = Math.min(360, Math.max(40, l1 - l0, 2 * (b1 - b0)));
    lon0 = (l0 + l1) / 2 - span / 2;
    top = Math.min(90, Math.max(-90 + span / 2, (b0 + b1) / 2 + span / 4));
  }
  if (air) {
    site = air.site;
    const latSpan = (2 * air.radius_km) / 111.2;
    span = 2 * latSpan; lon0 = site[0] - span / 2; top = site[1] + latSpan / 2;
  }
  const X = (lon) => ((lon - lon0) / span) * w, Y = (lat) => ((top - lat) / (span / 2)) * h;
  // World copies needed to cover the view.
  const copies = [-360, 0, 360].filter((k) => lon0 < 180 + k && lon0 + span > -180 + k);
  ctx.fillStyle = cssVar('--sea'); ctx.fillRect(0, 0, w, h);
  if (land) {
    ctx.fillStyle = cssVar('--land');
    for (const k of copies) for (const ring of land.rings) {
      ctx.beginPath();
      ring.forEach(([lo, la], i) => (i ? ctx.lineTo(X(lo + k), Y(la)) : ctx.moveTo(X(lo + k), Y(la))));
      ctx.fill();
    }
  }
  // Daylight, twilight and night at `now` (the pass time for a preview), and the subsolar point.
  const s = subsolar(now);
  ctx.drawImage(lightingLayer(canvas, w, h, s, (px) => lon0 + (px / w) * span, (py) => top - (py / h) * (span / 2), now), 0, 0, w, h);
  // Coastlines on top, so land and sea stay distinct at night and in the dim red theme.
  if (land) {
    ctx.strokeStyle = cssVar('--coast'); ctx.lineWidth = 1;
    for (const k of copies) for (const ring of land.rings) {
      ctx.beginPath();
      ring.forEach(([lo, la], i) => (i ? ctx.lineTo(X(lo + k), Y(la)) : ctx.moveTo(X(lo + k), Y(la))));
      ctx.closePath(); ctx.stroke();
    }
  }
  ctx.fillStyle = cssVar('--sun'); ctx.strokeStyle = cssVar('--sun'); ctx.lineWidth = 1.5;
  for (const k of [-360, 0, 360]) {
    const [x, y] = [X(s.lon + k), Y(s.dec)];
    if (x < -10 || x > w + 10 || y < -10 || y > h + 10) continue;
    ctx.beginPath(); ctx.arc(x, y, 5, 0, 2 * Math.PI); ctx.fill();
    for (let a = 0; a < 8; a++) {
      const c = Math.cos((a * Math.PI) / 4), d = Math.sin((a * Math.PI) / 4);
      ctx.beginPath(); ctx.moveTo(x + 8 * c, y + 8 * d); ctx.lineTo(x + 11 * c, y + 11 * d); ctx.stroke();
    }
  }
  // Graticule, spaced to suit the zoom.
  const step = span > 120 ? 30 : span > 40 ? 10 : span > 15 ? 5 : span > 6 ? 2 : span > 2.5 ? 1 : 0.5;
  ctx.strokeStyle = cssVar('--grid'); ctx.lineWidth = 1;
  for (let lon = Math.ceil(lon0 / step) * step; lon < lon0 + span; lon += step) {
    if (span > 120 && Math.abs(wrap180(lon)) === 180) continue;
    ctx.beginPath(); ctx.moveTo(Math.round(X(lon)) + 0.5, 0); ctx.lineTo(Math.round(X(lon)) + 0.5, h); ctx.stroke();
  }
  for (let lat = Math.ceil((top - span / 2) / step) * step; lat < top && lat < 90; lat += step) {
    if (span > 120 && Math.abs(lat) === 90) continue;
    ctx.beginPath(); ctx.moveTo(0, Math.round(Y(lat)) + 0.5); ctx.lineTo(w, Math.round(Y(lat)) + 0.5); ctx.stroke();
  }
  if (span <= 120) {
    const ls = step >= 10 ? 20 : step * 2, dp = ls < 1 ? 1 : 0;
    ctx.fillStyle = cssVar('--muted'); ctx.font = SANS; ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    for (let lon = Math.ceil(lon0 / ls) * ls; lon < lon0 + span; lon += ls) {
      const v = wrap180(lon); ctx.fillText(`${Math.abs(v).toFixed(dp)}°${v < 0 ? 'W' : v > 0 && v < 180 ? 'E' : ''}`, X(lon) + 3, h - 14);
    }
    for (let lat = Math.ceil((top - span / 2) / ls) * ls; lat <= 80 && lat < top; lat += ls) if (Y(lat) > 10 && Y(lat) < h - 20) ctx.fillText(`${Math.abs(lat).toFixed(dp)}°${lat < 0 ? 'S' : lat > 0 ? 'N' : ''}`, 3, Y(lat) + 2);
  }
  if (air) { drawAir(ctx, air, X, Y, w); return; }
  if (!track) return;
  // Live target: flag 0 = flown (solid), 1 = upcoming (dashed). Pass preview:
  // flag 1 = above the horizon mask (thick, solid), 0 = before/after the pass (thin, dashed).
  const acc = cssVar(pv ? '--accent2' : '--accent');
  for (const future of [0, 1]) {
    const solid = pv ? future === 1 : future === 0;
    ctx.strokeStyle = acc; ctx.lineWidth = pv ? (solid ? 3 : 1.2) : (solid ? 2.2 : 1.5); ctx.setLineDash(solid ? [] : [5, 4]);
    ctx.beginPath();
    let prev = null;
    for (let i = 0; i < pts.length; i++) {
      const [lo, la, f] = pts[i];
      // Each style's line starts at the last point of the other so the two join up.
      const include = f === future || (i + 1 < pts.length && pts[i + 1][2] === future);
      if (!include) { prev = null; continue; }
      if (prev !== null && Math.abs(lo - prev) > 180) prev = null; // date line (live view)
      if (prev === null) ctx.moveTo(X(lo), Y(la)); else ctx.lineTo(X(lo), Y(la));
      prev = lo;
    }
    ctx.stroke();
  }
  ctx.setLineDash([]);
  // Visibility circle around the sub-satellite point (now, or at culmination for a preview).
  const [lo0, la0] = now0;
  const r = track.visibility_deg * DEG;
  ctx.strokeStyle = cssVar('--ok'); ctx.lineWidth = 1.5; ctx.beginPath();
  for (let b = 0; b <= 360; b += 5) {
    const br = b * DEG, la1 = la0 * DEG;
    const lat = Math.asin(Math.sin(la1) * Math.cos(r) + Math.cos(la1) * Math.sin(r) * Math.cos(br));
    const dlon = Math.atan2(Math.sin(br) * Math.sin(r) * Math.cos(la1), Math.cos(r) - Math.sin(la1) * Math.sin(lat)) / DEG;
    const x = X(lo0 + dlon), y = Y(lat / DEG);
    b ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  if (pv) endLabels(ctx, [X(aos[0]), Y(aos[1])], [X(los[0]), Y(los[1])], acc);
  if (pv && track.mark) cross(ctx, [X(site[0] + wrap180(track.mark[0] - site[0])), Y(track.mark[1])], cssVar('--text'));
  else { ctx.fillStyle = acc; ctx.beginPath(); ctx.arc(X(lo0), Y(la0), 4.5, 0, 2 * Math.PI); ctx.fill(); }
  if (site) {
    ctx.fillStyle = cssVar('--warn'); const [sx, sy] = [X(site[0]), Y(site[1])];
    ctx.beginPath(); ctx.moveTo(sx, sy - 6); ctx.lineTo(sx + 5, sy + 4); ctx.lineTo(sx - 5, sy + 4); ctx.closePath(); ctx.fill();
  }
}

// Histogram (64 bins, 8-bit luminance), log counts.
export function drawHist(canvas, hist) {
  const { ctx, w, h } = setup(canvas, 0.25);
  ctx.fillStyle = cssVar('--img'); ctx.fillRect(0, 0, w, h);
  if (!hist.length) return;
  const max = Math.log10(Math.max(...hist) + 1) || 1;
  const bw = w / hist.length;
  ctx.fillStyle = cssVar('--accent');
  hist.forEach((n, i) => { const bh = (Math.log10(n + 1) / max) * (h - 2); ctx.fillRect(i * bw, h - bh, Math.max(1, bw - 1), bh); });
}
