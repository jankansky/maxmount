// SPDX-License-Identifier: GPL-3.0-or-later
// Formatting and per-browser preferences shared by the screens.
// ---- formatting ---------------------------------------------------------------------------
export const pad = (n, w = 2) => String(Math.floor(Math.abs(n))).padStart(w, '0');
export function dms(deg, signed = true, wDeg = 2) {
  if (deg === undefined || deg === null || !Number.isFinite(deg)) return '—';
  const s = deg < 0 ? '−' : signed ? '+' : '';
  let a = Math.abs(deg);
  let d = Math.floor(a), m = Math.floor((a - d) * 60), sec = Math.round(((a - d) * 60 - m) * 60);
  if (sec === 60) { sec = 0; m += 1; }
  if (m === 60) { m = 0; d += 1; }
  return `${s}${pad(d, wDeg)}°${pad(m)}′${pad(sec)}″`;
}
export function hms(hours, dec = 1) {
  if (hours === undefined || !Number.isFinite(hours)) return '—';
  const h = ((hours % 24) + 24) % 24;
  const H = Math.floor(h), M = Math.floor((h - H) * 60), S = ((h - H) * 60 - M) * 60;
  return `${pad(H)}h${pad(M)}m${S.toFixed(dec).padStart(dec ? dec + 3 : 2, '0')}s`;
}
export const utc = (ns) => (ns ? new Date(Number(ns) / 1e6).toISOString().slice(11, 19) : '—');
export function dur(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const s = Math.round(seconds);
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${pad((s % 3600) / 60)}m` : `${pad(s / 60)}:${pad(s % 60)}`;
}
export function loadPref(key, fallback) {
  try { const v = localStorage.getItem('maxmount.' + key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
}
export function savePref(key, value) { try { localStorage.setItem('maxmount.' + key, JSON.stringify(value)); } catch (e) { /* private mode */ } }
