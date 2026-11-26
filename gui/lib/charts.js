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
export function drawStrip(canvas, { series, now_s, span_s = 60, unit = '', minTop = 1, aspect = 0.38 }) {
  const { ctx, w, h } = setup(canvas, aspect);
  const L = 40, R = 8, T = 8, B = 20;
  const pw = w - L - R, ph = h - T - B;
  let top = minTop;
  for (const s of series) for (const [t, v] of s.points) if (t >= now_s - span_s && Number.isFinite(v)) top = Math.max(top, v);
  const { ticks } = niceTicks(0, top * 1.1, 4);
  const ymax = ticks[ticks.length - 1] || top;
  const X = (t) => L + pw * (1 - (now_s - t) / span_s);
  const Y = (v) => T + ph * (1 - v / ymax);
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
  ctx.lineWidth = 1.6;
  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.beginPath();
    let started = false;
    for (const [t, v] of s.points) {
      if (t < now_s - span_s || !Number.isFinite(v)) { started = false; continue; }
      const x = X(t), y = Y(Math.min(v, ymax));
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
}

// Sky dome: az/el polar projection, north up, east left (as seen looking up from below).
// preview: [[az, el, sunlit], ...] for a pass being looked at (solid where sunlit, dotted in shadow).
export function drawSky(canvas, { maskEl = 10, path = [], target = null, mount = null, sun = null, preview = null }) {
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
    endLabels(ctx, P(...preview[0]), P(...preview[preview.length - 1]), cssVar('--accent2'));
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

// AOS (open circle) and LOS (filled circle) markers with labels.
function endLabels(ctx, [ax, ay], [lx, ly], color) {
  ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(ax, ay, 5, 0, 2 * Math.PI); ctx.stroke();
  ctx.beginPath(); ctx.arc(lx, ly, 5, 0, 2 * Math.PI); ctx.fill();
  ctx.font = SANS; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillStyle = cssVar('--text');
  ctx.fillText('AOS', ax + 8, ay); ctx.fillText('LOS', lx + 8, ly);
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
export function drawMap(canvas, { land = null, track = null, now = new Date() }) {
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
  // Night side.
  const s = subsolar(now);
  ctx.fillStyle = cssVar('--night');
  const north = s.dec < 0; // the dark pole
  for (const k of copies) {
    ctx.beginPath(); ctx.moveTo(X(-180 + k), Y(north ? 90 : -90));
    for (let lon = -180; lon <= 180; lon += 3) {
      const H = (lon - s.lon) * DEG;
      const lat = Math.atan(-Math.cos(H) / Math.tan(Math.abs(s.dec) < 0.05 ? 0.05 * DEG : s.dec * DEG)) / DEG;
      ctx.lineTo(X(lon + k), Y(lat));
    }
    ctx.lineTo(X(180 + k), Y(north ? 90 : -90)); ctx.closePath(); ctx.fill();
  }
  // Graticule (30 deg on the world view, 10 deg zoomed in).
  const step = span > 120 ? 30 : 10;
  ctx.strokeStyle = cssVar('--grid'); ctx.lineWidth = 1;
  for (let lon = Math.ceil(lon0 / step) * step; lon < lon0 + span; lon += step) {
    if (span > 120 && Math.abs(wrap180(lon)) === 180) continue;
    ctx.beginPath(); ctx.moveTo(Math.round(X(lon)) + 0.5, 0); ctx.lineTo(Math.round(X(lon)) + 0.5, h); ctx.stroke();
  }
  for (let lat = -90 + step; lat < 90; lat += step) {
    if (span > 120 && Math.abs(lat) === 90) continue;
    ctx.beginPath(); ctx.moveTo(0, Math.round(Y(lat)) + 0.5); ctx.lineTo(w, Math.round(Y(lat)) + 0.5); ctx.stroke();
  }
  if (span <= 120) {
    ctx.fillStyle = cssVar('--muted'); ctx.font = SANS; ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    for (let lon = Math.ceil(lon0 / 20) * 20; lon < lon0 + span; lon += 20) {
      const v = wrap180(lon); ctx.fillText(`${Math.abs(v)}°${v < 0 ? 'W' : v > 0 && v < 180 ? 'E' : ''}`, X(lon) + 3, h - 14);
    }
    for (let lat = -80; lat <= 80; lat += 20) if (Y(lat) > 10 && Y(lat) < h - 20) ctx.fillText(`${Math.abs(lat)}°${lat < 0 ? 'S' : lat > 0 ? 'N' : ''}`, 3, Y(lat) + 2);
  }
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
  else { ctx.fillStyle = acc; ctx.beginPath(); ctx.arc(X(lo0), Y(la0), 4.5, 0, 2 * Math.PI); ctx.fill(); }
  if (site) {
    ctx.fillStyle = cssVar('--warn'); const [sx, sy] = [X(site[0]), Y(site[1])];
    ctx.beginPath(); ctx.moveTo(sx, sy - 6); ctx.lineTo(sx + 5, sy + 4); ctx.lineTo(sx - 5, sy + 4); ctx.closePath(); ctx.fill();
  }
}
