// SPDX-License-Identifier: GPL-3.0-or-later
// Pointing model screen (#pointing): runs in mount-svc (svc/pointing, through the gateway's
// `pointing` op). A run slews to points over the sky, plate-solves the widefield camera at
// each, and fits the pointing model's terms; Apply makes the fit the active model.
import { html, useState, useEffect, useRef } from './vendor/preact-htm-standalone.module.js';
import { cssVar } from './lib/charts.js';
import { Select } from './lib/ui.js';
import { loadPref, savePref } from './lib/util.js';

const DEG = Math.PI / 180;
const MODES = [['site', 'Site refit (IA IE AN AW)'], ['full', 'Full model (every term)']];
const TERM_INFO = {
  IA: 'azimuth index', IE: 'elevation index', CA: 'collimation (boresight ⟂ elevation axis)', NPAE: 'axes not perpendicular',
  AN: 'azimuth axis tilt north', AW: 'azimuth axis tilt west', TF: 'tube flexure (cos el)', BE: 'flip-side elevation index',
};
const fmt = (v, d = 1) => (v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toFixed(d));
const hyp = (x, y) => Math.hypot(Number(x) || 0, Number(y) || 0);

// Az/el polar plot (north up, east left, like the Track screen's sky) with each point's
// pointing error as a vector: before the fit (the model in use during the run) or after.
function drawResiduals(canvas, points, which, maskEl) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 300, h = w;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); canvas.style.height = h + 'px'; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2, R = w / 2 - 20;
  const P = (az, el) => { const r = (R * (90 - el)) / 90; return [cx - r * Math.sin(az * DEG), cy - r * Math.cos(az * DEG)]; };
  ctx.fillStyle = cssVar('--ground'); ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = cssVar('--sky'); ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.fill();
  ctx.fillStyle = cssVar('--mask'); ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.arc(cx, cy, (R * (90 - maskEl)) / 90, 0, 2 * Math.PI, true); ctx.fill();
  ctx.strokeStyle = cssVar('--grid'); ctx.lineWidth = 1;
  for (let el = 15; el < 90; el += 15) { ctx.beginPath(); ctx.arc(cx, cy, (R * (90 - el)) / 90, 0, 2 * Math.PI); ctx.stroke(); }
  for (let az = 0; az < 360; az += 30) { const [x, y] = P(az, 0); ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke(); }
  ctx.strokeStyle = cssVar('--horizon'); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.stroke();
  ctx.fillStyle = cssVar('--muted'); ctx.font = '11px Inter, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  const names = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' };
  for (let az = 0; az < 360; az += 90) { const r = R + 10; ctx.fillText(names[az], cx - r * Math.sin(az * DEG), cy - r * Math.cos(az * DEG)); }
  ctx.strokeStyle = cssVar('--warn'); ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.arc(cx, cy, (R * 10) / 90, 0, 2 * Math.PI); ctx.stroke(); ctx.setLineDash([]);
  // Vector scale: the largest error spans about R / 4; a round number for the scale bar.
  const key = which === 'after' ? ['after_xel_arcsec', 'after_el_arcsec'] : ['before_xel_arcsec', 'before_el_arcsec'];
  const solved = points.filter((p) => p.state === 'solved');
  const withRes = solved.filter((p) => p[key[0]] != null && (hyp(p[key[0]], p[key[1]]) > 0 || which === 'after'));
  const big = Math.max(1, ...withRes.map((p) => hyp(p[key[0]], p[key[1]])));
  const bar = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000].find((b) => b >= big / 2) || big;
  const k = (R / 4) / big; // px per arcsec
  const acc = cssVar(which === 'after' ? '--ok' : '--accent2');
  for (const p of points) {
    const [x, y] = P(Number(p.az_deg), Number(p.el_deg));
    const col = p.state === 'solved' ? (p.rejected ? cssVar('--warn') : cssVar('--accent'))
      : p.state === 'failed' ? cssVar('--stop') : ['slewing', 'settling', 'solving'].includes(p.state) ? cssVar('--sun') : cssVar('--muted');
    ctx.fillStyle = col; ctx.strokeStyle = col; ctx.lineWidth = 1.5;
    ctx.beginPath();
    if (p.flipped) { ctx.rect(x - 4, y - 4, 8, 8); } else { ctx.arc(x, y, 4, 0, 2 * Math.PI); }
    p.state === 'pending' ? ctx.stroke() : ctx.fill();
    if (p.state === 'solved' && p[key[0]] != null) {
      // Screen directions of +azimuth and +elevation at this point.
      const az = Number(p.az_deg), el = Number(p.el_deg);
      const [ax, ay] = P(az + 0.5 / Math.max(Math.cos(el * DEG), 0.1), el), [ex, ey] = P(az, el + 0.5);
      const na = Math.hypot(ax - x, ay - y) || 1, ne = Math.hypot(ex - x, ey - y) || 1;
      const dx = (Number(p[key[0]]) * (ax - x)) / na + (Number(p[key[1]]) * (ex - x)) / ne;
      const dy = (Number(p[key[0]]) * (ay - y)) / na + (Number(p[key[1]]) * (ey - y)) / ne;
      const tx = x + dx * k, ty = y + dy * k;
      ctx.strokeStyle = acc; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(tx, ty); ctx.stroke();
      const ang = Math.atan2(ty - y, tx - x);
      if (Math.hypot(tx - x, ty - y) > 6) {
        ctx.fillStyle = acc; ctx.beginPath(); ctx.moveTo(tx, ty);
        ctx.lineTo(tx - 7 * Math.cos(ang - 0.4), ty - 7 * Math.sin(ang - 0.4)); ctx.lineTo(tx - 7 * Math.cos(ang + 0.4), ty - 7 * Math.sin(ang + 0.4)); ctx.fill();
      }
    }
  }
  if (withRes.length) {
    const L = bar * k, x0 = 12, y0 = h - 12;
    ctx.strokeStyle = acc; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x0 + L, y0); ctx.stroke();
    ctx.fillStyle = cssVar('--text'); ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
    ctx.fillText(bar >= 60 ? `${(bar / 60).toFixed(bar % 60 ? 1 : 0)}′` : `${bar}″`, x0, y0 - 4);
  }
}

