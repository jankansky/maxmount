// SPDX-License-Identifier: GPL-3.0-or-later
// Sky chart screen (#sky): a planetarium view for choosing astrophotography targets. Stars
// (HYG, to mag 8), deep-sky objects (OpenNGC) and constellations (d3-celestial) come from
// gui/vendor/sky; the gateway supplies the ICRS -> east/north/up rotation, refraction and the
// Sun, Moon and planets (sky_frame, once a second), search, rise/set and the Tonight list.
import { html, useState, useEffect, useRef, useCallback } from './vendor/preact-htm-standalone.module.js';
import { Select } from './lib/ui.js';
import { cssVar } from './lib/charts.js';
import { dms, hms, utc, dur, loadPref, savePref } from './lib/util.js';

const DEG = Math.PI / 180;
const SANS = '11px Inter, system-ui, sans-serif';
const FOV_MIN = 0.5, FOV_MAX = 200;
const TYPE_OPTS = [['galaxy', 'Galaxies'], ['nebula', 'Nebulae'], ['planetary nebula', 'Planetary nebulae'], ['open cluster', 'Open clusters'],
  ['globular cluster', 'Globular clusters'], ['supernova remnant', 'Supernova remnants'], ['planet', 'Planets']];
const MINALT_OPTS = [20, 30, 45, 60].map((d) => [d, `≥ ${d}°`]);
const FILL_OPTS = [[0, 'any size'], [0.05, '≥ 5% of the field'], [0.1, '≥ 10%'], [0.25, '≥ 25%'], [0.5, '≥ 50%']];
const DMAG_OPTS = [['any', 'any'], [6, '≤ 6'], [8, '≤ 8'], [10, '≤ 10'], [11.5, '≤ 11.5']];
const SORT_OPTS = [['alt', 'highest'], ['size', 'largest'], ['mag', 'brightest'], ['time', 'best time']];
const PLANET_RGB = { mercury: '#b9ab95', venus: '#f3e8c8', mars: '#e5773f', jupiter: '#e9cba3', saturn: '#e8d391', uranus: '#9fd8db', neptune: '#7895ec' };

// ---- catalogue ----------------------------------------------------------------------------------
let catalog = null;
const unit = (raDeg, decDeg) => {
  const a = raDeg * DEG, d = decDeg * DEG;
  return [Math.cos(d) * Math.cos(a), Math.cos(d) * Math.sin(a), Math.sin(d)];
};
const slerp = (p, q, f) => {
  const v = [p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f, p[2] + (q[2] - p[2]) * f];
  const n = Math.hypot(...v);
  return [v[0] / n, v[1] / n, v[2] / n];
};
function loadCatalog() {
  if (catalog) return catalog;
  const get = (f) => fetch(`vendor/sky/${f}`).then((r) => r.json());
  catalog = Promise.all([get('stars.json'), get('dso.json'), get('constellations.json')]).then(([s, d, c]) => {
    const n = s.ra.length, years = new Date().getUTCFullYear() + new Date().getUTCMonth() / 12 - 2000;
    const xyz = new Float64Array(3 * n);
    for (let i = 0; i < n; i++) { // proper motion to the current epoch
      const dec = s.dec[i] + (s.pmdec[i] / 3.6e6) * years;
      const ra = s.ra[i] + (s.pmra[i] / 3.6e6) * years / Math.max(Math.cos(s.dec[i] * DEG), 1e-6);
      const v = unit(ra, dec);
      xyz[3 * i] = v[0]; xyz[3 * i + 1] = v[1]; xyz[3 * i + 2] = v[2];
    }
    const lines = [];
    for (const segs of Object.values(c.lines)) {
      for (const seg of segs) {
        const pts = [];
        for (let i = 0; i < seg.length; i++) {
          const p = unit(seg[i][0], seg[i][1]);
          if (i) { const q = unit(seg[i - 1][0], seg[i - 1][1]); for (let k = 1; k < 4; k++) pts.push(slerp(q, p, k / 4)); }
          pts.push(p);
        }
        lines.push(pts);
      }
    }
    const conNames = Object.fromEntries(Object.entries(c.labels).map(([k, v]) => [k, v.name]));
    return {
      stars: { n, xyz, mag: s.mag, bv: s.bv, names: s.names, ra: s.ra, dec: s.dec, pmra: s.pmra, pmdec: s.pmdec, hip: s.hip },
      dso: d.objects.map((o) => ({ ...o, v: unit(o.ra, o.dec), messier: o.id.startsWith('M ') })),
      lines,
      labels: Object.entries(c.labels).map(([id, l]) => ({ id, name: l.name, v: unit(l.ra, l.dec) })),
      conNames,
    };
  });
  return catalog;
}
const starObject = (cat, i) => {
  const s = cat.stars, nm = s.names[i] || ['', ''];
  return { kind: 'star', name: nm[0] || nm[1] || (s.hip[i] ? `HIP ${s.hip[i]}` : 'star'), alt: nm[0] ? nm[1] : '', mag: s.mag[i], bv: s.bv[i],
    ra_deg: s.ra[i], dec_deg: s.dec[i], pm_ra_mas_yr: s.pmra[i], pm_dec_mas_yr: s.pmdec[i], hip: s.hip[i], type: 'star' };
};

// ---- geometry -----------------------------------------------------------------------------------
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a) => { const n = Math.hypot(a[0], a[1], a[2]); return [a[0] / n, a[1] / n, a[2] / n]; };
const mul = (R, v) => [R[0] * v[0] + R[1] * v[1] + R[2] * v[2], R[3] * v[0] + R[4] * v[1] + R[5] * v[2], R[6] * v[0] + R[7] * v[1] + R[8] * v[2]];
const mulT = (R, v) => [R[0] * v[0] + R[3] * v[1] + R[6] * v[2], R[1] * v[0] + R[4] * v[1] + R[7] * v[2], R[2] * v[0] + R[5] * v[1] + R[8] * v[2]];
const azelVec = (az, el) => [Math.sin(az) * Math.cos(el), Math.cos(az) * Math.cos(el), Math.sin(el)];
const vecRaDec = (v) => [((Math.atan2(v[1], v[0]) / DEG) + 360) % 360, Math.asin(Math.max(-1, Math.min(1, v[2]))) / DEG];

// Refraction (ERFA's constants from the gateway) applied to a geometric east/north/up vector.
function refract(e, refa, refb) {
  const el = Math.asin(Math.max(-1, Math.min(1, e[2])));
  if (el < -2 * DEG || !refa) return e;
  const tz = Math.tan(Math.min(Math.PI / 2 - el, 87 * DEG));
  const el2 = el + refa * tz + refb * tz * tz * tz, h = Math.hypot(e[0], e[1]) || 1e-12;
  const c = Math.cos(el2) / h;
  return [e[0] * c, e[1] * c, Math.sin(el2)];
}

