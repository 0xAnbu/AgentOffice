// AGENT OFFICE — Activity Center: the floating, translucent event panel beside the building (§9).
// Every card comes from a structured, persisted event (type + data), never from parsing free text. Untrusted text is
// only ever assigned with textContent.
import { CATEGORIAS } from './datos.js';

const MAX = 300;
const hora = (ms) => new Date(ms).toTimeString().slice(0, 5);

export function crearActividad({ raiz, alAgente }) {
  const lista = raiz.querySelector('.tarjetas');
  const fCat = raiz.querySelector('[name=categoria]');
  const fAgente = raiz.querySelector('[name=agente]');
  const fOwner = raiz.querySelector('[name=propietario]');
  const fTarea = raiz.querySelector('[name=tarea]');
  const fSistema = raiz.querySelector('[name=sistema]');
  const pill = raiz.querySelector('.conexion');
  const items = [];
  const vistos = new Set();
  const agentes = new Map(); // name -> owner

  for (const [k, c] of Object.entries(CATEGORIAS)) fCat.append(new Option(c.nombre, k));
  for (const el of [fCat, fAgente, fOwner, fTarea, fSistema]) el.addEventListener('input', pintar);
  raiz.querySelector('.expandir').addEventListener('click', () => {
    const ex = raiz.classList.toggle('expandida');
    raiz.querySelector('.expandir').setAttribute('aria-expanded', String(ex));
  });
  raiz.querySelector('.plegar').addEventListener('click', () => raiz.classList.toggle('plegada'));

  function pasa(it) {
    const e = it.e;
    if (fCat.value && it.c.cat !== fCat.value) return false;
    if (!fSistema.checked && it.c.sistema && it.c.cat === 'sistema') return false;
    const actor = e.actor || '';
    const menciones = e.data?.mentions || [];
    if (fAgente.value && actor !== fAgente.value && !menciones.includes(fAgente.value) && e.data?.agent !== fAgente.value && e.data?.previous_holder !== fAgente.value) return false;
    if (fOwner.value && agentes.get(actor) !== fOwner.value && !menciones.some((m) => agentes.get(m) === fOwner.value)) return false;
    if (fTarea.value && String(e.task_id || e.data?.task_id || '') !== fTarea.value.replace('#', '')) return false;
    return true;
  }

  function tarjeta(it) {
    const { e, c } = it;
    const cat = CATEGORIAS[c.cat];
    const li = document.createElement('li');
    li.className = `tarjeta cat-${c.cat}${e.data?.priority === 1 ? ' urgente' : ''}`;
    li.dataset.ev = String(e.id);
    if (e.message_id) li.dataset.msg = String(e.message_id);
    li.style.setProperty('--cc', cat.color);
    const cab = document.createElement('div'); cab.className = 'cab';
    const ic = document.createElement('span'); ic.className = 'icono'; ic.textContent = cat.icono; ic.setAttribute('aria-hidden', 'true');
    const ti = document.createElement('span'); ti.className = 'titulo'; ti.textContent = c.titulo;
    const hr = document.createElement('time'); hr.textContent = hora(e.ts); hr.dateTime = new Date(e.ts).toISOString();
    cab.append(ic, ti, hr);
    const ruta = document.createElement('div'); ruta.className = 'ruta';
    const para = (e.data?.mentions || []).join(', ') || e.data?.assignee || e.data?.agent || (e.data?.previous_holder ? `antes: ${e.data.previous_holder}` : '');
    ruta.textContent = [e.actor || (c.sistema ? 'Agent Office' : ''), para].filter(Boolean).join(' → ');
    const res = document.createElement('div'); res.className = 'resumen-ev';
    res.textContent = resumen(e);
    const pie = document.createElement('div'); pie.className = 'pie';
    const ref = document.createElement('span');
    const tid = e.task_id || e.data?.task_id;
    ref.textContent = [tid ? `TASK #${tid}` : '', e.data?.priority === 1 ? 'PRIORIDAD ALTA' : '', e.channel ? `#${e.channel}` : '', e.project || ''].filter(Boolean).join(' · ');
    pie.append(ref);
    if (e.message_id) { const a = document.createElement('a'); a.href = `/chat#m=${e.message_id}`; a.textContent = 'Ver conversación'; pie.append(a); }
    if (e.actor) {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = 'Ver agente';
      b.addEventListener('click', () => alAgente(e.actor));
      pie.append(b);
    }
    li.append(cab, ruta, res, pie);
    return li;
  }

  function pintar() {
    const vis = items.filter(pasa).slice(-120).reverse();
    lista.replaceChildren(...vis.map(tarjeta));
    raiz.querySelector('.cuenta').textContent = `${vis.length} eventos`;
  }

  return {
    agregar(evs, clasificar) {
      for (const e of evs) {
        if (vistos.has(e.id)) continue;
        vistos.add(e.id);
        items.push({ e, c: clasificar(e) });
      }
      items.sort((a, b) => a.e.id - b.e.id);
      if (items.length > MAX) items.splice(0, items.length - MAX);
      pintar();
    },
    actualizarAgentes(sesiones) {
      let cambio = false;
      for (const s of sesiones) if (agentes.get(s.nombre) !== s.owner) { agentes.set(s.nombre, s.owner); cambio = true; }
      if (!cambio) return;
      const sel = (el, valores) => { const v = el.value; el.replaceChildren(new Option(el.dataset.todos, '')); for (const x of [...valores].sort()) el.append(new Option(x, x)); el.value = v; };
      sel(fAgente, agentes.keys());
      sel(fOwner, new Set(agentes.values()));
    },
    filtrarAgente(nombre) { if (nombre) { fAgente.value = nombre; pintar(); } },
    estado(texto, clase) { pill.textContent = texto; pill.className = `conexion ${clase}`; },
  };
}

