// Agent Office · full chat (no 3D). Evolved from the AgentLink monitor (app.js): same security rules — untrusted text is
// only ever assigned via textContent (never as HTML), and the token stays in sessionStorage. It consumes exactly the API
// the 3D dashboard uses (snapshot + durable events over WebSocket); no business logic is duplicated here.
import { api, token, conectar, clasificar, CATEGORIAS } from '/office/datos.js';

const $ = (id) => document.getElementById(id);
const KINDS = ['chat', 'question', 'answer', 'status', 'task', 'review', 'decision', 'handoff', 'alert', 'context', 'event', 'system'];
const st = { project: null, channel: null, thread: null, agent: null, snap: null, msgs: new Map(), oldest: null, admin: false, conn: null, recovery: [] };

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'style') n.style.cssText = v; // CSSOM: allowed by the strict CSP (no inline style attributes)
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
const hora = (iso) => new Date(iso).toLocaleString('sv').slice(5, 16);

/** GitHub references are structured (validated server-side); links are built from fields, never from free text. */
function refLink(r) {
  const repo = r.repo || st.snap?.projects.find((p) => p.name === st.project)?.repo;
  let href = null, label = Object.entries(r).map(([k, v]) => `${k}:${v}`).join(' ');
  if (repo && /^[\w.-]+\/[\w.-]+$/.test(repo)) {
    if (r.pr) href = `https://github.com/${repo}/pull/${Number(r.pr)}`;
    else if (r.issue) href = `https://github.com/${repo}/issues/${Number(r.issue)}`;
    else if (r.commit && /^[0-9a-f]{6,40}$/i.test(r.commit)) href = `https://github.com/${repo}/commit/${r.commit}`;
    else if (r.branch) href = `https://github.com/${repo}/tree/${encodeURIComponent(r.branch)}`;
  }
  return href ? el('a', { href, target: '_blank', rel: 'noopener noreferrer' }, label) : el('span', {}, label);
}

function visible(m) {
  if ($('hide-system').checked && (m.kind === 'system' || m.kind === 'event')) return false;
  if ($('kind').value && m.kind !== $('kind').value) return false;
  if ($('only-mine').checked && !(m.mentions || []).length) return false;
  if (st.agent && m.author !== st.agent && !(m.mentions || []).includes(st.agent)) return false;
  if (st.channel && m.channel !== st.channel) return false;
  if (st.thread && m.id !== st.thread && m.thread_id !== st.thread) return false;
  return true;
}

function item(m) {
  const sys = m.kind === 'system' || m.kind === 'event';
  const li = el('li', { class: `msg kind-${m.kind}${sys ? ' system' : ''}${m.priority === 1 ? ' urgent' : ''}`, id: `m${m.id}` },
    el('div', { class: 'meta' },
      el('span', { class: 'kind' }, m.kind), ' ',
      el('strong', {}, sys ? 'Agent Office' : `@${m.author}`), ' ',
      el('span', { class: 'muted' }, `#${m.channel} · ${hora(m.ts)} · #${m.id}`),
      m.reply_to ? el('span', { class: 'muted' }, ` ↩ #${m.reply_to}`) : null,
      m.mentions?.length ? el('span', { class: 'to' }, ` → ${m.mentions.map((x) => `@${x}`).join(' ')}`) : null),
    el('div', { class: 'body' }, m.body));
  if (m.refs?.length) li.append(el('div', { class: 'refs' }, ...m.refs.map(refLink)));
  if (m.data && !sys) li.append(el('details', {}, el('summary', {}, 'data'), el('pre', {}, JSON.stringify(m.data, null, 2))));
  const acc = el('div', { class: 'acts' });
  if (!st.thread) acc.append(el('button', { type: 'button', onclick: () => abrirHilo(m.thread_id || m.id) }, 'hilo'));
  acc.append(el('button', { type: 'button', onclick: () => entregas(m, li) }, 'entregas'));
  if (st.admin) acc.append(el('button', { type: 'button', class: 'del', onclick: () => borrar(m, li) }, 'borrar'));
  li.append(acc);
  return li;
}

