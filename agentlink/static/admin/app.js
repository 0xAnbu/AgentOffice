// Agent Office · administration page (global admins only).
// Everything ./admin does (users, tokens, channels, messages, audit log) plus projects and a client-onboarding command
// builder. Every change is a regular admin API call, so the server applies its own permission checks. Untrusted text
// is only ever assigned via textContent; the token stays in sessionStorage (shared with /chat, /tareas and the building).
import { token } from '/office/datos.js';

const $ = (id) => document.getElementById(id);
const TABS = ['usuarios', 'canales', 'proyectos', 'conectar', 'mensajes', 'registro'];
const ROLES = { member: 'member', monitor: 'monitor', admin: 'admin' };
const BOOTSTRAP_ADMIN = 'admin';  // controlled by AGENTLINK_ADMIN_TOKEN: can't be disabled or deleted
const PLACEHOLDER = '<TOKEN>';

const st = { me: null, agents: [], channels: [], projects: [], audit: [], tab: 'usuarios', connectToken: null };

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}

async function pedir(path, opts = {}) {
  const r = await fetch(path, { ...opts, cache: 'no-store', credentials: 'omit',
    headers: { Authorization: `Bearer ${token.get()}`, ...(opts.body ? { 'Content-Type': 'application/json' } : {}) } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(body.message || body.error?.message || `HTTP ${r.status}`);
    Object.assign(e, { status: r.status, body });
    if (r.status === 401) salir('Token inválido o revocado');
    throw e;
  }
  return body;
}
const enviar = (path, method, data) => pedir(path, { method, body: JSON.stringify(data ?? {}) });
const enc = encodeURIComponent;

const fecha = (ms) => (ms ? new Date(ms).toLocaleString('es-ES', { dateStyle: 'short', timeStyle: 'short' }) : '—');
const hace = (ms) => {
  if (!ms) return 'nunca';
  const s = Math.max(0, (Date.now() - ms) / 1000);
  return s < 90 ? 'ahora' : s < 5400 ? `hace ${Math.round(s / 60)} min` : s < 129600 ? `hace ${Math.round(s / 3600)} h` : `hace ${Math.round(s / 86400)} d`;
};

let toastT = 0;
function aviso(msg, malo = false) {
  const t = $('toast');
  t.textContent = msg;
  t.className = malo ? 'malo' : '';
  t.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => { t.hidden = true; }, malo ? 6000 : 3000);
}

async function copiar(texto, boton) {
  try {
    await navigator.clipboard.writeText(texto);
  } catch {
    // http:// outside localhost has no clipboard API: fall back to a selection the user can copy by hand
    const ta = el('textarea', { class: 'offscreen' }, texto);
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    if (!ok) { aviso('No se pudo copiar: selecciona el texto y cópialo a mano', true); return; }
  }
  if (boton) { const o = boton.textContent; boton.textContent = '✓ Copiado'; setTimeout(() => { boton.textContent = o; }, 1500); }
}

// ------------------------------------------------------------------------------------------------ dialogs
/** A form dialog. fields: [{name, label, type: text|select|checkbox|textarea|number|checks, options, value, hint, required, pattern}].
 *  onSubmit(values) may throw: the message is shown in the dialog and it stays open. */
