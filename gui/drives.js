// SPDX-License-Identifier: GPL-3.0-or-later
// Drives screen: per-axis CiA 402 state, statusword, faults (with the drive's vendor error
// register), limit inputs, controller flags, configuration, enable/disable and a supervised
// jog for commissioning. Enable, disable and fault reset go straight to mount-rt; jogs go
// through mount-svc, which owns the trajectory stream.
import { html, useState, useEffect, useRef } from './vendor/preact-htm-standalone.module.js';
import { drawStrip, cssVar } from './lib/charts.js';

// ---- decoding -------------------------------------------------------------------------------
const LADDER = ['NOT_READY_TO_SWITCH_ON', 'SWITCH_ON_DISABLED', 'READY_TO_SWITCH_ON', 'SWITCHED_ON', 'OPERATION_ENABLED'];
const STATE_LABEL = {
  UNKNOWN: 'unknown', NOT_READY_TO_SWITCH_ON: 'not ready', SWITCH_ON_DISABLED: 'switch-on disabled',
  READY_TO_SWITCH_ON: 'ready', SWITCHED_ON: 'switched on', OPERATION_ENABLED: 'operation enabled',
  QUICK_STOP_ACTIVE: 'quick stop', FAULT_REACTION_ACTIVE: 'fault reaction', FAULT: 'FAULT',
};

// CiA 402 statusword (0x6041). Bits 12/13 are the CSP meanings; 8, 14, 15 are manufacturer-specific.
const SW_BITS = [
  [0, 'ready to switch on'], [1, 'switched on'], [2, 'operation enabled'], [3, 'fault'],
  [4, 'voltage enabled'], [5, 'quick stop off', true], [6, 'switch on disabled'], [7, 'warning'],
  [8, 'mfr bit 8'], [9, 'remote'], [10, 'target reached'], [11, 'internal limit'],
  [12, 'follows target'], [13, 'following error'], [14, 'mfr bit 14'], [15, 'mfr bit 15'],
];

// Controller flags from mount-rt.
const FLAGS = [[0, 'limited'], [1, 'starved'], [2, 'holding'], [3, 'before start'], [4, 'FE trip']];

// 0x60FD digital inputs: bits 0-2 are standard; 16-31 are manufacturer-specific.
const DI_BITS = [[0, 'neg. limit'], [1, 'pos. limit'], [2, 'home']];

// Vendor error registers, by the descriptor's diagnostics.error_register.decode.
const VENDOR_DECODERS = {
  // Technosoft Motion Error Register (MER); technosoftmotion.com ESM-um-html/tml_mer.htm.
  technosoft_mer: [
    [0, 'CAN bus error'], [1, 'short-circuit'], [2, 'invalid setup table'], [3, 'control error'],
    [4, 'serial/internal communication error'], [5, 'Hall / resolver / BiSS / position wrap-around error'],
    [6, 'positive limit switch active'], [7, 'negative limit switch active'], [8, 'over-current'],
    [9, 'I²t protection (drive or motor)'], [10, 'motor over-temperature'], [11, 'drive over-temperature'],
    [12, 'over-voltage'], [13, 'under-voltage'], [14, 'command error'], [15, 'drive/motor disabled'],
  ],
};

// CiA 301/402 emergency error codes: the high nibble is standardised; a few common full codes.
const ERR_CODES = {
  0x2310: 'continuous over-current', 0x2340: 'short-circuit (motor side)', 0x3210: 'DC-link over-voltage',
  0x3220: 'DC-link under-voltage', 0x4210: 'device over-temperature', 0x4310: 'drive over-temperature',
  0x7300: 'sensor / feedback', 0x7500: 'communication', 0x8130: 'life guard / heartbeat',
  0x8611: 'following error', 0x8612: 'reference limit',
};
const ERR_CLASS = {
  0x2: 'current', 0x3: 'voltage', 0x4: 'temperature', 0x5: 'device hardware', 0x6: 'device software',
  0x7: 'additional modules', 0x8: 'monitoring / communication', 0x9: 'external', 0xF: 'manufacturer-specific',
};
export function describeError(code) {
  if (!code) return 'no error';
  return ERR_CODES[code] || `${ERR_CLASS[code >> 12] || 'unclassified'} error (see the drive manual)`;
}
const hex = (v, digits = 4) => '0x' + (Number(v) >>> 0).toString(16).toUpperCase().padStart(digits, '0');
const bit = (v, b) => ((Number(v) >>> b) & 1) === 1;
const f = (v, d = 4) => (Number.isFinite(v) ? v.toFixed(d) : '—');
// An axis without counts_per_rev reports raw encoder counts in its "radian" fields; the
// gateway converts radians to degrees, so undo that.
const counts = (deg) => (Number.isFinite(deg) ? Math.round((deg * Math.PI) / 180).toLocaleString() + ' cnt' : '—');