function TermsTable({ fit, current }) {
  const cur = Object.fromEntries((current?.terms || []).map((t) => [t.name, t]));
  const fitted = Object.fromEntries((fit?.terms || []).map((t) => [t.name, t]));
  const names = Object.keys(TERM_INFO).filter((n) => cur[n] || fitted[n]);
  if (!names.length) return html`<p class="small">No terms yet: the mount points with no model (all zero).</p>`;
  return html`<div class="tablewrap"><table class="terms" aria-label="Pointing terms">
    <thead><tr><th>Term</th><th></th><th class="num">Active</th>${fit && html`<th class="num">This run</th><th class="num nocase" title="1-sigma uncertainty of the fitted value">±σ</th>`}</tr></thead>
    <tbody class="mono">${names.map((n) => html`<tr>
      <td>${n}</td><td class="muted" style="font-family:inherit">${TERM_INFO[n]}</td>
      <td class="num">${cur[n] ? fmt(cur[n].value_arcsec) + '″' : '—'}</td>
      ${fit && html`<td class="num">${fitted[n] ? fmt(fitted[n].value_arcsec) + '″' : '—'}</td>
        <td class="num">${fitted[n]?.fitted ? fmt(fitted[n].sigma_arcsec) : fitted[n] ? 'kept' : ''}</td>`}
    </tr>`)}</tbody></table></div>`;
}

