// SPDX-License-Identifier: GPL-3.0-or-later
// WebSocket link to the gateway, with reconnect and request/reply matching.

export function connect({ onMessage, onOpen, onClose }) {
  let ws = null;
  let nextId = 0;
  const pending = new Map();

  function open() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${scheme}://${location.host}/ws`);
    ws.onopen = () => onOpen && onOpen();
    ws.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m.type === 'reply') {
        const done = pending.get(m.id);
        if (done) { pending.delete(m.id); done(m); }
      } else {
        onMessage(m);
      }
    };
    ws.onclose = () => {
      for (const [id, done] of pending) { done({ ok: false, message: 'connection lost' }); pending.delete(id); }
      onClose && onClose();
      setTimeout(open, 1500);
    };
  }
  open();

  return {
    request(op, args = {}, timeoutMs = 90000) {
      return new Promise((resolve) => {
        if (!ws || ws.readyState !== WebSocket.OPEN) { resolve({ ok: false, message: 'not connected to the gateway' }); return; }
        const id = ++nextId;
        pending.set(id, resolve);
        ws.send(JSON.stringify({ type: 'request', id, op, args }));
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); resolve({ ok: false, message: 'no reply (timed out)' }); } }, timeoutMs);
      });
    },
  };
}