// ---- pieces ------------------------------------------------------------------------------------
function Led({ on, kind = 'on', children, title }) {
  return html`<span class=${'led ' + (on ? kind : '')} title=${title}><i></i>${children}</span>`;
}

function StateLadder({ state }) {
  const faulty = state === 'FAULT' || state === 'FAULT_REACTION_ACTIVE' || state === 'QUICK_STOP_ACTIVE';
  return html`<div class="ladder" role="list" aria-label="CiA 402 state">
    ${LADDER.map((s) => html`<span role="listitem" class=${'step ' + (s === state ? 'cur' : '')} aria-current=${s === state}>${STATE_LABEL[s]}</span>`)}
    <span role="listitem" class=${'step fault ' + (faulty ? 'cur' : '')}>${faulty ? STATE_LABEL[state] : 'fault'}</span>
  </div>`;
}

function RangeBar({ min, max, value, cmd }) {
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return null;
  const x = (v) => `${Math.min(100, Math.max(0, ((v - min) / (max - min)) * 100))}%`;
  return html`<div class="rangebar" aria-label=${`position ${f(value, 2)}° within soft limits ${min}° to ${max}°`}>
    <div class="track">
      ${min < 0 && max > 0 && html`<span class="zero" style=${{ left: x(0) }}></span>`}
      ${Number.isFinite(cmd) && html`<span class="cmd" style=${{ left: x(cmd) }}></span>`}
      ${Number.isFinite(value) && html`<span class="act" style=${{ left: x(value) }}></span>`}
    </div>
    <div class="ends mono small"><span>${min}°</span><span>soft limits</span><span>${max}°</span></div>
  </div>`;
}

function FaultBox({ a, cfg, last, send }) {
  const code = a?.error_code || 0;
  const vendor = a?.vendor_error || 0;
  const er = cfg.error_register;
  const shown = code || vendor ? { code, vendor, t: null } : last;
  const decoder = er && VENDOR_DECODERS[er.decode];
  const active = a?.state === 'FAULT' || a?.state === 'FAULT_REACTION_ACTIVE';
  if (!active && !shown) return html`<div class="small">No fault since the GUI connected.</div>`;
  return html`<div class=${'faultbox ' + (active ? 'active' : '')}>
    <div class="head"><strong>${active ? 'Fault' : 'Last fault'}</strong>
      ${shown?.t && html`<span class="small mono">${new Date(shown.t).toISOString().slice(11, 19)} UTC</span>`}</div>
    <div class="kv mono">
      <span class="k">Error code</span><span class="v">${shown?.code ? hex(shown.code) : active ? 'reading…' : '—'}</span>
    </div>
    <div class="small">${shown?.code ? describeError(shown.code) : ''}</div>
    ${er && html`<div class="kv mono"><span class="k">${er.name || 'vendor register'}</span><span class="v">${shown?.vendor ? hex(shown.vendor) : '—'}</span></div>`}
    ${decoder && shown?.vendor ? html`<ul class="bitlist">${decoder.filter(([b]) => bit(shown.vendor, b)).map(([b, name]) => html`<li><span class="mono">bit ${b}</span> ${name}</li>`)}</ul>` : null}
    ${active && html`<button class="btn" onClick=${() => send('fault_reset')}>Fault reset (all drives)</button>`}
  </div>`;
}

const STEPS = [0.01, 0.1, 1, 5, 10, 45];
const SPEEDS = [0.1, 0.5, 1, 2, 5];