export function PointingScreen({ send, hello, connected, active, cam }) {
  const [st, setSt] = useState(null);
  const [note, setNote] = useState('');
  const [mode, setMode] = useState(loadPref('pt_mode', 'site'));
  const [npts, setNpts] = useState(loadPref('pt_points', 10));
  const [flipped, setFlipped] = useState(loadPref('pt_flipped', true));
  const [view, setView] = useState('before');
  const plot = useRef(null);
  const ask = async (op, extra = {}) => {
    const r = await send('pointing', { op, ...extra });
    if (r.data) setSt(r.data);
    if (op !== 'status') setNote(r.message);
    return r;
  };
  useEffect(() => {
    if (!active || !connected) return undefined;
    let live = true;
    const poll = async () => { const r = await send('pointing', { op: 'status' }); if (live && r.data) setSt(r.data); };
    poll();
    const id = setInterval(poll, 1000);
    return () => { live = false; clearInterval(id); };
  }, [active, connected]);
  const points = st?.points || [];
  const fit = st?.fit?.terms?.length ? st.fit : null;
  useEffect(() => { if (plot.current) drawResiduals(plot.current, points, fit ? view : 'before', hello?.tracking?.horizon_min_el_deg ?? 10); });
  const running = st?.state === 'running';
  const start = () => {
    savePref('pt_mode', mode); savePref('pt_points', npts); savePref('pt_flipped', flipped);
    ask('start', { mode, points: Number(npts) || 0, flipped: mode === 'full' && flipped });
  };
  const setModeAndPoints = (m) => { setMode(m); setNpts(m === 'site' ? 10 : 40); };
  const done = points.filter((p) => p.state === 'solved').length, failed = points.filter((p) => p.state === 'failed').length;
  const camNote = !cam ? 'The widefield camera service is not running: start it on the Pi with mount-svc --system $SYS widefield (add --sim to try it without the camera).'
    : !cam.connected ? `Widefield camera not connected: ${cam.message}` : '';
  return html`<div class="pointing">
    <section class="panel" aria-label="Pointing run">
      <div class="head"><h2>Pointing run</h2>
        <span class="small mono">${st ? `${st.state}${points.length ? ` · ${done} solved${failed ? ` · ${failed} failed` : ''} of ${points.length}` : ''}` : connected ? '…' : 'offline'}</span></div>
      <div class="toolbar">
        <label>Model <${Select} aria-label="Pointing mode" value=${mode} options=${MODES} disabled=${running} onChange=${setModeAndPoints} /></label>
        <label>Points <input type="number" min="4" max="120" step="1" style="width:70px" aria-label="Number of points" value=${npts}
          disabled=${running} onInput=${(e) => setNpts(e.target.value)} /></label>
        ${mode === 'full' && html`<label class="check" title="Every fourth point past the zenith (elevation axis > 90°), so the flip-side term can be fitted">
          <input type="checkbox" checked=${flipped} disabled=${running} onChange=${(e) => setFlipped(e.target.checked)} /> Both sides</label>`}
        ${running ? html`<button class="btn" onClick=${() => ask('abort')}>Abort</button>`
          : html`<button class="btn primary" disabled=${!connected} onClick=${start}>Start run</button>`}
        ${fit && !st.applied && html`<button class="btn primary" onClick=${() => ask('apply')}>Apply</button>
          <button class="btn" onClick=${() => ask('discard')}>Discard</button>`}
      </div>
      <span class="small">${st?.message || ''}${note && note !== st?.message ? html`<br />${note}` : ''}</span>
      ${camNote && html`<span class="small warn">${camNote}</span>`}
      <p class="small muted">The mount slews to each point, the widefield camera's frame is plate-solved, and the
        pointing terms are fitted (az/el offsets are zeroed and the camera loop opened first). Site refit: 8–12 points,
        a few minutes, after moving the mount. Full model: 30–60 points. The fit applies to new targets.</p>
    </section>
    <div class="pt-row">
      <section class="panel" aria-label="Pointing errors">
        <div class="head"><h2>Pointing errors</h2>
          ${fit && html`<div class="seg" role="group" aria-label="Errors shown">
            <button class=${view === 'before' ? 'on' : ''} aria-pressed=${view === 'before'} onClick=${() => setView('before')}>Before</button>
            <button class=${view === 'after' ? 'on' : ''} aria-pressed=${view === 'after'} onClick=${() => setView('after')}>After fit</button></div>`}
        </div>
        <canvas ref=${plot} role="img" aria-label="Sky plot of the pointing points with their errors"></canvas>
        <span class="small">North up, east left · dot: point (square: past the zenith) · arrow: where the camera really pointed
          relative to where the mount believed, ${fit && view === 'after' ? 'after the fit' : 'with the model in use'} · red: failed · orange: outlier</span>
      </section>
      <div class="pt-side">
      <section class="panel" aria-label="Pointing fit">
        <div class="head"><h2>Model</h2>
          <span class="small mono">${fit ? `rms ${fmt(st.fit.rms_before_arcsec, 0)}″ → ${fmt(st.fit.rms_arcsec)}″${st.applied ? ' · applied' : ''}` : ''}</span></div>
        <${TermsTable} fit=${fit} current=${st?.current} />
        ${(st?.notes || []).length > 0 && html`<ul class="small problems">${st.notes.map((n) => html`<li>${n}</li>`)}</ul>`}
        <h3>Active model</h3>
        <span class="small mono">${st?.current ? `${st.current.version || 'none'}${st.current.date ? ` · ${st.current.date}` : ''}${st.current.rms_arcsec ? ` · rms ${fmt(st.current.rms_arcsec)}″` : ''}` : '—'}
          ${st?.current?.path ? html`<br /><span class="muted">${st.current.path}</span>` : ''}</span>
        ${(st?.history || []).length > 0 && html`<h3>History</h3><div class="tablewrap"><table aria-label="Model history"><tbody class="mono">
          ${st.history.map((m) => html`<tr class=${m.active ? 'current' : ''}><td>${m.version}</td><td>${m.mode}</td><td>${m.points} pts</td>
            <td>${fmt(m.rms_arcsec)}″</td></tr>`)}</tbody></table></div>`}
      </section>
    <section class="panel" aria-label="Pointing points">
      <div class="head"><h2>Points</h2></div>
      <div class="tablewrap"><table>
        <thead><tr><th>#</th><th>Az</th><th>El</th><th>Side</th><th>State</th><th class="num">Stars</th>
          <th class="num" title="pointing error with the model in use during the run">Before</th><th class="num" title="residual after the fit">After</th><th>Note</th></tr></thead>
        <tbody class="mono">
          ${!points.length && html`<tr class="empty"><td colspan="9">No run yet. Start one above.</td></tr>`}
          ${points.map((p) => html`<tr class=${['slewing', 'settling', 'solving'].includes(p.state) ? 'lit' : ''}>
            <td>${p.index + 1}</td><td>${fmt(p.az_deg)}°</td><td>${fmt(p.el_deg)}°</td><td>${p.flipped ? 'flipped' : 'normal'}</td>
            <td class=${p.state === 'failed' ? 'warn' : p.state === 'solved' ? '' : 'muted'}>${p.state}</td>
            <td class="num">${p.state === 'solved' ? p.matched : ''}</td>
            <td class="num">${p.state === 'solved' ? fmt(hyp(p.before_xel_arcsec, p.before_el_arcsec), 0) + '″' : ''}</td>
            <td class="num">${p.state === 'solved' && fit ? html`<span class=${p.rejected ? 'warn' : ''}>${fmt(hyp(p.after_xel_arcsec, p.after_el_arcsec))}″${p.rejected ? ' (outlier)' : ''}</span>` : ''}</td>
            <td class="small" style="font-family:inherit;max-width:320px">${p.state === 'failed' ? p.message : ''}</td>
          </tr>`)}
        </tbody></table></div>
    </section>
    </div>
    </div>
  </div>`;
}