// The view: a stereographic projection about the centre direction, "up" towards the world's z
// axis (the zenith in Horizon mode, the celestial pole in Equatorial mode).
function makeView(view, w, h) {
  const c = view.mode === 'horizon' ? azelVec(view.az * DEG, view.alt * DEG) : unit(view.ra, view.dec);
  const u = norm([-c[2] * c[0], -c[2] * c[1], 1 - c[2] * c[2]]);
  const r = cross(c, u);
  const k = (w / 2) / (2 * Math.tan((view.fov * DEG) / 4));
  const cx = w / 2, cy = h / 2;
  const radius = Math.min(179, (view.fov / 2) * Math.hypot(w, h) / w * 1.2 + 2) * DEG; // culling cone
  return {
    c, u, r, k, cx, cy, w, h, cosR: Math.cos(radius),
    proj(v) { // -> [x, y] or null (behind the projection's far side)
      const d = dot(v, c);
      if (d < -0.995) return null;
      const s = (2 * k) / (1 + d);
      return [cx + s * dot(v, r), cy - s * dot(v, u)];
    },
    unproj(x, y) {
      const px = (x - cx) / k, py = (cy - y) / k, rho2 = px * px + py * py;
      const a = (4 - rho2) / (4 + rho2), b = 4 / (4 + rho2);
      return norm([a * c[0] + b * (px * r[0] + py * u[0]), a * c[1] + b * (px * r[1] + py * u[1]), a * c[2] + b * (px * r[2] + py * u[2])]);
    },
  };
}

// ---- drawing ------------------------------------------------------------------------------------
function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600, h = canvas.clientHeight || 400;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}
const BV = [[-0.4, [155, 176, 255]], [0, [202, 215, 255]], [0.4, [248, 247, 255]], [0.65, [255, 244, 234]], [1.0, [255, 222, 180]], [1.4, [255, 204, 140]], [2.0, [255, 180, 100]]];
function bvRgb(bv) {
  for (let i = 1; i < BV.length; i++) {
    if (bv <= BV[i][0] || i === BV.length - 1) {
      const f = Math.max(0, Math.min(1, (bv - BV[i - 1][0]) / (BV[i][0] - BV[i - 1][0])));
      return BV[i - 1][1].map((a, j) => Math.round(a + (BV[i][1][j] - a) * f));
    }
  }
  return [255, 255, 255];
}
const magLimit = (fov) => 5.0 + 3.0 * Math.max(0, Math.min(1, Math.log10(180 / fov) / Math.log10(180 / 5)));

// A polyline of world vectors, broken where it leaves the projectable side or jumps.
function path(ctx, V, pts, closed = false) {
  let prev = null, started = false;
  const lim = 4 * Math.max(V.w, V.h);
  ctx.beginPath();
  for (const p of closed ? [...pts, pts[0]] : pts) {
    const q = V.proj(p);
    const off = !q || Math.abs(q[0] - V.cx) > lim || Math.abs(q[1] - V.cy) > lim;
    if (off) { prev = null; continue; }
    if (prev && Math.hypot(q[0] - prev[0], q[1] - prev[1]) < lim / 2) ctx.lineTo(q[0], q[1]);
    else ctx.moveTo(q[0], q[1]);
    prev = q; started = true;
  }
  if (started) ctx.stroke();
}

// Small circle at "elevation" el (rad) about the world's zenith axis zen, sampled.
function smallCircle(zen, el, n = 180) {
  const a = norm(Math.abs(zen[2]) < 0.9 ? cross(zen, [0, 0, 1]) : cross(zen, [1, 0, 0])), b = cross(zen, a);
  const out = [];
  for (let i = 0; i < n; i++) {
    const t = (2 * Math.PI * i) / n, ce = Math.cos(el), se = Math.sin(el);
    out.push([ce * (Math.cos(t) * a[0] + Math.sin(t) * b[0]) + se * zen[0], ce * (Math.cos(t) * a[1] + Math.sin(t) * b[1]) + se * zen[1], ce * (Math.cos(t) * a[2] + Math.sin(t) * b[2]) + se * zen[2]]);
  }
  return out;
}

// Fills the part of the sphere below elevation el (the side holding the nadir).
function fillBelow(ctx, V, zen, el, style) {
  const big = 1e5;
  const pts = smallCircle(zen, el, 240).map((p) => {
    const d = dot(p, V.c), s = (2 * V.k) / Math.max(1 + d, 1e-6);
    return [Math.max(-big, Math.min(big, V.cx + s * dot(p, V.r))), Math.max(-big, Math.min(big, V.cy - s * dot(p, V.u)))];
  });
  const poly = new Path2D();
  pts.forEach(([x, y], i) => (i ? poly.lineTo(x, y) : poly.moveTo(x, y)));
  poly.closePath();
  const nadir = V.proj([-zen[0], -zen[1], -zen[2]]);
  const inside = nadir && Math.abs(nadir[0]) < big && ctx.isPointInPath(poly, nadir[0] * (window.devicePixelRatio || 1), nadir[1] * (window.devicePixelRatio || 1));
  ctx.fillStyle = style;
  if (inside) { ctx.fill(poly); return; }
  const outer = new Path2D();
  outer.rect(-10, -10, V.w + 20, V.h + 20);
  outer.addPath(poly);
  ctx.fill(outer, 'evenodd');
}

