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
export function drawSky(canvas, { maskEl = 10, path = [], target = null, mount = null, sun = null }) {
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

export function drawMap(canvas, { land = null, track = null, now = new Date() }) {
  const { ctx, w, h } = setup(canvas, 0.5);
  const X = (lon) => ((lon + 180) / 360) * w, Y = (lat) => ((90 - lat) / 180) * h;
  ctx.fillStyle = cssVar('--sea'); ctx.fillRect(0, 0, w, h);
  if (land) {
    ctx.fillStyle = cssVar('--land');
    for (const ring of land.rings) {
      ctx.beginPath();
      ring.forEach(([lo, la], i) => (i ? ctx.lineTo(X(lo), Y(la)) : ctx.moveTo(X(lo), Y(la))));
      ctx.fill();
    }
  }
  // Night side.
  const s = subsolar(now);
  ctx.fillStyle = cssVar('--night'); ctx.beginPath();
  const north = s.dec < 0; // the dark pole
  ctx.moveTo(0, north ? 0 : h);
  for (let lon = -180; lon <= 180; lon += 3) {
    const H = (lon - s.lon) * DEG;
    const lat = Math.atan(-Math.cos(H) / Math.tan(Math.abs(s.dec) < 0.05 ? 0.05 * DEG : s.dec * DEG)) / DEG;
    ctx.lineTo(X(lon), Y(lat));
  }
  ctx.lineTo(w, north ? 0 : h); ctx.closePath(); ctx.fill();
  // Graticule.
  ctx.strokeStyle = cssVar('--grid'); ctx.lineWidth = 1;
  for (let lon = -150; lon <= 150; lon += 30) { ctx.beginPath(); ctx.moveTo(X(lon) + 0.5, 0); ctx.lineTo(X(lon) + 0.5, h); ctx.stroke(); }
  for (let lat = -60; lat <= 60; lat += 30) { ctx.beginPath(); ctx.moveTo(0, Y(lat) + 0.5); ctx.lineTo(w, Y(lat) + 0.5); ctx.stroke(); }
  if (!track) return;
  // Ground track: flown solid, upcoming dashed; break at the date line.
  const acc = cssVar('--accent');
  for (const future of [0, 1]) {
    ctx.strokeStyle = acc; ctx.lineWidth = future ? 1.5 : 2.2; ctx.setLineDash(future ? [5, 4] : []);
    ctx.beginPath();
    const pts = track.points;
    let prev = null;
    for (let i = 0; i < pts.length; i++) {
      const [lo, la, f] = pts[i];
      // The dashed (future) line starts at the last flown point so the two join up.
      const include = f === future || (future === 1 && f === 0 && i + 1 < pts.length && pts[i + 1][2] === 1);
      if (!include) { prev = null; continue; }
      if (prev !== null && Math.abs(lo - prev) > 180) prev = null; // date line
      if (prev === null) ctx.moveTo(X(lo), Y(la)); else ctx.lineTo(X(lo), Y(la));
      prev = lo;
    }
    ctx.stroke();
  }
  ctx.setLineDash([]);
  // Visibility circle around the sub-satellite point.
  const [lo0, la0] = track.now;
  const r = track.visibility_deg * DEG;
  ctx.strokeStyle = cssVar('--ok'); ctx.lineWidth = 1.5; ctx.beginPath();
  for (let b = 0; b <= 360; b += 5) {
    const br = b * DEG, la1 = la0 * DEG, lo1 = lo0 * DEG;
    const lat = Math.asin(Math.sin(la1) * Math.cos(r) + Math.cos(la1) * Math.sin(r) * Math.cos(br));
    const lon = lo1 + Math.atan2(Math.sin(br) * Math.sin(r) * Math.cos(la1), Math.cos(r) - Math.sin(la1) * Math.sin(lat));
    const x = X((((lon / DEG) + 540) % 360) - 180), y = Y(lat / DEG);
    b ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  ctx.fillStyle = acc; ctx.beginPath(); ctx.arc(X(lo0), Y(la0), 4.5, 0, 2 * Math.PI); ctx.fill();
  if (track.site) {
    ctx.fillStyle = cssVar('--warn'); const [sx, sy] = [X(track.site[0]), Y(track.site[1])];
    ctx.beginPath(); ctx.moveTo(sx, sy - 6); ctx.lineTo(sx + 5, sy + 4); ctx.lineTo(sx - 5, sy + 4); ctx.closePath(); ctx.fill();
  }
}
