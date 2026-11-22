// Agent Office · task manager for project administrators.
// Reads one model (GET /api/board) and writes only through the regular endpoints (tasks, messages, coordinator),
// so the server applies the same permissions, fencing, events and notifications as for agents. Untrusted text is
// only ever assigned via textContent, and the token stays in sessionStorage (shared with /chat and the building).
import { token, conectar } from '/office/datos.js';

const $ = (id) => document.getElementById(id);

const ESTADO = {
  orphaned: { txt: 'Huérfana', ico: '⚠', orden: 0 },
  blocked: { txt: 'Bloqueada', ico: '■', orden: 1 },
  open: { txt: 'Pendiente', ico: '○', orden: 2 },
  pending: { txt: 'Sin atender', ico: '✉', orden: 3 },
  claimed: { txt: 'Reclamada', ico: '◔', orden: 4 },
  in_progress: { txt: 'En curso', ico: '▶', orden: 5 },
  review: { txt: 'En revisión', ico: '◎', orden: 6 },
  acknowledged: { txt: 'Recibido', ico: '✓', orden: 7 },
  done: { txt: 'Hecha', ico: '✔', orden: 8 },
  failed: { txt: 'Fallida', ico: '✖', orden: 9 },
  cancelled: { txt: 'Cancelada', ico: '–', orden: 10 },
};
const TIPO = { task: 'Tarea', handoff: 'Handoff', alert: 'Alerta', question: 'Pregunta', review: 'Revisión' };
const PRIO = { 1: 'Alta', 2: 'Normal', 3: 'Baja' };
const KIND_TXT = { task: 'Tarea', chat: 'Chat', question: 'Pregunta', handoff: 'Handoff', alert: 'Alerta', review: 'Revisión',
  decision: 'Decisión', context: 'Contexto', status: 'Estado', answer: 'Respuesta' };
const GRUPOS = ['canal', 'estado', 'responsable', 'prioridad'];

const url = new URLSearchParams(location.search);
const st = {
  project: url.get('project') || '', group: GRUPOS.includes(url.get('group')) ? url.get('group') : 'canal',
  view: url.get('view') === 'lista' ? 'lista' : 'tablero', type: url.get('type') || '', who: url.get('who') || '',
  prio: url.get('prio') || '', q: url.get('q') || '',
  board: null, panel: null, conn: null, me: null,
};

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}

/** fetch with the token; errors carry the server's message (shown to the admin as is, via textContent). */
async function pedir(path, opts = {}) {
  const r = await fetch(path, { ...opts, cache: 'no-store', credentials: 'omit',
    headers: { Authorization: `Bearer ${token.get()}`, ...(opts.body ? { 'Content-Type': 'application/json' } : {}) } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(body.message || `HTTP ${r.status}`);
    Object.assign(e, { status: r.status, body });
    if (r.status === 401) salir('Token inválido o revocado');
    throw e;
  }
  return body;
}
const enviar = (path, method, data) => pedir(path, { method, body: JSON.stringify(data) });

const hace = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 90 ? 'ahora' : s < 5400 ? `hace ${Math.round(s / 60)} min` : s < 129600 ? `hace ${Math.round(s / 3600)} h` : `hace ${Math.round(s / 86400)} d`;
};
const primeraLinea = (t) => { const l = String(t).split('\n')[0]; return l.length > 140 ? `${l.slice(0, 139)}…` : l; };

// ------------------------------------------------------------------------------------------- model -> cards
/** Tasks and message items as one list of cards with the fields every grouping and filter needs. */
function tarjetas() {
  const b = st.board;
  const out = b.tasks.map((t) => ({
    key: `t${t.id}`, type: 'task', id: t.id, title: t.title, channel: t.channel, priority: t.priority, status: t.status,
    who: t.assignee ? [t.assignee] : [], updated: t.updated, raw: t,
    flags: [t.lease_valid ? 'lease activo' : t.lease_expires ? 'lease caducado' : null, t.depends_on.length && !t.ready ? 'espera dependencias' : null].filter(Boolean),
  }));
  for (const m of b.items) out.push({
    key: `m${m.id}`, type: m.type, id: m.id, title: primeraLinea(m.body), channel: m.channel, priority: m.priority, status: m.status,
    who: m.to.map((x) => x.agent), updated: m.created, raw: m, flags: [`de @${m.author}`],
  });
  return out;
}