function drawChart(canvas, st) {
  const { ctx, w, h } = setupCanvas(canvas);
  const { view, frame, cat, tel, status, profile, widefield, sel, show } = st;
  const red = document.documentElement.dataset.theme === 'red';
  ctx.fillStyle = cssVar('--sky'); ctx.fillRect(0, 0, w, h);
  const hits = [];
  if (!frame || !cat) {
    ctx.fillStyle = cssVar('--muted'); ctx.font = SANS; ctx.textAlign = 'center';
    ctx.fillText(frame === false ? 'No sky frame from the gateway (is a site location set?)' : 'Loading the sky…', w / 2, h / 2);
    return hits;
  }
  const R = frame.R, horizonMode = view.mode === 'horizon';
  const V = makeView(view, w, h);
  // ICRS -> world: east/north/up (refracted) in Horizon mode, ICRS itself in Equatorial mode.
  const toWorld = horizonMode ? (v) => refract(mul(R, v), frame.refa, frame.refb) : (v) => v;
  const enuToWorld = horizonMode ? (e) => e : (e) => mulT(R, e);
  const zen = enuToWorld([0, 0, 1]);
  const visible = (p) => dot(p, V.c) > V.cosR;
  const fov = view.fov;

  // Grid of the view's own coordinates: altitude/azimuth in Horizon mode, RA/Dec in Equatorial.
  ctx.lineWidth = 1;
  ctx.strokeStyle = cssVar('--grid'); ctx.setLineDash([]);
  // Labels: parallels (alt, Dec) along the meridian through the centre, meridians (az, RA)
  // along a parallel below the centre (the horizon in a wide Horizon view).
  const gridLabels = [];
  const gridLabel = (v, text) => {
    const q = V.proj(v);
    if (q && dot(v, V.c) > V.cosR && q[0] > 16 && q[0] < w - 16 && q[1] > 22 && q[1] < h - 8) gridLabels.push([q, text]);
  };
  const lowerPar = (c) => Math.max(c - 0.8 * (fov / 2) * (h / w), -80); // 80% of the way to the bottom edge
  if (show.grid && horizonMode) {
    const stepAlt = fov > 90 ? 15 : fov > 30 ? 10 : fov > 10 ? 5 : 1, stepAz = fov > 90 ? 30 : fov > 30 ? 15 : fov > 10 ? 5 : 1;
    for (let a = stepAlt; a < 90; a += stepAlt) {
      path(ctx, V, smallCircle(zen, a * DEG, 180), true);
      gridLabel(azelVec(view.az * DEG, a * DEG), `${a}°`);
    }
    const labelAlt = Math.max(lowerPar(view.alt), 1);
    for (let az = 0; az < 360; az += stepAz) {
      const pts = [];
      for (let e = 0; e <= 90; e += 2) pts.push(enuToWorld(azelVec(az * DEG, e * DEG)));
      path(ctx, V, pts);
      gridLabel(azelVec(az * DEG, labelAlt * DEG), `az ${az}°`);
    }
  }
  if (show.grid && !horizonMode) {
    const stepDec = fov > 90 ? 15 : fov > 30 ? 10 : fov > 10 ? 5 : 1, stepRa = fov > 90 ? 2 : fov > 30 ? 1 : fov > 10 ? 1 / 3 : 1 / 12;
    for (let d = -90 + stepDec; d < 90; d += stepDec) {
      const pts = [];
      for (let a = 0; a <= 360; a += 2) pts.push(toWorld(unit(a, d)));
      path(ctx, V, pts);
      gridLabel(unit(view.ra, d), `${d > 0 ? '+' : d < 0 ? '−' : ''}${Math.abs(d)}°`);
    }
    const labelDec = lowerPar(view.dec);
    for (let i = 0; i * stepRa < 24 - 1e-9; i++) {
      const hr = i * stepRa, pts = [];
      for (let d = -88; d <= 88; d += 2) pts.push(toWorld(unit(hr * 15, d)));
      path(ctx, V, pts);
      const hh = Math.floor(hr + 1e-9), mm = Math.round((hr - hh) * 60);
      gridLabel(unit(hr * 15, labelDec), mm ? `${hh}h${String(mm).padStart(2, '0')}m` : `${hh}h`);
    }
  }
  if (gridLabels.length) {
    ctx.fillStyle = cssVar('--muted'); ctx.font = '10px Inter, system-ui, sans-serif';
    ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
    for (const [q, t] of gridLabels) ctx.fillText(t, q[0] + 3, q[1] - 2);
  }
  // Constellations.
  if (show.const) {
    ctx.strokeStyle = cssVar('--const'); ctx.lineWidth = 1;
    for (const line of cat.lines) {
      if (!line.some((p) => dot(toWorld(p), V.c) > V.cosR)) continue;
      path(ctx, V, line.map(toWorld));
    }
    if (show.labels && fov > 8) {
      ctx.fillStyle = cssVar('--const-label'); ctx.font = '10px Inter, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      for (const l of cat.labels) {
        const p = toWorld(l.v);
        if (!visible(p)) continue;
        const q = V.proj(p);
        if (q) ctx.fillText(l.name.toUpperCase(), q[0], q[1]);
      }
    }
  }
  // Deep-sky objects.
  const lim = magLimit(fov);
  if (show.dso) {
    const dsoLim = 6 + 6 * Math.max(0, Math.min(1, Math.log10(180 / fov) / Math.log10(180 / 5)));
    ctx.font = SANS; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    for (const o of cat.dso) {
      const big = (o.maj / 60) * DEG * V.k > 6;
      if (!(o.messier || (o.mag != null && o.mag <= dsoLim) || big)) continue;
      const p = toWorld(o.v);
      if (!visible(p)) continue;
      const q = V.proj(p);
      if (!q || q[0] < -50 || q[0] > w + 50 || q[1] < -50 || q[1] > h + 50) continue;
      const col = cssVar(o.type === 'galaxy' ? '--dso-gal' : o.type === 'globular cluster' || o.type === 'open cluster' ? '--dso-cl' : o.type === 'planetary nebula' ? '--dso-pn' : '--dso-neb');
      const rx = Math.max(4, ((o.maj / 60) * DEG * V.k) / 2), ry = Math.max(o.type === 'galaxy' ? 2 : 4, ((o.min / 60) * DEG * V.k) / 2);
      ctx.strokeStyle = col; ctx.lineWidth = 1.2;
      // Position angle (north through east) on screen: from the projected north and east.
      let ang = 0;
      if (o.type === 'galaxy' && rx > ry + 0.5) {
        const nv = norm([o.v[0] - 0.002 * o.v[2] * o.v[0], o.v[1] - 0.002 * o.v[2] * o.v[1], o.v[2] + 0.002 * (1 - o.v[2] * o.v[2])]);
        const qn = V.proj(toWorld(nv)), qe = V.proj(toWorld(norm([o.v[0] - 0.002 * o.v[1], o.v[1] + 0.002 * o.v[0], o.v[2]])));
        if (qn && qe) {
          const an = Math.atan2(qn[1] - q[1], qn[0] - q[0]), ae = Math.atan2(qe[1] - q[1], qe[0] - q[0]);
          const sgn = Math.sin(ae - an) > 0 ? 1 : -1; // east is +90 deg or -90 deg from north on screen
          ang = an + sgn * o.pa * DEG;
        }
      }
      ctx.beginPath();
      if (o.type === 'galaxy') ctx.ellipse(q[0], q[1], rx, ry, ang, 0, 2 * Math.PI);
      else if (o.type === 'open cluster') { ctx.setLineDash([2, 2]); ctx.arc(q[0], q[1], rx, 0, 2 * Math.PI); }
      else if (o.type === 'globular cluster') { ctx.arc(q[0], q[1], rx, 0, 2 * Math.PI); ctx.moveTo(q[0] - rx, q[1]); ctx.lineTo(q[0] + rx, q[1]); ctx.moveTo(q[0], q[1] - rx); ctx.lineTo(q[0], q[1] + rx); }
      else if (o.type === 'planetary nebula') { ctx.arc(q[0], q[1], Math.min(rx, 5) + 1, 0, 2 * Math.PI); for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) { const r0 = Math.min(rx, 5) + 1; ctx.moveTo(q[0] + dx * r0, q[1] + dy * r0); ctx.lineTo(q[0] + dx * (r0 + 3), q[1] + dy * (r0 + 3)); } }
      else { const s = Math.max(rx, ry); ctx.rect(q[0] - s, q[1] - s, 2 * s, 2 * s); }
      ctx.stroke(); ctx.setLineDash([]);
      hits.push({ x: q[0], y: q[1], r: Math.max(rx, 6), prio: o.messier ? 3 : 2, obj: { kind: 'dso', name: o.id, ...o, ra_deg: o.ra, dec_deg: o.dec } });
      if (show.labels && ((o.messier && fov < 100) || fov < 20 || (o.mag != null && o.mag < 7 && fov < 60))) {
        ctx.fillStyle = col;
        ctx.fillText(o.messier && fov < 30 && o.common ? `${o.id} ${o.common}` : o.id, q[0] + rx + 3, q[1]);
      }
    }
  }
  // Stars, brightest first (the catalogue is sorted by magnitude).
  const s = cat.stars, rScale = Math.max(1, Math.min(1.6, 70 / fov));
  ctx.font = SANS; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  const labels = [];
  for (let i = 0; i < s.n; i++) {
    const m = s.mag[i];
    if (m > lim) break;
    const v = [s.xyz[3 * i], s.xyz[3 * i + 1], s.xyz[3 * i + 2]];
    const p = toWorld(v);
    if (dot(p, V.c) < V.cosR) continue;
    if (horizonMode && show.ground && p[2] < -0.01) continue;
    const q = V.proj(p);
    if (!q || q[0] < -5 || q[0] > w + 5 || q[1] < -5 || q[1] > h + 5) continue;
    const rad = Math.max(0.55, (0.45 + 0.62 * (lim - m)) * rScale * 0.85);
    const bright = Math.max(0.35, Math.min(1, 0.45 + 0.2 * (lim - m)));
    if (red) { const c = Math.round(120 + 135 * bright); ctx.fillStyle = `rgb(${c},${Math.round(c * 0.22)},${Math.round(c * 0.18)})`; }
    else { const [r0, g0, b0] = show.color ? bvRgb(s.bv[i]) : [235, 240, 248]; ctx.fillStyle = `rgba(${r0},${g0},${b0},${bright})`; }
    if (rad < 0.9) ctx.fillRect(q[0] - rad, q[1] - rad, 2 * rad, 2 * rad);
    else { ctx.beginPath(); ctx.arc(q[0], q[1], rad, 0, 2 * Math.PI); ctx.fill(); }
    const nm = s.names[i];
    if (nm || m < lim - 1.5) hits.push({ x: q[0], y: q[1], r: rad + 2, prio: nm ? 1.5 : 1, star: i });
    if (show.labels && nm && nm[0] && (m < 1.5 + 2.2 * Math.log10(180 / fov) + 0.8)) labels.push([nm[0], q[0] + rad + 3, q[1]]);
  }
  ctx.fillStyle = cssVar('--star-label');
  for (const [t, x, y] of labels) ctx.fillText(t, x, y);

  // Sun, Moon and planets.
  for (const b of frame.bodies || []) {
    const v = unit(b.ra_deg, b.dec_deg), p = toWorld(v);
    if (!visible(p)) continue;
    const q = V.proj(p);
    if (!q) continue;
    const rad = Math.max(b.body === 'sun' || b.body === 'moon' ? 6 : 3.2, (b.diameter_arcsec / 3600) * DEG * V.k / 2);
    const obj = { kind: 'body', body: b.body, name: b.name, ra_deg: b.ra_deg, dec_deg: b.dec_deg, mag: b.mag, type: b.body === 'sun' ? 'star (the Sun)' : b.body === 'moon' ? 'moon' : 'planet', diameter_arcsec: b.diameter_arcsec, phase: b.phase };
    if (b.body === 'sun') {
      ctx.fillStyle = cssVar('--sun'); ctx.beginPath(); ctx.arc(q[0], q[1], rad, 0, 2 * Math.PI); ctx.fill();
    } else if (b.body === 'moon') {
      drawMoon(ctx, V, toWorld, v, q, rad, b, red);
    } else {
      ctx.fillStyle = red ? cssVar('--accent') : PLANET_RGB[b.body]; ctx.beginPath(); ctx.arc(q[0], q[1], rad, 0, 2 * Math.PI); ctx.fill();
      if (b.body === 'saturn' && rad < 20) { ctx.strokeStyle = ctx.fillStyle; ctx.lineWidth = 1; ctx.beginPath(); ctx.ellipse(q[0], q[1], rad * 2.1, rad * 0.75, -0.35, 0, 2 * Math.PI); ctx.stroke(); }
    }
    if (show.labels) { ctx.fillStyle = cssVar('--text'); ctx.font = SANS; ctx.textAlign = 'left'; ctx.fillText(b.name, q[0] + rad + 4, q[1] - rad - 2); }
    hits.push({ x: q[0], y: q[1], r: rad + 4, prio: 4, obj });
  }

  // Horizon mask, ground, horizon line, cardinal points.
  const mask = (frame.mask_deg ?? 10) * DEG;
  if (show.ground) {
    fillBelow(ctx, V, zen, mask, cssVar('--mask'));
    ctx.globalAlpha = horizonMode ? 1 : 0.55; // Equatorial: objects below the horizon stay visible
    fillBelow(ctx, V, zen, 0, cssVar('--ground-chart'));
    ctx.globalAlpha = 1;
  }
  ctx.strokeStyle = cssVar('--horizon'); ctx.lineWidth = 1.6; path(ctx, V, smallCircle(zen, 0, 360), true);
  ctx.strokeStyle = cssVar('--warn'); ctx.globalAlpha = 0.6; ctx.lineWidth = 1; ctx.setLineDash([5, 4]); path(ctx, V, smallCircle(zen, mask, 360), true);
  ctx.setLineDash([]); ctx.globalAlpha = 1;
  ctx.font = '600 12px Inter, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
  ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'].forEach((t, i) => {
    const p = enuToWorld(azelVec(i * 45 * DEG, 0));
    if (!visible(p)) return;
    const q = V.proj(p);
    if (q && q[0] > 0 && q[0] < w && q[1] > 0 && q[1] < h + 20) { ctx.fillStyle = i % 2 ? cssVar('--muted') : cssVar('--warn'); ctx.fillText(t, q[0], Math.min(q[1] - 4, h - 4)); }
  });

  // Mount, its fields of view, the target, the selection.
  const sky = tel?.sky;
  if (sky && Number.isFinite(sky.az_deg)) {
    const pE = azelVec(sky.az_deg * DEG, sky.el_deg * DEG);
    const p = horizonMode ? pE : unit(sky.ra_h * 15, sky.dec_deg);
    const q = visible(p) && V.proj(p);
    const zW = zen;
    const fovRect = (wDeg, hDeg, color, label) => {
      const up = norm([zW[0] - dot(zW, p) * p[0], zW[1] - dot(zW, p) * p[1], zW[2] - dot(zW, p) * p[2]]), side = cross(p, up);
      const tw = Math.tan((wDeg * DEG) / 2), th = Math.tan((hDeg * DEG) / 2), pts = [];
      const corner = (a, b) => norm([p[0] + a * tw * side[0] + b * th * up[0], p[1] + a * tw * side[1] + b * th * up[1], p[2] + a * tw * side[2] + b * th * up[2]]);
      for (const [a0, b0, a1, b1] of [[-1, -1, 1, -1], [1, -1, 1, 1], [1, 1, -1, 1], [-1, 1, -1, -1]]) for (let k = 0; k < 8; k++) pts.push(corner(a0 + (a1 - a0) * k / 8, b0 + (b1 - b0) * k / 8));
      ctx.strokeStyle = color; ctx.lineWidth = 1.4; path(ctx, V, pts, true);
      const lq = V.proj(corner(-1, 1));
      if (lq && label && V.k * wDeg * DEG > 40) { ctx.fillStyle = color; ctx.font = '10px Inter, system-ui, sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom'; ctx.fillText(label, lq[0], lq[1] - 2); }
    };
    if (profile?.fov_arcmin) fovRect(profile.fov_arcmin[0] / 60, profile.fov_arcmin[1] / 60, cssVar('--accent'), profile.camera || profile.name);
    if (widefield?.fov_deg) fovRect(widefield.fov_deg[0], widefield.fov_deg[1], cssVar('--accent2'), 'widefield');
    if (q) {
      ctx.strokeStyle = cssVar('--accent'); ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.arc(q[0], q[1], 9, 0, 2 * Math.PI);
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) { ctx.moveTo(q[0] + dx * 5, q[1] + dy * 5); ctx.lineTo(q[0] + dx * 15, q[1] + dy * 15); }
      ctx.stroke();
    }
  }
  if (status?.target_name && Number.isFinite(Number(status.target_az_deg))) {
    const e = azelVec(Number(status.target_az_deg) * DEG, Number(status.target_el_deg) * DEG), p = enuToWorld(e);
    const q = visible(p) && V.proj(p);
    if (q) {
      ctx.strokeStyle = cssVar('--warn'); ctx.lineWidth = 1.6; ctx.beginPath();
      ctx.moveTo(q[0], q[1] - 8); ctx.lineTo(q[0] + 8, q[1]); ctx.lineTo(q[0], q[1] + 8); ctx.lineTo(q[0] - 8, q[1]); ctx.closePath(); ctx.stroke();
    }
  }
  if (sel) {
    const b = sel.kind === 'body' && (frame.bodies || []).find((x) => x.body === sel.body);
    const p = toWorld(b ? unit(b.ra_deg, b.dec_deg) : unit(sel.ra_deg, sel.dec_deg));
    const q = visible(p) && V.proj(p);
    if (q) {
      const g = 12 + (sel.maj ? Math.min(60, (sel.maj / 60) * DEG * V.k / 2) : 0), t = 6;
      ctx.strokeStyle = cssVar('--text'); ctx.lineWidth = 1.5; ctx.beginPath();
      for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) { ctx.moveTo(q[0] + sx * g, q[1] + sy * (g - t)); ctx.lineTo(q[0] + sx * g, q[1] + sy * g); ctx.lineTo(q[0] + sx * (g - t), q[1] + sy * g); }
      ctx.stroke();
    }
  }
  // Caption.
  const cen = horizonMode ? `az ${view.az.toFixed(0)}° alt ${view.alt.toFixed(0)}°` : `RA ${hms(view.ra / 15, 0)} Dec ${dms(view.dec).slice(0, 7)}`;
  ctx.font = SANS; ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.fillStyle = cssVar('--muted');
  ctx.fillText(`${horizonMode ? 'Horizon' : 'Equatorial'} · field ${view.fov >= 10 ? view.fov.toFixed(0) : view.fov.toFixed(1)}° · stars to mag ${lim.toFixed(1)} · centre ${cen}`, 10, 8);
  return hits;
}