// Scroll QoS: new messages scroll the feed to the bottom only while the reader is already there. If they scrolled
// up to read history, the view stays put and a floating button counts the new messages; clicking it (or scrolling
// back down) jumps to the bottom and resumes following new messages.
const scroller = document.querySelector('section.feed');
const scrollState = { pegado: true, sinLeer: 0, hashHecho: false };
const alFondo = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
function pintarBoton() {
  const b = $('to-bottom');
  b.hidden = scrollState.pegado;
  b.textContent = scrollState.sinLeer ? `↓ ${scrollState.sinLeer} ${scrollState.sinLeer === 1 ? 'mensaje nuevo' : 'mensajes nuevos'}` : '↓ Ir al final';
}
function bajar() {
  scroller.scrollTop = scroller.scrollHeight;
  Object.assign(scrollState, { pegado: true, sinLeer: 0 });
  pintarBoton();
}
scroller.addEventListener('scroll', () => {
  scrollState.pegado = alFondo();
  if (scrollState.pegado) scrollState.sinLeer = 0;
  pintarBoton();
});
$('to-bottom').addEventListener('click', bajar);

function pintarFeed({ older = false, nuevos = 0 } = {}) {
  // New messages: judge "at the bottom" from the real position now, not from the last scroll event — that event
  // arrives a frame later, and a message landing in between would otherwise yank a reader who just scrolled up.
  if (nuevos) scrollState.pegado = alFondo();
  const arriba = scroller.scrollTop, desdeAbajo = scroller.scrollHeight - scroller.scrollTop;
  const lista = [...st.msgs.values()].sort((a, b) => a.id - b.id).filter(visible);
  $('feed').replaceChildren(...lista.map(item));
  $('feed-title').textContent = st.thread ? `Hilo #${st.thread}` : st.channel ? `#${st.channel}` : `Conversaciones${st.project ? ` · ${st.project}` : ''}`;
  $('back').hidden = !(st.thread || st.channel || st.agent);
  const h = location.hash.match(/m=(\d+)/);
  const objetivo = h && document.getElementById(`m${h[1]}`);
  if (objetivo) objetivo.classList.add('target');
  if (objetivo && !scrollState.hashHecho) {  // a link to one message: show it once, then behave normally
    scrollState.hashHecho = true;
    objetivo.scrollIntoView({ block: 'center' });
    scrollState.pegado = alFondo();
  } else if (scrollState.pegado) {
    bajar();
    return;
  } else if (older) {
    scroller.scrollTop = scroller.scrollHeight - desdeAbajo;  // older page prepended: keep what the reader sees
  } else {
    scroller.scrollTop = arriba;
    scrollState.sinLeer += nuevos;
  }
  pintarBoton();
}

async function cargar(older = false) {
  const q = new URLSearchParams({ limit: '100' });
  if ($('search').value.trim()) q.set('q', $('search').value.trim());
  if (older && st.oldest) q.set('before', String(st.oldest));
  if (st.project && !st.channel) q.set('project', st.project);
  const path = st.thread && st.channel ? `/api/channels/${encodeURIComponent(st.channel)}/messages?${q}&thread=${st.thread}`
    : st.channel ? `/api/channels/${encodeURIComponent(st.channel)}/messages?${q}` : `/api/feed?${q}`;
  const r = await api(path);
  if (!older) { st.msgs.clear(); scrollState.pegado = true; scrollState.sinLeer = 0; }  // new view: start at the end
  for (const m of r.messages) st.msgs.set(m.id, m);
  st.oldest = Math.min(...[...st.msgs.keys(), Infinity]);
  $('older').hidden = r.messages.length < 100;
  pintarFeed({ older });
}