function pasaFiltros(c) {
  if (st.type && c.type !== st.type) return false;
  if (st.who === '-' ? c.who.length : st.who && !c.who.includes(st.who)) return false;
  if (st.prio && String(c.priority) !== st.prio) return false;
  if (st.q) {
    const q = st.q.toLowerCase();
    const texto = `${c.title} ${c.raw.description || c.raw.body || ''} #${c.id} ${c.channel} ${c.who.join(' ')}`.toLowerCase();
    if (!texto.includes(q)) return false;
  }
  return true;
}

/** Groups as [{key, title, cards}] in a stable, meaningful order. Every channel appears, even when empty. */
function agrupar(cards) {
  const g = new Map();
  const add = (k, title, orden) => g.get(k) || g.set(k, { key: k, title, orden, cards: [] }).get(k);
  if (st.group === 'canal') for (const ch of st.board.channels) add(ch.name, `#${ch.name}`, ch.name);
  if (st.group === 'prioridad') for (const p of [1, 2, 3]) add(String(p), `Prioridad ${PRIO[p].toLowerCase()}`, p);
  for (const c of cards) {
    if (st.group === 'canal') add(c.channel, `#${c.channel}`, c.channel).cards.push(c);
    else if (st.group === 'estado') { const e = ESTADO[c.status] || { txt: c.status, ico: '·', orden: 99 }; add(c.status, `${e.ico} ${e.txt}`, e.orden).cards.push(c); }
    else if (st.group === 'prioridad') add(String(c.priority), `Prioridad ${PRIO[c.priority].toLowerCase()}`, c.priority).cards.push(c);
    else if (!c.who.length) add('-', 'Sin asignar', '\u0000').cards.push(c);
    else for (const w of c.who) add(w, `@${w}`, w).cards.push(c);
  }
  const lista = [...g.values()].sort((a, b) => (a.orden < b.orden ? -1 : a.orden > b.orden ? 1 : 0));
  for (const x of lista) x.cards.sort((a, b) => a.priority - b.priority || (ESTADO[a.status]?.orden ?? 99) - (ESTADO[b.status]?.orden ?? 99) || a.id - b.id);
  return lista;
}

// ------------------------------------------------------------------------------------------------ render
function chipEstado(status) {
  const e = ESTADO[status] || { txt: status, ico: '·' };
  return el('span', { class: `chip st-${status}` }, el('span', { 'aria-hidden': 'true' }, e.ico), ` ${e.txt}`);
}
const chipTipo = (type) => el('span', { class: `chip ty-${type}` }, TIPO[type] || type);
const chipPrio = (p) => el('span', { class: `chip p${p}`, title: `Prioridad ${PRIO[p]}` }, `P${p} ${PRIO[p]}`);

function tarjeta(c) {
  const sel = st.panel?.key === c.key;
  const abrir = () => abrirPanel(c.key);
  return el('article', { class: `card p${c.priority}${sel ? ' sel' : ''}`, tabindex: '0', role: 'button', 'aria-pressed': String(sel),
    'aria-label': `${TIPO[c.type]} ${c.type === 'task' ? `#${c.id} ` : ''}${c.title}`, 'data-key': c.key,
    onclick: abrir, onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); abrir(); } } },
  el('div', { class: 'card-top' }, chipTipo(c.type), chipPrio(c.priority), chipEstado(c.status)),
  el('div', { class: 'card-title' }, c.type === 'task' ? el('span', { class: 'muted' }, `#${c.id} `) : null, c.title),
  el('div', { class: 'card-meta muted' },
    st.group !== 'canal' ? `#${c.channel} · ` : '',
    c.who.length ? c.who.map((w) => `@${w}`).join(' ') : 'sin asignar', ' · ', hace(c.updated),
    c.flags.length ? ` · ${c.flags.join(' · ')}` : ''));
}