function Jog({ cfg, a, send, svcBusy, hardware, tripped }) {
  const [step, setStep] = useState(1);
  const [speed, setSpeed] = useState(1);
  const [to, setTo] = useState('');
  const [armed, setArmed] = useState(false);
  const [reply, setReply] = useState(null);
  const faulted = a?.state === 'FAULT' || a?.state === 'FAULT_REACTION_ACTIVE';
  useEffect(() => { if (faulted || tripped) setArmed(false); }, [faulted, tripped]);
  const lim = cfg.limits || {};
  const speeds = SPEEDS.filter((v) => !(lim.max_vel_deg_s > 0) || v <= lim.max_vel_deg_s);
  const why = !a ? `${cfg.name} is not on the bus (mount-rt --axes?)`
    : !cfg.commissioned ? 'not commissioned: set counts_per_rev in the system file'
      : faulted ? 'drive in fault: reset it first'
        : tripped ? 'following-error trip latched: fault reset first'
          : svcBusy ? 'mount-svc has an active target: Hold or STOP it first'
            : hardware && !armed ? 'arm motion first' : '';
  const go = async (args) => {
    setReply({ ok: true, message: 'sending…' });
    const r = await send('jog', { axis: cfg.name, vel_deg_s: speed, acc_deg_s2: Math.min(2 * speed, lim.max_acc_deg_s2 || 2 * speed), ...args });
    setReply(r);
  };
  const goTo = () => {
    const v = Number(to);
    if (to.trim() === '' || !Number.isFinite(v)) { setReply({ ok: false, message: 'position in degrees' }); return; }
    go({ absolute: true, to_deg: v });
  };
  return html`<div class="jog">
    ${hardware && html`<label class="arm"><input type="checkbox" checked=${armed} disabled=${!a || !cfg.commissioned} onChange=${(e) => setArmed(e.target.checked)} />
      Arm motion: I can see the mount and the path and cables are clear</label>`}
    <div class="toolbar">
      <label>Step <select value=${step} onChange=${(e) => setStep(Number(e.target.value))}>
        ${STEPS.map((s) => html`<option value=${s}>${s}°</option>`)}</select></label>
      <label>Speed <select value=${speed} onChange=${(e) => setSpeed(Number(e.target.value))}>
        ${speeds.map((s) => html`<option value=${s}>${s} °/s</option>`)}</select></label>
    </div>
    <div class="grid2">
      <button class="btn" disabled=${!!why} onClick=${() => go({ by_deg: -step })} aria-label=${`Jog ${cfg.name} minus ${step} degrees`}>− ${step}°</button>
      <button class="btn" disabled=${!!why} onClick=${() => go({ by_deg: step })} aria-label=${`Jog ${cfg.name} plus ${step} degrees`}>+ ${step}°</button>
    </div>
    <div class="toolbar">
      <label style="flex:1;display:flex;gap:6px;align-items:center">Move to <input style="flex:1;min-width:0" value=${to} onInput=${(e) => setTo(e.target.value)} placeholder="axis °" aria-label=${`${cfg.name} absolute target in degrees`} /></label>
      <button class="btn" disabled=${!!why} onClick=${goTo}>Go</button>
    </div>
    ${why && html`<span class="small">${why}</span>`}
    ${reply && html`<div class=${'reply ' + (reply.ok ? '' : 'bad')}>${reply.message}</div>`}
  </div>`;
}

