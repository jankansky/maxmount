// SPDX-License-Identifier: GPL-3.0-or-later
// Small shared widgets.
import { html, useMemo } from '../vendor/preact-htm-standalone.module.js';

// A <select> whose <option> elements are built once. The GUI re-renders ~10 times a second
// with telemetry; recreating the options each time rewrites their attributes, which closes
// or resets a dropdown the user has open. options: [[value, text], ...]; onChange gets the
// chosen value converted back to the type of the matching option value.
export function Select({ value, onChange, options, ...rest }) {
  const key = JSON.stringify(options);
  const opts = useMemo(() => options.map(([v, text]) => html`<option value=${String(v)}>${text}</option>`), [key]);
  const pick = (e) => {
    const hit = options.find(([v]) => String(v) === e.target.value);
    onChange(hit ? hit[0] : e.target.value);
  };
  return html`<select value=${String(value)} onChange=${pick} ...${rest}>${opts}</select>`;
}