function pintar() {
  const b = st.board;
  if (!b) return;
  for (const x of document.querySelectorAll('[data-group]')) x.setAttribute('aria-pressed', String(x.dataset.group === st.group));
  for (const x of document.querySelectorAll('[data-view]')) x.setAttribute('aria-pressed', String(x.dataset.view === st.view));
  const todas = tarjetas();
  const cards = todas.filter(pasaFiltros);
  const grupos = agrupar(cards);
  const n = (t) => cards.filter((c) => (t === 'task' ? c.type === 'task' : c.type !== 'task')).length;
  $('summary').textContent = `${n('task')} tareas · ${n('items')} avisos${cards.length !== todas.length ? ` (de ${todas.length})` : ''}`;
  const board = $('board');
  board.className = `board ${st.view}`;
  if (!todas.length) {
    board.replaceChildren(el('div', { class: 'empty' }, el('strong', {}, 'No hay tareas ni avisos abiertos en este proyecto.'),
      el('p', { class: 'muted' }, 'Crea una tarea o envía un mensaje con «+ Nuevo». Las tareas terminadas o canceladas no aparecen aquí.')));
  } else if (!cards.length) {
    board.replaceChildren(el('div', { class: 'empty' }, el('strong', {}, 'Nada coincide con los filtros.'),
      el('button', { type: 'button', onclick: limpiarFiltros }, 'Quitar filtros')));
  } else if (st.view === 'tablero') {
    board.replaceChildren(...grupos.map((g) => el('section', { class: 'col', 'aria-label': g.title },
      el('h2', {}, g.title, el('span', { class: 'count' }, String(g.cards.length))),
      g.cards.length ? g.cards.map(tarjeta) : el('p', { class: 'muted vacio' }, 'Sin elementos'))));
  } else {
    board.replaceChildren(el('table', { class: 'tabla' },
      el('thead', {}, el('tr', {}, ...['Tipo', 'Título', 'Estado', 'Prioridad', 'Canal', 'Responsable', 'Actualizado'].map((h) => el('th', { scope: 'col' }, h)))),
      ...grupos.filter((g) => g.cards.length).map((g) => el('tbody', {},
        el('tr', { class: 'grupo' }, el('th', { colspan: '7', scope: 'colgroup' }, `${g.title} (${g.cards.length})`)),
        ...g.cards.map((c) => el('tr', { class: st.panel?.key === c.key ? 'sel' : null, tabindex: '0', 'data-key': c.key,
          onclick: () => abrirPanel(c.key), onkeydown: (e) => { if (e.key === 'Enter') abrirPanel(c.key); } },
        el('td', {}, chipTipo(c.type)), el('td', {}, c.type === 'task' ? `#${c.id} ` : '', c.title), el('td', {}, chipEstado(c.status)),
        el('td', {}, chipPrio(c.priority)), el('td', {}, `#${c.channel}`), el('td', {}, c.who.map((w) => `@${w}`).join(' ') || '—'),
        el('td', { class: 'muted' }, hace(c.updated))))))));
  }
  pintarCoordinador();
  pintarFiltros();
  refrescarPanel();
}

function pintarFiltros() {
  const b = st.board;
  const tipos = [['', 'Todos los tipos'], ...Object.entries(TIPO).map(([k, v]) => [k, v])];
  rellenar($('f-type'), tipos, st.type);
  const agentes = [...new Set([...b.agents.map((a) => a.name), ...tarjetas().flatMap((c) => c.who)])].sort();
  rellenar($('f-who'), [['', 'Cualquier responsable'], ['-', 'Sin asignar'], ...agentes.map((a) => [a, `@${a}`])], st.who);
  rellenar($('f-prio'), [['', 'Cualquier prioridad'], ...[1, 2, 3].map((p) => [String(p), `P${p} ${PRIO[p]}`])], st.prio);
}
function rellenar(sel, opciones, valor) {
  const claves = opciones.map(([k]) => k).join('|');
  if (sel.dataset.claves !== claves) { sel.replaceChildren(...opciones.map(([k, t]) => new Option(t, k))); sel.dataset.claves = claves; }
  sel.value = opciones.some(([k]) => k === valor) ? valor : '';
}