function dialogo({ title, intro, fields = [], ok = 'Aceptar', danger = false, onSubmit }) {
  const d = $('dlg');
  $('dlg-title').textContent = title;
  $('dlg-error').textContent = '';
  const body = $('dlg-body');
  body.replaceChildren();
  if (intro) body.append(intro instanceof Node ? intro : el('p', { class: 'intro' }, intro));
  const inputs = {};
  for (const f of fields) {
    let input;
    if (f.type === 'select') {
      input = el('select', { name: f.name, required: f.required });
      for (const o of f.options) input.append(new Option(o.label ?? o, o.value ?? o));
      if (f.value != null) input.value = f.value;
    } else if (f.type === 'textarea') {
      input = el('textarea', { name: f.name, rows: 3, maxlength: f.max || 1000 }, f.value || '');
    } else if (f.type === 'checkbox') {
      input = el('input', { type: 'checkbox', name: f.name, checked: !!f.value });
      inputs[f.name] = input;
      body.append(el('label', { class: 'check' }, input, ` ${f.label}`), f.hint ? el('p', { class: 'hint' }, f.hint) : null);
      continue;
    } else if (f.type === 'checks') {
      const box = el('div', { class: 'checks' });
      if (!f.options.length) box.append(el('span', { class: 'muted' }, f.empty || 'Nada que elegir'));
      for (const o of f.options) box.append(el('label', {}, el('input', { type: 'checkbox', value: o, checked: (f.value || []).includes(o) }), ` ${o}`));
      inputs[f.name] = box;
      body.append(el('fieldset', {}, el('legend', {}, f.label), box), f.hint ? el('p', { class: 'hint' }, f.hint) : null);
      continue;
    } else {
      input = el('input', { name: f.name, type: f.type || 'text', value: f.value ?? '', required: f.required, pattern: f.pattern,
        placeholder: f.placeholder, autocomplete: 'off', spellcheck: 'false', min: f.min, max: f.max });
    }
    inputs[f.name] = input;
    body.append(el('label', { class: 'field' }, el('span', {}, f.label), input), f.hint ? el('p', { class: 'hint' }, f.hint) : null);
  }
  const okBtn = $('dlg-ok');
  okBtn.textContent = ok;
  okBtn.className = danger ? 'danger' : 'primary';
  const form = $('dlg-form');
  form.onsubmit = async (e) => {
    e.preventDefault();
    const values = {};
    for (const [k, i] of Object.entries(inputs)) {
      if (i.classList?.contains('checks')) values[k] = [...i.querySelectorAll('input:checked')].map((x) => x.value);
      else if (i.type === 'checkbox') values[k] = i.checked;
      else values[k] = i.value.trim();
    }
    okBtn.disabled = true;
    try {
      await onSubmit(values);
      d.close();
    } catch (err) {
      $('dlg-error').textContent = err.message;
    } finally {
      okBtn.disabled = false;
    }
  };
  d.showModal();
  const first = body.querySelector('input:not([type=checkbox]), select, textarea');
  first?.focus();
}
$('dlg-cancel').addEventListener('click', () => $('dlg').close());

/** Confirmation that needs the name typed back for irreversible deletions. */
function confirmarBorrado({ title, name, detail, ok, extra = [], onConfirm }) {
  dialogo({
    title, ok, danger: true,
    intro: el('div', {}, el('p', { class: 'intro' }, detail), el('p', { class: 'warn' }, '⚠ No se puede deshacer.')),
    fields: [...extra, { name: 'confirm', label: `Escribe «${name}» para confirmar`, required: true, placeholder: name }],
    onSubmit: async (v) => {
      if (v.confirm !== name) throw new Error(`Escribe exactamente «${name}»`);
      await onConfirm(v);
    },
  });
}

function mostrarSecreto(who, secret) {
  $('secret-who').replaceChildren('Para ', el('b', {}, `@${who}`), '.');
  $('secret-value').textContent = secret;
  $('secret').showModal();
}
$('secret-copy').addEventListener('click', (e) => copiar($('secret-value').textContent, e.currentTarget));
$('secret-done').addEventListener('click', () => { $('secret-value').textContent = ''; $('secret').close(); });
$('secret').addEventListener('close', () => { $('secret-value').textContent = ''; });

// ------------------------------------------------------------------------------------------------ data
async function cargar() {
  const [agents, channels, projects] = await Promise.all([pedir('/api/admin/agents'), pedir('/api/channels'), pedir('/api/projects')]);
  const [chDet, prDet] = await Promise.all([
    Promise.all(channels.map((c) => pedir(`/api/channels/${enc(c.name)}`).catch(() => ({ ...c, members: [] })))),
    Promise.all(projects.map((p) => pedir(`/api/projects/${enc(p.name)}`).catch(() => ({ ...p, members: [], channels: [] })))),
  ]);
  st.agents = agents;
  st.channels = chDet;
  st.projects = prDet;
  pintar();
}
async function cargarRegistro() {
  st.audit = await pedir('/api/admin/audit');
  pintarRegistro();
}
/** Run an action, report it, reload. */
async function hacer(fn, ok) {
  try {
    await fn();
    if (ok) aviso(ok);
  } catch (e) {
    aviso(e.message, true);
  }
  await cargar().catch((e) => aviso(e.message, true));
}

const nombresActivos = () => st.agents.filter((a) => !a.disabled).map((a) => a.name);
const proyectosDe = (name) => st.projects.filter((p) => p.members.some((m) => m.name === name));
const canalesDe = (name) => st.channels.filter((c) => c.members.includes(name));

// ------------------------------------------------------------------------------------------------ render
function pintar() {
  $('n-usuarios').textContent = st.agents.length;
  $('n-canales').textContent = st.channels.length;
  $('n-proyectos').textContent = st.projects.length;
  pintarUsuarios();
  pintarCanales();
  pintarProyectos();
  pintarConectar();
}