function drawMoon(ctx, V, toWorld, v, q, rad, b, red) {
  // Bright limb on screen: rotate the projected north by the position angle towards east.
  const nv = norm([v[0] - 0.002 * v[2] * v[0], v[1] - 0.002 * v[2] * v[1], v[2] + 0.002 * (1 - v[2] * v[2])]);
  const ev = norm([v[0] - 0.002 * v[1], v[1] + 0.002 * v[0], v[2]]);
  const qn = V.proj(toWorld(nv)), qe = V.proj(toWorld(ev));
  let ang = -Math.PI / 2;
  if (qn && qe) {
    const n = norm([qn[0] - q[0], qn[1] - q[1], 0]), e = norm([qe[0] - q[0], qe[1] - q[1], 0]), pa = (b.bright_limb_pa_deg || 0) * DEG;
    ang = Math.atan2(Math.cos(pa) * n[1] + Math.sin(pa) * e[1], Math.cos(pa) * n[0] + Math.sin(pa) * e[0]);
  }
  const k = b.phase ?? 1;
  ctx.save(); ctx.translate(q[0], q[1]); ctx.rotate(ang);
  ctx.fillStyle = cssVar('--moon-dark'); ctx.beginPath(); ctx.arc(0, 0, rad, 0, 2 * Math.PI); ctx.fill();
  ctx.fillStyle = red ? cssVar('--text') : '#e9e6dc';
  ctx.beginPath(); ctx.arc(0, 0, rad, -Math.PI / 2, Math.PI / 2, false);
  const rx = rad * Math.abs(2 * k - 1);
  if (k >= 0.5) ctx.ellipse(0, 0, rx, rad, 0, Math.PI / 2, (3 * Math.PI) / 2, false);
  else ctx.ellipse(0, 0, rx, rad, 0, Math.PI / 2, -Math.PI / 2, true);
  ctx.fill(); ctx.restore();
}