function pintarCoordinador() {
  const c = st.board.coordinator;
  const ses = st.board.sessions;
  const estado = c.status === 'active' ? (c.valid ? `@${c.agent || '?'}` : `@${c.agent || '?'} (sin renovar)`)
    : c.status === 'offered' ? `oferta pendiente a @${c.agent || '?'}` : 'sin coordinador';
  const sel = el('select', { id: 'coord-sel', 'aria-label': 'Elegir coordinador' },
    new Option(ses.length ? 'Elegir sesión…' : 'No hay sesiones activas', ''),
    ...ses.map((s) => new Option(`@${s.agent} · ${s.runtime} · ${s.status}`, s.id)));
  const prev = $('coord-sel')?.value;
  if (prev && ses.some((s) => s.id === prev)) sel.value = prev;
  $('coord').replaceChildren(
    el('span', { class: `coord-state ${c.status === 'active' && c.valid ? 'ok' : 'warn'}`, title: `Coordinador: ${estado}` }, 'Coordinador: ', el('strong', {}, estado)),
    sel, el('button', { type: 'button', disabled: !ses.length, onclick: () => designar(sel.value) }, 'Designar'));
}

async function designar(sid) {
  if (!sid) return aviso('Elige una sesión para coordinar el proyecto.');
  const c = st.board.coordinator;
  const s = st.board.sessions.find((x) => x.id === sid);
  const activo = c.status === 'active' && c.valid;
  if (activo && !confirm(`El proyecto ya tiene coordinador (@${c.agent}). ¿Proponer a @${s.agent} en su lugar?`)) return;
  try {
    await enviar(`/api/projects/${encodeURIComponent(st.project)}/coordinator/designate`, 'POST', { session_id: sid, replace: activo });
    aviso(`Oferta enviada a @${s.agent}. Será coordinador cuando la acepte.`);
    recargar();
  } catch (e) { aviso(`No se pudo designar: ${e.message}`, true); }
}

// ------------------------------------------------------------------------------------------------- panel
function buscar(key) { return tarjetas().find((c) => c.key === key); }

function abrirPanel(key) {
  st.panel = { key, sucio: false, version: null };
  pintar();  // redraws the board (selection) and the panel
  $('panel').querySelector('input, textarea, button')?.focus();
}
function cerrarPanel() {
  const key = st.panel?.key;
  st.panel = null;
  $('panel').hidden = true;
  pintar();
  document.querySelector(`[data-key="${key}"]`)?.focus();
}

/** After a reload: redraw the panel unless the admin is typing; if the task changed meanwhile, say so. */
function refrescarPanel() {
  if (!st.panel) return;
  const c = buscar(st.panel.key);
  const nota = $('panel-stale');
  if (!st.panel.sucio) { pintarPanel(); return; }
  if (nota && (!c || (c.type === 'task' && c.raw.version !== st.panel.version))) {
    nota.hidden = false;
    nota.replaceChildren(c ? 'Alguien ha cambiado esta tarea mientras la editabas. ' : 'Este elemento ya no está abierto. ',
      el('button', { type: 'button', onclick: () => { st.panel.sucio = false; pintarPanel(); } }, c ? 'Ver la versión actual' : 'Cerrar'));
  }
}

function pintarPanel() {
  const p = $('panel');
  const c = buscar(st.panel.key);
  p.hidden = false;
  if (!c) {
    p.replaceChildren(cabecera('Ya no está abierto'), el('p', { class: 'muted' }, 'Se ha terminado, cancelado o resuelto.'));
    return;
  }
  st.panel.version = c.raw.version ?? null;
  p.replaceChildren(...(c.type === 'task' ? panelTarea(c.raw) : panelAviso(c.raw)));
}
function cabecera(titulo) {
  return el('div', { class: 'panel-head' }, el('h2', { id: 'panel-title' }, titulo),
    el('button', { type: 'button', class: 'x', 'aria-label': 'Cerrar', onclick: cerrarPanel }, '✕'));
}
const campo = (etiqueta, control) => el('label', { class: 'field' }, el('span', {}, etiqueta), control);
const marcarSucio = () => { if (st.panel) st.panel.sucio = true; };