const chip = (txt, cls = '') => el('span', { class: `chip ${cls}` }, txt);
const vacio = (txt) => el('p', { class: 'empty muted' }, txt);
const btn = (txt, onclick, cls = '', title = null) => el('button', { type: 'button', class: cls, onclick, title }, txt);

function pintarUsuarios() {
  const q = $('q-usuarios').value.trim().toLowerCase();
  const list = st.agents.filter((a) => !q || `${a.name} ${a.owner} ${a.specialization} ${a.description}`.toLowerCase().includes(q));
  $('lista-usuarios').replaceChildren(...(list.length ? list.map(tarjetaUsuario) : [vacio(q ? 'Ningún usuario coincide.' : 'Todavía no hay usuarios.')]));
}

function tarjetaUsuario(a) {
  const boot = a.name === BOOTSTRAP_ADMIN;
  const self = st.me && a.name === st.me.agent;
  const activos = a.tokens.filter((t) => !t.revoked_at && !(t.expires_at && t.expires_at < Date.now()));
  const tokens = el('table', { class: 'mini' },
    el('thead', {}, el('tr', {}, el('th', {}, 'ID'), el('th', {}, 'Nota'), el('th', {}, 'Creado'), el('th', {}, 'Último uso'), el('th', {}, 'Estado'), el('th', {}))),
    el('tbody', {}, ...(a.tokens.length ? [...a.tokens].sort((x, y) => (!!x.revoked_at - !!y.revoked_at) || y.id - x.id).map((t) => {
      const caducado = t.expires_at && t.expires_at < Date.now();
      const estado = t.revoked_at ? chip('revocado', 'bad') : caducado ? chip('caducado', 'warn') : chip(t.expires_at ? `válido hasta ${fecha(t.expires_at)}` : 'válido', 'ok');
      return el('tr', { class: t.revoked_at || caducado ? 'off' : '' },
        el('td', {}, `#${t.id}`), el('td', {}, t.label || '—'), el('td', {}, fecha(t.created_at)), el('td', {}, hace(t.last_used_at)), el('td', {}, estado),
        el('td', {}, t.revoked_at ? null : btn('Revocar', () => revocarToken(a, t), 'small danger-ghost')));
    }) : [el('tr', {}, el('td', { colspan: 6, class: 'muted' }, boot ? 'Usa AGENTLINK_ADMIN_TOKEN del servidor.' : 'Sin tokens.'))])));
  const proj = proyectosDe(a.name);
  const chans = canalesDe(a.name);
  return el('article', { class: `card ${a.disabled ? 'disabled' : ''}` },
    el('div', { class: 'card-head' },
      el('div', { class: 'title' }, el('span', { class: 'avatar', 'aria-hidden': 'true' }, a.name.slice(0, 2).toUpperCase()),
        el('div', {}, el('h3', {}, `@${a.name}`), el('p', { class: 'muted small' }, [a.owner && `de ${a.owner}`, a.specialization, a.description].filter(Boolean).join(' · ') || 'sin descripción'))),
      el('div', { class: 'chips' }, chip(a.role, `role-${a.role}`), a.disabled ? chip('desactivado', 'bad') : null, self ? chip('tú', 'accent') : null,
        chip(`${activos.length} token${activos.length === 1 ? '' : 's'} activo${activos.length === 1 ? '' : 's'}`))),
    el('div', { class: 'meta' },
      el('div', {}, el('span', { class: 'label' }, 'Proyectos'), ...(proj.length ? proj.map((p) => chip(`${p.name}${p.members.find((m) => m.name === a.name).role === 'admin' ? ' (admin)' : ''}`)) : [el('span', { class: 'muted' }, 'ninguno')])),
      el('div', {}, el('span', { class: 'label' }, 'Canales'), ...(chans.length ? chans.map((c) => chip(`#${c.name}`)) : [el('span', { class: 'muted' }, 'ninguno')]))),
    el('details', { class: 'tokens' }, el('summary', {}, `Tokens (${a.tokens.length})`), tokens),
    el('div', { class: 'actions' },
      boot ? null : btn('🔑 Nuevo token', () => nuevoToken(a)),
      boot ? null : btn('↻ Rotar tokens', () => rotarTokens(a), '', 'Token nuevo y revoca todos los anteriores'),
      btn('✎ Editar', () => editarUsuario(a)),
      btn('⚡ Comando de conexión', () => irAConectar(a.name)),
      boot ? null : a.disabled ? btn('Activar', () => hacer(() => enviar(`/api/admin/agents/${enc(a.name)}/enable`, 'POST'), `@${a.name} activado`))
        : btn('Desactivar', () => desactivar(a), 'warn-ghost'),
      boot || self ? null : btn('Borrar', () => borrarUsuario(a), 'danger-ghost')));
}

