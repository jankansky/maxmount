// SPDX-License-Identifier: GPL-3.0-or-later
// maxmount GUI: Track screen and app shell (Drives screen in drives.js). Talks only to the gateway (mount-svc gui) over a WebSocket.
import { html, render, useState, useEffect, useRef, useCallback, useMemo } from './vendor/preact-htm-standalone.module.js';
import { Select } from './lib/ui.js';
import { connect } from './lib/ws.js';
import { drawStrip, drawSky, drawMap, cssVar } from './lib/charts.js';
import { pad, dms, hms, utc, dur, loadPref, savePref } from './lib/util.js';
import { DrivesScreen } from './drives.js';


const STATE = {
  SERVICE_STATE_IDLE: 'idle', SERVICE_STATE_WAITING: 'waiting for pass', SERVICE_STATE_SLEWING: 'slewing',
  SERVICE_STATE_TRACKING: 'tracking', SERVICE_STATE_ERROR: 'error', SERVICE_STATE_UNKNOWN: '—',
};
function tleAgeDays(line1) {
  if (!line1) return null;
  const yy = parseInt(line1.slice(18, 20), 10), doy = parseFloat(line1.slice(20, 32));
  const year = yy < 57 ? 2000 + yy : 1900 + yy;
  const epoch = Date.UTC(year, 0, 1) + (doy - 1) * 86400000;
  return (Date.now() - epoch) / 86400000;
}
function parseSexagesimal(text, hours) {
  const t = text.trim();
  if (!t) return NaN;
  const sign = t.startsWith('-') || t.startsWith('−') ? -1 : 1;
  const parts = t.replace(/^[+\-−]/, '').split(/[:\sdhms°′″'"]+/).filter(Boolean).map(Number);
  if (parts.some((p) => !Number.isFinite(p))) return NaN;
  const v = parts.reduce((acc, p, i) => acc + p / Math.pow(60, i), 0);
  return sign * v * (hours ? 15 : 1);
}

// ---- small components -----------------------------------------------------------------------
function Chip({ cls = '', children, title }) {
  return html`<span class=${'chip mono ' + cls} title=${title}>${children}</span>`;
}

const NUDGE_OPTS = [[10, '10″'], [60, '1′'], [600, '10′']];
const OBJECT_OPTS = [['watch', 'watch list'], ['all', 'all in group']];
const HOURS_OPTS = [6, 12, 24, 48].map((h) => [h, `${h} h`]);
const MINEL_OPTS = [0, 20, 30, 45, 60].map((d) => [d, `${d}°`]);
// Angular-size filter: steps for the narrow-field camera, labelled in its pixels.
const SIZE_STEPS = [5, 10, 20, 40];
const sizeOpts = (asp) => [['any', 'any'], ...SIZE_STEPS.map((a) => [a, `≥ ${a}″${asp ? ` (~${Math.round(a / asp)} px)` : ''}`])];
const MAG_OPTS = [['any', 'any'], ...[0, 1, 2, 3, 4, 5, 6].map((m) => [m, `≤ ${m}`])];
// Row fields computed by the gateway's pass search; kept when a row is planned on selection.
const SEEN_KEYS = ['sunlit_frac', 'sun_el_deg', 'peak_mag', 'peak_mag_ns', 'mag_estimated', 'std_mag', 'size_m',
  'size_estimated', 'ang_size_arcsec', 'min_range_km'];
const fmtMag = (p) => (p.peak_mag == null ? null : `${p.mag_estimated ? '≈' : ''}${Number(p.peak_mag).toFixed(1)}`);
const fmtAng = (a) => (a >= 60 ? `${(a / 60).toFixed(1)}′` : `${a < 10 ? Number(a).toFixed(1) : Math.round(a)}″`);
const fmtSize = (p) => (p.ang_size_arcsec == null ? null : `${p.size_estimated ? '≈' : ''}${fmtAng(p.ang_size_arcsec)}`);
const LIGHT_OPTS = [['any', 'any'], ['sunlit', 'sunlit'], ['dark_sky', 'sunlit, dark sky']];
const SCREENS = [['passes', 'Passes'], ['track', 'Track'], ['drives', 'Drives']];
const screenFromHash = () => { const h = location.hash.slice(1); return SCREENS.some(([id]) => id === h) ? h : 'track'; };
const LATER = ['Objects', 'Sequences', 'Recordings', 'Calibration', 'Pointing model', 'Setup'];

// The active narrow-field imaging profile (system file `imaging:`), from hello / 'imaging' messages.
const activeProfile = (im) => im?.profiles?.find((p) => p.key === im.active) || null;
const profileTitle = (p) => p && [
  `${p.name}${p.camera ? ` · ${p.camera}` : ''}`,
  `${p.focal_mm} mm f/${p.f_ratio} · ${p.pixel_um} µm pixels · ${p.arcsec_per_px.toFixed(3)}″/px${p.fov_arcmin ? ` · field ${p.fov_arcmin.join('′ × ')}′` : ''}`,
  `diffraction λ/D ${p.diffraction_arcsec}″ (550 nm); pixel / Nyquist pixel: ${p.sampling_550} at 550 nm, ${p.sampling_685} at 685 nm (≤ 1 samples the diffraction limit)`,
  p.note,
].filter(Boolean).join('\n');

function ImagingPicker({ imaging, send }) {
  const p = activeProfile(imaging);
  if (!p) return null;
  const opts = imaging.profiles.map((x) => [x.key, `${x.name} · ${x.arcsec_per_px.toFixed(2)}″/px`]);
  return html`<label class="imaging" title=${profileTitle(p)}>
    <span class="small">Imaging</span>
    <${Select} aria-label="Imaging profile" value=${imaging.active} options=${opts} onChange=${(k) => send('set_imaging', { profile: k })} />
    <span class=${'chip ' + (p.sampling_550 <= 1.05 ? 'ok' : '')} title="pixel scale; green: samples the diffraction limit at 550 nm">${p.arcsec_per_px.toFixed(2)}″/px</span>
  </label>`;
}

function Header({ theme, setTheme, line, status, tel, hello, connected, screen, setScreen, imaging, send }) {
  const clk = status ? Number(status.clock_error_us) : -1;
  const maxClk = hello?.max_clock_error_us ?? 10;
  const clkText = clk < 0 ? 'clock ?' : clk < 1000 ? `clock ±${clk.toFixed(0)} µs` : `clock ±${(clk / 1000).toFixed(1)} ms`;
  const sunEl = tel?.sun?.el_deg;
  return html`<header>
    <span class="brand">maxmount</span>
    <nav aria-label="Sections">
      ${SCREENS.map(([id, label]) => html`<button class=${screen === id ? 'active' : ''} aria-current=${screen === id ? 'page' : undefined} onClick=${() => setScreen(id)}>${label}</button>`)}
      ${LATER.map((n) => html`<button disabled title="not built yet">${n}</button>`)}
    </nav>
    <div class="statusline mono" title=${line}>${line || '…'}</div>
    <div class="chips">
      ${!connected && html`<${Chip} cls="bad">gateway offline<//>`}
      <${Chip} cls=${clk < 0 || clk > maxClk ? 'warn' : ''} title=${status?.clock_source || ''}>${clkText}<//>
      <${Chip} title="telescope position source">${hello?.site ? (status?.site_source || hello.site.source) : 'no site'}<//>
      <${Chip} cls=${tel && tel.bus.lost > 0 ? 'warn' : ''}>bus ${tel ? tel.bus.lost : '—'} lost<//>
      <${Chip} cls=${sunEl > -6 ? 'warn' : ''}>Sun ${Number.isFinite(sunEl) ? sunEl.toFixed(0) + '°' : '—'}<//>
    </div>
    <${ImagingPicker} imaging=${imaging} send=${send} />
    <div class="seg" role="group" aria-label="Display theme">
      <button class=${theme === 'dark' ? 'on' : ''} aria-pressed=${theme === 'dark'} onClick=${() => setTheme('dark')}>Dark</button>
      <button class=${theme === 'red' ? 'on' : ''} aria-pressed=${theme === 'red'} onClick=${() => setTheme('red')}>Red</button>
    </div>
  </header>`;
}

function MountPanel({ tel, act }) {
  const sky = tel?.sky || {};
  const axisState = (name) => tel?.axes.find((a) => a.axis === name)?.state;
  const led = (name, label) => {
    const s = axisState(name);
    const cls = s === 'OPERATION_ENABLED' ? 'on' : s === 'FAULT' ? 'fault' : '';
    return html`<span class=${'led ' + cls} title=${s || 'no data'}><i></i>${label}</span>`;
  };
  const utcNow = tel ? new Date(Number(tel.t_ns) / 1e6).toISOString().slice(11, 21) : '—';
  return html`<section class="panel" aria-label="Mount">
    <h2>Mount position</h2>
    <div class="lcd mono">
      <span class="k">Az</span><span class="v">${dms(sky.az_deg, false, 3)}</span>
      <span class="k">El</span><span class="v">${dms(sky.el_deg)}</span>
      <span class="k">RA</span><span class="v">${hms(sky.ra_h)}</span>
      <span class="k">Dec</span><span class="v">${dms(sky.dec_deg)}</span>
      <span class="k">LST</span><span class="v">${hms(sky.lst_h, 0)}</span>
      <span class="k">UTC</span><span class="v">${utcNow}</span>
    </div>
    <div class="leds">
      ${led('az', 'az')} ${led('el', 'el')}
      <span class=${'led ' + (tel?.enabled ? 'on' : '')}><i></i>enabled</span>
      <span class=${'led ' + (tel?.tripped ? 'fault' : '')}><i></i>${tel?.tripped ? 'TRIPPED' : 'no trip'}</span>
    </div>
    <button class="btn stop" onClick=${() => act('stop')}>STOP</button>
    <div class="grid2">
      <button class="btn" onClick=${() => act('hold')}>Hold</button>
      <button class="btn" onClick=${() => act('park')}>Park</button>
    </div>
  </section>`;
}

function TargetCard({ status, openDialog, now }) {
  const st = status || {};
  const plan = st.plan || {};
  const left = plan.end_ns ? (Number(plan.end_ns) / 1e6 - now) / 1000 : NaN;
  return html`<section class="panel" aria-label="Target">
    <h2>Target</h2>
    <div class="lcd mono" style="display:block"><span style="color:var(--lcd-text);font-size:14px">${st.target_name || 'none'}</span></div>
    <div class="kv mono">
      <span class="k">State</span><span class="v">${STATE[st.state] || '—'}</span>
      <span class="k">Plan</span><span class="v">${plan.mode || '—'}</span>
      <span class="k">Ends in</span><span class="v">${st.target_name ? dur(left) : '—'}</span>
      ${st.tle_line1 && html`<span class="k">Light</span><span class="v">${st.sunlit ? 'sunlit' : 'eclipsed'}</span>`}
    </div>
    <button class="btn" onClick=${openDialog}>Choose target…</button>
  </section>`;
}

function TargetDialog({ dialogRef, send }) {
  const [tab, setTab] = useState('radec');
  const [reply, setReply] = useState(null);
  const [f, setF] = useState({ ra: '', dec: '', minutes: '0', az: '', el: '', hold: '0' });
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const submit = async (e) => {
    e.preventDefault();
    let cmd;
    if (tab === 'radec') {
      const ra = parseSexagesimal(f.ra, true), dec = parseSexagesimal(f.dec, false);
      if (!Number.isFinite(ra) || !Number.isFinite(dec)) { setReply({ ok: false, message: 'RA as hh:mm:ss, Dec as ±dd:mm:ss' }); return; }
      cmd = { track_radec: { ra_deg: ra, dec_deg: dec, duration_s: Number(f.minutes || 0) * 60 } };
    } else {
      const az = Number(f.az), el = Number(f.el);
      if (!Number.isFinite(az) || !Number.isFinite(el) || f.az === '' || f.el === '') { setReply({ ok: false, message: 'az and el in degrees' }); return; }
      cmd = { goto_altaz: { az_deg: az, el_deg: el, hold_s: Number(f.hold || 0) } };
    }
    setReply({ ok: true, message: 'planning…' });
    const r = await send('target', cmd);
    setReply(r);
    if (r.ok) setTimeout(() => dialogRef.current?.close(), 800);
  };
  return html`<dialog ref=${dialogRef} aria-label="Choose target">
    <form onSubmit=${submit}>
      <div class="tabs" role="tablist">
        <button type="button" role="tab" class=${tab === 'radec' ? 'on' : ''} onClick=${() => setTab('radec')}>Star / RA-Dec</button>
        <button type="button" role="tab" class=${tab === 'altaz' ? 'on' : ''} onClick=${() => setTab('altaz')}>Az / El</button>
      </div>
      ${tab === 'radec' ? html`
        <label>RA (J2000, hh:mm:ss)<input value=${f.ra} onInput=${set('ra')} placeholder="06:45:08.9" /></label>
        <label>Dec (J2000, ±dd:mm:ss)<input value=${f.dec} onInput=${set('dec')} placeholder="-16:42:58" /></label>
        <label>Track for (minutes, 0 = until it sets)<input value=${f.minutes} onInput=${set('minutes')} /></label>` : html`
        <label>Azimuth (deg, north = 0, east = 90)<input value=${f.az} onInput=${set('az')} placeholder="180" /></label>
        <label>Elevation (deg)<input value=${f.el} onInput=${set('el')} placeholder="45" /></label>
        <label>Hold for (s, 0 = until the next command)<input value=${f.hold} onInput=${set('hold')} /></label>`}
      <span class="small">Satellites: use Track in the passes table.</span>
      ${reply && html`<div class=${'reply ' + (reply.ok ? '' : 'bad')}>${reply.message}</div>`}
      <div class="grid2">
        <button type="button" class="btn" onClick=${() => dialogRef.current?.close()}>Close</button>
        <button type="submit" class="btn primary">Go</button>
      </div>
    </form>
  </dialog>`;
}

function CameraPanel({ title, info, square }) {
  return html`<section class=${'panel camera ' + (square ? '' : 'wide')} aria-label=${title}>
    <div class="head"><h2>${title}</h2><span class="small mono">${info}</span></div>
    <div class=${'camview ' + (square ? 'square' : '')}>
      ${square && html`<svg viewBox="0 0 100 100" aria-hidden="true">
        <line x1="50" y1="40" x2="50" y2="60" stroke="var(--accent)" stroke-width="0.4"></line>
        <line x1="40" y1="50" x2="60" y2="50" stroke="var(--accent)" stroke-width="0.4"></line>
        <circle cx="50" cy="50" r="5" fill="none" stroke="var(--accent)" stroke-width="0.4"></circle></svg>`}
      <span>Camera service not running yet.<br/>Live frames, overlays and click-to-point arrive with camera-svc.</span>
    </div>
  </section>`;
}

const STEPS = { timeFine: 0.1, timeCoarse: 1.0, perpFine: 0.25 / 60, perpCoarse: 5 / 60 };

function Corrections({ status, send }) {
  const o = status?.offsets || {};
  const cur = {
    time_offset_s: Number(o.time_offset_s || 0), cross_track_deg: Number(o.cross_track_deg || 0),
    az_arcsec: Number(o.az_arcsec || 0), el_arcsec: Number(o.el_arcsec || 0),
  };
  const [nudge, setNudge] = useState(10);
  const apply = (patch) => send('target', { set_offsets: { ...cur, ...patch } });
  const isSat = !!status?.tle_line1;
  return html`<section class="panel corr" aria-label="Corrections">
    <div class="group">
      <span class="small">Timing along the path · ${STEPS.timeFine} s / ${STEPS.timeCoarse} s${isSat ? '' : ' (satellites)'}</span>
      <div class="pad5">
        <button aria-label="Timing back coarse" onClick=${() => apply({ time_offset_s: cur.time_offset_s - STEPS.timeCoarse })}>⏪</button>
        <button aria-label="Timing back fine" onClick=${() => apply({ time_offset_s: cur.time_offset_s - STEPS.timeFine })}>◀</button>
        <button class="zero" aria-label="Zero timing" onClick=${() => apply({ time_offset_s: 0 })}>0</button>
        <button aria-label="Timing forward fine" onClick=${() => apply({ time_offset_s: cur.time_offset_s + STEPS.timeFine })}>▶</button>
        <button aria-label="Timing forward coarse" onClick=${() => apply({ time_offset_s: cur.time_offset_s + STEPS.timeCoarse })}>⏩</button>
      </div>
      <span class="mono">${cur.time_offset_s >= 0 ? '+' : ''}${cur.time_offset_s.toFixed(2)} s</span>
    </div>
    <div class="group">
      <span class="small">Perpendicular · 0.25′ / 5′${isSat ? '' : ' (satellites)'}</span>
      <div class="pad5">
        <button aria-label="Perpendicular minus coarse" onClick=${() => apply({ cross_track_deg: cur.cross_track_deg - STEPS.perpCoarse })}>⇊</button>
        <button aria-label="Perpendicular minus fine" onClick=${() => apply({ cross_track_deg: cur.cross_track_deg - STEPS.perpFine })}>↓</button>
        <button class="zero" aria-label="Zero perpendicular" onClick=${() => apply({ cross_track_deg: 0 })}>0</button>
        <button aria-label="Perpendicular plus fine" onClick=${() => apply({ cross_track_deg: cur.cross_track_deg + STEPS.perpFine })}>↑</button>
        <button aria-label="Perpendicular plus coarse" onClick=${() => apply({ cross_track_deg: cur.cross_track_deg + STEPS.perpCoarse })}>⇈</button>
      </div>
      <span class="mono">${(cur.cross_track_deg * 60).toFixed(2)}′</span>
    </div>
    <div class="group" style="flex:0 0 auto;align-items:center">
      <span class="small">Nudge az / el</span>
      <div class="pad3">
        <span></span><button aria-label="Elevation up" onClick=${() => apply({ el_arcsec: cur.el_arcsec + nudge })}>↑</button><span></span>
        <button aria-label="Azimuth minus" onClick=${() => apply({ az_arcsec: cur.az_arcsec - nudge })}>←</button>
        <button class="zero" aria-label="Zero nudge" onClick=${() => apply({ az_arcsec: 0, el_arcsec: 0 })}>0</button>
        <button aria-label="Azimuth plus" onClick=${() => apply({ az_arcsec: cur.az_arcsec + nudge })}>→</button>
        <span></span><button aria-label="Elevation down" onClick=${() => apply({ el_arcsec: cur.el_arcsec - nudge })}>↓</button><span></span>
      </div>
      <${Select} aria-label="Nudge step" value=${nudge} onChange=${setNudge} options=${NUDGE_OPTS} />
      <span class="mono small">az ${cur.az_arcsec.toFixed(0)}″ · el ${cur.el_arcsec.toFixed(0)}″</span>
    </div>
    <div class="group">
      <div class="seg" role="group" aria-label="Camera loop">
        <button disabled title="needs the camera service">Auto (camera loop)</button>
        <button class="on" aria-pressed="true">Manual</button>
      </div>
      <div class="grid2 mono">
        <div class="readout"><span>Separation</span><span>—</span></div>
        <div class="readout"><span>Tracking RMS</span><span>—</span></div>
      </div>
      <span class="small">Separation and RMS come from the camera service.</span>
    </div>
  </section>`;
}

// Satellites need an accurate clock: mount-svc refuses a pass when chrony's error bound is
// above time.max_clock_error_us, unless the operator explicitly overrides it.
function useClockGate(status, hello) {
  const clk = status ? Number(status.clock_error_us) : -1;
  const maxClk = hello?.max_clock_error_us ?? 10;
  const clockBad = clk < 0 || clk > maxClk;
  const [override, setOverride] = useState(false);
  useEffect(() => { if (!clockBad) setOverride(false); }, [clockBad]);
  const clkMs = clk / 1000;
  const banner = clockBad && html`<div class="clockwarn" role="status">
    <span><b>Clock not good enough for satellites:</b> ${clk < 0 ? 'error unknown (chrony not answering)'
      : `chrony's error bound is ±${clkMs >= 1 ? clkMs.toFixed(1) + ' ms' : clk.toFixed(0) + ' µs'}, limit ${maxClk} µs`}.
      ${clk > 0 ? `At 1°/s that is up to ${(clkMs * 3.6).toFixed(clkMs * 3.6 < 10 ? 1 : 0)}″ along the track; the timing correction can take it out.` : ''}</span>
    <label class="arm"><input type="checkbox" checked=${override} onChange=${(e) => setOverride(e.target.checked)} />
      Track anyway</label>
  </div>`;
  return { allow: clockBad && override, banner };
}

function ListTabs({ tab, setTab }) {
  const t = (id, label) => html`<button role="tab" class=${tab === id ? 'on' : ''} aria-selected=${tab === id} onClick=${() => setTab(id)}>${label}</button>`;
  return html`<div class="tabs" role="tablist">
    ${t('passes', 'Upcoming passes')}${t('conj', 'Conjunctions (SOCRATES)')}${t('air', 'Aircraft (ADS-B)')}
  </div>`;
}

function GoTrack({ tracked, setTracked, status, setScreen }) {
  return tracked && status?.target_name && html`<div class="gotrack" role="status">
    <span><b>${status.target_name}</b>: ${status.message || 'planned'}</span>
    <button class="btn primary" onClick=${() => { setTracked(''); setScreen('track'); }}>Open the Track screen →</button></div>`;
}

function Passes({ hello, send, status, connected, preview, setPreview, updatePreviewRow, setScreen, tab, setTab, imaging }) {
  const [group, setGroup] = useState(loadPref('group', 'stations'));
  const [names, setNames] = useState(loadPref('watch', 'ISS (ZARYA), CSS (TIANHE)'));
  const [hours, setHours] = useState(loadPref('hours', 12));
  const [all, setAll] = useState(loadPref('allgroup', false));     // every object in the group, or the watch list
  const [minEl, setMinEl] = useState(loadPref('minmaxel', 0));     // hide passes culminating lower than this
  const [lighting, setLighting] = useState(loadPref('lighting', 'any'));
  const [maxMag, setMaxMag] = useState(loadPref('maxmag', 'any'));
  const [minSize, setMinSize] = useState(loadPref('minsize', 'any'));
  const profile = activeProfile(imaging);
  const asp = profile?.arcsec_per_px;
  const [rows, setRows] = useState([]);
  const [groupNames, setGroupNames] = useState([]);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);
  const refresh = async () => {
    savePref('group', group); savePref('watch', names); savePref('hours', hours); savePref('allgroup', all); savePref('minmaxel', minEl); savePref('lighting', lighting); savePref('maxmag', maxMag); savePref('minsize', minSize);
    const id = ++seq.current;
    setBusy(true); setNote(all ? `searching every object in '${group}'…` : 'computing passes…');
    const r = await send('passes', { group, all, names: names.split(',').map((s) => s.trim()).filter(Boolean),
      hours: Number(hours), min_max_el_deg: Number(minEl), lighting, max_mag: maxMag, min_size_arcsec: minSize });
    if (id !== seq.current) return; // a newer search was started
    setBusy(false); setNote(r.message);
    setRows(r.ok ? r.data.passes : []);
    if (r.ok) setGroupNames(r.data.names || []);
  };
  // Search again whenever a selector changes (the watch list: on Enter or Refresh).
  useEffect(() => { if (connected) refresh(); }, [connected, group, all, hours, minEl, lighting, maxMag, minSize]);
  const nameOpts = useMemo(() => groupNames.map((n) => html`<option value=${n} />`), [groupNames]);
  const addName = (n) => {
    const list = names.split(',').map((x) => x.trim()).filter(Boolean);
    if (n && !list.includes(n)) setNames([...list, n].join(', '));
  };
  // Rows from an all-in-group search are planned on demand (planning takes ~0.25 s a pass).
  const planRow = async (p) => {
    const r = await send('pass_plan', { name: p.name, line1: p.line1, line2: p.line2, rise_ns: p.rise_ns, set_ns: p.set_ns });
    if (!r.ok) return;
    // Keep what the search gave the row (lighting, merged modules) and add the plans.
    const row = { ...p, ...r.data.pass, rise_ns: p.rise_ns, also: p.also, ...Object.fromEntries(SEEN_KEYS.map((k) => [k, p[k]])) };
    setRows((rs) => rs.map((x) => (passKey(x) === passKey(p) ? row : x)));
    updatePreviewRow(row);
  };
  const clock = useClockGate(status, hello);
  const track = async (p) => {
    setNote(`planning ${p.name}…`);
    const r = await send('target', { track_tle: { name: p.name, line1: p.line1, line2: p.line2, rise_ns: p.rise_ns },
      allow_clock_error: clock.allow });
    setNote(r.message);
    if (r.ok) { setPreview(null); setTracked(p.name); } // the live track takes over; keep the preview if refused
  };
  const [tracked, setTracked] = useState('');
  return html`<section class="panel" aria-label="Passes" hidden=${tab !== 'passes'}>
    <${ListTabs} tab=${tab} setTab=${setTab} />
    <div class="toolbar">
      <label>CelesTrak group <${Select} value=${group} onChange=${setGroup}
        options=${(hello?.tle_groups || ['stations']).map((g) => [g, g])} /></label>
      <label>Objects <${Select} value=${all ? 'all' : 'watch'} onChange=${(v) => setAll(v === 'all')} options=${OBJECT_OPTS} /></label>
      <label>Next <${Select} value=${hours} onChange=${setHours} options=${HOURS_OPTS} /></label>
      <label>Max el ≥ <${Select} value=${minEl} onChange=${setMinEl} options=${MINEL_OPTS} /></label>
      <label>Lighting <${Select} value=${lighting} onChange=${setLighting} options=${LIGHT_OPTS}
        title="sunlit: the satellite is in sunlight for part of the pass; dark sky: and the Sun is below −6° at culmination" /></label>
      <label>Mag <${Select} value=${maxMag} onChange=${setMaxMag} options=${MAG_OPTS}
        title="keep passes predicted at least this bright (passes without a magnitude are hidden)" /></label>
      <label>Size <${Select} value=${minSize} onChange=${setMinSize} options=${sizeOpts(asp)}
        title=${`largest angular size at the closest point, at least this (passes without a size are hidden)${profile ? `; pixels for ${profile.name} (${asp.toFixed(2)}″/px); seeing is usually 1.5–3″` : ''}`} /></label>
      <button class="btn" onClick=${refresh} disabled=${busy || !connected}>${busy ? 'Working…' : 'Refresh'}</button>
    </div>
    ${!all && html`<div class="toolbar">
      <label style="flex:1 1 260px;display:flex;gap:6px;align-items:center">Watch
        <input style="flex:1" value=${names} onInput=${(e) => setNames(e.target.value)} onKeyDown=${(e) => e.key === 'Enter' && refresh()}
          placeholder="e.g. ISS, CSS, 25544"
          title="names, parts of names or catalog numbers, comma separated; Enter to search" /></label>
      <label style="display:flex;gap:6px;align-items:center">Add
        <input list="tle-names" placeholder=${`object in '${group}'…`} aria-label="Add an object to the watch list"
          onChange=${(e) => { addName(e.target.value.trim()); e.target.value = ''; }} /></label>
      <datalist id="tle-names">${nameOpts}</datalist>
    </div>`}
    ${clock.banner}
    <${GoTrack} tracked=${tracked} setTracked=${setTracked} status=${status} setScreen=${setScreen} />
    <span class="small">${note}${rows.length ? ' · click a pass to preview it' + (all ? ' and plan it' : '') : ''}</span>
    <div class="tablewrap"><table>
      <thead><tr><th>Object</th><th>AOS (UTC)</th><th>Dur</th><th>Max el</th><th title="share of the pass with the satellite sunlit; hover a value for the Sun's elevation">Sunlit</th><th title="brightest predicted visual magnitude (≈: estimated from radar size)">Mag</th><th title="largest angular size, at the closest point (≈: estimated from radar size)">Size</th><th>Plan</th><th></th></tr></thead>
      <tbody class="mono">
        ${!rows.length && html`<tr class="empty"><td colspan="9">${busy ? 'Searching…' : html`No passes. ${note}
          <br /><span class="muted">${all ? 'Try a longer window or a lower Max el.'
            : (lighting !== 'any' ? 'Try Lighting: any, or a longer window. ' : '') + 'Watch takes names or parts of names from the group (e.g. ISS, HUBBLE, NOAA 19) or catalog numbers (25544), comma separated.'}</span>`}</td></tr>`}
        ${rows.map((p) => {
          const chosen = p.chosen || {};
          const current = status?.tle_line1 && status.tle_line1 === p.line1 && status?.plan?.start_ns && Number(status.plan.start_ns) >= Number(p.rise_ns) - 5e9 && Number(status.plan.start_ns) <= Number(p.set_ns);
          const plan = p.planned === false ? html`<span class="muted" title="select the pass to plan it">—</span>`
            : chosen.mode ? (chosen.mode === 'over_the_top' ? html`<span class="warn">over-top ${Number(chosen.max_error_deg).toFixed(1)}°</span>` : chosen.mode)
            : html`<span class="warn" title=${(p.plans || []).map((x) => x.mode + ': ' + (x.problems || []).join(', ')).join('\n')}>no plan fits</span>`;
          const key = passKey(p), sel = preview?.key === key;
          const pick = () => { setPreview(sel ? null : p); if (!sel && p.planned === false) planRow(p); };
          return html`<tr class=${(current ? 'current ' : '') + (sel ? 'selected' : '')} tabindex="0" aria-selected=${sel}
            title=${sel ? 'click to clear the preview' : 'click to preview this pass'}
            onClick=${pick} onKeyDown=${(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } }}>
            <td style="font-family:inherit">${p.name}${p.also?.length ? html` <span class="muted" title=${'same pass (docked): ' + p.also.join(', ')}>+${p.also.length}</span>` : ''}</td>
            <td>${utc(p.rise_ns)}</td>
            <td>${dur((Number(p.set_ns) - Number(p.rise_ns)) / 1e9)}</td>
            <td>${Number(p.max_el_deg).toFixed(1)}°</td>
            <td title=${p.sun_el_deg != null ? `Sun ${Number(p.sun_el_deg).toFixed(0)}° at max el` : ''}
              class=${p.sun_el_deg != null && p.sun_el_deg > -6 ? 'muted' : ''}>${p.sunlit_frac == null ? '?' : p.sunlit_frac > 0 ? `${Math.round(p.sunlit_frac * 100)}%${p.sun_el_deg > -6 ? ' ☀' : ''}` : '—'}</td>
            <td title=${p.std_mag != null ? `standard mag ${p.std_mag}${p.mag_estimated ? ' (from radar size)' : ''}` : 'no magnitude data'}>${fmtMag(p) ?? '—'}</td>
            <td title=${p.size_m ? `${p.size_m.join(' × ')} m at ${Math.round(p.min_range_km)} km${asp ? ` · ~${Math.round(p.ang_size_arcsec / asp)} px (${profile.name})` : ''}` : 'no size data'}>${fmtSize(p) ?? '—'}</td>
            <td>${plan}</td>
            <td>${current ? 'active' : html`<button class="btn" style="min-height:28px" disabled=${p.planned !== false && !chosen.mode} onClick=${(e) => { e.stopPropagation(); track(p); }}>Track</button>`}</td>
          </tr>`;
        })}
      </tbody>
    </table></div>
  </section>`;
}

const passKey = (p) => `${p.name}|${p.rise_ns}`;
const conjKey = (c) => `conj|${c.cat1}|${c.cat2}|${c.tca_ns}`;
const CONJ_HOURS = [[12, '12 h'], [24, '24 h'], [48, '48 h'], [96, '4 d'], [168, '7 d']];
const CONJ_RANGE = [0.1, 0.2, 0.5, 1, 2, 5].map((r) => [r, `${r} km`]);
// SATCAT operational status in brackets after a SOCRATES name, e.g. 'STARLINK-1 [+]'.
const STATUS = { '+': 'operational', '-': 'not operational', P: 'partially operational', B: 'backup', S: 'spare',
  X: 'extended mission', D: 'decayed', '?': 'unknown status' };
const conjName = (n) => {
  const m = n.match(/^(.*?)\s*\[(.)\]$/);
  return m ? html`${m[1]} <span class="muted" title=${STATUS[m[2]] || ''}>[${m[2]}]</span>` : n;
};

const AIR_RANGE = [25, 50, 100, 200].map((r) => [r, `${r} km`]);
const AIR_SHOW = [['up', 'above the mask now'], ['soon', 'up now or within 15 min'], ['all', 'any elevation']];
// Time to closest approach; 0 means it is already flying away.
const fmtIn = (s) => { const t = Math.round(s); return t <= 1 ? 'receding' : `in ${Math.floor(t / 60)}:${pad(t % 60)}`; };
const feet = (m) => `${Math.round(m / 0.3048).toLocaleString('en-US')} ft`;

// Aircraft from the local ADS-B receiver, refreshed every 2 s while the tab is shown. ask: a
// request that does not write to the status line.
function Aircraft({ ask, send, status, connected, active, setAirView, setScreen, tab, setTab, mask }) {
  const [range, setRange] = useState(loadPref('air_range', 100));
  const [show, setShow] = useState(loadPref('air_show', 'soon'));
  const [sel, setSel] = useState('');
  const [rows, setRows] = useState([]);
  const [ok, setOk] = useState(true);
  const [note, setNote] = useState('');
  const [tracked, setTracked] = useState('');
  const [kick, setKick] = useState(0);
  const selRef = useRef(sel);
  selRef.current = sel;
  useEffect(() => {
    if (!active || !connected) return undefined;
    savePref('air_range', range); savePref('air_show', show);
    let live = true;
    const poll = async () => {
      const r = await ask('aircraft', { max_range_km: Number(range), show, selected: selRef.current });
      if (!live) return;
      setOk(r.ok); setNote(r.message);
      const d = r.data || { aircraft: [] };
      setRows(d.aircraft);
      setAirView({ ...d, sel: selRef.current, range: Number(range) });
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => { live = false; clearInterval(id); };
  }, [active, connected, range, show, kick]);
  const pick = (icao) => { setSel((s) => (s === icao ? '' : icao)); setKick((k) => k + 1); };
  const track = async (a) => {
    const r = await send('target', { track_aircraft: { icao: a.icao } });
    setNote(r.message);
    if (r.ok) setTracked(a.label);
  };
  const trackedIcao = status?.target_kind === 'aircraft' ? status.aircraft_icao : '';
  return html`<section class="panel" aria-label="Aircraft" hidden=${tab !== 'air'}>
    <${ListTabs} tab=${tab} setTab=${setTab} />
    <div class="toolbar">
      <label>Within <${Select} aria-label="Aircraft range" value=${range} onChange=${setRange} options=${AIR_RANGE} /></label>
      <label>Show <${Select} aria-label="Which aircraft" value=${show} onChange=${setShow} options=${AIR_SHOW} /></label>
      <span class="small">updates every 2 s</span>
    </div>
    <${GoTrack} tracked=${tracked} setTracked=${setTracked} status=${status} setScreen=${setScreen} />
    <span class="small">${note}${rows.length ? ' · click an aircraft to see its trail and predicted path' : ''}</span>
    ${!ok && html`<p class="small muted">The Aircraft tab reads a local 1090 MHz receiver (an RTL-SDR with readsb or dump1090;
      see <code>adsb.source</code> in the system file). To try it without one, run
      <code>mount-svc --system …/system.yaml adsb-sim</code> on the Pi.</p>`}
    <div class="tablewrap"><table>
      <thead><tr><th>Aircraft</th><th>Altitude</th><th>Range</th><th>El / az</th><th title="angular rate across the sky now">Rate</th>
        <th title="closest approach on its current track (straight line, next 15 min)">Closest</th><th title="highest elevation on its current track">Max el</th>
        <th title="seconds since the last position">Age</th><th></th></tr></thead>
      <tbody class="mono">
        ${!rows.length && html`<tr class="empty"><td colspan="9">${ok ? 'No aircraft match. Try a larger range or Show: any elevation.' : 'No ADS-B data.'}</td></tr>`}
        ${rows.map((a) => {
          const isSel = sel === a.icao, below = a.el_deg < mask;
          return html`<tr class=${(isSel ? 'selected ' : '') + (trackedIcao === a.icao ? 'current' : '')} tabindex="0" aria-selected=${isSel}
            onClick=${() => pick(a.icao)} onKeyDown=${(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(a.icao); } }}>
            <td style="font-family:inherit">${a.label}${a.type ? html` <span class="muted">${a.type}</span>` : ''}</td>
            <td title=${`${Math.round(a.alt_m)} m (${a.alt_source === 'geom' ? 'GNSS' : 'pressure'})`}>${feet(a.alt_m)}${a.alt_source === 'baro' ? '*' : ''}</td>
            <td>${Number(a.range_km).toFixed(1)} km</td>
            <td class=${below ? 'muted' : ''}>${Math.round(a.el_deg)}° / ${Math.round(a.az_deg)}°</td>
            <td>${Number(a.rate_deg_s).toFixed(2)}°/s</td>
            <td>${fmtIn(a.cpa_in_s)} · ${Number(a.cpa_km).toFixed(1)} km</td>
            <td class=${a.max_el_deg < mask ? 'muted' : ''}>${Math.round(a.max_el_deg)}°</td>
            <td class=${a.age_s > 5 ? 'warn' : ''}>${Number(a.age_s).toFixed(0)}</td>
            <td>${trackedIcao === a.icao ? 'active' : html`<button class="btn" style="min-height:28px" disabled=${a.max_el_deg < mask}
              onClick=${(e) => { e.stopPropagation(); track(a); }}>Track</button>`}</td>
          </tr>`;
        })}
      </tbody>
    </table></div>
    <span class="small">* pressure altitude (no GNSS height): can be a few hundred feet off, ~1° at 10 km.</span>
  </section>`;
}

function AircraftDetail({ air }) {
  const a = air?.sel && air.aircraft.find((x) => x.icao === air.sel);
  if (!a) {
    return html`<section class="panel" aria-label="Aircraft details"><div class="head"><h2>Aircraft</h2></div>
      <p class="small">Click an aircraft to see its trail and predicted path on the map and sky, and its closest approach.
      Predictions assume it keeps flying straight at its current speed and climb rate.</p></section>`;
  }
  const row = (k, v) => html`<tr><th>${k}</th><td>${v}</td></tr>`;
  return html`<section class="panel" aria-label="Aircraft details">
    <div class="head"><h2>${a.label}</h2><span class="small">${[a.type, a.reg].filter(Boolean).join(' · ')}</span></div>
    <table class="kv mono"><tbody>
      ${row('ICAO', `${a.icao.toUpperCase()}${a.squawk ? ` · squawk ${a.squawk}` : ''}${a.category ? ` · ${a.category}` : ''}`)}
      ${row('Altitude', `${feet(a.alt_m)} (${Math.round(a.alt_m)} m, ${a.alt_source === 'geom' ? 'GNSS' : 'pressure'})`)}
      ${row('Speed', `${Math.round(a.gs_m_s / 0.514444)} kt · track ${Math.round(a.track_deg)}° · ${a.vrate_m_s >= 0 ? '+' : ''}${Math.round(a.vrate_m_s / 0.3048 * 60)} ft/min`)}
      ${row('Now', `${Number(a.range_km).toFixed(1)} km · el ${Number(a.el_deg).toFixed(1)}° az ${Number(a.az_deg).toFixed(1)}° · ${Number(a.rate_deg_s).toFixed(2)}°/s`)}
      ${row('Closest', `${fmtIn(a.cpa_in_s)} · ${Number(a.cpa_km).toFixed(2)} km at el ${a.cpa_el_deg}°`)}
      ${row('Highest', `${a.max_el_deg}° · fastest ${Number(a.max_rate_deg_s).toFixed(2)}°/s`)}
      ${row('Position', `${Number(a.age_s).toFixed(1)} s old`)}
    </tbody></table>
    <p class="small"><b>Track</b> follows each new position (extrapolated along its track) and holds the mount if
      positions stop for longer than <code>adsb.max_age_s</code>. Turns are not predicted.</p>
  </section>`;
}

function Conjunctions({ hello, send, status, connected, preview, setConjPreview, setScreen, tab, setTab }) {
  const [hours, setHours] = useState(loadPref('cj_hours', 48));
  const [maxRange, setMaxRange] = useState(loadPref('cj_range', 1));
  const [lighting, setLighting] = useState(loadPref('cj_light', 'sunlit'));
  const [maxMag, setMaxMag] = useState(loadPref('cj_mag', 'any'));
  const [coorb, setCoorb] = useState(loadPref('cj_coorb', false));
  const [rows, setRows] = useState([]);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [tracked, setTracked] = useState('');
  const clock = useClockGate(status, hello);
  const seq = useRef(0);
  const refresh = async () => {
    savePref('cj_hours', hours); savePref('cj_range', maxRange); savePref('cj_light', lighting); savePref('cj_mag', maxMag); savePref('cj_coorb', coorb);
    const id = ++seq.current;
    setBusy(true); setNote('screening SOCRATES for conjunctions visible from here (the first time downloads ~20 MB)…');
    const r = await send('conjunctions', { hours: Number(hours), max_range_km: Number(maxRange), lighting, max_mag: maxMag, coorbiting: coorb });
    if (id !== seq.current) return;
    setBusy(false); setNote(r.message);
    setRows(r.ok ? r.data.conjunctions : []);
  };
  // Search when the tab is first opened and whenever a selector changes.
  useEffect(() => { if (connected && tab === 'conj') refresh(); }, [connected, tab === 'conj', hours, maxRange, lighting, maxMag, coorb]);
  const track = async (c) => {
    const d = preview?.key === conjKey(c) ? preview : await setConjPreview(c);
    if (!d) return;
    const p = d.row;
    setNote(`planning ${p.name} through the conjunction…`);
    // rise_ns = TCA: mount-svc tracks the pass that contains it.
    const r = await send('target', { track_tle: { name: p.name, line1: p.line1, line2: p.line2, rise_ns: c.tca_ns }, allow_clock_error: clock.allow });
    setNote(r.message);
    if (r.ok) setTracked(p.name);
  };
  const mag = (c) => (c.mag == null ? '—' : `${(c.mag === c.mag1 ? c.mag_estimated1 : c.mag_estimated2) ? '≈' : ''}${Number(c.mag).toFixed(1)}`);
  return html`<section class="panel" aria-label="Conjunctions" hidden=${tab !== 'conj'}>
    <${ListTabs} tab=${tab} setTab=${setTab} />
    <div class="toolbar">
      <label>Next <${Select} value=${hours} onChange=${setHours} options=${CONJ_HOURS} /></label>
      <label>Miss distance ≤ <${Select} value=${maxRange} onChange=${setMaxRange} options=${CONJ_RANGE} /></label>
      <label>Lighting <${Select} value=${lighting} onChange=${setLighting} options=${LIGHT_OPTS}
        title="at the time of closest approach: sunlit, and for dark sky the Sun below −6° at the site" /></label>
      <label>Mag <${Select} value=${maxMag} onChange=${setMaxMag} options=${MAG_OPTS} title="either object predicted at least this bright" /></label>
      <label class="check"><input type="checkbox" checked=${coorb} onChange=${(e) => setCoorb(e.target.checked)} />
        co-orbiting pairs</label>
      <button class="btn" onClick=${refresh} disabled=${busy || !connected}>${busy ? 'Working…' : 'Refresh'}</button>
    </div>
    ${clock.banner}
    <${GoTrack} tracked=${tracked} setTracked=${setTracked} status=${status} setScreen=${setScreen} />
    <span class="small">${note}${rows.length ? ' · click a conjunction to see both objects and how close they look from here' : ''}</span>
    <div class="tablewrap"><table>
      <thead><tr><th>TCA (UTC)</th><th>Objects</th><th title="distance between the two objects at closest approach">Miss</th>
        <th title="relative speed">Speed</th><th>El / az</th><th title="largest separation as seen from the site at TCA (miss distance over range)">Sep ≤</th>
        <th title="brighter object, predicted at TCA">Mag</th><th></th></tr></thead>
      <tbody class="mono">
        ${!rows.length && html`<tr class="empty"><td colspan="8">${busy ? 'Searching…' : html`No conjunctions. ${note}
          <br /><span class="muted">Try a longer window, a larger miss distance or Lighting: any.</span>`}</td></tr>`}
        ${rows.map((c) => {
          const key = conjKey(c), sel = preview?.key === key;
          const pick = () => setConjPreview(sel ? null : c);
          return html`<tr class=${sel ? 'selected' : ''} tabindex="0" aria-selected=${sel} onClick=${pick}
            onKeyDown=${(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } }}>
            <td>${utc(c.tca_ns)}</td>
            <td style="font-family:inherit">${conjName(c.name1)} × ${conjName(c.name2)}</td>
            <td>${c.range_km < 1 ? `${Math.round(c.range_km * 1000)} m` : `${Number(c.range_km).toFixed(2)} km`}</td>
            <td>${Number(c.speed_km_s).toFixed(c.speed_km_s < 1 ? 2 : 1)}</td>
            <td>${Math.round(c.el_deg)}° / ${Math.round(c.az_deg)}°</td>
            <td>${fmtAng(c.sep_max_arcsec)}</td>
            <td>${mag(c)}</td>
            <td><button class="btn" style="min-height:28px" onClick=${(e) => { e.stopPropagation(); track(c); }}>Track</button></td>
          </tr>`;
        })}
      </tbody>
    </table></div>
  </section>`;
}

function SkyPanel({ status, tel, hello, track, land }) {
  const [view, setView] = useState(loadPref('skyview', 'sky'));
  const ref = useRef(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    if (view === 'sky') {
      drawSky(c, {
        maskEl: hello?.tracking?.horizon_min_el_deg ?? 10,
        path: (status?.path || []).map((p) => [Number(p.az_deg), Number(p.el_deg)]),
        target: status?.target_name ? [Number(status.target_az_deg), Number(status.target_el_deg)] : null,
        mount: tel?.sky ? [tel.sky.az_deg, tel.sky.el_deg] : null,
        sun: tel?.sun ? [tel.sun.az_deg, tel.sun.el_deg] : null,
      });
    } else {
      drawMap(c, { land, track: status?.tle_line1 ? track : null });
    }
  });
  const pick = (v) => { setView(v); savePref('skyview', v); };
  return html`<section class="panel">
    <div class="tabs" role="tablist">
      <button role="tab" class=${view === 'sky' ? 'on' : ''} aria-selected=${view === 'sky'} onClick=${() => pick('sky')}>Sky</button>
      <button role="tab" class=${view === 'map' ? 'on' : ''} aria-selected=${view === 'map'} onClick=${() => pick('map')}>Ground track</button>
    </div>
    <canvas ref=${ref} role="img" aria-label=${view === 'sky' ? 'Sky dome: mount, target, planned track, horizon mask' : 'World map: satellite ground track and visibility circle'}></canvas>
    <span class="small">${view === 'sky'
      ? 'North up, east left · dashed: planned track · ring: mount · dot: target'
      : status?.tle_line1 ? 'solid: flown · dashed: next 50 min · circle: visible above the horizon mask' : 'Ground track shows when the target is a satellite (choose one on the Passes screen).'}</span>
  </section>`;
}

// Passes screen: large ground track and sky views of the selected pass (or the live target).
function PassViews({ status, tel, hello, track, land, preview, air }) {
  const mapRef = useRef(null), skyRef = useRef(null);
  useEffect(() => {
    if (air) {
      const sel = air.selected && air.selected.icao === air.sel ? air.selected : null;
      if (mapRef.current) drawMap(mapRef.current, { land, now: new Date(), air: { site: air.site, radius_km: air.range,
        items: air.aircraft.map((a) => ({ lon: a.lon, lat: a.lat, track_deg: a.track_deg, label: a.label, sel: a.icao === air.sel })),
        trail: sel?.trail, path: sel?.path } });
      if (skyRef.current) drawSky(skyRef.current, {
        maskEl: hello?.tracking?.horizon_min_el_deg ?? 10,
        path: (status?.path || []).map((p) => [Number(p.az_deg), Number(p.el_deg)]),
        target: status?.target_name ? [Number(status.target_az_deg), Number(status.target_el_deg)] : null,
        mount: tel?.sky ? [tel.sky.az_deg, tel.sky.el_deg] : null,
        sun: tel?.sun ? [tel.sun.az_deg, tel.sun.el_deg] : null,
        points: air.aircraft.map((a) => ({ az: a.az_deg, el: a.el_deg, label: a.label, sel: a.icao === air.sel })),
        preview: sel?.sky || null, labels: ['', '+10 min'], // the path starts at the aircraft's own dot
      });
      return;
    }
    // A preview shows day and night at the pass's highest point; otherwise now.
    const when = preview ? new Date(Number(preview.row.culmination_ns) / 1e6) : new Date();
    if (mapRef.current) drawMap(mapRef.current, { land, track: preview?.ground || (status?.tle_line1 ? track : null), now: when });
    if (skyRef.current) drawSky(skyRef.current, {
      maskEl: hello?.tracking?.horizon_min_el_deg ?? 10,
      path: (status?.path || []).map((p) => [Number(p.az_deg), Number(p.el_deg)]),
      target: status?.target_name ? [Number(status.target_az_deg), Number(status.target_el_deg)] : null,
      mount: tel?.sky ? [tel.sky.az_deg, tel.sky.el_deg] : null,
      sun: tel?.sun ? [tel.sun.az_deg, tel.sun.el_deg] : null,
      preview: preview?.sky || null,
      preview2: preview?.sky2 || null,
      mark: preview?.tca_azel || null,
    });
  });
  const conj = preview?.kind === 'conj';
  return html`<div class="pv-views">
    <section class="panel" aria-label="Ground track">
      <div class="head"><h2>${air ? 'Aircraft' : 'Ground track'}</h2><span class="small">${air ? `within ${air.range} km` : preview ? preview.row.name : status?.tle_line1 ? status.target_name + ' (live)' : ''}</span></div>
      <canvas ref=${mapRef} role="img" aria-label="Map: ground track, visibility circle, site"></canvas>
      <span class="small">${air ? 'Arrows: aircraft along their track · selected: trail (solid) and the next 10 min if it flies straight (dashed) · rings: 10/25/50/100 km · triangle: site'
        : preview ? `Thick: above the horizon mask, AOS to LOS · thin: 15 min either side · circle: visible at culmination · triangle: site · shading: day, civil / nautical / astronomical twilight and night at ${utc(preview.row.culmination_ns)} UTC (max el)`
        : status?.tle_line1 ? 'solid: flown · dashed: next 50 min · circle: visible above the horizon mask'
        : 'Select a pass in the table to see its ground track.'}</span>
    </section>
    <section class="panel" aria-label="Sky">
      <div class="head"><h2>Sky</h2></div>
      <canvas ref=${skyRef} role="img" aria-label="Sky dome: pass path, mount, target, horizon mask"></canvas>
      <span class="small">${air ? 'North up, east left · dots: aircraft now · selected: the next 10 min if it flies straight · dashed: tracking plan · ring: mount' : html`North up, east left · selected pass: solid where sunlit, dotted in shadow${conj ? ' · thin: the other object · ✕: closest approach' : ''} · dashed: tracking plan · ring: mount`}</span>
    </section></div>`;
}

function ConjDetail({ preview, clearPreview }) {
  const c = preview.conj, p = preview.row, ch = p.chosen || {};
  const row = (k, v) => html`<tr><th>${k}</th><td>${v}</td></tr>`;
  const magTxt = (m, est) => (m == null ? 'unknown' : `${est ? '≈' : ''}${Number(m).toFixed(1)}`);
  const prob = Number(c.max_prob);
  return html`<section class="panel" aria-label="Conjunction details">
    <div class="head"><h2>Conjunction</h2>
      <button class="btn" style="min-height:26px;margin-left:auto" onClick=${clearPreview}>Clear</button></div>
    <table class="kv mono"><tbody>
      ${row('Objects', html`${conjName(c.name1)} (${c.cat1})<br />${conjName(c.name2)} (${c.cat2})`)}
      ${row('TCA', `${utc(c.tca_ns)} UTC · el ${Number(c.el_deg).toFixed(1)}° az ${Number(c.az_deg).toFixed(1)}°`)}
      ${row('Miss', `${c.range_km < 1 ? Math.round(c.range_km * 1000) + ' m' : Number(c.range_km).toFixed(2) + ' km'} at ${Number(c.speed_km_s).toFixed(2)} km/s`)}
      ${row('From here', `${Math.round(c.obs_range_km)} km away; ${preview.min_sep_arcsec != null
        ? `${fmtAng(preview.min_sep_arcsec)} apart at ${utc(preview.min_sep_ns)}` : `at most ${fmtAng(c.sep_max_arcsec)} apart (no elements for the other object)`}`)}
      ${row('Lighting', `${c.sunlit ? 'sunlit' : 'in shadow'} · Sun ${Number(c.sun_el_deg).toFixed(0)}° at the site`)}
      ${row('Brightness', `${magTxt(c.mag1, c.mag_estimated1)} / ${magTxt(c.mag2, c.mag_estimated2)}`)}
      ${row('Max prob', `${prob >= 0.001 ? prob.toFixed(4) : prob.toExponential(1)} · dilution ${Number(c.dilution_km).toFixed(2)} km`)}
      ${row('Elements', `${Number(c.dse1).toFixed(1)} / ${Number(c.dse2).toFixed(1)} days old at TCA`)}
      ${row('Track', `${p.name}, pass ${utc(p.rise_ns)}–${utc(p.set_ns)} · ${p.planned === false ? 'planning…' : ch.mode || 'no plan fits'}`)}
    </tbody></table>
    ${!ch.mode && p.planned !== false && html`<ul class="small problems">${(p.plans || []).map((x) => html`<li><b>${x.mode}</b>: ${(x.problems || []).join('; ')}</li>`)}</ul>`}
    <p class="small">The tracked object uses its latest element set, the other SOCRATES's; predictions get worse as elements age.
      <b>Track</b> follows ${p.name} through the pass; the other object crosses the field near TCA.</p>
  </section>`;
}

function PassDetail({ preview, status, clearPreview, air, asp }) {
  if (air) return html`<${AircraftDetail} air=${air} />`;
  if (preview?.kind === 'conj') return html`<${ConjDetail} preview=${preview} clearPreview=${clearPreview} />`;
  if (!preview) {
    return html`<section class="panel" aria-label="Pass details"><div class="head"><h2>Pass</h2></div>
      <p class="small">Click a pass in the table to preview it here: path across the sky, ground track, and whether the satellite is sunlit. ${status?.target_name ? html`<br /><br />Current target: <b>${status.target_name}</b>.` : ''}</p></section>`;
  }
  const p = preview.row, sky = preview.sky;
  const top = sky.reduce((a, b) => (b[1] > a[1] ? b : a), sky[0]);
  const ch = p.chosen || {};
  const az = (a) => `${a.toFixed(0)}° ${['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(a / 45) % 8]}`;
  const row = (k, v) => html`<tr><th>${k}</th><td>${v}</td></tr>`;
  return html`<section class="panel" aria-label="Pass details">
    <div class="head"><h2>${p.name}</h2><span class="small">${p.also?.length ? '+ ' + p.also.join(', ') : ''}</span>
      <button class="btn" style="min-height:26px;margin-left:auto" onClick=${clearPreview}>Clear</button></div>
    <table class="kv mono"><tbody>
      ${row('AOS', `${utc(p.rise_ns)} · az ${az(sky[0][0])}`)}
      ${row('Max el', `${utc(p.culmination_ns)} · ${Number(p.max_el_deg).toFixed(1)}° at az ${az(top[0])}`)}
      ${row('LOS', `${utc(p.set_ns)} · az ${az(sky[sky.length - 1][0])}`)}
      ${row('Duration', dur((Number(p.set_ns) - Number(p.rise_ns)) / 1e9))}
      ${row('Lighting', preview.sunlit)}
      ${row('Brightness', p.peak_mag != null ? `mag ${fmtMag(p)} at ${utc(p.peak_mag_ns)} (standard ${p.std_mag}${p.mag_estimated ? ', from radar size' : ''})`
        : p.std_mag != null ? 'not sunlit' : 'no magnitude data (add it to config/satellites.yaml)')}
      ${row('Size', p.size_m ? `${p.size_m.join(' × ')} m${p.size_estimated ? ' (from radar size)' : ''} · up to ${fmtAng(p.ang_size_arcsec)} at ${Math.round(p.min_range_km)} km${asp ? ` (~${Math.round(p.ang_size_arcsec / asp)} px)` : ''}`
        : p.min_range_km != null ? `no size data · closest ${Math.round(p.min_range_km)} km` : '—')}
      ${row('Plan', p.planned === false ? 'planning…' : ch.mode ? `${ch.mode}${ch.mode === 'over_the_top' ? ` (error ${Number(ch.max_error_deg).toFixed(1)}°)` : ''}` : 'no plan fits')}
      ${ch.mode && row('Peak rates', `az ${Number(ch.peak_az_deg_s).toFixed(2)} °/s · el ${Number(ch.peak_el_deg_s).toFixed(2)} °/s`)}
    </tbody></table>
    ${!ch.mode && p.planned !== false && html`<ul class="small problems">${(p.plans || []).map((x) => html`<li><b>${x.mode}</b>: ${(x.problems || []).join('; ')}</li>`)}</ul>`}
    <p class="small">Use <b>Track</b> in the table, then follow it on the Track screen.</p>
  </section>`;
}

function TelemetryPanel({ status, history, now }) {
  const errRef = useRef(null), latRef = useRef(null);
  useEffect(() => {
    const t = now / 1000;
    if (errRef.current) drawStrip(errRef.current, {
      now_s: t, unit: '″', minTop: 2,
      series: [{ points: history.az, color: cssVar('--accent') }, { points: history.el, color: cssVar('--accent2') }],
    });
    if (latRef.current) drawStrip(latRef.current, {
      now_s: t, unit: '', minTop: 20, aspect: 0.3, series: [{ points: history.lat, color: cssVar('--accent') }],
    });
  });
  const st = status || {};
  const age = tleAgeDays(st.tle_line1);
  const n = (v, d = 3) => (Number.isFinite(Number(v)) && st.target_name ? Number(v).toFixed(d) : '—');
  return html`<section class="panel">
    <h2>Target telemetry</h2>
    <div class="kv mono">
      <span class="k">Az / El</span><span class="v">${st.target_name ? `${Number(st.target_az_deg).toFixed(3)}° / ${Number(st.target_el_deg).toFixed(3)}°` : '—'}</span>
      <span class="k">Az rate</span><span class="v">${n(st.target_az_rate_deg_s)} °/s</span>
      <span class="k">El rate</span><span class="v">${n(st.target_el_rate_deg_s)} °/s</span>
      ${st.tle_line1 && html`
        <span class="k">Range</span><span class="v">${n(st.range_km, 0)} km</span>
        <span class="k">Range rate</span><span class="v">${n(st.range_rate_km_s)} km/s</span>
        <span class="k">Altitude</span><span class="v">${n(st.altitude_km, 0)} km</span>
        <span class="k">TLE age</span><span class="v">${age === null ? '—' : age.toFixed(2) + ' d'}</span>`}
    </div>
  </section>
  <section class="panel">
    <h2>Following error |cmd − act| · 60 s</h2>
    <canvas ref=${errRef} role="img" aria-label="Following error over the last minute, azimuth and elevation, arcseconds"></canvas>
    <div class="legend"><span><i style="background:var(--accent)"></i>az</span><span><i style="background:var(--accent2)"></i>el</span></div>
    <h2>Loop wake-up latency · 60 s <span style="text-transform:none">(µs)</span></h2>
    <canvas ref=${latRef} role="img" aria-label="Real-time loop wake-up latency over the last minute, microseconds"></canvas>
  </section>`;
}

// Drive state changes, faults and trips, for the Drives screen's event log; also remembers
// each axis's last fault (error code and vendor register) after it has been reset.
function noteEvents(prev, m, events, faults) {
  const stamp = new Date(Number(m.t_ns) / 1e6).toISOString().slice(11, 19);
  const push = (text, bad = false) => { events.push({ t: stamp, text, bad }); if (events.length > 200) events.shift(); };
  for (const a of m.axes) {
    const p = prev?.axes.find((x) => x.axis === a.axis);
    if (!p || p.state !== a.state) push(`${a.axis}: ${p ? p.state + ' → ' : ''}${a.state}`, a.state === 'FAULT');
    if (a.error_code || a.vendor_error) {
      const f = faults[a.axis];
      if (!f || f.code !== a.error_code || f.vendor !== a.vendor_error) {
        if (!p || p.error_code !== a.error_code || p.vendor_error !== a.vendor_error) {
          push(`${a.axis}: error code 0x${a.error_code.toString(16).toUpperCase().padStart(4, '0')}` +
            (a.vendor_error ? `, vendor register 0x${a.vendor_error.toString(16).toUpperCase().padStart(4, '0')}` : ''), true);
        }
        faults[a.axis] = { code: a.error_code, vendor: a.vendor_error, t: Number(m.t_ns) / 1e6 };
      }
    }
  }
  if (prev && !prev.tripped && m.tripped) push('following-error trip: all axes quick-stopped', true);
  if (prev && prev.tripped && !m.tripped) push('trip cleared');
}

// ---- app -----------------------------------------------------------------------------------
function App() {
  const [theme, setThemeState] = useState(document.documentElement.dataset.theme || 'dark');
  const [connected, setConnected] = useState(false);
  const [hello, setHello] = useState(null);
  const [tel, setTel] = useState(null);
  const [status, setStatus] = useState(null);
  const [track, setTrack] = useState(null);
  const [preview, setPreviewState] = useState(null);
  const previewReq = useRef(null);
  const [line, setLine] = useState('connecting to the gateway…');
  const [land, setLand] = useState(null);
  const [now, setNow] = useState(Date.now());
  const history = useRef({ az: [], el: [], lat: [] });
  const [screen, setScreenState] = useState(screenFromHash);
  const events = useRef([]);
  const faults = useRef({});
  const prevTel = useRef(null);
  const link = useRef(null);
  const dialogRef = useRef(null);
  const lastMsg = useRef('');

  const setScreen = (id) => { setScreenState(id); location.hash = id === 'track' ? '' : id; };
  useEffect(() => {
    const onHash = () => setScreenState(screenFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const setTheme = (t) => {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem('maxmount.theme', t); } catch (e) { /* ignore */ }
    setThemeState(t);
  };

  useEffect(() => {
    fetch('vendor/ne_110m_land.json').then((r) => r.json()).then(setLand).catch(() => setLand(null));
    link.current = connect({
      onOpen: () => { setConnected(true); setLine('connected'); },
      onClose: () => { setConnected(false); setLine('gateway connection lost; reconnecting…'); },
      onMessage: (m) => {
        if (m.type === 'hello') { setHello(m); setImaging(m.imaging || null); } else if (m.type === 'imaging') setImaging(m);
        else if (m.type === 'telemetry') {
          setTel(m);
          const t = Number(m.t_ns) / 1e9;
          const h = history.current;
          for (const a of m.axes) (h[a.axis] ||= []).push([t, Math.abs(a.err_arcsec)]);
          h.lat.push([t, m.loop.max_lat_us]);
          for (const k of Object.keys(h)) while (h[k].length && h[k][0][0] < t - 65) h[k].shift();
          noteEvents(prevTel.current, m, events.current, faults.current);
          prevTel.current = m;
        } else if (m.type === 'status') {
          setStatus(m);
          if (m.message && m.message !== lastMsg.current) {
            lastMsg.current = m.message;
            setLine(`${new Date().toISOString().slice(11, 19)} ${m.target_name ? m.target_name + ': ' : ''}${m.message}`);
          }
        } else if (m.type === 'groundtrack') setTrack(m);
      },
    });
    const timer = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(timer);
  }, []);

  const send = useCallback(async (op, args) => {
    const r = await link.current.request(op, args);
    setLine(`${new Date().toISOString().slice(11, 19)} ${op}: ${r.ok ? '' : 'refused: '}${r.message}`);
    return r;
  }, []);
  const act = (op) => send(op, {});
  const setPreview = useCallback(async (p) => {
    previewReq.current = p ? passKey(p) : null;
    if (!p) { setPreviewState(null); return; }
    const r = await send('pass_preview', { name: p.name, line1: p.line1, line2: p.line2, rise_ns: p.rise_ns, culmination_ns: p.culmination_ns, set_ns: p.set_ns });
    if (previewReq.current !== passKey(p)) return; // a newer click won
    setPreviewState(r.ok ? { key: passKey(p), ...r.data, row: p } : null);
  }, [send]);
  // A conjunction preview: both objects' paths through the tracked object's pass. Returns the
  // preview (Track uses its pass), or null.
  const setConjPreview = useCallback(async (c) => {
    previewReq.current = c ? conjKey(c) : null;
    if (!c) { setPreviewState(null); return null; }
    const r = await send('conjunction_detail', { cat1: c.cat1, cat2: c.cat2, primary: c.primary, tca_ns: c.tca_ns });
    if (previewReq.current !== conjKey(c)) return null;
    const pv = r.ok ? { key: conjKey(c), kind: 'conj', conj: c, ...r.data, row: r.data.pass } : null;
    setPreviewState(pv);
    return pv;
  }, [send]);
  const [listTab, setListTabState] = useState(loadPref('listtab', 'passes'));
  const [airView, setAirView] = useState(null);
  const [imaging, setImaging] = useState(null);
  const ask = useCallback((op, args) => link.current.request(op, args), []);
  const setListTab = (t) => { setListTabState(t); savePref('listtab', t); setPreviewState(null); previewReq.current = null; };
  const updatePreviewRow = useCallback((row) => setPreviewState((pv) => (pv && pv.key === passKey(row) ? { ...pv, row } : pv)), []);
  const openDialog = () => dialogRef.current?.showModal();

  return html`
    <${Header} theme=${theme} setTheme=${setTheme} line=${line} status=${status} tel=${tel} hello=${hello} connected=${connected}
      imaging=${imaging} send=${send}
      screen=${screen} setScreen=${setScreen} />
    <div class="layout" hidden=${screen !== 'drives'}>
      <${DrivesScreen} tel=${tel} hello=${hello} status=${status} send=${send}
        history=${history.current} now=${now} events=${events.current} faults=${faults.current} />
    </div>
    <div class="passes-screen" hidden=${screen !== 'passes'}>
      <${PassViews} status=${status} tel=${tel} hello=${hello} track=${track} land=${land} preview=${preview}
        air=${listTab === 'air' ? airView : null} />
      <div class="pv-lower">
        <div class="pv-lists">
          <${Passes} hello=${hello} send=${send} status=${status} connected=${connected} preview=${preview} setPreview=${setPreview} imaging=${imaging}
            updatePreviewRow=${updatePreviewRow} setScreen=${setScreen} tab=${listTab} setTab=${setListTab} />
          <${Conjunctions} hello=${hello} send=${send} status=${status} connected=${connected} preview=${preview}
            setConjPreview=${setConjPreview} setScreen=${setScreen} tab=${listTab} setTab=${setListTab} />
          <${Aircraft} ask=${ask} send=${send} status=${status} connected=${connected} active=${screen === 'passes' && listTab === 'air'}
            setAirView=${setAirView} setScreen=${setScreen} tab=${listTab} setTab=${setListTab} mask=${hello?.tracking?.horizon_min_el_deg ?? 10} />
        </div>
        <${PassDetail} preview=${preview} status=${status} clearPreview=${() => setPreview(null)} air=${listTab === 'air' ? airView : null}
          asp=${activeProfile(imaging)?.arcsec_per_px} />
      </div>
    </div>
    <div class="layout" hidden=${screen !== 'track'}>
      <aside class="col-left">
        <${MountPanel} tel=${tel} act=${act} />
        <${TargetCard} status=${status} openDialog=${openDialog} now=${now} />
      </aside>
      <main class="col-main">
        <div class="row">
          <${CameraPanel} title="Widefield · acquisition" info="GigE" />
          <${CameraPanel} title="C8 · ZWO" info="ROI" square=${true} />
        </div>
        <${Corrections} status=${status} send=${send} />
      </main>
      <aside class="col-right">
        <${SkyPanel} status=${status} tel=${tel} hello=${hello} track=${track} land=${land} />
        <${TelemetryPanel} status=${status} history=${history.current} now=${now} />
      </aside>
    </div>
    <${TargetDialog} dialogRef=${dialogRef} send=${send} />
    ${!connected && html`<div class="offline" role="status">No connection to the gateway (mount-svc gui)</div>`}
  `;
}

render(html`<${App} />`, document.getElementById('app'));