function panelTarea(t) {
  const b = st.board;
  const titulo = el('input', { value: t.title, maxlength: '200', oninput: marcarSucio });
  const desc = el('textarea', { rows: '6', maxlength: '16000', oninput: marcarSucio });
  desc.value = t.description || '';
  const prio = el('select', { onchange: marcarSucio }, ...[1, 2, 3].map((x) => new Option(`P${x} ${PRIO[x]}`, String(x))));
  prio.value = String(t.priority);
  const estados = [t.status, ...(b.transitions[t.status] || [])];
  const estado = el('select', { onchange: marcarSucio }, ...estados.map((s) => new Option(`${ESTADO[s]?.ico || ''} ${ESTADO[s]?.txt || s}${s === t.status ? ' (actual)' : ''}`, s)));
  estado.value = t.status;
  const quien = el('select', { onchange: marcarSucio }, new Option('— Sin asignar —', ''), ...b.agents.map((a) => new Option(`@${a.name}${a.owner ? ` · ${a.owner}` : ''}`, a.name)));
  if (t.assignee && !b.agents.some((a) => a.name === t.assignee)) quien.append(new Option(`@${t.assignee}`, t.assignee));
  quien.value = t.assignee || '';
  const nota = el('input', { placeholder: 'Opcional: se publica en el canal con el cambio', maxlength: '2000', oninput: marcarSucio });
  const error = el('p', { class: 'error', role: 'alert' });
  const guardar = async (e) => {
    e.preventDefault();
    const cambios = { if_version: t.version };
    if (titulo.value.trim() !== t.title) cambios.title = titulo.value.trim();
    if (desc.value !== (t.description || '')) cambios.description = desc.value;
    if (Number(prio.value) !== t.priority) cambios.priority = Number(prio.value);
    if (quien.value !== (t.assignee || '')) cambios.assignee = quien.value || null;
    if (estado.value !== t.status) cambios.status = estado.value;
    if (nota.value.trim()) cambios.note = nota.value.trim();
    if (Object.keys(cambios).length === 1) { error.textContent = 'No hay cambios que guardar.'; return; }
    if (t.lease_valid && cambios.assignee !== undefined && !confirm(`@${t.assignee} está trabajando en esta tarea. ¿Quitársela?`)) return;
    try {
      await enviar(`/api/tasks/${t.id}`, 'PATCH', cambios);
      st.panel.sucio = false;
      aviso(cambios.assignee ? `Guardado. @${cambios.assignee} recibe el aviso en #${t.channel}.` : 'Guardado.');
      recargar();
    } catch (err) {
      error.textContent = err.status === 409 && err.body?.current ? 'Otra persona o agente cambió la tarea a la vez. Revisa la versión actual antes de guardar.' : err.message;
      if (err.status === 409 && err.body?.current) { st.panel.sucio = false; recargar(); }
    }
  };
  const info = [
    ['Canal', `#${t.channel}`], ['Creada por', `@${t.created_by}`], ['Actualizada', hace(t.updated)],
    t.lease_expires ? ['Lease', `${t.lease_valid ? 'activo' : 'caducado'} · vence ${new Date(t.lease_expires).toLocaleTimeString()}`] : null,
    t.last_progress ? ['Último progreso', hace(t.last_progress)] : null,
    t.block_reason ? ['Motivo de bloqueo', t.block_reason] : null,
    t.depends_on.length ? ['Depende de', `${t.depends_on.map((d) => `#${d}`).join(', ')}${t.ready ? ' (listas)' : ' (pendientes)'}`] : null,
  ].filter(Boolean);
  const historial = el('ol', { class: 'cps' }, el('li', { class: 'muted' }, 'Cargando…'));
  pedir(`/api/tasks/${t.id}/checkpoints`).then((cps) => historial.replaceChildren(...(cps.length ? cps.slice(0, 10).map((cp) =>
    el('li', {}, el('div', { class: 'muted' }, `${hace(cp.at || cp.created)} · @${cp.author || '?'}`), el('pre', {}, resumenCheckpoint(cp))))
    : [el('li', { class: 'muted' }, 'Sin checkpoints todavía.')]))).catch(() => historial.replaceChildren(el('li', { class: 'muted' }, 'No disponible.')));
  return [
    cabecera(`Tarea #${t.id}`),
    el('p', { id: 'panel-stale', class: 'stale', hidden: true, role: 'status' }),
    el('div', { class: 'card-top' }, chipEstado(t.status), chipPrio(t.priority)),
    el('form', { class: 'edit', onsubmit: guardar },
      campo('Título', titulo), campo('Descripción', desc),
      el('div', { class: 'two' }, campo('Prioridad', prio), campo('Estado', estado)),
      campo('Responsable', quien),
      el('p', { class: 'hint' }, 'Al asignar, el agente recibe un aviso en el canal de la tarea con su misma prioridad. Para que pase a «En curso» el agente debe reclamarla.'),
      campo('Nota', nota), error,
      el('div', { class: 'row' }, el('button', { type: 'submit', class: 'primary' }, 'Guardar cambios'))),
    el('dl', { class: 'info' }, ...info.flatMap(([k, val]) => [el('dt', {}, k), el('dd', {}, val)])),
    el('h3', {}, 'Checkpoints'), historial,
  ];
}
function resumenCheckpoint(cp) {
  const { id, at, created, author, epoch, lease_epoch, session_id, task_id, ...resto } = cp;
  const d = resto.data && typeof resto.data === 'object' ? resto.data : resto;
  return Object.entries(d).map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join('\n') || '—';
}

function panelAviso(m) {
  const texto = el('textarea', { rows: '4', maxlength: '16000', placeholder: m.type === 'question' ? 'Tu respuesta…' : 'Responder en el hilo…', oninput: marcarSucio });
  const resolver = el('input', { type: 'checkbox', checked: true });
  const error = el('p', { class: 'error', role: 'alert' });
  const responder = async (e) => {
    e.preventDefault();
    const body = texto.value.trim() || (resolver.checked ? `Resuelto por @${st.me.agent}.` : '');
    if (!body) { error.textContent = 'Escribe una respuesta o marca «resuelto».'; return; }
    const data = { kind: m.type === 'question' ? 'answer' : 'chat', body, reply_to: m.id, priority: m.priority,
      mentions: [...new Set([m.author, ...m.to.map((x) => x.agent)])].filter((x) => x !== st.me.agent && x !== 'admin') };
    if (resolver.checked) data.data = { resolves: m.id };
    try {
      await enviar(`/api/channels/${encodeURIComponent(m.channel)}/messages`, 'POST', data);
      st.panel.sucio = false;
      aviso(resolver.checked ? 'Respondido y marcado como resuelto.' : 'Respuesta enviada.');
      recargar();
    } catch (err) { error.textContent = err.message; }
  };
  return [
    cabecera(`${TIPO[m.type] || m.type} #${m.id}`),
    el('div', { class: 'card-top' }, chipTipo(m.type), chipPrio(m.priority), chipEstado(m.status)),
    el('p', { class: 'muted' }, `@${m.author} en #${m.channel} · ${hace(m.created)}`),
    el('div', { class: 'body' }, m.body),
    el('h3', {}, 'Destinatarios'),
    m.to.length ? el('ul', { class: 'dest' }, ...m.to.map((x) => el('li', {}, `@${x.agent}: `, el('span', { class: `dv ${x.state}` }, ENTREGA[x.state] || x.state))))
      : el('p', { class: 'muted' }, 'Sin destinatarios directos (visible para todo el canal).'),
    el('form', { class: 'edit', onsubmit: responder },
      campo('Respuesta', texto),
      el('label', { class: 'check' }, resolver, ' Marcar como resuelto (deja de aparecer aquí)'), error,
      el('div', { class: 'row' }, el('button', { type: 'submit', class: 'primary' }, 'Enviar'))),
    el('p', { class: 'hint' }, el('a', { href: `/chat#m=${m.id}` }, 'Ver la conversación en el chat')),
  ];
}
const ENTREGA = { queued: 'en cola', delivered_to_supervisor: 'entregado al equipo', injected_to_runtime: 'entregado al agente', acknowledged: 'leído', processed: 'atendido' };

// ---------------------------------------------------------------------------------------------- composer
function abrirComposer() {
  const b = st.board;
  rellenar($('c-type'), [['task', 'Tarea'], ...b.kinds.filter((k) => k !== 'task' && k !== 'answer').map((k) => [k, KIND_TXT[k] || k])], $('c-type').value || 'task');
  rellenar($('c-channel'), b.channels.map((c) => [c.name, `#${c.name}`]), $('c-channel').value);
  rellenar($('c-assignee'), [['', '— Sin asignar —'], ...b.agents.map((a) => [a.name, `@${a.name}`])], '');
  rellenar($('c-prio'), [1, 2, 3].map((p) => [String(p), `P${p} ${PRIO[p]}`]), '2');
  $('c-to').replaceChildren(...b.agents.filter((a) => a.name !== st.me.agent).map((a) => el('label', {}, el('input', { type: 'checkbox', value: a.name }), ` @${a.name}`)));
  $('c-error').textContent = '';
  if (!b.channels.length) { aviso('Este proyecto no tiene canales todavía.', true); return; }
  ajustarComposer();
  $('composer').showModal();
  $('c-type').focus();
}
function ajustarComposer() {
  const tarea = $('c-type').value === 'task';
  $('c-title-row').hidden = !tarea;
  $('c-assignee-row').hidden = !tarea;
  $('c-to-row').hidden = tarea;
  $('c-title').required = tarea;
  $('c-body').required = !tarea;
  $('c-body-label').textContent = tarea ? 'Descripción' : 'Texto';
  $('c-send').textContent = tarea ? 'Crear tarea' : 'Enviar mensaje';
  $('c-hint').textContent = tarea ? 'Si eliges responsable, recibe el aviso en el canal con la prioridad de la tarea.'
    : 'Los mencionados lo reciben en su bandeja; la prioridad alta interrumpe al agente.';
}
async function enviarComposer(e) {
  e.preventDefault();
  const ch = $('c-channel').value, prio = Number($('c-prio').value);
  try {
    if ($('c-type').value === 'task') {
      const t = await enviar(`/api/channels/${encodeURIComponent(ch)}/tasks`, 'POST',
        { title: $('c-title').value.trim(), description: $('c-body').value, priority: prio, assignee: $('c-assignee').value || undefined });
      aviso(`Tarea #${t.id} creada en #${ch}.`);
    } else {
      const mentions = [...$('c-to').querySelectorAll('input:checked')].map((x) => x.value);
      await enviar(`/api/channels/${encodeURIComponent(ch)}/messages`, 'POST', { kind: $('c-type').value, body: $('c-body').value, priority: prio, mentions });
      aviso(`Mensaje enviado a #${ch}${mentions.length ? ` para ${mentions.map((m) => `@${m}`).join(' ')}` : ''}.`);
    }
    $('composer-form').reset();
    $('composer').close();
    recargar();
  } catch (err) { $('c-error').textContent = err.message; }
}

// ------------------------------------------------------------------------------------------------- shell
let avisoT = 0;
function aviso(texto, malo = false) {
  let n = $('toast');
  if (!n) { n = el('div', { id: 'toast', role: 'status' }); document.body.append(n); }
  n.className = malo ? 'malo' : '';
  n.textContent = texto;
  n.hidden = false;
  clearTimeout(avisoT);
  avisoT = setTimeout(() => { n.hidden = true; }, 5000);
}

function guardarUrl() {
  const q = new URLSearchParams();
  for (const k of ['project', 'group', 'view', 'type', 'who', 'prio', 'q']) if (st[k] && !(k === 'group' && st[k] === 'canal') && !(k === 'view' && st[k] === 'tablero')) q.set(k, st[k]);
  history.replaceState(null, '', `${location.pathname}${String(q) ? `?${q}` : ''}`);
}
function limpiarFiltros() { Object.assign(st, { type: '', who: '', prio: '', q: '' }); $('f-q').value = ''; guardarUrl(); pintar(); }

let cargando = null, otraVez = false, espera = 0;
async function recargar() {
  if (cargando) { otraVez = true; return cargando; }
  cargando = (async () => {
    try {
      st.board = await pedir(`/api/board?project=${encodeURIComponent(st.project)}`);
      $('denied').hidden = true;
      $('toolbar').hidden = false;
      pintar();
    } catch (e) {
      if (e.status === 403 || e.status === 404) mostrarDenegado(e.message);
    }
  })();
  await cargando;
  cargando = null;
  if (otraVez) { otraVez = false; recargar(); }
}
const pronto = () => { clearTimeout(espera); espera = setTimeout(recargar, 300); };

function mostrarDenegado(msg) {
  $('toolbar').hidden = true;
  $('board').replaceChildren();
  $('panel').hidden = true;
  $('coord').replaceChildren();
  $('denied').hidden = false;
  $('denied').textContent = msg;
}

async function arrancar() {
  if (!token.get()) { $('login').hidden = false; return; }
  $('login').hidden = true;
  $('app').hidden = false;
  try {
    st.me = await pedir('/api/me');
    $('whoami').textContent = `${st.me.agent} (${st.me.role})`;
    const proyectos = await pedir('/api/board/projects');
    if (!proyectos.length) {
      mostrarDenegado('Esta página es para administradores: tu token no administra ningún proyecto (hace falta rol admin global o admin del proyecto). Puedes seguir el trabajo en el chat.');
      return;
    }
    // without ?project=, open the first project that has work rather than an empty one
    if (!proyectos.some((p) => p.name === st.project)) st.project = (proyectos.find((p) => p.open_tasks) || proyectos[0]).name;
    $('project').replaceChildren(...proyectos.map((p) => new Option(`${p.name} (${p.open_tasks})`, p.name)));
    $('project').value = st.project;
    guardarUrl();
    await recargar();
  } catch (e) {
    if (e.status !== 401) mostrarDenegado(`No se pudo cargar: ${e.message}`);
    return;
  }
  st.conn = conectar({
    onSnapshot: pronto,  // every reconnection and the periodic refresh: derived states (leases, sessions) move with time
    onEvents: pronto,
    onEstado: (texto, clase) => { $('conn').textContent = texto; $('conn').className = `pill ${clase}`; },
    onAuth: () => salir('Token inválido o revocado'),
  });
}
function salir(msg) {
  st.conn?.cerrar();
  st.conn = null;
  token.clear();
  $('app').hidden = true;
  $('login').hidden = false;
  $('login-error').textContent = msg || '';
}

$('login-form').addEventListener('submit', (e) => { e.preventDefault(); token.set($('token').value.trim()); arrancar(); });
$('logout').addEventListener('click', () => salir());
$('project').addEventListener('change', () => { st.project = $('project').value; st.panel = null; $('panel').hidden = true; guardarUrl(); recargar(); });
for (const b of document.querySelectorAll('[data-group]')) b.addEventListener('click', () => { st.group = b.dataset.group; guardarUrl(); pintar(); });
for (const b of document.querySelectorAll('[data-view]')) b.addEventListener('click', () => { st.view = b.dataset.view; guardarUrl(); pintar(); });
for (const [id, k] of [['f-type', 'type'], ['f-who', 'who'], ['f-prio', 'prio']]) $(id).addEventListener('change', () => { st[k] = $(id).value; guardarUrl(); pintar(); });
let tecleo = 0;
$('f-q').value = st.q;
$('f-q').addEventListener('input', () => { clearTimeout(tecleo); tecleo = setTimeout(() => { st.q = $('f-q').value.trim(); guardarUrl(); pintar(); }, 200); });
$('new').addEventListener('click', abrirComposer);
$('c-type').addEventListener('change', ajustarComposer);
$('composer-form').addEventListener('submit', enviarComposer);
$('c-cancel').addEventListener('click', () => $('composer').close());
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && st.panel && !$('composer').open) cerrarPanel(); });
arrancar();