function AxisCard({ cfg, a, tel, send, status, history, now, hardware, lastFault }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) drawStrip(ref.current, { now_s: now / 1000, unit: '″', minTop: 2, aspect: 0.16, series: [{ points: history[cfg.name] || [], color: cssVar('--accent') }] });
  });
  const lim = cfg.limits || {};
  const state = a?.state || 'UNKNOWN';
  const enabled = state === 'OPERATION_ENABLED';
  const badge = state === 'OPERATION_ENABLED' ? 'ok' : state === 'FAULT' || state === 'FAULT_REACTION_ACTIVE' ? 'bad' : state === 'QUICK_STOP_ACTIVE' ? 'warn' : '';
  const svcBusy = !!status && ['SERVICE_STATE_WAITING', 'SERVICE_STATE_SLEWING', 'SERVICE_STATE_TRACKING'].includes(status.state) && status.target_kind !== 'jog';
  const di = a?.di ?? 0;
  return html`<section class="panel axis" aria-label=${`Axis ${cfg.name}`}>
    <div class="head">
      <div><span class="axisname">${cfg.name}</span> <span class="small">${cfg.vendor} ${cfg.model} · bus ${cfg.bus_position ?? '—'}</span></div>
      <span class=${'badge ' + badge}>${a ? STATE_LABEL[state] || state : 'no data'}</span>
    </div>
    ${cfg.mechanics && html`<span class="small">${cfg.mechanics}</span>`}
    <${StateLadder} state=${state} />
    <div class="cols">
      <div class="col">
        <h3>Position (axis frame)</h3>
        <div class="kv mono">
          <span class="k">Actual</span><span class="v">${!a ? '—' : cfg.commissioned ? f(a.act_deg) + '°' : counts(a.act_deg)}</span>
          <span class="k">Command</span><span class="v">${!a ? '—' : cfg.commissioned ? f(a.cmd_deg) + '°' : counts(a.cmd_deg)}</span>
          <span class="k">Cmd − act</span><span class="v">${a && cfg.commissioned ? f(a.err_arcsec, 1) + '″' : '—'}</span>
          <span class="k">Cmd velocity</span><span class="v">${a && cfg.commissioned ? f(a.cmd_vel_deg_s, 3) + ' °/s' : '—'}</span>
          <span class="k">Drive ferr</span><span class="v">${a ? a.ferr_counts + ' cnt' : '—'}</span>
          <span class="k">Buffered pts</span><span class="v">${a ? a.buffered : '—'}</span>
        </div>
        ${cfg.commissioned && html`<${RangeBar} min=${lim.min_deg} max=${lim.max_deg} value=${a?.act_deg} cmd=${a?.cmd_deg} />`}
        <h3>Controller</h3>
        <div class="leds">${FLAGS.map(([b, n]) => html`<${Led} on=${a && bit(a.flags, b)} kind=${b === 4 ? 'fault' : 'on'}>${n}<//>`)}</div>
        <h3>Inputs <span class="mono small">0x60FD ${a ? hex(di, 8) : ''}</span></h3>
        <div class="leds">${DI_BITS.map(([b, n]) => html`<${Led} on=${a && bit(di, b)} kind=${b < 2 ? 'fault' : 'on'}>${n}<//>`)}</div>
      </div>
      <div class="col">
        <h3>Statusword <span class="mono small">${a ? hex(a.sw) : ''}</span></h3>
        <div class="bits">${SW_BITS.map(([b, n, inv]) => {
          const on = a && bit(a.sw, b);
          const kind = (b === 3 || b === 13 || (b === 7) || b === 11) ? 'fault' : 'on';
          return html`<${Led} on=${on} kind=${kind} title=${`bit ${b}${inv ? ' (0 = quick stop active)' : ''}`}>${n}<//>`;
        })}</div>
        <h3>Fault</h3>
        <${FaultBox} a=${a} cfg=${cfg} last=${lastFault} send=${send} />
      </div>
    </div>
    <h3>Following error |cmd − act| · 60 s</h3>
    <canvas ref=${ref} role="img" aria-label=${`${cfg.name} following error over the last minute, arcseconds`}></canvas>
    <div class="cols">
      <div class="col">
        <h3>Power</h3>
        <div class="grid2">
          <button class="btn primary" disabled=${!a || enabled || !cfg.commissioned} onClick=${() => send('enable', { axis: cfg.name })} title=${!cfg.commissioned ? 'not commissioned' : ''}>Enable</button>
          <button class="btn" disabled=${!a || !enabled} onClick=${() => send('disable', { axis: cfg.name })}>Disable</button>
        </div>
        <span class="small">Enable holds the current position. Disable decelerates to rest first.</span>
        <h3>Configuration</h3>
        <div class="kv mono">
          <span class="k">counts/rev</span><span class="v">${cfg.counts_per_rev ?? html`<span class="warn">not set</span>`}</span>
          <span class="k">invert / offset</span><span class="v">${cfg.invert ? 'yes' : 'no'} / ${cfg.offset_deg}°</span>
          <span class="k">Soft limits</span><span class="v">${lim.min_deg}° … ${lim.max_deg}°</span>
          <span class="k">Max vel / acc</span><span class="v">${lim.max_vel_deg_s} °/s · ${lim.max_acc_deg_s2} °/s²</span>
          <span class="k">Max foll. error</span><span class="v">${lim.max_following_error_deg ?? html`<span class="warn">none</span>`}${lim.max_following_error_deg != null ? '°' : ''}</span>
        </div>
      </div>
      <div class="col">
        <h3>Jog (axis frame)</h3>
        <${Jog} cfg=${cfg} a=${a} send=${send} svcBusy=${svcBusy} hardware=${hardware} tripped=${tel?.tripped} />
      </div>
    </div>
  </section>`;
}

function BusPanel({ tel, hello, status, send, history, now }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) drawStrip(ref.current, { now_s: now / 1000, minTop: 20, aspect: 0.22, series: [{ points: history.lat, color: cssVar('--accent') }] });
  });
  const fb = hello?.fieldbus || {};
  const anyFault = tel?.axes.some((a) => a.state === 'FAULT' || a.state === 'FAULT_REACTION_ACTIVE');
  return html`<section class="panel bus" aria-label="Bus and controller">
    <div class="head"><h2>EtherCAT · mount-rt</h2>
      <span class="small mono">${fb.backend || hello?.backend || '—'}${fb.interface ? ' on ' + fb.interface : ''} · ${fb.cycle_us || '—'} µs cycle</span></div>
    <div class="cols">
      <div class="col">
        <div class="kv mono">
          <span class="k">Cycles</span><span class="v">${tel ? Number(tel.loop.cycles).toLocaleString() : '—'}</span>
          <span class="k">Overruns</span><span class="v">${tel ? tel.loop.overruns : '—'}</span>
          <span class="k">Lost frames</span><span class="v">${tel ? tel.bus.lost : '—'}</span>
          <span class="k">WKC errors</span><span class="v">${tel ? tel.bus.wkc : '—'}</span>
          <span class="k">mount-svc</span><span class="v">${status ? (status.state || '').replace('SERVICE_STATE_', '').toLowerCase() : 'not running'}</span>
        </div>
        <div class="leds">
          <${Led} on=${tel?.inputs_fresh}>inputs fresh<//>
          <${Led} on=${tel?.enabled}>any enabled<//>
          <${Led} on=${tel?.tripped} kind="fault">FE trip latched<//>
          <${Led} on=${anyFault} kind="fault">drive fault<//>
        </div>
      </div>
      <div class="col" style="flex:2 1 320px">
        <h3>Loop wake-up latency · 60 s <span style="text-transform:none">(µs)</span></h3>
        <canvas ref=${ref} role="img" aria-label="Real-time loop wake-up latency over the last minute, microseconds"></canvas>
      </div>
      <div class="col actions" style="flex:0 0 200px">
        <button class="btn stop" onClick=${() => send('stop')}>STOP</button>
        <button class="btn" onClick=${() => send('fault_reset')} title="reset drive faults and a latched following-error trip">Fault reset</button>
        <button class="btn" onClick=${() => send('disable', { axis: '' })}>Disable all</button>
      </div>
    </div>
  </section>`;
}

function EventLog({ events }) {
  return html`<section class="panel" aria-label="Drive events">
    <h2>Drive events (this browser session)</h2>
    <div class="eventlog mono">
      ${events.length === 0 ? html`<span class="small">No state changes yet.</span>`
        : events.slice().reverse().map((e) => html`<div class=${e.bad ? 'bad' : ''}><span class="t">${e.t}</span> ${e.text}</div>`)}
    </div>
  </section>`;
}

export function DrivesScreen({ tel, hello, status, send, history, now, events, faults }) {
  const axes = hello?.axes || [];
  const hardware = (hello?.backend || '') !== 'sim';
  return html`<div class="drives">
    <${BusPanel} tel=${tel} hello=${hello} status=${status} send=${send} history=${history} now=${now} />
    <div class="row">
      ${axes.map((cfg) => html`<${AxisCard} cfg=${cfg} a=${tel?.axes.find((x) => x.axis === cfg.name)} tel=${tel} send=${send}
        status=${status} history=${history} now=${now} hardware=${hardware} lastFault=${faults[cfg.name]} />`)}
      ${axes.length === 0 && html`<section class="panel"><span class="small">Waiting for the gateway's axis list…</span></section>`}
    </div>
    <${EventLog} events=${events} />
  </div>`;
}