// ---- view helpers -------------------------------------------------------------------------------
const clampView = (v) => ({ ...v, fov: Math.max(FOV_MIN, Math.min(FOV_MAX, v.fov)), alt: Math.max(-30, Math.min(89.5, v.alt)), dec: Math.max(-89.5, Math.min(89.5, v.dec)), az: ((v.az % 360) + 360) % 360, ra: ((v.ra % 360) + 360) % 360 });
function panBy(view, dx, dy, k) {
  const rad = 1 / k / DEG;
  if (view.mode === 'horizon') return clampView({ ...view, az: view.az - (dx * rad) / Math.max(Math.cos(view.alt * DEG), 0.15), alt: view.alt + dy * rad });
  return clampView({ ...view, ra: view.ra + (dx * rad) / Math.max(Math.cos(view.dec * DEG), 0.15), dec: view.dec + dy * rad });
}
// Points the view at an ICRS direction (through the frame in Horizon mode).
function centerOn(view, raDeg, decDeg, frame) {
  if (view.mode === 'equatorial' || !frame) return clampView({ ...view, ra: raDeg, dec: decDeg });
  const e = refract(mul(frame.R, unit(raDeg, decDeg)), frame.refa, frame.refb);
  return clampView({ ...view, az: Math.atan2(e[0], e[1]) / DEG, alt: Math.asin(e[2]) / DEG });
}