function nuevoUsuario() {
  dialogo({
    title: 'Nuevo usuario', ok: 'Crear y mostrar token',
    fields: [
      { name: 'name', label: 'Nombre (minúsculas, p. ej. claude-backend)', required: true, pattern: '[a-z0-9][a-z0-9._\\-]{0,63}' },
      { name: 'role', label: 'Rol', type: 'select', value: 'member', options: [
        { value: 'member', label: 'member — agente normal: lee y escribe en sus canales' },
        { value: 'monitor', label: 'monitor — solo lectura, para personas mirando la web' },
        { value: 'admin', label: 'admin — puede hacerlo todo' }] },
      { name: 'owner', label: 'Persona responsable (opcional)' },
      { name: 'specialization', label: 'Especialidad (opcional, p. ej. backend)' },
      { name: 'description', label: 'Descripción (opcional, p. ej. «Claude Code del portátil»)' },
      { name: 'projects', label: 'Proyectos en los que participa', type: 'checks', options: st.projects.map((p) => p.name) },
      { name: 'ttl_days', label: 'Caducidad del token en días (0 = no caduca)', type: 'number', value: '0', min: 0, max: 3650 },
    ],
    onSubmit: async (v) => {
      const r = await enviar('/api/admin/agents', 'POST', { ...v, ttl_days: Number(v.ttl_days || 0) });
      mostrarSecreto(r.agent.name, r.token);
      await cargar();
    },
  });
}

function nuevoToken(a) {
  dialogo({
    title: `Nuevo token para @${a.name}`, ok: 'Crear y mostrar',
    intro: 'Los tokens actuales siguen funcionando. Para sustituirlos usa «Rotar tokens».',
    fields: [
      { name: 'label', label: 'Nota para reconocerlo (p. ej. portátil)', value: '' },
      { name: 'ttl_days', label: 'Caducidad en días (0 = no caduca)', type: 'number', value: '0', min: 0, max: 3650 },
    ],
    onSubmit: async (v) => {
      const r = await enviar(`/api/admin/agents/${enc(a.name)}/tokens`, 'POST', { label: v.label || 'web', ttl_days: Number(v.ttl_days || 0) });
      mostrarSecreto(a.name, r.token);
      await cargar();
    },
  });
}

function rotarTokens(a) {
  const viejos = a.tokens.filter((t) => !t.revoked_at);
  dialogo({
    title: `Rotar tokens de @${a.name}`, ok: 'Rotar', danger: true,
    intro: `Se crea un token nuevo y se revocan los ${viejos.length} actuales: los clientes que los usen dejarán de conectar hasta que reciban el nuevo.`,
    fields: [{ name: 'label', label: 'Nota del token nuevo', value: 'rotated' }],
    onSubmit: async (v) => {
      const r = await enviar(`/api/admin/agents/${enc(a.name)}/tokens`, 'POST', { label: v.label || 'rotated' });
      for (const t of viejos) await pedir(`/api/admin/tokens/${t.id}`, { method: 'DELETE' });
      mostrarSecreto(a.name, r.token);
      await cargar();
    },
  });
}

function revocarToken(a, t) {
  dialogo({
    title: `Revocar el token #${t.id}`, ok: 'Revocar', danger: true,
    intro: `El token «${t.label || 'sin nota'}» de @${a.name} deja de valer al momento.`,
    onSubmit: async () => { await pedir(`/api/admin/tokens/${t.id}`, { method: 'DELETE' }); aviso(`Token #${t.id} revocado`); await cargar(); },
  });
}

function editarUsuario(a) {
  dialogo({
    title: `Editar @${a.name}`, ok: 'Guardar',
    intro: 'El nombre y el rol no se cambian: para eso crea otro usuario.',
    fields: [
      { name: 'owner', label: 'Persona responsable', value: a.owner },
      { name: 'specialization', label: 'Especialidad', value: a.specialization },
      { name: 'description', label: 'Descripción', value: a.description },
    ],
    onSubmit: async (v) => { await enviar(`/api/admin/agents/${enc(a.name)}`, 'PATCH', v); aviso('Guardado'); await cargar(); },
  });
}