async function nuevos() {
  const ultimo = Math.max(0, ...st.msgs.keys());
  const r = await api(`/api/feed?after=${ultimo}&limit=200${st.project ? `&project=${st.project}` : ''}`);
  for (const m of r.messages) st.msgs.set(m.id, m);
  if (r.messages.length) pintarFeed({ nuevos: r.messages.filter(visible).length });
}

function abrirHilo(id) {
  const m = st.msgs.get(id);
  st.thread = id;
  st.channel = m?.channel || st.channel;
  cargar();
}

async function entregas(m, li) {
  const r = await api(`/api/messages/${m.id}/deliveries`);
  li.querySelector('.deliv')?.remove();
  li.append(el('div', { class: 'deliv' }, r.recipients.length ? r.recipients.map((x) => `@${x.agent}: ${x.state}`).join(' · ') : 'sin destinatarios directos'));
}

async function borrar(m, li) {
  if (!confirm(`¿Borrar el mensaje #${m.id}? No se puede deshacer.`)) return;
  await api(`/api/admin/messages/${m.id}`, { method: 'DELETE' });
  st.msgs.delete(m.id);
  li.remove();
}

function pintarLado() {
  const s = st.snap;
  $('projects').replaceChildren(...s.projects.map((p) => el('li', { 'aria-current': String(st.project === p.name), onclick: () => { st.project = p.name; st.channel = null; st.thread = null; cargar(); pintarLado(); } }, p.name)));
  api('/api/channels').then((chs) => $('channels').replaceChildren(...chs.filter((c) => !st.project || c.project === st.project).map((c) =>
    el('li', { 'aria-current': String(st.channel === c.name), onclick: () => { st.channel = c.name; st.thread = null; cargar(); pintarLado(); } }, `#${c.name}`)))).catch(() => {});
  const ses = s.sessions.filter((x) => !st.project || x.project === st.project);
  const agentes = new Map();
  for (const x of ses) if (!agentes.has(x.agent) || x.status !== 'terminated') agentes.set(x.agent, x);
  $('agents').replaceChildren(...[...agentes.values()].sort((a, b) => a.agent.localeCompare(b.agent)).map((x) =>
    el('li', { 'aria-current': String(st.agent === x.agent), onclick: () => { st.agent = st.agent === x.agent ? null : x.agent; pintarFeed(); pintarLado(); } },
      el('span', { class: `dot ${x.status}` }), el('strong', {}, x.agent), ' ', el('span', { class: 'muted' }, `${x.owner} · ${x.runtime} · ${x.status}`))));
  const coords = Object.entries(s.coordinators).filter(([p]) => !st.project || p === st.project);
  $('coord').replaceChildren(...coords.map(([p, c]) => {
    const who = c.session_id && s.sessions.find((x) => x.id === c.session_id)?.agent;
    return el('div', { class: `stat ${c.valid ? 'ok' : 'warn'}` }, el('b', {}, p), el('span', {}, c.status === 'active' ? `coordinador ${who || '?'} · epoch ${c.epoch}` : c.status === 'offered' ? `oferta pendiente · epoch ${c.epoch}` : 'sin coordinador'));
  }));
  const tareas = s.tasks.filter((t) => !st.project || t.project === st.project).filter((t) => !st.agent || t.assignee === st.agent);
  $('tasks').replaceChildren(...(tareas.length ? tareas.map((t) => el('li', { class: `task ${t.status}` },
    el('strong', {}, `#${t.id} `), t.title, el('div', { class: 'muted' }, `${t.status} · p${t.priority}${t.assignee ? ` · @${t.assignee}` : ''}${t.lease_epoch ? ` · epoch ${t.lease_epoch}` : ''}${t.block_reason ? ` · ${t.block_reason}` : ''}`))) : [el('li', { class: 'muted' }, 'sin tareas')]));
  $('leases').replaceChildren(...(s.leases.length ? s.leases.map((l) => el('li', {}, el('code', {}, l.resource), ` @${l.holder}`, el('div', { class: 'muted' }, `${l.task_id ? `task #${l.task_id} · ` : ''}vence ${new Date(l.expires_at).toLocaleTimeString()}`))) : [el('li', { class: 'muted' }, 'sin leases')]));
  const det = st.agent && ses.filter((x) => x.agent === st.agent);
  $('agent-detail').replaceChildren(...(det?.length ? det.map((x) => el('div', { class: 'agent' },
    el('strong', {}, `${x.agent}${x.parent_id ? ' (subagente)' : ''}`),
    el('div', { class: 'muted' }, `propietario ${x.owner} · ${x.runtime}${x.model ? ` · ${x.model}` : ''} · ${x.specialization || '—'} · planta ${x.floor}`),
    el('div', {}, `estado: ${x.status}${x.progress_note ? ` — ${x.progress_note}` : ''}`),
    el('div', { class: 'muted' }, `sesión ${x.id} · último latido ${hora(new Date(x.last_heartbeat_at).toISOString())}`))) : [document.createTextNode('Selecciona un agente')]));
}

function pintarRecuperacion() {
  $('recovery').replaceChildren(...st.recovery.slice(-40).reverse().map((e) => {
    const c = clasificar(e);
    return el('li', { class: 'ev', style: `--cc:${CATEGORIAS[c.cat].color}` }, el('strong', {}, c.titulo), el('div', { class: 'muted' }, `${hora(new Date(e.ts).toISOString())}${e.task_id ? ` · task #${e.task_id}` : ''}${e.project ? ` · ${e.project}` : ''}${e.data?.reason ? ` · ${e.data.reason}` : ''}${e.data?.epoch ? ` · epoch ${e.data.epoch}` : ''}`));
  }));
}

function arrancar() {
  if (!token.get()) { $('login').hidden = false; return; }
  $('login').hidden = true;
  $('app').hidden = false;
  api('/api/me').then((me) => { st.admin = me.role === 'admin'; $('whoami').textContent = `${me.agent} (${me.role})`; cargar(); }).catch((e) => { if (e.status === 401) salir('Token inválido o revocado'); });
  st.conn = conectar({
    onSnapshot: (s) => { st.snap = s; pintarLado(); },
    onEvents: (evs) => {
      const recov = evs.filter((e) => /^(coordinator|session\.(expired|stalled|ended)|task\.(orphaned|recovered|stale_write|wait_timeout|blocked)|delivery\.overdue|lease\.expired)/.test(e.type));
      if (recov.length) { st.recovery.push(...recov); pintarRecuperacion(); }
      if (evs.some((e) => e.type === 'message.created')) nuevos().catch(() => {});
      if (evs.some((e) => e.type !== 'message.created')) st.conn?.snapshot().catch(() => {});
    },
    onEstado: (texto, clase) => { $('conn').textContent = texto; $('conn').className = `pill ${clase}`; },
    onAuth: () => salir('Token inválido o revocado'),
  });
}
function salir(msg) { st.conn?.cerrar(); token.clear(); $('app').hidden = true; $('login').hidden = false; $('login-error').textContent = msg || ''; }

for (const k of KINDS) $('kind').append(new Option(k, k));
$('login-form').addEventListener('submit', (e) => { e.preventDefault(); token.set($('token').value.trim()); arrancar(); });
$('logout').addEventListener('click', () => salir());
$('back').addEventListener('click', () => { st.thread = null; st.channel = null; st.agent = null; cargar(); pintarLado(); });
$('older').addEventListener('click', () => cargar(true));
let espera = 0;
$('search').addEventListener('input', () => { clearTimeout(espera); espera = setTimeout(() => cargar(), 300); });
for (const id of ['kind', 'hide-system', 'only-mine']) $(id).addEventListener('input', pintarFeed);
arrancar();