const fmtSize = (o) => {
  if (o.kind === 'body' && o.diameter_arcsec == null) return '—';
  if (o.kind === 'body') return o.diameter_arcsec >= 120 ? `${(o.diameter_arcsec / 60).toFixed(1)}′` : `${o.diameter_arcsec.toFixed(1)}″`;
  if (!o.maj) return '—';
  const f = (a) => (a >= 10 ? a.toFixed(0) : a.toFixed(1));
  return o.min && Math.abs(o.min - o.maj) > 0.05 ? `${f(o.maj)}′ × ${f(o.min)}′` : `${f(o.maj)}′`;
};
const sizeArcmin = (o) => (o.kind === 'body' ? (o.diameter_arcsec || 0) / 60 : o.maj || 0);
function fieldFit(o, profile) {
  const a = sizeArcmin(o);
  if (!profile?.fov_arcmin || !a) return null;
  const [fw, fh] = profile.fov_arcmin, short = Math.min(fw, fh);
  const f = a / short;
  if (f > 1) return `larger than the ${short.toFixed(0)}′ field: about ${Math.ceil(a / fw)} × ${Math.ceil(a / fh)} frames to cover`;
  return `${Math.round(f * 100)}% of the field's ${short.toFixed(0)}′ side (${profile.camera || profile.name})`;
}
const TYPE_ABBR = { galaxy: 'galaxy', nebula: 'nebula', 'planetary nebula': 'PN', 'open cluster': 'open cl.', 'globular cluster': 'glob. cl.', 'supernova remnant': 'SNR', planet: 'planet' };
const inText = (ns) => { const s = (Number(ns) - Date.now() * 1e6) / 1e9; return s >= 0 ? `in ${dur(s)}` : `${dur(-s)} ago`; };