function desactivar(a) {
  dialogo({
    title: `Desactivar @${a.name}`, ok: 'Desactivar', danger: true,
    intro: 'Sus tokens dejan de valer hasta que lo vuelvas a activar. No se borra nada.',
    onSubmit: async () => { await enviar(`/api/admin/agents/${enc(a.name)}/disable`, 'POST'); aviso(`@${a.name} desactivado`); await cargar(); },
  });
}

function borrarUsuario(a) {
  confirmarBorrado({
    title: `Borrar @${a.name}`, name: a.name, ok: 'Borrar usuario',
    detail: 'Se borran sus tokens, sus permisos y su presencia. Sus mensajes se conservan como «(deleted)» salvo que marques la casilla.',
    extra: [{ name: 'purge', label: 'Borrar también todos sus mensajes', type: 'checkbox' }],
    onConfirm: async (v) => {
      const r = await pedir(`/api/admin/agents/${enc(a.name)}${v.purge ? '?purge_messages=1' : ''}`, { method: 'DELETE' });
      aviso(`@${r.deleted} borrado (${r.tokens} tokens${v.purge ? `, ${r.messages_purged} mensajes` : ''})`);
      await cargar();
    },
  });
}

// channels
function pintarCanales() {
  const q = $('q-canales').value.trim().toLowerCase();
  const list = st.channels.filter((c) => !q || `${c.name} ${c.project} ${c.repo || ''} ${c.description}`.toLowerCase().includes(q));
  $('lista-canales').replaceChildren(...(list.length ? list.map(tarjetaCanal) : [vacio(q ? 'Ningún canal coincide.' : 'Todavía no hay canales.')]));
}

function tarjetaCanal(c) {
  const fuera = nombresActivos().filter((n) => !c.members.includes(n));
  const sel = el('select', { 'aria-label': `Añadir a #${c.name}` }, new Option('+ añadir miembro…', ''), ...fuera.map((n) => new Option(`@${n}`, n)));
  sel.addEventListener('change', () => {
    const n = sel.value;
    if (n) hacer(() => enviar(`/api/channels/${enc(c.name)}/members`, 'POST', { agent: n }), `@${n} entra en #${c.name}`);
  });
  return el('article', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h3', {}, `#${c.name}`), el('p', { class: 'muted small' }, c.description || 'sin descripción')),
      el('div', { class: 'chips' }, chip(`▣ ${c.project || '—'}`), c.repo ? chip(`⎇ ${c.repo}`, 'accent') : null, chip(`${c.members.length} miembros`))),
    el('div', { class: 'members' }, ...(c.members.length ? c.members.map((m) => el('span', { class: 'chip member' }, `@${m}`,
      el('button', { type: 'button', class: 'x', title: `Sacar a @${m}`, 'aria-label': `Sacar a @${m} de #${c.name}`,
        onclick: () => hacer(() => pedir(`/api/channels/${enc(c.name)}/members/${enc(m)}`, { method: 'DELETE' }), `@${m} sale de #${c.name}`) }, '×'))) : [el('span', { class: 'muted' }, 'sin miembros')])),
    el('div', { class: 'actions' }, fuera.length ? sel : null,
      btn('Borrar canal', () => confirmarBorrado({
        title: `Borrar #${c.name}`, name: c.name, ok: 'Borrar canal',
        detail: 'Se borran todos sus mensajes, tareas, leases y contexto.',
        onConfirm: async () => { const r = await pedir(`/api/admin/channels/${enc(c.name)}`, { method: 'DELETE' }); aviso(`#${r.deleted} borrado (${r.messages} mensajes)`); await cargar(); },
      }), 'danger-ghost')));
}

function nuevoCanal(proyecto = '') {
  dialogo({
    title: 'Nuevo canal', ok: 'Crear canal',
    fields: [
      { name: 'name', label: 'Nombre (minúsculas, p. ej. dev)', required: true, pattern: '[a-z0-9][a-z0-9._\\-]{0,63}' },
      { name: 'project', label: 'Proyecto', type: 'select', options: st.projects.map((p) => p.name), value: proyecto || (st.projects[0]?.name ?? '') },
      { name: 'repo', label: 'Repositorio de GitHub (opcional, dueño/repo: sus eventos llegan a este canal)', placeholder: 'dueño/repo' },
      { name: 'description', label: 'Descripción (opcional)' },
      { name: 'members', label: 'Miembros desde el principio', type: 'checks', options: nombresActivos() },
    ],
    onSubmit: async (v) => {
      const body = { name: v.name, project: v.project, description: v.description, members: v.members };
      if (v.repo) body.repo = v.repo;
      await enviar('/api/channels', 'POST', body);
      aviso(`#${v.name} creado`);
      await cargar();
    },
  });
}