function resumen(e) {
  const d = e.data || {};
  switch (e.type) {
    case 'message.created': return d.summary || '';
    case 'task.created': return d.title || '';
    case 'task.claimed': return `${d.title || ''} · epoch ${d.epoch}${d.recovered ? ' · recuperada' : ''}`;
    case 'task.updated': return Object.entries(d).filter(([k, v]) => v != null && !['from', 'note'].includes(k)).map(([k, v]) => `${k}: ${v}`).join(', ') + (d.note ? ` — ${d.note}` : '');
    case 'task.blocked': return d.reason || d.note || 'bloqueada';
    case 'task.orphaned': return `${d.reason} · antes: ${d.previous_holder || '—'} (epoch ${d.epoch})${d.checkpoint_age_s != null ? ` · checkpoint de hace ${d.checkpoint_age_s}s` : ' · sin checkpoint'}`;
    case 'task.recovered': return 'disponible de nuevo';
    case 'task.checkpoint': return [d.summary, d.branch && `rama ${d.branch}`, d.commit && `commit ${String(d.commit).slice(0, 10)}`].filter(Boolean).join(' · ');
    case 'task.stale_write': return `generación ${d.your_epoch} rechazada; vigente ${d.current_epoch}`;
    case 'task.wait_timeout': return `esperando ${Math.round((d.waiting_s || 0) / 60)} min — requiere intervención`;
    case 'lease.acquired': case 'lease.released': case 'lease.expired': return `${d.resource || ''}${d.reason ? ` · ${d.reason}` : ''}`;
    case 'session.registered': return `${d.runtime || ''}${d.parent_id ? ' · subagente' : ''}${d.specialization ? ` · ${d.specialization}` : ''}`;
    case 'session.stalled': return `${d.agent || ''} sin progreso desde hace ${Math.round((d.no_progress_s || 0) / 60)} min`;
    case 'session.ended': case 'session.expired': return d.reason || '';
    case 'coordinator.offered': return `${d.agent} · epoch ${d.epoch} · pendiente de aceptar`;
    case 'coordinator.accepted': return `${d.agent} · epoch ${d.epoch}`;
    case 'coordinator.declined': return `${d.reason || 'sin motivo'}${d.proposed ? ' · propone otro candidato' : ''}`;
    case 'coordinator.lost': return `${d.reason} · epoch ${d.epoch}`;
    case 'coordinator.unavailable': return 'Ningún candidato disponible. Las tareas siguen recuperables.';
    case 'delivery.overdue': return `${d.recipient} no ha confirmado un mensaje urgente`;
    default: return '';
  }
}
