"""
Jarvis OS — The phone page (2026-08-03)

One self-contained HTML document, served ONLY by the remote listener. No build
step, no bundle, no framework: the desktop frontend has essentially zero
responsive breakpoints and cannot be reused on a phone, and adding a second
build pipeline to ship four screens' worth of UI would cost more than it earns.

⚠️ THE PAGE ITSELF IS INERT AND IS SERVED WITHOUT A TOKEN. It has to be — it is
where the token gets installed, from the QR link's `#t=` fragment. Every piece
of DATA on it comes from an authed call, so an unpaired visitor on the LAN sees
an empty shell that can do nothing. That is the same shape as any login page.

The fragment is used rather than a query string deliberately: a URL fragment is
never sent to the server and never lands in a server log.

WHAT IT CAN DO is bounded by what is MOUNTED, not by this file: read what is
happening, approve or decline, answer, pause, cancel. There is no compose box,
because `/chat/stream` is not on this port.
"""

REMOTE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Jarvis</title>
<style>
  :root { --bg:#0A0A0F; --card:#14141c; --line:#24242f; --text:#e2e8f0;
          --dim:#94a3b8; --cyan:#22d3ee; --red:#f87171; --amber:#fbbf24; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom); }
  header { padding:14px 16px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:10px; position:sticky; top:0;
           background:var(--bg); z-index:2; }
  header h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.2px; }
  #dot { width:8px; height:8px; border-radius:50%; background:var(--dim); }
  #dot.ok { background:var(--cyan); }
  main { padding:16px; display:flex; flex-direction:column; gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
          padding:14px; }
  .card.approve { border-color:rgba(251,191,36,.35); }
  .goal { font-weight:600; margin:0 0 10px; }
  .step { border-left:2px solid var(--line); padding:2px 0 2px 10px; margin:8px 0; }
  .step.destructive { border-left-color:var(--red); }
  .lvl { font-size:11px; text-transform:uppercase; letter-spacing:.6px;
         color:var(--dim); }
  .lvl.destructive { color:var(--red); }
  pre { margin:6px 0 0; padding:8px; background:#0d0d14; border-radius:8px;
        font-size:12px; white-space:pre-wrap; word-break:break-word;
        color:var(--dim); overflow-x:auto; }
  .row { display:flex; gap:10px; margin-top:12px; }
  button { flex:1; padding:13px; border-radius:10px; border:1px solid var(--line);
           background:#1c1c26; color:var(--text); font-size:15px; font-weight:600; }
  button.go { background:rgba(34,211,238,.14); border-color:rgba(34,211,238,.4);
              color:var(--cyan); }
  button.no { color:var(--dim); }
  button:disabled { opacity:.45; }
  input { width:100%; padding:12px; border-radius:10px; border:1px solid var(--line);
          background:#0d0d14; color:var(--text); font-size:15px; margin-top:10px; }
  .dim { color:var(--dim); font-size:13px; }
  .err { color:var(--red); font-size:13px; margin-top:8px; }
  .empty { text-align:center; color:var(--dim); padding:48px 16px; }
</style>
</head>
<body>
<header><span id="dot"></span><h1>Jarvis</h1><span id="sub" class="dim"></span></header>
<main id="app"><div class="empty">Connecting…</div></main>
<script>
// The token arrives once in the URL fragment (never sent to a server, never
// logged) and lives in localStorage afterwards.
const KEY = 'jarvis.remote.token';
if (location.hash.startsWith('#t=')) {
  localStorage.setItem(KEY, decodeURIComponent(location.hash.slice(3)));
  history.replaceState(null, '', location.pathname);
}
const token = localStorage.getItem(KEY) || '';
const app = document.getElementById('app');
const dot = document.getElementById('dot');
const sub = document.getElementById('sub');
let busy = false;

async function api(path, options) {
  const res = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Jarvis-Token': token },
  });
  if (!res.ok) throw new Error((await res.text().catch(() => '')) || res.statusText);
  return res.json();
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function stepHtml(s) {
  const d = s.permission_level === 'destructive';
  return `<div class="step ${d ? 'destructive' : ''}">
    <div class="lvl ${d ? 'destructive' : ''}">${esc(s.permission_level)}</div>
    <div>${esc(s.description)}</div>
    ${s.action_detail ? `<pre>${esc(s.action_detail)}</pre>` : ''}
  </div>`;
}

function taskHtml(t) {
  const p = t.plan || {};
  const pending = (p.steps || []).filter(s => s.status === 'pending');
  const waiting = t.status === 'awaiting_approval';
  const asking = t.status === 'awaiting_choice';
  // ⚠️ THE TASK'S OWN plan_id, not the payload's. `tasks.py` serializes
  // `plan_id` explicitly AND sets `plan` to null when `plan_payload` is missing
  // or unparseable — so reading `p.id` posted an EMPTY plan_id and the approve
  // 404'd, on exactly the tasks whose snapshot failed to round-trip. The
  // payload's id is kept only as a fallback for an older row that has one.
  const planId = t.plan_id || p.id || '';
  // ⚠️ THE CONTRACT THIS CARD IS DRAWING (2026-08-04). `serialize_plan_for_api`
  // stamps it on every AWAITING_APPROVAL payload; we echo it back on approve so
  // the server can refuse if the steps moved since this snapshot was polled —
  // the phone renders `Task.plan_payload`, which is a POLL, not a live card.
  // Absent (a paused plan, an older row) means no echo and no check.
  const contractHash = p.contract_hash || '';
  return `<div class="card ${waiting || asking ? 'approve' : ''}" data-task="${esc(t.id)}"
              data-plan="${esc(planId)}" data-contract="${esc(contractHash)}">
    <p class="goal">${esc(t.goal)}</p>
    <div class="dim">${esc(t.agent || 'Jarvis')} · ${esc(t.status)}</div>
    ${waiting ? pending.map(stepHtml).join('') : ''}
    ${asking && p.question ? `<div class="step">${esc(p.question.text)}</div>` : ''}
    ${waiting ? `<div class="row">
        <button class="go" data-act="approve">Approve</button>
        <button class="no" data-act="cancel">Cancel</button>
      </div>` : ''}
    ${asking ? `<input data-answer placeholder="Answer…">
      <div class="row"><button class="go" data-act="answer">Send</button></div>` : ''}
    ${t.status === 'running' ? `<div class="row">
        <button class="no" data-act="pause">Pause</button>
        <button class="no" data-act="stop">Stop</button>
      </div>` : ''}
    <div class="err" data-err></div>
  </div>`;
}

async function refresh() {
  if (busy) return;
  try {
    const tasks = await api('/api/tasks?limit=25');
    dot.className = 'ok';
    const live = tasks.filter(t =>
      ['awaiting_approval', 'awaiting_choice', 'paused', 'running'].includes(t.status));
    sub.textContent = live.length ? `${live.length} waiting` : 'nothing waiting';
    app.innerHTML = live.length
      ? live.map(taskHtml).join('')
      : `<div class="empty">Nothing needs you right now.</div>`;
  } catch (e) {
    dot.className = '';
    app.innerHTML = `<div class="empty">${
      String(e).includes('401')
        ? 'This device is not paired. Scan the QR code in Settings on the machine.'
        : 'Can’t reach Jarvis.'}</div>`;
  }
}

app.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn || busy) return;
  const card = btn.closest('[data-task]');
  const taskId = card.dataset.task, planId = card.dataset.plan;
  const contractHash = card.dataset.contract;
  const err = card.querySelector('[data-err]');
  busy = true;
  card.querySelectorAll('button').forEach(b => (b.disabled = true));
  err.textContent = '';
  try {
    const act = btn.dataset.act;
    if (act === 'approve' || act === 'cancel') {
      const body = { plan_id: planId, approved: act === 'approve' };
      // Only on approve: cancelling is always safe whatever the steps are now,
      // and refusing a cancel over a stale hash would strand the card.
      if (act === 'approve' && contractHash) body.contract_hash = contractHash;
      await api('/api/agent/approve', { method: 'POST', body: JSON.stringify(body) });
    } else if (act === 'answer') {
      const answer = card.querySelector('[data-answer]').value.trim();
      if (!answer) throw new Error('Type an answer first.');
      await api('/api/agent/choose', {
        method: 'POST', body: JSON.stringify({ plan_id: planId, answer }),
      });
    } else if (act === 'pause') {
      await api(`/api/tasks/${taskId}/pause`, { method: 'POST', body: '{}' });
    } else if (act === 'stop') {
      await api(`/api/tasks/${taskId}/cancel`, { method: 'POST', body: '{}' });
    }
  } catch (e) {
    err.textContent = String(e).slice(0, 300);
  } finally {
    busy = false;
    await refresh();
  }
});

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