// ---- screen -------------------------------------------------------------------------------------
export function SkyChartScreen({ active, hello, tel, status, profile, send, ask }) {
  const canvasRef = useRef(null);
  const [cat, setCat] = useState(null);
  const [frame, setFrame] = useState(null);
  const [view, setViewState] = useState(() => clampView({ mode: 'horizon', az: 180, alt: 45, ra: 0, dec: 0, fov: 120, ...loadPref('skychart_view', {}) }));
  const [show, setShowState] = useState(() => {
    const v = { const: true, labels: true, dso: true, ground: true, color: true, ...loadPref('skychart_show', {}) };
    // Saved before the grid followed the mode: separate alt-az and RA/Dec toggles.
    if (v.grid === undefined) v.grid = v.altaz !== false || !!v.radec;
    return v;
  });
  const [sel, setSel] = useState(null);
  const [info, setInfo] = useState(null);
  const [reply, setReply] = useState(null);
  const [q, setQ] = useState('');
  const [results, setResults] = useState([]);
  const [filt, setFiltState] = useState(() => ({ min_alt_deg: 30, types: ['galaxy', 'nebula', 'planetary nebula', 'globular cluster', 'open cluster', 'supernova remnant', 'planet'], fill: 0.1, mag: 'any', sort: 'alt', ...loadPref('skychart_tonight', {}) }));
  const [tonight, setTonight] = useState(null);
  const st = useRef({});
  const hitsRef = useRef([]);
  const raf = useRef(0);

  const draw = useCallback(() => {
    raf.current = 0;
    if (canvasRef.current && st.current.active) hitsRef.current = drawChart(canvasRef.current, st.current);
  }, []);
  const redraw = useCallback(() => { if (!raf.current) raf.current = requestAnimationFrame(draw); }, [draw]);
  // The App re-renders several times a second: keep the latest state for the drawing, but redraw
  // only on interaction, a new frame (1 Hz) or a change of what is shown.
  Object.assign(st.current, { active, view, frame, cat, tel, status, profile, widefield: hello?.widefield, sel, show });
  useEffect(redraw, [active, view, frame, cat, sel, show, profile]);
  const setView = (v) => { setViewState(v); savePref('skychart_view', v); };
  const setShow = (k, v) => setShowState((s) => { const n = { ...s, [k]: v }; savePref('skychart_show', n); return n; });
  const setFilt = (k, v) => setFiltState((f) => { const n = { ...f, [k]: v }; savePref('skychart_tonight', n); return n; });

  useEffect(() => { if (active && !cat) loadCatalog().then(setCat).catch(() => setCat(null)); }, [active]);
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    const poll = async () => { const r = await ask('sky_frame', {}); if (live) setFrame(r.ok ? r.data : false); };
    poll();
    const id = setInterval(poll, 1000);
    const onResize = () => redraw();
    window.addEventListener('resize', onResize);
    const onTheme = new MutationObserver(redraw);
    onTheme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => { live = false; clearInterval(id); window.removeEventListener('resize', onResize); onTheme.disconnect(); };
  }, [active]);

  // Details of the selection: where it is now and when it is up (refreshed every 15 s).
  useEffect(() => {
    setInfo(null); setReply(null);
    if (!sel || !active) return undefined;
    let live = true;
    const get = async () => {
      const r = await ask('sky_object', sel.kind === 'body' ? { body: sel.body } : { ra_deg: sel.ra_deg, dec_deg: sel.dec_deg });
      if (live) setInfo(r.ok ? r.data : { error: r.message });
    };
    get();
    const id = setInterval(get, 15000);
    return () => { live = false; clearInterval(id); };
  }, [sel?.name, sel?.ra_deg, active]);

  // Search, debounced.
  useEffect(() => {
    if (!q.trim()) { setResults([]); return undefined; }
    const id = setTimeout(async () => { const r = await ask('sky_search', { q }); setResults(r.ok ? r.data.results : []); }, 200);
    return () => clearTimeout(id);
  }, [q]);

  // Tonight list.
  const fovShort = profile?.fov_arcmin ? Math.min(...profile.fov_arcmin) : 0;
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    const get = async () => {
      const r = await ask('sky_tonight', { min_alt_deg: filt.min_alt_deg, types: filt.types, mag_max: filt.mag === 'any' ? null : filt.mag,
        min_size_arcmin: filt.fill * fovShort, sort: filt.sort, planets: filt.types.includes('planet') });
      if (live) setTonight(r.ok ? { ...r.data, message: r.message } : { objects: [], message: r.message });
    };
    get();
    const id = setInterval(get, 300000);
    return () => { live = false; clearInterval(id); };
  }, [active, filt, fovShort]);

  const select = (o, center = true) => {
    setSel(o);
    if (center && o) setView(centerOn(view, o.ra_deg, o.dec_deg, frame || null));
  };
  const goTo = async () => {
    if (!sel) return;
    const cmd = sel.kind === 'body' ? { track_body: { body: sel.body, name: sel.name } }
      : { track_radec: { ra_deg: sel.ra_deg, dec_deg: sel.dec_deg, pm_ra_mas_yr: sel.pm_ra_mas_yr || 0, pm_dec_mas_yr: sel.pm_dec_mas_yr || 0, name: sel.name } };
    setReply({ ok: true, message: 'planning…' });
    setReply(await send('target', cmd));
  };

  // Pointer interaction: drag pans, wheel and pinch zoom about the pointer, click selects.
  const ptr = useRef({ pts: new Map(), moved: 0, pinch: 0 });
  const viewRef = useRef(view);
  viewRef.current = view;
  const zoomAt = (x, y, factor) => {
    const c = canvasRef.current;
    let v = viewRef.current;
    const V0 = makeView(v, c.clientWidth, c.clientHeight), p = V0.unproj(x, y);
    v = clampView({ ...v, fov: v.fov * factor });
    for (let i = 0; i < 2; i++) {
      const V1 = makeView(v, c.clientWidth, c.clientHeight), q1 = V1.proj(p);
      if (!q1) break;
      v = panBy(v, x - q1[0], y - q1[1], V1.k);
    }
    setView(v);
  };
  const local = (e) => { const b = canvasRef.current.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; };
  const onDown = (e) => { canvasRef.current.setPointerCapture(e.pointerId); ptr.current.pts.set(e.pointerId, local(e)); ptr.current.moved = 0; ptr.current.pinch = 0; };
  const onMove = (e) => {
    const P = ptr.current;
    if (!P.pts.has(e.pointerId)) return;
    const [x, y] = local(e), [x0, y0] = P.pts.get(e.pointerId);
    P.pts.set(e.pointerId, [x, y]);
    if (P.pts.size === 2) {
      const [a, b] = [...P.pts.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      if (P.pinch) zoomAt((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, P.pinch / d);
      P.pinch = d; P.moved += 10;
      return;
    }
    P.moved += Math.abs(x - x0) + Math.abs(y - y0);
    if (P.moved > 3) { const c = canvasRef.current; setView(panBy(viewRef.current, x - x0, y - y0, makeView(viewRef.current, c.clientWidth, c.clientHeight).k)); }
  };
  const onUp = (e) => {
    const P = ptr.current;
    const wasTap = P.pts.size === 1 && P.moved <= 3;
    P.pts.delete(e.pointerId);
    if (!wasTap) return;
    const [x, y] = local(e);
    let best = null, bestScore = 1e9;
    for (const h of hitsRef.current) {
      const d = Math.hypot(h.x - x, h.y - y);
      if (d > Math.max(10, h.r + 4)) continue;
      const score = d - 3 * h.prio;
      if (score < bestScore) { bestScore = score; best = h; }
    }
    if (best) select(best.obj || starObject(cat, best.star), false);
  };
  const onWheel = (e) => { e.preventDefault(); const [x, y] = local(e); zoomAt(x, y, Math.exp(e.deltaY * 0.0015)); };
  const onDbl = (e) => { const [x, y] = local(e); zoomAt(x, y, 0.5); };
  const onKey = (e) => {
    const c = canvasRef.current, k = makeView(view, c.clientWidth, c.clientHeight).k, step = c.clientWidth * 0.1;
    const m = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }[e.key];
    if (m) { e.preventDefault(); setView(panBy(view, m[0], m[1], k)); }
    else if (e.key === '+' || e.key === '=') zoomAt(c.clientWidth / 2, c.clientHeight / 2, 0.7);
    else if (e.key === '-') zoomAt(c.clientWidth / 2, c.clientHeight / 2, 1 / 0.7);
    else if (e.key === 'Escape') setSel(null);
  };
  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return undefined;
    c.addEventListener('wheel', onWheel, { passive: false }); // preventDefault needs a non-passive listener
    return () => c.removeEventListener('wheel', onWheel);
  });

  const setMode = (mode) => {
    if (mode === view.mode) return;
    // Keep looking at the same part of the sky.
    const c = canvasRef.current, V = makeView(view, c.clientWidth || 600, c.clientHeight || 400);
    let v = { ...view, mode };
    if (frame) {
      const icrs = view.mode === 'horizon' ? mulT(frame.R, V.c) : V.c, [ra, dec] = vecRaDec(icrs);
      v = centerOn(v, ra, dec, frame);
    }
    setView(clampView(v));
  };
  // A body's live numbers (size, phase, position) come from the latest frame.
  const live = sel?.kind === 'body' ? (frame?.bodies || []).find((b) => b.body === sel.body) : null;
  const selD = live ? { ...sel, ...live, kind: 'body', name: sel.name, type: live.body === 'sun' ? 'star (the Sun)' : live.body === 'moon' ? 'moon' : 'planet' } : sel;
  const conName = (abbr) => cat?.conNames?.[abbr] || abbr || '—';
  const mask = frame?.mask_deg ?? hello?.tracking?.horizon_min_el_deg ?? 10;
  const canTrack = sel && sel.body !== 'sun' && info && !info.error && info.state !== 'never rises';
  const fit = selD && fieldFit(selD, profile);

  return html`<div class="skychart">
    <section class="panel chartpanel" aria-label="Sky chart">
      <canvas ref=${canvasRef} tabindex="0" role="img" aria-label="Sky chart: stars, constellations, galaxies, nebulae, clusters, planets, mount and target"
        onPointerDown=${onDown} onPointerMove=${onMove} onPointerUp=${onUp} onPointerCancel=${(e) => ptr.current.pts.delete(e.pointerId)}
        onDblClick=${onDbl} onKeyDown=${onKey}></canvas>
      <div class="chartbar">
        <div class="seg" role="group" aria-label="Orientation">
          <button class=${view.mode === 'horizon' ? 'on' : ''} aria-pressed=${view.mode === 'horizon'} onClick=${() => setMode('horizon')}>Horizon</button>
          <button class=${view.mode === 'equatorial' ? 'on' : ''} aria-pressed=${view.mode === 'equatorial'} onClick=${() => setMode('equatorial')}>Equatorial</button>
        </div>
        ${[['const', 'Constellations'], ['dso', 'Deep sky'], ['labels', 'Labels'], ['ground', 'Ground'], ['grid', view.mode === 'horizon' ? 'Alt-az grid' : 'RA/Dec grid'], ['color', 'Star colours']].map(([k, t]) => html`
          <label class="check"><input type="checkbox" checked=${show[k]} onChange=${(e) => setShow(k, e.target.checked)} />${t}</label>`)}
        <button class="btn small" onClick=${() => tel?.sky && setView(view.mode === 'horizon' ? clampView({ ...view, az: tel.sky.az_deg, alt: tel.sky.el_deg }) : clampView({ ...view, ra: tel.sky.ra_h * 15, dec: tel.sky.dec_deg }))}>Centre on mount</button>
        <span class="small">drag: pan · wheel/pinch: zoom · click: select · double-click: zoom in</span>
      </div>
    </section>
    <aside class="skyside">
      <section class="panel" aria-label="Find">
        <div class="head"><h2>Find</h2></div>
        <input type="search" aria-label="Find an object" placeholder="M 31, NGC 7000, Vega, Jupiter…" value=${q} onInput=${(e) => setQ(e.target.value)}
          onKeyDown=${(e) => { if (e.key === 'Enter' && results[0]) { select(results[0]); setQ(''); } }} />
        ${results.length > 0 && html`<ul class="results" role="listbox" aria-label="Search results">
          ${results.map((r) => html`<li role="option" tabindex="0" onClick=${() => { select(r); setQ(''); }}
            onKeyDown=${(e) => { if (e.key === 'Enter') { select(r); setQ(''); } }}>
            <b>${r.name}</b> <span class="muted">${[r.common, r.alt, r.kind === 'body' ? 'solar system' : r.type || (r.kind === 'star' ? 'star' : ''), r.mag != null ? `mag ${Number(r.mag).toFixed(1)}` : ''].filter(Boolean).join(' · ')}</span></li>`)}
        </ul>`}
      </section>
      <section class="panel" aria-label="Selected object">
        <div class="head"><h2>Selected</h2>${sel && html`<button class="linkbtn" onClick=${() => setSel(null)}>clear</button>`}</div>
        ${!sel ? html`<p class="small">Click an object on the chart, search for one, or pick one from Tonight.</p>` : html`
          <div class="selname">${selD.name}${selD.common ? html` <span class="muted">${selD.common}</span>` : ''}</div>
          ${selD.alt && html`<div class="small muted">${selD.alt}</div>`}
          <div class="kv mono">
            <span class="k">Type</span><span>${selD.type || selD.kind}${selD.con ? ` in ${conName(selD.con)}` : ''}</span>
            <span class="k">Magnitude</span><span>${selD.mag != null ? Number(selD.mag).toFixed(1) : '—'}${selD.kind === 'body' && selD.phase != null && selD.body !== 'sun' ? ` · ${Math.round(selD.phase * 100)}% lit` : ''}</span>
            ${(selD.maj || selD.kind === 'body') && html`<span class="k">Size</span><span>${fmtSize(selD)}</span>`}
            <span class="k">RA / Dec</span><span>${hms(selD.ra_deg / 15, 1)} ${dms(selD.dec_deg)}${selD.kind === 'body' ? ' (now)' : ''}</span>
            ${info && !info.error && html`
              <span class="k">Az / Alt now</span><span>${info.az_deg.toFixed(1)}° / ${info.el_deg.toFixed(1)}°</span>
              <span class="k">Above ${mask}°</span><span>${info.state === 'rises and sets'
                ? html`${info.rise_ns ? `rises ${utc(info.rise_ns)} (${inText(info.rise_ns)})` : info.up_now ? 'up now' : ''}${info.set_ns ? html`<br />sets ${utc(info.set_ns)} (${inText(info.set_ns)})` : ''}`
                : info.state}</span>
              <span class="k">Highest</span><span>${info.transit_alt_deg.toFixed(0)}° at ${utc(info.transit_ns)} UTC</span>`}
            ${info?.error && html`<span class="k">Visibility</span><span class="warn">${info.error}</span>`}
          </div>
          ${fit && html`<p class="small">Field: ${fit}</p>`}
          ${selD.kind === 'body' && selD.body !== 'sun' && html`<p class="small muted">Tracked from the ERFA ephemeris (moves against the stars).</p>`}
          <div class="grid2">
            <button class="btn" onClick=${() => setView(centerOn(view, selD.ra_deg, selD.dec_deg, frame || null))}>Centre</button>
            <button class="btn primary" disabled=${!canTrack} title=${sel.body === 'sun' ? 'never point at the Sun' : ''} onClick=${goTo}>Go to & track</button>
          </div>
          ${reply && html`<div class=${'reply ' + (reply.ok ? '' : 'bad')} role="status">${reply.message}</div>`}`}
      </section>
      <section class="panel tonight" aria-label="Tonight">
        <div class="head"><h2>Tonight</h2><span class="small">${tonight?.dark ? `dark ${utc(tonight.dark.start_ns)}–${utc(tonight.dark.end_ns)} UTC` : ''}</span></div>
        <div class="toolbar">
          <label>Alt <${Select} aria-label="Minimum altitude" value=${filt.min_alt_deg} onChange=${(v) => setFilt('min_alt_deg', v)} options=${MINALT_OPTS} /></label>
          <label>Size <${Select} aria-label="Size in the field" value=${filt.fill} onChange=${(v) => setFilt('fill', v)} options=${FILL_OPTS} /></label>
          <label>Mag <${Select} aria-label="Magnitude limit" value=${filt.mag} onChange=${(v) => setFilt('mag', v)} options=${DMAG_OPTS} /></label>
          <label>Sort <${Select} aria-label="Sort by" value=${filt.sort} onChange=${(v) => setFilt('sort', v)} options=${SORT_OPTS} /></label>
        </div>
        <div class="typechips">${TYPE_OPTS.map(([k, t]) => html`<button class=${'chipbtn ' + (filt.types.includes(k) ? 'on' : '')} aria-pressed=${filt.types.includes(k)}
          onClick=${() => setFilt('types', filt.types.includes(k) ? filt.types.filter((x) => x !== k) : [...filt.types, k])}>${t}</button>`)}</div>
        <span class="small">${tonight?.message || '…'}${fovShort && filt.fill ? ` · size ≥ ${(filt.fill * fovShort).toFixed(1)}′` : ''}</span>
        <div class="tablewrap"><table>
          <thead><tr><th>Object</th><th>Type</th><th>Mag</th><th title="major axis">Size</th><th title="highest altitude while dark, and when (UTC)">Highest</th><th title="hours above the altitude limit while dark">Up</th></tr></thead>
          <tbody class="mono">
            ${tonight && !tonight.objects.length && html`<tr class="empty"><td colspan="6">Nothing matches.</td></tr>`}
            ${(tonight?.objects || []).map((o) => html`<tr class=${sel && sel.name === o.name ? 'selected' : ''} tabindex="0" onClick=${() => select(o)}
              onKeyDown=${(e) => { if (e.key === 'Enter') select(o); }}>
              <td class="obj" title=${[o.name, o.common, o.alt].filter(Boolean).join(' · ')}>${o.name}${o.common ? html` <span class="muted">${o.common}</span>` : ''}</td>
              <td title=${o.type}>${TYPE_ABBR[o.type] || o.type}</td><td>${o.mag != null ? Number(o.mag).toFixed(1) : '—'}</td><td>${fmtSize({ ...o, min: o.maj })}</td>
              <td>${Math.round(o.max_alt_deg)}° ${utc(o.best_ns).slice(0, 5)}</td><td>${o.hours_up}h</td></tr>`)}
          </tbody>
        </table></div>
      </section>
    </aside>
  </div>`;
}
