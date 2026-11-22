// AGENT OFFICE: the data layer shared by the 3D building and the Activity Center.
//
// Single source of truth = the backend. This module only reads: GET /api/office/snapshot (consistent state + cursor)
// and the durable event log, live over WS /ws/events (single-use ticket; never a token in a URL), replayed by cursor
// after any disconnection. If the cursor is older than retained history the server says "resync" and we reload the
// snapshot. There is NO simulation fallback: without the server the page says so instead of inventing activity.

const TOKEN_KEY = 'agentoffice_token';
export const token = { get: () => sessionStorage.getItem(TOKEN_KEY) || '', set: (t) => sessionStorage.setItem(TOKEN_KEY, t), clear: () => sessionStorage.removeItem(TOKEN_KEY) };

export async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, cache: 'no-store', credentials: 'omit', headers: { Authorization: `Bearer ${token.get()}`, ...(opts.body ? { 'Content-Type': 'application/json' } : {}) } });
  if (r.status === 401) { const e = new Error('unauthorized'); e.status = 401; throw e; }
  if (!r.ok) { const e = new Error(`HTTP ${r.status}`); e.status = r.status; throw e; }
  return r.json();
}

// Colours per runtime (the shirt) — identification without touching the characters' shape.
export const RUNTIME = {
  'claude-code': { nombre: 'Claude', paleta: ['#E07A3F', '#F2A36B', '#A8471B', '#FFE8D6'] },
  codex: { nombre: 'Codex', paleta: ['#1F9D6E', '#5ED1A3', '#0B5E40', '#E3FFF3'] },
  opencode: { nombre: 'OpenCode', paleta: ['#3D6FE0', '#86A8F5', '#1D3F9A', '#E6EEFF'] },
  generic: { nombre: 'Agente', paleta: ['#8E8E93', '#D1D1D6', '#3A3A3C', '#FFFFFF'] },
  synthetic: { nombre: 'Sintético', paleta: ['#A1A1A6', '#E5E5EA', '#636366', '#FFFFFF'] },
  human: { nombre: 'Humano', paleta: ['#5E5CE6', '#A3A1FF', '#2F2BA0', '#EDECFF'] },
};

/** Session status (backend) -> the visual states the original office already knows how to draw. */
export function estadoVisual(status) {
  if (status === 'executing') return 'trabajando';
  if (status === 'waiting' || status === 'stalled') return 'espera';
  if (status === 'idle' || status === 'registered') return 'libre';
  return 'cerrado'; // offline, terminated
}
const FRASE = { executing: 'Trabajando', waiting: 'Esperando', stalled: 'Sin progreso', idle: 'Libre', registered: 'Conectado', offline: 'Desconectado', terminated: 'Finalizado' };

/**
 * The snapshot as floors -> rooms -> desks, in the shape plano3d/mundo/oficina already use.
 * One desk per real session (sub-agents included, only when the runtime registered them). Rooms on a floor group
 * the sessions by owner, so who-owns-whom is visible; the floor comes from the session's specialization (server).
 */
export function aEdificio(snap) {
  const floors = snap.building.floors;
  const now = snap.server_time;
  const coordSes = new Set(Object.values(snap.coordinators || {}).filter((c) => c.status === 'active' && c.valid).map((c) => c.session_id));
  const ofrecida = new Set(Object.values(snap.coordinators || {}).filter((c) => c.status === 'offered').map((c) => c.offered_session));
  const tareas = new Map();
  for (const t of snap.tasks || []) if (t.assignee_session) (tareas.get(t.assignee_session) || tareas.set(t.assignee_session, []).get(t.assignee_session)).push(t);
  const reservadas = new Map();
  for (const t of snap.tasks || []) if (!t.assignee_session && t.assignee && t.status === 'open') reservadas.set(t.assignee, (reservadas.get(t.assignee) || 0) + 1);
  const recientes = (snap.sessions || []).filter((s) => s.status !== 'terminated' || now - (s.ended_at || 0) < 5 * 60e3);
  const pisos = floors.map((f) => ({ key: f.key, nombre: f.name, color: f.color, zonas: [], puestos: {} }));
  const porKey = new Map(pisos.map((p) => [p.key, p]));
  const sesiones = new Map();
  for (const s of recientes) {
    const piso = porKey.get(s.floor) || pisos[pisos.length - 1];
    const rt = RUNTIME[s.runtime] || RUNTIME.generic;
    const mias = tareas.get(s.id) || [];
    const estado = estadoVisual(s.status);
    const zonaClave = `o-${String(s.owner).replace(/[^a-z0-9]/gi, '').toLowerCase() || 'x'}`;
    let z = piso.zonas.find((q) => q.clave === zonaClave);
    if (!z) { z = { clave: zonaClave, nombre: s.owner, puestos: [] }; piso.zonas.push(z); }
    z.puestos.push(s.id);
    const p = {
      nombre: s.agent, alias: null, genero: hashGenero(s.agent), zona: zonaClave, paleta: rt.paleta, estado,
      actividad: estado === 'trabajando' ? 'terminal' : null,
      frase: FRASE[s.status] || s.status, detalle: mias.length ? `TASK #${mias[0].id} · ${mias[0].title}` : (s.progress_note || ''),
      desde: s.last_progress_at || s.last_heartbeat_at, bandeja: mias.length + (s.parent_id ? 0 : reservadas.get(s.agent) || 0),
      // Agent Office identity, shown on the label (all of it from the backend)
      sesion: s, runtime: rt.nombre, owner: s.owner, specialization: s.specialization, sub: !!s.parent_id,
      coordinador: coordSes.has(s.id), candidato: ofrecida.has(s.id), tareas: mias, bloqueada: mias.some((t) => t.status === 'blocked'),
    };
    piso.puestos[s.id] = p;
    sesiones.set(s.id, { ...p, piso: piso.key });
  }
  for (const p of pisos) p.zonas.sort((a, b) => a.nombre.localeCompare(b.nombre));
  return { pisos, sesiones };
}
function hashGenero(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 2 ? 'f' : 'm'; }