// projects
function pintarProyectos() {
  $('lista-proyectos').replaceChildren(...(st.projects.length ? st.projects.map(tarjetaProyecto) : [vacio('Todavía no hay proyectos.')]));
}

function tarjetaProyecto(p) {
  const fuera = nombresActivos().filter((n) => !p.members.some((m) => m.name === n));
  const sel = el('select', { 'aria-label': `Añadir a ${p.name}` }, new Option('+ añadir participante…', ''), ...fuera.map((n) => new Option(`@${n}`, n)));
  sel.addEventListener('change', () => {
    const n = sel.value;
    if (n) hacer(() => enviar(`/api/projects/${enc(p.name)}/members`, 'POST', { agent: n, role: 'member' }), `@${n} participa en ${p.name}`);
  });
  const miembro = (m) => {
    const rol = el('select', { class: 'role-sel', 'aria-label': `Rol de @${m.name} en ${p.name}` }, new Option('member', 'member'), new Option('admin', 'admin'));
    rol.value = m.role;
    rol.addEventListener('change', () => hacer(() => enviar(`/api/projects/${enc(p.name)}/members`, 'POST', { agent: m.name, role: rol.value }), `@${m.name}: ${rol.value} en ${p.name}`));
    return el('span', { class: 'chip member' }, `@${m.name}`, rol,
      el('button', { type: 'button', class: 'x', title: `Sacar a @${m.name}`, 'aria-label': `Sacar a @${m.name} de ${p.name}`, onclick: () => dialogo({
        title: `Sacar a @${m.name} de ${p.name}`, ok: 'Sacar', danger: true,
        intro: 'Sus sesiones en el proyecto terminan, sus tareas quedan huérfanas y sale de los canales del proyecto.',
        onSubmit: async () => { await pedir(`/api/projects/${enc(p.name)}/members/${enc(m.name)}`, { method: 'DELETE' }); aviso(`@${m.name} fuera de ${p.name}`); await cargar(); },
      }) }, '×'));
  };
  return el('article', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h3', {}, `▣ ${p.name}`), el('p', { class: 'muted small' }, p.description || 'sin descripción')),
      el('div', { class: 'chips' }, p.repo ? chip(`⎇ ${p.repo}`, 'accent') : null, chip(`${p.members.length} participantes`), chip(`${p.channels.length} canales`))),
    el('div', { class: 'meta' },
      el('div', {}, el('span', { class: 'label' }, 'Canales'), ...(p.channels.length ? p.channels.map((c) => chip(`#${c.name}`)) : [el('span', { class: 'muted' }, 'ninguno')]))),
    el('div', { class: 'members' }, ...(p.members.length ? p.members.map(miembro) : [el('span', { class: 'muted' }, 'sin participantes')])),
    el('div', { class: 'actions' }, fuera.length ? sel : null,
      btn('+ Canal', () => nuevoCanal(p.name)),
      btn('⚡ Comando de conexión', () => irAConectar('', p.name)),
      btn('Borrar proyecto', () => confirmarBorrado({
        title: `Borrar el proyecto ${p.name}`, name: p.name, ok: 'Borrar proyecto',
        detail: `Se borran sus ${p.channels.length} canales (con mensajes, tareas y contexto), sus sesiones, su coordinador y su historial de eventos. Los usuarios y sus tokens se conservan.`,
        onConfirm: async () => { const r = await pedir(`/api/admin/projects/${enc(p.name)}`, { method: 'DELETE' }); aviso(`${r.deleted} borrado (${r.channels} canales, ${r.messages} mensajes)`); await cargar(); },
      }), 'danger-ghost')));
}

function nuevoProyecto() {
  dialogo({
    title: 'Nuevo proyecto', ok: 'Crear proyecto',
    fields: [
      { name: 'name', label: 'Nombre (minúsculas, p. ej. honey)', required: true, pattern: '[a-z0-9][a-z0-9._\\-]{0,63}' },
      { name: 'repo', label: 'Repositorio de GitHub (opcional, dueño/repo)', placeholder: 'dueño/repo' },
      { name: 'description', label: 'Descripción (opcional)', type: 'textarea' },
      { name: 'members', label: 'Participantes', type: 'checks', options: nombresActivos().filter((n) => n !== st.me?.agent) },
      { name: 'channel', label: 'Crear también un canal con este nombre (opcional, p. ej. dev)', pattern: '[a-z0-9][a-z0-9._\\-]{0,63}' },
    ],
    onSubmit: async (v) => {
      const body = { name: v.name, description: v.description, members: v.members };
      if (v.repo) body.repo = v.repo;
      await enviar('/api/projects', 'POST', body);
      if (v.channel) {
        const ch = { name: v.channel, project: v.name, members: v.members };
        if (v.repo) ch.repo = v.repo;
        await enviar('/api/channels', 'POST', ch);
      }
      aviso(`Proyecto ${v.name} creado`);
      await cargar();
    },
  });
}