/** Main live session of an agent (for animations that only know the agent name). */
export function sesionDeAgente(sesiones, agente) {
  let mejor = null;
  for (const [id, s] of sesiones) if (s.nombre === agente && !s.sub && s.estado !== 'cerrado' && (!mejor || (s.desde || 0) > (mejor.desde || 0))) mejor = { id, ...s };
  if (mejor) return mejor.id;
  for (const [id, s] of sesiones) if (s.nombre === agente) return id;
  return null;
}

/**
 * Live connection. Calls onSnapshot(snap) on start/resync/periodic refresh, onEvents(list, {replay}) for new events
 * (deduplicated by id), onEstado(texto, clase) for the connection pill.
 */
export function conectar({ onSnapshot, onEvents, onEstado, onAuth }) {
  let cursor = 0, ws = null, intentos = 0, vivo = true, refresco = 0;
  const vistos = new Set();
  const entregar = (evs, replay) => {
    const nuevos = evs.filter((e) => !vistos.has(e.id));
    for (const e of nuevos) { vistos.add(e.id); cursor = Math.max(cursor, e.id); }
    if (vistos.size > 5000) { const viejo = [...vistos].slice(0, 2500); for (const v of viejo) vistos.delete(v); }
    if (nuevos.length) onEvents(nuevos, { replay });
  };
  async function snapshot() {
    const s = await api('/api/office/snapshot');
    onSnapshot(s);
    return s;
  }
  async function arrancar() {
    try {
      const s = await snapshot();
      // the recent past for the Activity Center (not animated), then live from the snapshot cursor
      const hist = await api(`/api/events?after=${Math.max(0, s.cursor - 150)}&limit=150`);
      entregar(hist.events, true);
      cursor = Math.max(cursor, s.cursor);
      abrirWs();
    } catch (e) {
      if (e.status === 401) { onAuth(); return; }
      onEstado('sin servidor', 'caido');
      setTimeout(arrancar, 3000);
    }
  }
  async function abrirWs() {
    if (!vivo) return;
    try {
      const { ticket } = await api('/api/ws-ticket', { method: 'POST' });
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws/events?ticket=${encodeURIComponent(ticket)}&after=${cursor}`);
    } catch (e) {
      if (e.status === 401) { onAuth(); return; }
      return reintentar();
    }
    ws.onopen = () => { intentos = 0; onEstado('en vivo', 'vivo'); };
    ws.onmessage = (m) => {
      const d = JSON.parse(m.data);
      if (d.type === 'events') entregar(d.events, false);
      else if (d.type === 'resync') { snapshot().catch(() => {}); }
    };
    ws.onclose = () => { if (vivo) reintentar(); };
  }
  function reintentar() {
    onEstado('reconectando…', 'reconectando');
    intentos++;
    // on reconnection: a fresh snapshot (state may have changed while away) + replay from the cursor through the WS
    setTimeout(() => snapshot().then(abrirWs).catch((e) => (e.status === 401 ? onAuth() : reintentar())), Math.min(15000, 500 * 2 ** intentos));
  }
  // Derived states (offline, stalled, lease expiry) depend on the server clock: refresh the snapshot periodically.
  refresco = setInterval(() => { if (!document.hidden) snapshot().catch(() => {}); }, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) snapshot().catch(() => {}); });
  arrancar();
  // soltar(): drop the socket as a network failure would (the normal reconnection path runs); used by visual_check.py
  return { cerrar() { vivo = false; clearInterval(refresco); ws?.close(); }, soltar() { ws?.close(); }, snapshot };
}

// ------------------------------------------------------------------------------- event -> visual category (§9.2)
export const CATEGORIAS = {
  comunicacion: { nombre: 'Comunicación', color: '#3D8BFD', icono: 'M' },
  tarea: { nombre: 'Tarea', color: '#8E6CEF', icono: 'T' },
  progreso: { nombre: 'Progreso', color: '#30B46C', icono: 'P' },
  revision: { nombre: 'Revisión', color: '#D9A400', icono: 'R' },
  alerta: { nombre: 'Alerta', color: '#E5484D', icono: '!' },
  coordinacion: { nombre: 'Coordinación', color: '#E07A3F', icono: 'C' },
  sistema: { nombre: 'Sistema', color: '#8E8E93', icono: 'S' },
  liderazgo: { nombre: 'Liderazgo', color: '#C2410C', icono: '★' },
  recursos: { nombre: 'Recursos', color: '#0EA5A4', icono: 'L' },
};
const KIND = { chat: 'comunicacion', question: 'comunicacion', answer: 'comunicacion', context: 'comunicacion', status: 'progreso', task: 'tarea',
  review: 'revision', alert: 'alerta', decision: 'coordinacion', handoff: 'coordinacion', event: 'sistema', system: 'sistema' };
const TITULO = {
  'task.created': 'Tarea creada', 'task.claimed': 'Tarea reclamada', 'task.updated': 'Tarea actualizada', 'task.blocked': 'Tarea bloqueada',
  'task.unblocked': 'Tarea desbloqueada', 'task.orphaned': 'Tarea huérfana', 'task.recovered': 'Tarea recuperada', 'task.checkpoint': 'Checkpoint',
  'task.stale_write': 'Escritura rechazada (lease antiguo)', 'task.wait_timeout': 'Espera excedida', 'task.renewed': 'Lease renovado', 'task.released': 'Tarea liberada',
  'lease.acquired': 'Lease adquirido', 'lease.released': 'Lease liberado', 'lease.expired': 'Lease expirado',
  'session.registered': 'Sesión conectada', 'session.ended': 'Sesión finalizada', 'session.expired': 'Sesión perdida', 'session.stalled': 'Sesión sin progreso', 'session.resumed': 'Sesión reanudada',
  'coordinator.offered': 'Coordinador propuesto', 'coordinator.accepted': 'Nuevo coordinador', 'coordinator.declined': 'Coordinación rechazada',
  'coordinator.offer_expired': 'Oferta caducada', 'coordinator.lost': 'Coordinador perdido', 'coordinator.unavailable': 'Sin coordinador', 'coordinator.released': 'Coordinador saliente',
  'delivery.overdue': 'Mensaje urgente sin atender', 'project.created': 'Proyecto creado', 'kv.set': 'Contexto actualizado',
};

/** Structured classification: event type + message kind. Never parses free text. */
export function clasificar(e) {
  const t = e.type;
  if (t === 'message.created') {
    const kind = e.data?.kind || 'chat';
    const cat = e.data?.priority === 1 && kind !== 'system' ? 'alerta' : KIND[kind] || 'comunicacion';
    return { cat, titulo: kind === 'system' ? 'Aviso del sistema' : { question: 'Pregunta', answer: 'Respuesta', review: 'Revisión', alert: 'Alerta', decision: 'Decisión', handoff: 'Handoff', status: 'Estado', task: 'Tarea' }[kind] || 'Mensaje', sistema: kind === 'system' };
  }
  let cat = 'sistema';
  if (t.startsWith('coordinator.')) cat = 'liderazgo';
  else if (t.startsWith('lease.')) cat = 'recursos';
  else if (t === 'task.created' || t === 'task.claimed' || t === 'task.released') cat = 'tarea';
  else if (t === 'task.checkpoint' || t === 'task.unblocked' || (t === 'task.updated' && ['done', 'in_progress', 'review'].includes(e.data?.status))) cat = e.data?.status === 'review' ? 'revision' : 'progreso';
  else if (['task.blocked', 'task.orphaned', 'task.stale_write', 'task.wait_timeout', 'session.stalled', 'delivery.overdue', 'coordinator.unavailable'].includes(t)) cat = 'alerta';
  else if (t === 'task.updated' || t === 'task.recovered') cat = 'coordinacion';
  return { cat, titulo: TITULO[t] || t, sistema: true };
}