// ------------------------------------------------------------------------------------------------ connect
function irAConectar(user = '', project = '') {
  cambiarTab('conectar');
  if (user) $('k-user').value = user;
  if (project) $('k-project').value = project;
  st.connectToken = null;
  pintarComando();
}

function pintarConectar() {
  const u = $('k-user').value;
  const p = $('k-project').value;
  const users = st.agents.filter((a) => !a.disabled && a.name !== BOOTSTRAP_ADMIN);
  $('k-user').replaceChildren(...users.map((a) => new Option(`@${a.name} (${a.role})`, a.name)));
  $('k-project').replaceChildren(...st.projects.map((x) => new Option(x.name, x.name)));
  if (users.some((a) => a.name === u)) $('k-user').value = u;
  if (st.projects.some((x) => x.name === p)) $('k-project').value = p;
  const repos = new Set(['0xAnbu/AgentOffice']);
  $('k-repos').replaceChildren(...[...repos].map((r) => el('option', { value: r })));
  if (!$('k-server').value) $('k-server').value = location.origin;
  pintarComando();
}

const shq = (s) => (/^[A-Za-z0-9_@%+=:,./-]+$/.test(s) ? s : `'${s.replaceAll("'", "'\\''")}'`);

function pintarComando() {
  const user = $('k-user').value;
  const project = $('k-project').value;
  const runtime = $('k-runtime').value;
  const repo = $('k-repo').value.trim().replace(/^https:\/\/github\.com\//, '').replace(/\.git$/, '') || '0xAnbu/AgentOffice';
  const server = $('k-server').value.trim().replace(/\/+$/, '');
  const tok = st.connectToken && st.connectToken.user === user ? st.connectToken.token : PLACEHOLDER;
  const lines = [
    'python3 -m venv ~/.venvs/agentoffice',
    'source ~/.venvs/agentoffice/bin/activate',
    `python -m pip install ${/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) ? `"git+https://github.com/${repo}"` : shq(`git+https://github.com/${repo}`)}`,
    '',
    `agentlink connect --server ${shq(server)} --token ${tok === PLACEHOLDER ? tok : shq(tok)} --project ${shq(project)} --runtime ${runtime}`,
  ];
  $('k-cmd').textContent = lines.join('\n');
  const a = st.agents.find((x) => x.name === user);
  const pr = st.projects.find((x) => x.name === project);
  const notes = [];
  if (!users().length) notes.push('Crea antes un usuario en la pestaña Usuarios.');
  if (a && a.role === 'monitor') notes.push('Ojo: un monitor solo puede leer; un agente necesita rol member o admin.');
  if (a && pr && !pr.members.some((m) => m.name === user)) notes.push(`@${user} no participa en ${project}: se le añadirá al generar el token.`);
  $('k-note').textContent = notes.join(' ');
  $('k-warn').textContent = tok === PLACEHOLDER
    ? 'Pulsa «Generar token y comando»: el token se crea al momento y solo se muestra aquí, una vez.'
    : '⚠ Este token solo se muestra ahora. Cópialo antes de salir de la página.';
  $('k-warn').className = tok === PLACEHOLDER ? 'hint' : 'warn';
}
const users = () => st.agents.filter((a) => !a.disabled && a.name !== BOOTSTRAP_ADMIN);

async function generarConexion(e) {
  e.preventDefault();
  $('k-error').textContent = '';
  const user = $('k-user').value;
  const project = $('k-project').value;
  if (!user || !project) { $('k-error').textContent = 'Elige un usuario y un proyecto.'; return; }
  $('k-generate').disabled = true;
  try {
    const pr = st.projects.find((x) => x.name === project);
    if (pr && !pr.members.some((m) => m.name === user)) await enviar(`/api/projects/${enc(project)}/members`, 'POST', { agent: user, role: 'member' });
    const r = await enviar(`/api/admin/agents/${enc(user)}/tokens`, 'POST', { label: `connect ${project} ${$('k-runtime').value}`.slice(0, 100) });
    st.connectToken = { user, token: r.token };
    await cargar();
    aviso(`Token #${r.token_id} creado para @${user}`);
  } catch (err) {
    $('k-error').textContent = err.message;
  } finally {
    $('k-generate').disabled = false;
    pintarComando();
  }
}

// ------------------------------------------------------------------------------------------------ audit
function pintarRegistro() {
  const q = $('q-registro').value.trim().toLowerCase();
  const rows = st.audit.filter((e) => !q || `${e.actor} ${e.action} ${e.target} ${e.detail}`.toLowerCase().includes(q));
  $('lista-registro').replaceChildren(...(rows.length ? rows.map((e) => el('tr', {},
    el('td', { class: 'nowrap' }, fecha(e.created_at)), el('td', {}, e.actor ? `@${e.actor}` : '—'),
    el('td', {}, chip(e.action, e.action.endsWith('delete') || e.action.endsWith('revoke') ? 'bad' : '')), el('td', {}, e.target), el('td', { class: 'muted' }, e.detail)))
    : [el('tr', {}, el('td', { colspan: 5, class: 'muted' }, 'Sin entradas.'))]));
}

// ------------------------------------------------------------------------------------------------ shell
function cambiarTab(tab) {
  st.tab = TABS.includes(tab) ? tab : 'usuarios';
  for (const b of document.querySelectorAll('[data-tab]')) b.setAttribute('aria-selected', String(b.dataset.tab === st.tab));
  for (const t of TABS) $(`tab-${t}`).hidden = t !== st.tab;
  history.replaceState(null, '', `#${st.tab}`);
  if (st.tab === 'registro') cargarRegistro().catch((e) => aviso(e.message, true));
}

async function arrancar() {
  if (!token.get()) { $('login').hidden = false; return; }
  $('login').hidden = true;
  $('app').hidden = false;
  try {
    st.me = await pedir('/api/me');
    $('whoami').textContent = `@${st.me.agent} (${st.me.role})`;
    if (st.me.role !== 'admin') {
      $('shell').hidden = true;
      $('denied').hidden = false;
      $('denied').textContent = 'Esta página es solo para administradores (rol admin). Con tu token puedes usar el chat y el edificio.';
      return;
    }
    $('shell').hidden = false;
    $('denied').hidden = true;
    await cargar();
    cambiarTab(location.hash.slice(1));
  } catch (e) {
    if (e.status !== 401) { $('denied').hidden = false; $('denied').textContent = `No se pudo cargar: ${e.message}`; }
  }
}
function salir(msg) {
  token.clear();
  st.connectToken = null;
  $('app').hidden = true;
  $('login').hidden = false;
  $('login-error').textContent = msg || '';
}

$('login-form').addEventListener('submit', (e) => { e.preventDefault(); token.set($('token').value.trim()); $('token').value = ''; arrancar(); });
$('logout').addEventListener('click', () => salir());
for (const b of document.querySelectorAll('[data-tab]')) b.addEventListener('click', () => cambiarTab(b.dataset.tab));
$('nuevo-usuario').addEventListener('click', nuevoUsuario);
$('nuevo-canal').addEventListener('click', () => nuevoCanal());
$('nuevo-proyecto').addEventListener('click', nuevoProyecto);
$('q-usuarios').addEventListener('input', pintarUsuarios);
$('q-canales').addEventListener('input', pintarCanales);
$('q-registro').addEventListener('input', pintarRegistro);
$('recargar-registro').addEventListener('click', () => cargarRegistro().catch((e) => aviso(e.message, true)));
for (const id of ['k-user', 'k-project', 'k-runtime']) $(id).addEventListener('change', () => { if (id === 'k-user') st.connectToken = null; pintarComando(); });
for (const id of ['k-repo', 'k-server']) $(id).addEventListener('input', pintarComando);
$('connect-form').addEventListener('submit', generarConexion);
$('k-copy').addEventListener('click', (e) => copiar($('k-cmd').textContent, e.currentTarget));
$('msg-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const id = Number($('msg-id').value);
  if (!id) return;
  dialogo({
    title: `Borrar el mensaje #${id}`, ok: 'Borrar', danger: true, intro: 'Se borra el mensaje para todos. No se puede deshacer.',
    onSubmit: async () => { await pedir(`/api/admin/messages/${id}`, { method: 'DELETE' }); aviso(`Mensaje #${id} borrado`); $('msg-id').value = ''; },
  });
});
arrancar();
