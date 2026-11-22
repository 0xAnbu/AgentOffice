// AGENT OFFICE — the building: data from the backend, who is on which floor, what walks, the labels, the camera and the
// render loop. Evolved from oficina-virtual's oficina.js (MIT, Magma Consulting Group): the render pipeline, the camera,
// the procedural characters, their poses and the errand animations are the originals. What changed: the data source
// (the Agent Office API + event log, never a simulation), several floors stacked as a sectioned building (one floor per
// specialization, configurable), elevators between floors, labels with runtime/owner/task, and the Activity Center.
//
// Visualization plane only: it reads; it never changes coordination state, and agents never depend on it.
import * as THREE from './vendor/three.module.min.js';
import { OrbitControls } from './vendor/OrbitControls.js';
import { distribuir, caminoEntre, enRuta, hash, D, NUCLEO, SALA, CORREDOR } from './plano3d.js';
import { crearMundo } from './mundo.js';
import { crearPersonas } from './personas.js';
import * as TX from './texturas.js';
import { crearPost } from './post.js';
import { conectar, aEdificio, sesionDeAgente, token, clasificar } from './datos.js';
import { crearActividad } from './actividad.js';

const $ = (s) => document.querySelector(s);
const oficinaEl = $('.oficina');
const lienzo = $('.escena'), capaRotulos = $('.rotulos');
const resumenEl = $('.resumen'), zoomEl = $('.zoom');
const reducido = matchMedia('(prefers-reduced-motion: reduce)');
const params = new URLSearchParams(location.search);

const PASO = 1.45, PASO_RECADO = 1.95;       // metros por segundo: andando y llevando algo
const PLANTA = 5.6;                           // de una planta a la siguiente: losa + 3,3 m de muros + aire para ver dentro desde el lado
const TINTA = { dia: ['#3A3A3C', '#8E8E93', '#1C1C1E', '#E5E5EA'], noche: ['#D1D5DB', '#FFFFFF', '#8E96A3', '#FFFFFF'] };
const MANOS = { terminal: 'teclear', escribiendo: 'teclear', equipo: 'teclear', leyendo: 'raton', navegador: 'raton', pensando: 'pensar' };

// ?tema=dia o ?tema=noche fija la luz sin guardarla (para una pantalla en la pared, o una captura).
const temaPedido = params.get('tema');
let tema = (['dia', 'noche'].includes(temaPedido) && temaPedido)
  || (() => { try { return localStorage.getItem('oficina.tema'); } catch { return null; } })()
  || (matchMedia('(prefers-color-scheme: dark)').matches ? 'noche' : 'dia');
if (!['dia', 'noche'].includes(tema)) tema = 'dia';

// ============================================================================================ tiempo y texto
const hhmm = (s) => (s ? String(s).slice(11, 16) : '');
const MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'];
const DIAS = ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado'];
const hoyTexto = () => new Date().toLocaleString('sv').slice(0, 10);
function cuando(s) {
  if (!s) return '';
  const d = String(s).slice(0, 10);
  const ayer = new Date(Date.now() - 864e5).toLocaleString('sv').slice(0, 10);
  if (d === hoyTexto()) return `hoy, ${hhmm(s)}`;
  if (d === ayer) return `ayer, ${hhmm(s)}`;
  const [, m, dia] = d.split('-').map(Number);
  return `el ${dia} de ${MESES[m - 1]}, ${hhmm(s)}`;
}
const minutosDesde = (s) => (s ? (Date.now() - new Date(String(s).replace(' ', 'T')).getTime()) / 60000 : Infinity);
function hace(s) {
  const m = minutosDesde(s);
  if (!isFinite(m)) return '';
  if (m < 1) return 'hace un momento';
  if (m < 60) return `hace ${Math.round(m)} min`;
  return `desde las ${hhmm(s)}`;
}
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const suave = (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);
const angulo = (a) => Math.atan2(Math.sin(a), Math.cos(a));

// ============================================================================================ la escena
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas: lienzo, antialias: true, powerPreference: 'high-performance' });
} catch {
  document.body.classList.add('sin-3d');
  throw new Error('Este navegador no dibuja 3D');
}
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.NeutralToneMapping;
renderer.toneMappingExposure = 1.0;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.shadowMap.autoUpdate = false;
const escena = new THREE.Scene();
const camara = new THREE.PerspectiveCamera(30, 1, 0.4, 1800);
const controles = new OrbitControls(camara, lienzo);
controles.enableDamping = true;
controles.dampingFactor = 0.09;
controles.zoomToCursor = true;
controles.screenSpacePanning = false;
controles.minDistance = 3.5;
controles.maxDistance = 300;
controles.minPolarAngle = 0.12;
controles.maxPolarAngle = 1.33;
controles.rotateSpeed = 0.55;
controles.panSpeed = 0.9;
controles.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
controles.touches = { ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN };
if (params.has('vitrina')) { controles.autoRotate = true; controles.autoRotateSpeed = 0.25; }

const hemi = new THREE.HemisphereLight('#D6E7F6', '#B3A797', 1.1);
const sol = new THREE.DirectionalLight('#FFF2DE', 2.6);
sol.castShadow = true;
sol.shadow.mapSize.set(4096, 4096);
sol.shadow.bias = -0.00035;
sol.shadow.normalBias = 0.025;
sol.shadow.radius = 3;
escena.add(hemi, sol, sol.target);

// El render fino (oclusión, brillo y desenfoque de maqueta). Arranca en alta y baja solo si la gráfica no alcanza.
const post = crearPost(renderer, escena, camara);
const calidadPedida = ['alta', 'media', 'baja'].includes(params.get('calidad')) ? params.get('calidad') : null;
const pmrem = new THREE.PMREMGenerator(renderer);
/** El reflejo del cielo en los vidrios: una esfera con su degradado, del cielo al suelo. */
function entorno(noche) {
  const e = new THREE.Scene();
  const geo = new THREE.SphereGeometry(50, 32, 16);
  const c = [];
  const arriba = new THREE.Color(noche ? '#0B1222' : '#8FC2EC'), horizonte = new THREE.Color(noche ? '#3A1C2C' : '#F1EEE8'), abajo = new THREE.Color(noche ? '#07080A' : '#7A7670');
  for (let i = 0; i < geo.attributes.position.count; i++) {
    const y = geo.attributes.position.getY(i) / 50;
    const k = y > 0 ? horizonte.clone().lerp(arriba, Math.pow(y, 0.6)) : horizonte.clone().lerp(abajo, Math.min(1, -y * 3));
    c.push(k.r, k.g, k.b);
  }
  geo.setAttribute('color', new THREE.Float32BufferAttribute(c, 3));
  e.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.BackSide })));
  if (!noche) { const s = new THREE.Mesh(new THREE.SphereGeometry(4, 16, 8), new THREE.MeshBasicMaterial({ color: new THREE.Color(9, 8.4, 7.6) })); s.position.set(-22, 32, 26); e.add(s); }
  const rt = pmrem.fromScene(e, 0.03);
  geo.dispose();
  return rt.texture;
}


// ============================================================================================ el estado
let datos = null;                 // { pisos: [...], sesiones: Map } from the snapshot
let firma = '';
let pisos = [];                   // [{ key, nombre, y, plano, mundo, tropa }]  index 0 = ground floor
const pisoDe = new Map();         // slug (session id) -> floor index
const rotulos = new Map();
const gente = new Map();          // slug -> lo que hace la persona ahora
const enCaminoA = new Map();      // slug -> hojas que vienen en camino (todavía no están en su bandeja)
const caidas = new Map();         // slug -> instante en que le cayó una hoja
const pulsos = new Map();         // slug -> instante en que le llegó un mensaje
const marcas = new Map();         // slug -> { clase, hasta } indicador temporal (nuevo coordinador, éxito)
const vuelos = [];                // lo que va por el aire de una mano a una bandeja (coordenadas del mundo)
let resumen = null;
let encima = null;
let reloj = 0;
let actividad = null;

const P = (slug) => datos?.sesiones.get(slug);
const PU = (slug) => { const k = pisoDe.get(slug); return k === undefined ? null : pisos[k].plano.puestos[slug]; };
const aMundo = (k, x, y, z) => new THREE.Vector3(x, y, z).add(pisos[k].mundo.grupo.position);
const paletaDe = (slug) => P(slug)?.paleta || TINTA[tema];
const nombreVisual = (slug) => { const s = P(slug) || {}; return { nombre: s.nombre || slug, cargo: s.owner || '', genero: s.genero }; };
const esVoz = (tipo) => tipo === 'voz' || tipo === 'voz-respuesta';

// ============================================================================================ la cámara
let casa = null;            // la vista de todo el edificio
let vuelo = null;
let movidaEn = 0;

function cajaEdificio() {
  const { WT } = pisos[0].plano;
  return new THREE.Box3(new THREE.Vector3(-WT / 2 - 1, -0.6, -D / 2 - 1), new THREE.Vector3(WT / 2 + 1, (pisos.length - 1) * PLANTA + 3.3, D / 2 + 1));
}
function distanciaQueEncuadra(caja, objetivo, az, polar, aire = 0.9) {
  const puntos = [];
  for (const x of [caja.min.x, caja.max.x]) for (const y of [caja.min.y, caja.max.y]) for (const z of [caja.min.z, caja.max.z]) puntos.push(new THREE.Vector3(x, y, z));
  const area = areaUtil();
  const c = camara.clone();
  let a = 2, b = 900;
  for (let k = 0; k < 32; k++) {
    const d = (a + b) / 2;
    ponerEn(c, objetivo, az, polar, d);
    c.updateMatrixWorld();
    let cabe = true;
    for (const p of puntos) {
      const q = p.clone().project(c);
      const sx = (q.x + 1) / 2 * innerWidth, sy = (1 - q.y) / 2 * innerHeight;
      if (q.z > 1 || sx < area.x0 + area.w * (1 - aire) / 2 || sx > area.x1 - area.w * (1 - aire) / 2 || sy < area.y0 + area.h * (1 - aire) / 2 || sy > area.y1 - area.h * (1 - aire) / 2) { cabe = false; break; }
    }
    if (cabe) b = d; else a = d;
  }
  return b;
}
function areaUtil() {
  const arriba = Math.max($('.cabeza').getBoundingClientRect().bottom + innerHeight * 0.02, innerHeight * 0.12);
  return { x0: innerWidth * 0.02, x1: innerWidth * 0.98, y0: arriba, y1: innerHeight * 0.97, w: innerWidth * 0.96, h: innerHeight * 0.97 - arriba };
}
function ponerEn(c, objetivo, az, polar, d) {
  c.position.set(objetivo.x + d * Math.sin(polar) * Math.sin(az), objetivo.y + d * Math.cos(polar), objetivo.z + d * Math.sin(polar) * Math.cos(az));
  c.lookAt(objetivo);
}
/** La vista en la que la caja queda centrada en el área libre: se corre el objetivo para que no la tape el título. */
function vistaQueEnmarca(caja, az = 0.62, polar = 0.93, aire = 0.92) {
  const objetivo = caja.getCenter(new THREE.Vector3());
  objetivo.y = Math.max(objetivo.y, 0.6);
  const d = distanciaQueEncuadra(caja, objetivo, az, polar, aire);
  // Corrimiento vertical: el centro del área útil no es el centro de la pantalla.
  const area = areaUtil();
  const dy = ((area.y0 + area.y1) / 2 - innerHeight / 2) / innerHeight;   // fracción de la pantalla
  const altoMundo = 2 * d * Math.tan(THREE.MathUtils.degToRad(camara.fov / 2));
  const arribaPantalla = new THREE.Vector3(-Math.cos(polar) * Math.sin(az), Math.sin(polar), -Math.cos(polar) * Math.cos(az));
  objetivo.addScaledVector(arribaPantalla, dy * altoMundo);
  return { objetivo, az, polar, d };
}

// Dollhouse view: from the side, slightly above, so every floor is visible through its cut-away front.
const vistaCasa = () => vistaQueEnmarca(cajaEdificio(), 0.32, 1.3, 0.94);
function vistaDePiso(k) {
  const { WT } = pisos[k].plano;
  const y = pisos[k].y;
  return vistaQueEnmarca(new THREE.Box3(new THREE.Vector3(-WT / 2, y, -D / 2), new THREE.Vector3(WT / 2, y + 2.4, D / 2)), 0.4, 1.12, 0.92);
}
function vistaDeZona(z, k) {
  const { CX, CY } = pisos[k].mundo;
  const y = pisos[k].y;
  return vistaQueEnmarca(new THREE.Box3(new THREE.Vector3(z.x0 - CX, y, z.y0 - CY), new THREE.Vector3(z.x1 - CX, y + 2.2, z.y1 - CY)), 0.5, 0.95, 0.9);
}
function vistaDePuesto(slug) {
  const k = gente.get(slug)?.piso ?? pisoDe.get(slug);
  const { CX, CY } = pisos[k].mundo;
  const p = PU(slug);
  const g = gente.get(slug);
  const x = (g?.x ?? p.x) - CX, z = (g?.y ?? p.y) - CY, y = pisos[k].y;
  return vistaQueEnmarca(new THREE.Box3(new THREE.Vector3(x - 1.5, y, z - 1.6), new THREE.Vector3(x + 1.5, y + 2.1, z + 1.4)), 0.42, 0.98, 0.6);
}
function vistaActual() {
  const off = camara.position.clone().sub(controles.target);
  const d = off.length();
  return { objetivo: controles.target.clone(), az: Math.atan2(off.x, off.z), polar: Math.acos(clamp(off.y / d, -1, 1)), d };
}
function volarA(meta, dur = 1100) {
  const a = vistaActual();
  let daz = angulo(meta.az - a.az);
  vuelo = { a, b: { ...meta, az: a.az + daz }, t0: performance.now(), dur: reducido.matches ? 1 : dur };
  controles.enabled = false;
}
function avanzarVuelo(ahora) {
  if (!vuelo) return;
  const u = clamp((ahora - vuelo.t0) / vuelo.dur, 0, 1), e = suave(u);
  const { a, b } = vuelo;
  const objetivo = a.objetivo.clone().lerp(b.objetivo, e);
  const d = Math.exp(Math.log(a.d) + (Math.log(b.d) - Math.log(a.d)) * e);
  controles.target.copy(objetivo);
  ponerEn(camara, objetivo, a.az + (b.az - a.az) * e, a.polar + (b.polar - a.polar) * e, d);
  movidaEn = ahora;
  if (u >= 1) { vuelo = null; controles.enabled = true; controles.update(); }
}
const estaEnCasa = () => {
  if (!casa) return true;
  const v = vistaActual();
  return v.objetivo.distanceTo(casa.objetivo) < 1.2 && Math.abs(v.d / casa.d - 1) < 0.06 && Math.abs(angulo(v.az - casa.az)) < 0.06 && Math.abs(v.polar - casa.polar) < 0.05;
};
controles.addEventListener('change', () => { movidaEn = performance.now(); });


function encuadrar() {
  const dpr = Number(params.get('dpr')) || Math.min(window.devicePixelRatio || 1, 2);
  renderer.setPixelRatio(dpr);
  renderer.setSize(innerWidth, innerHeight, false);
  post.tamano();
  camara.aspect = innerWidth / innerHeight;
  camara.updateProjectionMatrix();
  if (!pisos.length) return;
  const enCasa = !casa || estaEnCasa();
  casa = vistaCasa();
  if (enCasa && !vuelo) { controles.target.copy(casa.objetivo); ponerEn(camara, casa.objetivo, casa.az, casa.polar, casa.d); controles.update(); }
  movidaEn = performance.now();
}
let esperaEncuadre = 0;
window.addEventListener('resize', () => { clearTimeout(esperaEncuadre); esperaEncuadre = setTimeout(encuadrar, 120); });

// ============================================================================================ día y noche
function ponerTema(nuevo, guardar) {
  tema = nuevo;
  const noche = tema === 'noche';
  document.documentElement.dataset.tema = tema;
  for (const b of document.querySelectorAll('.tema button')) b.setAttribute('aria-checked', String(b.dataset.tema === tema));
  if (guardar) { try { localStorage.setItem('oficina.tema', tema); } catch { /* sin almacenamiento */ } }
  const viejo = escena.environment;
  escena.environment = entorno(noche);
  viejo?.dispose?.();
  escena.environmentIntensity = noche ? 0.32 : 0.75;
  if (escena.background?.dispose) escena.background.dispose();
  escena.background = TX.cielo(noche);
  escena.fog = new THREE.FogExp2(noche ? '#1D1220' : '#E9EDF0', noche ? 0.0024 : 0.0019);
  hemi.color.set(noche ? '#4A5878' : '#D6E7F6');
  hemi.groundColor.set(noche ? '#2A1E18' : '#B3A797');
  hemi.intensity = noche ? 0.55 : 1.15;
  // De noche el sol se vuelve la luz del techo: cálida y casi vertical, como las lámparas de una oficina.
  sol.color.set(noche ? '#FFD9A8' : '#FFF2DE');
  sol.intensity = noche ? 1.5 : 2.7;
  const dir = noche ? new THREE.Vector3(-0.18, 1, 0.24) : new THREE.Vector3(-0.62, 1, 0.66);
  sol.position.copy(dir.normalize().multiplyScalar(70));
  renderer.toneMappingExposure = noche ? 1.08 : 1.0;
  for (const P2 of pisos) P2.mundo.noche(noche);
  post.ponerNoche(noche);
  renderer.shadowMap.needsUpdate = true;
}
for (const b of document.querySelectorAll('.tema button')) {
  b.addEventListener('click', () => {
    if (b.dataset.tema === tema) return;
    oficinaEl.classList.add('cambiando');
    setTimeout(() => { ponerTema(b.dataset.tema, true); oficinaEl.classList.remove('cambiando'); }, 260);
  });
}


// ============================================================================================ quién está dónde
// Each one stays at their desk: working (typing), waiting / without progress (turned, hand up now and then) or idle (with a
// coffee). Disconnected: the chair stays empty. They only stand up to carry something to someone (a message, a task)
// — taking the elevator when the recipient works on another floor — and then sit down again.
const SENTADAS = new Set(['sentado', 'sofa', 'taburete', 'tumbado']);

function destinoDe(slug) {
  const s = P(slug) || {};
  const p = PU(slug);
  if (!p) return null;
  const sentado = (manos, extra = {}) => ({ tipo: 'escritorio', piso: pisoDe.get(slug), x: p.x, y: p.y, rumbo: p.yaw, acceso: p.acceso, pose: { que: 'sentado', manos, ...extra } });
  if (s.estado === 'trabajando') return sentado(MANOS[s.actividad] || 'teclear');
  if (s.estado === 'espera') return sentado('saludar');
  if (s.estado === 'libre') return sentado('cafe', { lleva: 'taza' });
  return { tipo: 'fuera' };
}

function nuevaPersona(slug) {
  const g = { slug, piso: pisoDe.get(slug) ?? 0, visible: false, x: 0, y: 0, rumbo: 0, modo: 'quieto', destino: null, recados: [], pose: { que: 'de_pie' }, s: 0, paso: 0 };
  gente.set(slug, g);
  return g;
}

function ubicarDeUna() {
  for (const slug of pisoDe.keys()) {
    const g = gente.get(slug) || nuevaPersona(slug);
    g.piso = pisoDe.get(slug);
    const d = destinoDe(slug);
    g.destino = d;
    if (!d || d.tipo === 'fuera') { g.visible = false; g.modo = 'fuera'; continue; }
    Object.assign(g, { visible: true, x: d.x, y: d.y, rumbo: d.rumbo, modo: 'quieto', s: SENTADAS.has(d.pose.que) ? 1 : 0, pose: d.pose });
  }
}

function caminar(g, meta, rapido, luego) {
  const ruta = caminoEntre(pisos[g.piso].plano.nav, { x: g.x, y: g.y }, meta);
  g.modo = 'caminando';
  g.ruta = ruta;
  g.recorrido = 0;
  g.vel = rapido ? PASO_RECADO : PASO;
  g.luego = luego;
}
/** The elevator door of a floor, and the spot in front of it. */
const ascensor = (k) => { const a = pisos[k].plano.nucleo.ascensores[0]; return { x: a.x + 0.55, y: a.y }; };
/** Walk to the elevator, ride (never an instant jump: the cab travels and the doors open on both floors), step out. */
function tomarAscensor(g, hacia, luego) {
  caminar(g, ascensor(g.piso), true, () => {
    const desde = g.piso;
    pisos[desde].mundo.abrirAscensor(0, reloj, 1.4);
    g.modo = 'ascensor';
    g.transito = { desde, hacia, t0: reloj + 0.6, dur: 1.1 + 0.55 * Math.abs(hacia - desde), luego };
  });
}
function decidir(g, t) {
  if (['caminando', 'levantandose', 'sentandose', 'entregando', 'acercandose', 'ascensor'].includes(g.modo)) return;
  const recado = g.recados[0];
  const destino = destinoDe(g.slug);
  const p = PU(g.slug);
  if (!p) return;
  const casaPiso = pisoDe.get(g.slug);
  const sentado = g.s > 0.5;
  if (!recado) {
    if (!destino || destino.tipo === 'fuera') {
      if (g.visible && sentado) { g.modo = 'levantandose'; g.t0 = t; return; }
      if (g.visible) { g.visible = false; g.modo = 'fuera'; }
      return;
    }
    g.destino = destino;
    if (g.visible && g.piso === casaPiso && Math.hypot(g.x - destino.x, g.y - destino.y) < 0.05 && sentado) { g.pose = destino.pose; return; }
  }
  if (!g.visible) {
    g.piso = casaPiso;
    Object.assign(g, { visible: true, x: p.acceso.x, y: p.acceso.y, rumbo: p.yaw, s: 0, pose: { que: 'de_pie' } });
    if (!recado) { g.modo = 'acercandose'; g.t0 = t; g.desde = { x: g.x, y: g.y, rumbo: g.rumbo }; }
    return;
  }
  if (sentado) { g.modo = 'levantandose'; g.t0 = t; return; }
  if (recado) {
    const kB = pisoDe.get(recado.para);
    const B = PU(recado.para);
    if (!B) { g.recados.shift(); return; }
    g.pose = { que: 'caminar', lleva: esVoz(recado.tipo) ? 'nota' : 'carpeta' };
    if (kB !== g.piso) { tomarAscensor(g, kB, null); return; }
    caminar(g, { x: B.x + 0.85, y: B.y + 0.45 }, true, () => { g.modo = 'entregando'; g.t0 = reloj; g.rumboMeta = Math.atan2(B.x - g.x, B.y - g.y); });
    return;
  }
  if (!destino || destino.tipo === 'fuera') return;
  g.pose = { que: 'caminar' };
  if (g.piso !== casaPiso) { tomarAscensor(g, casaPiso, null); return; }
  caminar(g, destino.acceso, false, () => { g.modo = 'acercandose'; g.t0 = reloj; g.desde = { x: g.x, y: g.y, rumbo: g.rumbo }; });
}

function enAscensor(g, t) {
  const tr = g.transito;
  if (t < tr.t0) return;
  g.visible = false;                          // inside the cab: the cab mesh shows the ride
  const u = clamp((t - tr.t0) / tr.dur, 0, 1);
  cabinas.set(g.slug, { desde: tr.desde, hacia: tr.hacia, u: suave(u) });
  if (u >= 1) {
    cabinas.delete(g.slug);
    g.piso = tr.hacia;
    const e = ascensor(g.piso);
    pisos[g.piso].mundo.abrirAscensor(0, t, 1.4);
    Object.assign(g, { visible: true, x: e.x, y: e.y, modo: 'quieto', transito: null });
  }
}

function simular(t, dt) {
  for (const g of gente.values()) {
    decidir(g, t);
    if (g.modo === 'caminando') {
      g.recorrido += g.vel * dt;
      const p = enRuta(g.ruta, g.recorrido);
      g.x = p.x; g.y = p.y;
      g.rumbo += clamp(angulo(p.rumbo - g.rumbo), -8 * dt, 8 * dt);
      g.paso += g.vel * dt;
      g.pose = { ...g.pose, que: 'caminar', paso: g.paso };
      if (g.recorrido >= g.ruta.largo - 1e-3) { g.modo = 'quieto'; const l = g.luego; g.luego = null; l?.(); }
    } else if (g.modo === 'levantandose') {
      const u = clamp((t - g.t0) / 0.5, 0, 1);
      g.s = 1 - suave(u);
      if (u >= 1) {
        // Se aparta del asiento hasta su punto de acceso.
        const d = PU(g.slug);
        g.modo = 'quieto';
        g.pose = { que: 'de_pie' };
        if (d?.acceso) { g.x = d.acceso.x; g.y = d.acceso.y; }
        g.s = 0;
      }
    } else if (g.modo === 'acercandose') {
      // Del punto de acceso al asiento, girando hacia donde mira.
      const d = g.destino;
      const u = clamp((t - g.t0) / 0.55, 0, 1), e = suave(u);
      g.x = g.desde.x + (d.x - g.desde.x) * e;
      g.y = g.desde.y + (d.y - g.desde.y) * e;
      g.rumbo = g.desde.rumbo + angulo(d.rumbo - g.desde.rumbo) * e;
      g.pose = SENTADAS.has(d.pose.que) ? { ...d.pose, s: 0 } : d.pose;
      if (u >= 1) {
        if (SENTADAS.has(d.pose.que)) { g.modo = 'sentandose'; g.t0 = t; }
        else { g.modo = 'quieto'; g.pose = d.pose; }
      }
    } else if (g.modo === 'sentandose') {
      const u = clamp((t - g.t0) / 0.55, 0, 1);
      g.s = suave(u);
      g.pose = g.destino.pose;
      if (u >= 1) { g.modo = 'quieto'; g.s = 1; }
    } else if (g.modo === 'entregando') {
      const u = clamp((t - g.t0) / 1.1, 0, 1);
      g.rumbo += clamp(angulo(g.rumboMeta - g.rumbo), -6 * dt, 6 * dt);
      const recado = g.recados[0];
      g.pose = { que: 'de_pie', lleva: u < 0.35 ? (esVoz(recado.tipo) ? 'nota' : 'carpeta') : null };
      if (!g.solto && u >= 0.35) {
        g.solto = true;
        lanzarVuelo(g, recado, t);
      }
      if (u >= 1) { g.recados.shift(); g.solto = false; g.modo = 'quieto'; g.pose = { que: 'de_pie' }; }
    }
  }
}


/** What is handed over flies from the hand to the tray (a task) or to the recipient's sphere (a message). World coords. */
function lanzarVuelo(g, recado, t) {
  const kB = pisoDe.get(recado.para);
  const B = PU(recado.para);
  const per = personaDe(g.slug);
  const desde = per?.manoEn ? aMundo(g.piso, per.manoEn.x, per.manoEn.y, per.manoEn.z) : aMundo(g.piso, g.x, 1.0, g.y);
  let hasta;
  const voz = esVoz(recado.tipo);
  if (voz) {
    const pr = personaDe(recado.para);
    hasta = pr && !pr.oculto && pr.cabezaEn ? aMundo(kB, pr.cabezaEn.x, pr.cabezaEn.y + 0.32, pr.cabezaEn.z) : aMundo(kB, B.x, 1.6, B.y);
  } else {
    const b = pisos[kB].mundo.bandejas.get(recado.para);
    hasta = b ? aMundo(kB, b.pos.x, b.pos.y + 0.02, b.pos.z) : aMundo(kB, B.x, 0.8, B.y - 0.3);
  }
  vuelos.push({ desde, hasta, t0: t, dur: 0.62, voz, para: recado.para, tipo: recado.tipo, color: paletaDe(g.slug)[0] });
}

// ============================================================================================ lo que viene del backend
/** Visual queue, independent of the real message queue: at most three pending errands per person; never blocks. */
function lanzar(m) {
  const { de, para, tipo } = m;
  if (!PU(de) || !PU(para) || de === para) return;
  const g = gente.get(de) || nuevaPersona(de);
  if (!g.visible && P(de)?.estado === 'cerrado') { pulsos.set(para, reloj); return; } // sender offline: just pulse the recipient
  if (tipo === 'encargo') enCaminoA.set(para, (enCaminoA.get(para) || 0) + 1);
  g.recados.push(m);
  while (g.recados.length > 3) {
    const fuera = g.recados.splice(g.modo === 'caminando' || g.modo === 'entregando' || g.modo === 'ascensor' ? 1 : 0, 1)[0];
    if (fuera?.tipo === 'encargo') enCaminoA.set(fuera.para, Math.max(0, (enCaminoA.get(fuera.para) || 1) - 1));
  }
}
const sesionDe = (e) => (e.session_id && P(e.session_id) ? e.session_id : e.actor ? sesionDeAgente(datos.sesiones, e.actor) : null);
function marcar(slug, clase, segundos) { if (slug) marcas.set(slug, { clase, hasta: reloj + segundos }); }

/** Real events -> animations (§8.5). Replayed history is listed in the Activity Center but not animated. */
function alEvento(e) {
  if (!datos || !pisos.length) return;
  const de = sesionDe(e);
  if (e.type === 'message.created') {
    for (const quien of e.data?.mentions || []) {
      const para = sesionDeAgente(datos.sesiones, quien);
      const kind = e.data?.kind;
      if (de && para) lanzar({ de, para, tipo: kind === 'review' || kind === 'task' || kind === 'handoff' ? 'encargo' : 'voz' });
      else if (para) pulsos.set(para, reloj);
    }
  } else if (e.type === 'task.updated' && e.data?.assignee) {
    const para = sesionDeAgente(datos.sesiones, e.data.assignee);
    if (de && para) lanzar({ de, para, tipo: 'encargo' });
  } else if (e.type === 'task.updated' && e.data?.status === 'done') marcar(de, 'exito', 4);
  else if (e.type === 'coordinator.accepted') marcar(e.session_id, 'nuevo-coordinador', 10);
  else if (e.type === 'coordinator.offered') marcar(e.session_id, 'candidato', 6);
}

// ============================================================================================ los rótulos
function rotuloDe(slug) {
  let el = rotulos.get(slug);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'rotulo';
  el.setAttribute('role', 'listitem');
  for (const c of ['nombre', 'estado', 'detalle', 'mas']) { const d = document.createElement('div'); d.className = c; el.append(d); }
  const q = document.createElement('span'); q.className = 'quien';
  const insignias = document.createElement('span'); insignias.className = 'insignias';
  const cargo = document.createElement('span'); cargo.className = 'cargo';
  el.querySelector('.nombre').append(insignias, q, cargo);
  el.style.setProperty('--i', String(rotulos.size));
  el.addEventListener('click', () => { volarA(vistaDePuesto(slug)); actividad?.filtrarAgente(P(slug)?.nombre); });
  capaRotulos.append(el);
  rotulos.set(slug, el);
  return el;
}
function escribir(el, sel, texto) {
  const n = el.querySelector(sel);
  if (n.textContent !== texto) { n.textContent = texto; el._medida = null; }
}
function insignias(el, s) {
  const cont = el.querySelector('.insignias');
  const clave = `${s.runtime}|${s.coordinador}|${s.sub}|${s.candidato}|${s.bloqueada}`;
  if (cont._k === clave) return;
  cont._k = clave;
  cont.replaceChildren();
  const add = (cls, txt, title) => { const b = document.createElement('span'); b.className = `insignia ${cls}`; b.textContent = txt; b.title = title; cont.append(b); };
  add('runtime', s.runtime, `Runtime: ${s.sesion.runtime}${s.sesion.model ? ` · ${s.sesion.model}` : ''}`);
  if (s.coordinador) add('coord', '★', 'Coordinador activo');
  if (s.candidato) add('cand', '☆', 'Coordinador propuesto (pendiente de aceptar)');
  if (s.sub) add('sub', 'sub', 'Subagente');
  if (s.bloqueada) add('aviso', '!', 'Tarea bloqueada');
  el._medida = null;
}

const estadosAntes = new Map();
function pintarRotulos() {
  for (const [slug, s] of datos.sesiones) {
    if (!PU(slug)) continue;
    const el = rotuloDe(slug);
    el.style.setProperty('--c', s.paleta[0]);
    const clave = `${s.estado}|${s.frase}`;
    const cambio = estadosAntes.has(slug) && estadosAntes.get(slug) !== clave;
    estadosAntes.set(slug, clave);
    el.classList.remove('trabajando', 'espera', 'libre', 'cerrado', 'cambia');
    el.classList.add(s.estado);
    el.classList.toggle('coordinador', s.coordinador);
    el.classList.toggle('subagente', s.sub);
    if (cambio) { void el.offsetWidth; el.classList.add('cambia'); }
    insignias(el, s);
    escribir(el, '.quien', `${s.nombre}${s.specialization ? ` · ${s.specialization}` : ''}`);
    escribir(el, '.cargo', s.owner);
    escribir(el, '.estado', s.frase);
    escribir(el, '.detalle', s.estado === 'cerrado' ? '' : s.detalle || '');
    const sesion = s.sesion;
    escribir(el, '.mas', `${sesion.runtime}${sesion.model ? ` · ${sesion.model}` : ''} · ${hace(new Date(sesion.last_heartbeat_at).toISOString())}`);
    el.setAttribute('aria-label', `${s.nombre} (${s.runtime}, propietario ${s.owner}): ${s.frase}${s.detalle ? `, ${s.detalle}` : ''}`);
  }
  for (const [slug, el] of rotulos) if (!datos.sesiones.has(slug) || !PU(slug)) { el.remove(); rotulos.delete(slug); }
}

const _v = new THREE.Vector3();
function cabezaMundo(slug) {
  const g = gente.get(slug);
  const per = personaDe(slug);
  const k = g?.piso ?? pisoDe.get(slug);
  if (g?.visible && per && !per.oculto && per.cabezaEn) return aMundo(k, per.cabezaEn.x, per.cabezaEn.y + 0.12, per.cabezaEn.z);
  const p = PU(slug);
  return aMundo(pisoDe.get(slug), p.x, 1.25, p.y);
}
function ubicarRotulos() {
  if (!pisos.length) return;
  const H = innerHeight, W = innerWidth;
  const tanF = Math.tan(THREE.MathUtils.degToRad(camara.fov / 2));
  const dObjetivo = camara.position.distanceTo(controles.target);
  capaRotulos.classList.toggle('cerca', dObjetivo < 24);
  const lista = [];
  for (const [slug, el] of rotulos) {
    const g = gente.get(slug);
    _v.copy(cabezaMundo(slug));
    const dist = _v.distanceTo(camara.position);
    _v.project(camara);
    const fuera = _v.z > 1 || _v.x < -1.15 || _v.x > 1.15 || _v.y < -1.2 || _v.y > 1.2;
    const sx = (_v.x + 1) / 2 * W, sy = (1 - _v.y) / 2 * H;
    const ppm = H / (2 * dist * tanF);
    const u = clamp(Math.pow(ppm, 0.56) * 8, 36, 96);
    const m = marcas.get(slug);
    el.classList.toggle('marca-exito', !!m && m.clase === 'exito' && m.hasta > reloj);
    el.classList.toggle('marca-coordinador', !!m && m.clase === 'nuevo-coordinador' && m.hasta > reloj);
    lista.push({ el, sx, sy, u, dist, fuera, ausente: !g?.visible });
  }
  const cerca = capaRotulos.classList.contains('cerca');
  lista.sort((a, b) => (a.ausente - b.ausente) || (a.dist - b.dist));
  const puestos = [];
  const medir = (el, compacto) => {
    const k = `${cerca}|${compacto}`;
    if (el._medida?.k !== k) {
      el.classList.toggle('compacto', compacto);
      const u = parseFloat(el._u) || 40;
      el._medida = { k, w: el.offsetWidth / u, h: el.offsetHeight / u };
    }
    return el._medida;
  };
  for (const r of lista) {
    if (r.fuera) { if (!r.el.classList.contains('oculto')) r.el.classList.add('oculto'); continue; }
    r.el.classList.remove('oculto');
    const us = `${r.u.toFixed(1)}px`;
    if (r.el._u !== us) { r.el.style.setProperty('--u', us); r.el._u = us; }
    const choca = (x, y, w, h) => puestos.find((q) => Math.abs(q.x - x) < (w + q.w) / 2 && y - h < q.y && y > q.y - q.h);
    let compacto = !cerca;   // general view: name, state and owner badge only; hover/zoom expands (§8.3)
    let m = medir(r.el, compacto);
    let w = m.w * r.u, h = m.h * r.u, y = r.sy;
    if (r.ausente && !r.el.classList.contains('encima') && choca(r.sx, y, w * 1.06, h)) { r.el.classList.add('oculto'); continue; }
    for (let intento = 0; intento < 4; intento++) { const c = choca(r.sx, y, w, h); if (!c) break; y = c.y - c.h - 3; }
    if (r.sy - y > r.u * 1.6 || choca(r.sx, y, w, h)) {
      compacto = true;
      m = medir(r.el, true);
      w = m.w * r.u; h = m.h * r.u; y = r.sy;
      for (let intento = 0; intento < 3; intento++) { const c = choca(r.sx, y, w, h); if (!c) break; y = c.y - c.h - 2; }
    }
    if (r.ausente && choca(r.sx, y, w, h) && !r.el.classList.contains('encima')) { r.el.classList.add('oculto'); continue; }
    puestos.push({ x: r.sx, y, w, h });
    if (r.el.classList.contains('compacto') !== compacto) r.el.classList.toggle('compacto', compacto);
    const t = `${r.sx.toFixed(1)}px ${y.toFixed(1)}px`;
    if (r.el._t !== t) { r.el.style.translate = t; r.el._t = t; }
    r.el.style.zIndex = String(1000 - Math.round(r.dist));
  }
  // floor names, at the west end of each floor
  for (const [k, el] of letrerosPiso) {
    const { WT } = pisos[k].plano;
    _v.set(-WT / 2 - 0.6, pisos[k].y + 1.6, 0).project(camara);
    el.style.translate = `${((_v.x + 1) / 2 * W).toFixed(1)}px ${((1 - _v.y) / 2 * H).toFixed(1)}px`;
    el.classList.toggle('oculto', _v.z > 1);
  }
}

// ============================================================================================ resumen
function pintarResumen() {
  const v = [...datos.sesiones.values()];
  const n = (e) => v.filter((s) => s.estado === e).length;
  const d = new Date();
  const partes = [
    `${DIAS[d.getDay()][0].toUpperCase()}${DIAS[d.getDay()].slice(1)} ${d.getDate()} de ${MESES[d.getMonth()]}, ${d.toTimeString().slice(0, 5)}`,
    `<b>${n('trabajando')}</b> trabajando`,
    `<b>${n('espera')}</b> esperando o sin progreso`,
    `<b>${n('libre')}</b> ${n('libre') === 1 ? 'libre' : 'libres'}`,
    `<b>${n('cerrado')}</b> ${n('cerrado') === 1 ? 'desconectado' : 'desconectados'}`,
  ];
  const html = partes.join('<span class="sep">·</span>');   // only numbers and fixed words: no untrusted text here
  if (resumenEl.innerHTML !== html) resumenEl.innerHTML = html;
  const r = { trabajando: n('trabajando'), espera: n('espera'), libre: n('libre'), cerrado: n('cerrado') };
  const hora = d.toTimeString().slice(0, 5);
  const f = JSON.stringify(r) + hora;
  if (f !== resumen) { resumen = f; for (const p of pisos) p.mundo.pintarTeles(r, hora); }
}

// ============================================================================================ las bandejas, lo que vuela y la cabina
let mPilas = null, mVuelos = null, mNotas = null, mCabina = null, pozo = null;
const _m = new THREE.Matrix4(), _q = new THREE.Quaternion(), _s = new THREE.Vector3(), _p = new THREE.Vector3();
let bandejas = [];        // [{ slug, pos (world), rot }]
const cabinas = new Map();
function crearBandejasYVuelos() {
  for (const o of [mPilas, mVuelos, mNotas, mCabina, pozo]) if (o) escena.remove(o);
  bandejas = [];
  pisos.forEach((P2, k) => { for (const [slug, b] of P2.mundo.bandejas) bandejas.push({ slug, pos: aMundo(k, b.pos.x, b.pos.y, b.pos.z), rot: b.rot }); });
  mPilas = new THREE.InstancedMesh(new THREE.BoxGeometry(0.24, 1, 0.19).translate(0, 0.5, 0), new THREE.MeshStandardMaterial({ color: '#F6F3EC', roughness: 0.9, fog: false }), Math.max(1, bandejas.length));
  mPilas.count = bandejas.length;
  mPilas.receiveShadow = true;
  mVuelos = new THREE.InstancedMesh(new THREE.BoxGeometry(0.24, 0.012, 0.3), new THREE.MeshStandardMaterial({ color: '#F6F3EC', roughness: 0.8, fog: false }), 8);
  mVuelos.count = 0; mVuelos.frustumCulled = false;
  mNotas = new THREE.InstancedMesh(new THREE.SphereGeometry(0.07, 16, 10), new THREE.MeshBasicMaterial({ toneMapped: false, fog: false }), 8);
  mNotas.count = 0; mNotas.frustumCulled = false;
  // The elevator shaft (glass) through every floor, and its cab.
  const a = pisos[0].plano.nucleo.ascensores[0];
  const base = aMundo(0, a.x - 0.7, 0, a.y);
  const alto = (pisos.length - 1) * PLANTA + 3.2;
  pozo = new THREE.Mesh(new THREE.BoxGeometry(1.3, alto, 1.5), new THREE.MeshStandardMaterial({ color: '#BFD3E6', transparent: true, opacity: 0.16, roughness: 0.1, metalness: 0.2, depthWrite: false, fog: false }));
  pozo.position.set(base.x, alto / 2, base.z);
  mCabina = new THREE.Mesh(new THREE.BoxGeometry(1.1, 2.3, 1.3), new THREE.MeshStandardMaterial({ color: '#C9CDD2', roughness: 0.3, metalness: 0.8, fog: false }));
  mCabina.position.set(base.x, 1.15, base.z);
  escena.add(mPilas, mVuelos, mNotas, pozo, mCabina);
}
function actualizarBandejas(t) {
  bandejas.forEach((b, k) => {
    const s = P(b.slug) || {};
    const n = Math.max(0, (s.bandeja || 0) - (enCaminoA.get(b.slug) || 0));
    const caida = caidas.has(b.slug) ? Math.max(0, 0.012 * Math.sin(Math.min(1, (t - caidas.get(b.slug)) / 0.35) * Math.PI)) : 0;
    const alto = n ? Math.min(0.11, 0.008 + Math.min(n, 30) * 0.0034) : 0;
    _q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), b.rot + ((hash(b.slug) % 7) - 3) * 0.02);
    _m.compose(_p.copy(b.pos).add(new THREE.Vector3(0, caida, 0)), _q, _s.set(1, Math.max(1e-4, alto), 1));
    mPilas.setMatrixAt(k, _m);
  });
  mPilas.instanceMatrix.needsUpdate = true;
  let nv = 0, nn = 0;
  for (let i = vuelos.length - 1; i >= 0; i--) {
    const v = vuelos[i];
    const u = (t - v.t0) / v.dur;
    if (u >= 1) {
      vuelos.splice(i, 1);
      if (v.tipo === 'encargo') { enCaminoA.set(v.para, Math.max(0, (enCaminoA.get(v.para) || 1) - 1)); caidas.set(v.para, t); }
      if (v.voz) pulsos.set(v.para, t);
      continue;
    }
    const e = suave(clamp(u, 0, 1));
    _p.copy(v.desde).lerp(v.hasta, e);
    _p.y += Math.sin(Math.PI * e) * 0.32;
    if (v.voz && nn < 8) {
      _m.compose(_p, _q.identity(), _s.setScalar(1 - e * 0.4));
      mNotas.setMatrixAt(nn, _m);
      mNotas.setColorAt(nn, new THREE.Color(v.color));
      nn++;
    } else if (!v.voz && nv < 8) {
      _q.setFromEuler(new THREE.Euler(0.4 * (1 - e), e * 2, 0));
      _m.compose(_p, _q, _s.set(1, 1, 1));
      mVuelos.setMatrixAt(nv++, _m);
    }
  }
  mVuelos.count = nv; mVuelos.instanceMatrix.needsUpdate = true;
  mNotas.count = nn; mNotas.instanceMatrix.needsUpdate = true;
  if (mNotas.instanceColor) mNotas.instanceColor.needsUpdate = true;
  // the cab follows the first rider (others share the ride)
  const viaje = cabinas.values().next().value;
  if (viaje) mCabina.position.y = (viaje.desde + (viaje.hacia - viaje.desde) * viaje.u) * PLANTA + 1.15;
}

// ============================================================================================ los datos
let logoSvg = '', isotipoSvg = '';
const letrerosPiso = new Map();
function personaDe(slug) { const k = gente.get(slug)?.piso ?? pisoDe.get(slug); return k === undefined ? null : pisos[k]?.tropa.porSlug.get(slug); }

function alSnapshot(snap) {
  document.documentElement.classList.remove('sin-sesion');  // first authenticated data: show the controls
  const edificio = aEdificio(snap);
  datos = edificio;
  // Rebuild the static building only when its layout (floors, rooms, desks) changes — never on heartbeats (§8.4).
  const f = JSON.stringify(edificio.pisos.map((p) => [p.key, p.zonas.map((z) => [z.clave, z.puestos])]));
  const listo = f === firma ? Promise.resolve() : (firma = f, construir(edificio, snap));
  listo.then(() => {
    pintarRotulos();
    pintarResumen();
    for (const [slug, s] of datos.sesiones) {
      const k = pisoDe.get(slug);
      if (k === undefined) continue;
      pisos[k].mundo.ponerPantalla(slug, s.estado === 'trabajando' ? 'terminal' : s.estado === 'espera' ? 'espera' : s.estado === 'libre' ? 'libre' : 'apagada');
    }
    actividad?.actualizarAgentes([...datos.sesiones.values()]);
    oficinaEl.setAttribute('aria-busy', 'false');
    if (!oficinaEl.classList.contains('lista')) requestAnimationFrame(() => requestAnimationFrame(() => oficinaEl.classList.add('lista')));
  }).catch((e) => console.error(e));
}

let construyendo = Promise.resolve();
function construir(edificio, snap) {
  construyendo = construyendo.then(() => construirYa(edificio, snap));
  return construyendo;
}
let construcciones = 0;
async function construirYa(edificio, snap) {
  construcciones++;
  if (!logoSvg) {
    await Promise.all([document.fonts.load('600 64px Archivo'), document.fonts.load('500 32px "Space Grotesk"')]).catch(() => {});
    TX.ponerMarca({ vino: '#3B2A4A', rojo: '#D9503F', naranja: '#E07A3F', amarillo: '#F2C46B', negro: '#0B0B0B', gris: '#E3E3E3' });
    const titulo = snap.building.title || 'Agent Office';
    document.title = titulo; $('.cabeza h1').textContent = titulo;
    [logoSvg, isotipoSvg] = await Promise.all(['/office/marca/logo.svg', '/office/marca/isotipo.svg'].map((u) => fetch(u).then((r) => r.text())));
  }
  for (const P2 of pisos) escena.remove(P2.mundo.grupo);
  for (const o of [...escena.children]) if (o !== hemi && o !== sol && o !== sol.target) escena.remove(o);
  // Floors bottom-up: the config lists the top floor first.
  const orden = [...edificio.pisos].reverse();
  pisoDe.clear();
  const minimo = Math.max(...orden.map((p) => distribuir(p.zonas, p.puestos).interior));
  pisos = [];
  for (let k = 0; k < orden.length; k++) {
    const def = orden[k];
    const plano = distribuir(def.zonas, def.puestos, minimo);
    const mundo = await crearMundo({ escena, plano, renderer, logoSvg, isotipoSvg, datos: { zonas: def.zonas, puestos: def.puestos }, y: k * PLANTA, conCiudad: k === 0 });
    const quienes = Object.keys(plano.puestos).map((slug) => ({ slug, camisa: def.puestos[slug]?.paleta?.[0] || '#8E8E93', largo: def.puestos[slug]?.genero === 'f' }));
    const tropa = crearPersonas(mundo.grupo, quienes, TX.sombraSuave(), TX.halo());
    // A stacked floor would cast its slab's shadow over the whole floor below (one sun for the building): only the
    // ground floor casts shadows, so every floor stays readable from the side.
    if (k > 0) mundo.grupo.traverse((o) => { o.castShadow = false; });
    for (const slug of Object.keys(plano.puestos)) pisoDe.set(slug, k);
    pisos.push({ key: def.key, nombre: def.nombre, y: k * PLANTA, plano, mundo, tropa });
  }
  // floor labels (DOM) and floor buttons
  for (const el of letrerosPiso.values()) el.remove();
  letrerosPiso.clear();
  pisos.forEach((P2, k) => {
    const el = document.createElement('div');
    el.className = 'letrero-piso';
    el.textContent = `P${k + 1} · ${P2.nombre}`;
    capaRotulos.append(el);
    letrerosPiso.set(k, el);
  });
  pintarPlantas();
  crearBandejasYVuelos();
  sol.target.position.set(0, 0, 0);
  const lado = Math.max(pisos[0].plano.WT, D) / 2 + 8 + pisos.length * 2;
  Object.assign(sol.shadow.camera, { left: -lado, right: lado, top: lado, bottom: -lado, near: 10, far: 220 });
  sol.shadow.camera.updateProjectionMatrix();
  if (!post.nivel) post.armar(calidadPedida || 'alta');
  ponerTema(tema, false);
  ubicarDeUna();
  gente.forEach((g) => { if (!pisoDe.has(g.slug)) gente.delete(g.slug); });
  casa = null;
  construidoEn = performance.now();
  encuadrar();
  const pedido = params.get('planta');
  const kp = pisos.findIndex((p) => p.key === pedido);
  if (kp >= 0) { const v = vistaDePiso(kp); controles.target.copy(v.objetivo); ponerEn(camara, v.objetivo, v.az, v.polar, v.d); controles.update(); }
}

function pintarPlantas() {
  const nav = $('.plantas');
  nav.replaceChildren();
  for (let k = pisos.length - 1; k >= 0; k--) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = `P${k + 1} ${pisos[k].nombre}`;
    b.addEventListener('click', () => volarA(vistaDePiso(k)));
    nav.append(b);
  }
}

// ============================================================================================ el cuadro
let cuadro = 0, ultimo = 0, cuadrosSeg = 0, segundoEn = 0, lentos = 0, construidoEn = 0;
const medida = { ms: 0, fps: 0, llamadas: 0, triangulos: 0 };
window.oficinaMedida = medida;
// Inspection hook (tests/visual checks read it; the page itself does not).
window.oficina3d = {
  escena, camara, controles, renderer, gente, post, get pisos() { return pisos; }, get datos() { return datos; },
  resumen() {
    return {
      construcciones, pisos: pisos.map((p) => ({ key: p.key, nombre: p.nombre, y: p.y, personas: p.tropa.lista.length, salas: p.plano.salas.filter((s) => s.tipo === 'depto').map((s) => s.nombre) })),
      sesiones: [...(datos?.sesiones || new Map())].map(([id, s]) => ({ id, agente: s.nombre, owner: s.owner, runtime: s.runtime, piso: pisoDe.has(id) ? pisos[pisoDe.get(id)].key : null, estado: s.estado, visible: !!gente.get(id)?.visible, sub: s.sub, coordinador: s.coordinador })),
      rotulos: rotulos.size, enAscensor: [...gente.values()].filter((g) => g.modo === 'ascensor').length, caminando: [...gente.values()].filter((g) => g.modo === 'caminando').length,
      fps: medida.real, calidad: medida.calidad,
    };
  },
  cortarConexion() { conexion?.soltar(); },
  irAPiso(k) { volarA(vistaDePiso(k), 1); }, irACasa() { if (casa) volarA(casa, 1); }, irAPuesto(slug) { volarA(vistaDePuesto(slug), 1); },
};
function bucle(ahora) {
  cuadro = requestAnimationFrame(bucle);
  avanzarVuelo(ahora);
  const moviendo = !!vuelo || ahora - movidaEn < 300;
  const fps = moviendo ? 60 : reducido.matches ? 15 : 30;
  if (ahora - ultimo < 1000 / fps - 1.5) return;
  const dt = ultimo ? Math.min(0.1, (ahora - ultimo) / 1000) : 0;
  ultimo = ahora;
  if (!pisos.length || !datos) return;
  const t0 = performance.now();
  reloj += dt;
  if (!vuelo) controles.update(dt);
  const tg = controles.target;
  const lim = 240, techo = pisos.length * PLANTA + 12;
  if (Math.abs(tg.x) > lim || Math.abs(tg.z) > lim || tg.y < -78 || tg.y > techo) tg.set(clamp(tg.x, -lim, lim), clamp(tg.y, -78, techo), clamp(tg.z, -lim, lim));
  for (const g of gente.values()) if (g.modo === 'ascensor') enAscensor(g, reloj);
  simular(reloj, dt);
  const poses = pisos.map(() => new Map());
  for (const g of gente.values()) {
    if (!g.visible) continue;
    const pulso = pulsos.has(g.slug) ? Math.max(0, 1 - (reloj - pulsos.get(g.slug)) / 1.1) : 0;
    const s = P(g.slug) || {};
    const brillo = s.estado === 'trabajando' ? 1 : s.estado === 'espera' ? 0.75 + Math.sin(reloj * 2.4) * 0.25 : 0.35;
    poses[g.piso]?.set(g.slug, { visible: true, x: g.x, y: g.y, rumbo: g.rumbo, ...g.pose, s: g.s, esfera: brillo + pulso, pulso, esferaSube: s.estado === 'espera' ? 0.12 : 0 });
  }
  const t1 = performance.now();
  pisos.forEach((P2, k) => { P2.tropa.actualizar(reloj, poses[k]); P2.mundo.actualizar(reloj, reducido.matches ? 0 : dt, camara); });
  actualizarBandejas(reloj);
  const t2 = performance.now();
  post.enfocar(camara.position.distanceTo(controles.target));
  post.dibujar(dt);
  const t3 = performance.now();
  ubicarRotulos();
  pintarZoom();
  medida.simular = (medida.simular || 0) * 0.95 + (t1 - t0) * 0.05;
  medida.mover = (medida.mover || 0) * 0.95 + (t2 - t1) * 0.05;
  medida.dibujar = (medida.dibujar || 0) * 0.95 + (t3 - t2) * 0.05;
  medida.rotulos = (medida.rotulos || 0) * 0.95 + (performance.now() - t3) * 0.05;
  medida.ms = medida.ms * 0.95 + (performance.now() - t0) * 0.05;
  cuadrosSeg++;
  if (ahora - segundoEn >= 1000) {
    medida.real = Math.round(cuadrosSeg * 1000 / (ahora - segundoEn));
    cuadrosSeg = 0; segundoEn = ahora;
    const pedidos = moviendo ? 50 : fps;
    const calentando = performance.now() - construidoEn < 8000;
    lentos = !calentando && medida.real < pedidos * 0.72 && !document.hidden ? lentos + 1 : 0;
    if (lentos >= 3 && !calidadPedida && post.nivel !== 'baja') {
      post.armar(post.nivel === 'alta' ? 'media' : 'baja');
      post.ponerNoche(tema === 'noche');
      lentos = 0;
      construidoEn = performance.now();
    } else if (lentos >= 3 && !params.get('dpr') && renderer.getPixelRatio() > 1.01) {
      renderer.setPixelRatio(Math.max(1, renderer.getPixelRatio() * 0.82));
      renderer.setSize(innerWidth, innerHeight, false);
      post.tamano();
      medida.dpr = +renderer.getPixelRatio().toFixed(2);
      lentos = 0;
      construidoEn = performance.now();
    }
    medida.calidad = post.nivel;
  }
  medida.fps = fps;
  medida.llamadas = renderer.info.render.calls;
  medida.triangulos = renderer.info.render.triangles;
}
// Hidden tab: rendering stops; data keeps flowing (and a fresh snapshot is loaded on return, see datos.js).
function arrancar() { cancelAnimationFrame(cuadro); ultimo = 0; cuadro = requestAnimationFrame(bucle); }
function parar() { cancelAnimationFrame(cuadro); }
document.addEventListener('visibilitychange', () => (document.hidden ? parar() : arrancar()));

// ============================================================================================ tocar, pasar el cursor y las teclas
const rayo = new THREE.Raycaster();
function personaBajo(cx, cy) {
  let mejor = null, d = 46;
  for (const [slug, g] of gente) {
    if (!g.visible) continue;
    const c = cabezaMundo(slug);
    for (const h of [-0.12, -0.67]) {
      _v.set(c.x, c.y + h, c.z).project(camara);
      const sx = (_v.x + 1) / 2 * innerWidth, sy = (1 - _v.y) / 2 * innerHeight;
      const dd = Math.hypot(sx - cx, sy - cy);
      if (dd < d) { d = dd; mejor = slug; }
    }
  }
  return mejor;
}
/** Floor under the cursor: the first floor slab the ray crosses, top-down. */
function pisoBajo(cx, cy) {
  const ndc = new THREE.Vector2((cx / innerWidth) * 2 - 1, -(cy / innerHeight) * 2 + 1);
  rayo.setFromCamera(ndc, camara);
  for (let k = pisos.length - 1; k >= 0; k--) {
    const { WT } = pisos[k].plano;
    const caja = new THREE.Box3(new THREE.Vector3(-WT / 2, pisos[k].y - 0.4, -D / 2), new THREE.Vector3(WT / 2, pisos[k].y + 3.3, D / 2));
    if (rayo.ray.intersectsBox(caja)) return k;
  }
  return -1;
}
function queHariaUnToque(cx, cy) {
  if (!pisos.length) return null;
  const v = vistaActual();
  const slug = personaBajo(cx, cy);
  if (slug) { const vp = vistaDePuesto(slug); if (v.d > vp.d * 1.12) return vp; }
  const k = pisoBajo(cx, cy);
  if (k >= 0) { const vf = vistaDePiso(k); if (v.d > vf.d * 1.12) return vf; return null; }
  if (!estaEnCasa()) return casa;
  return null;
}
let abajo = null;
lienzo.addEventListener('pointerdown', (ev) => { abajo = { x: ev.clientX, y: ev.clientY, t: performance.now(), b: ev.button }; if (vuelo) { vuelo = null; controles.enabled = true; } });
lienzo.addEventListener('pointerup', (ev) => {
  if (!abajo) return;
  const quieto = Math.hypot(ev.clientX - abajo.x, ev.clientY - abajo.y) < 6 && performance.now() - abajo.t < 900 && abajo.b === 0;
  abajo = null;
  if (!quieto) return;
  const slug = personaBajo(ev.clientX, ev.clientY);
  if (slug) actividad?.filtrarAgente(P(slug)?.nombre);
  const meta = queHariaUnToque(ev.clientX, ev.clientY);
  if (meta) volarA(meta);
});
let ultimoSobre = 0;
lienzo.addEventListener('pointermove', (ev) => {
  if (ev.buttons) { oficinaEl.classList.add('arrastrando'); return; }
  oficinaEl.classList.remove('arrastrando');
  const ahora = performance.now();
  if (ahora - ultimoSobre < 60 || !pisos.length) return;
  ultimoSobre = ahora;
  const slug = personaBajo(ev.clientX, ev.clientY);
  if (slug !== encima) {
    if (encima) rotulos.get(encima)?.classList.remove('encima');
    encima = slug;
    if (encima) rotulos.get(encima)?.classList.add('encima');
  }
  oficinaEl.classList.toggle('acercable', !!queHariaUnToque(ev.clientX, ev.clientY));
});
lienzo.addEventListener('pointerleave', () => { if (encima) rotulos.get(encima)?.classList.remove('encima'); encima = null; oficinaEl.classList.remove('arrastrando'); });
lienzo.addEventListener('contextmenu', (ev) => ev.preventDefault());

function dolly(f) {
  const v = vistaActual();
  volarA({ ...v, d: clamp(v.d * f, controles.minDistance, controles.maxDistance) }, 420);
}
window.addEventListener('keydown', (ev) => {
  if (ev.target.closest?.('input, select, textarea')) return;
  if (ev.target.closest?.('button') && ev.key === 'Escape') ev.target.blur();
  const v = vistaActual();
  if (ev.key === '+' || ev.key === '=') dolly(1 / 1.4);
  else if (ev.key === '-' || ev.key === '_') dolly(1.4);
  else if (ev.key === 'Escape' || ev.key === '0') { if (casa) volarA(casa); }
  else if (/^[1-9]$/.test(ev.key) && pisos[Number(ev.key) - 1]) volarA(vistaDePiso(Number(ev.key) - 1));
  else if (ev.key === 'ArrowLeft') volarA({ ...v, az: v.az - 0.26 }, 380);
  else if (ev.key === 'ArrowRight') volarA({ ...v, az: v.az + 0.26 }, 380);
  else if (ev.key === 'ArrowUp') volarA({ ...v, polar: clamp(v.polar - 0.12, controles.minPolarAngle, controles.maxPolarAngle) }, 380);
  else if (ev.key === 'ArrowDown') volarA({ ...v, polar: clamp(v.polar + 0.12, controles.minPolarAngle, controles.maxPolarAngle) }, 380);
  else return;
  ev.preventDefault();
});
for (const b of zoomEl.querySelectorAll('button')) {
  b.addEventListener('click', () => {
    if (b.dataset.zoom === 'todo') { if (casa) volarA(casa); return; }
    dolly(b.dataset.zoom === 'mas' ? 1 / 1.6 : 1.6);
  });
}
let zoomAntes = '';
function pintarZoom() {
  const v = vistaActual();
  const lejos = v.d >= controles.maxDistance - 0.5, cerca = v.d <= controles.minDistance + 0.1;
  const f = `${!estaEnCasa()}|${lejos}|${cerca}`;
  if (f === zoomAntes) return;
  zoomAntes = f;
  zoomEl.classList.toggle('acercado', !estaEnCasa());
  document.documentElement.classList.toggle('acercada', !estaEnCasa());
  zoomEl.querySelector('[data-zoom="mas"]').disabled = cerca;
  zoomEl.querySelector('[data-zoom="menos"]').disabled = lejos;
}

// ============================================================================================ arranque
document.documentElement.dataset.tema = tema;
for (const b of document.querySelectorAll('.tema button')) b.setAttribute('aria-checked', String(b.dataset.tema === tema));
oficinaEl.setAttribute('aria-busy', 'true');

function login(mensaje) {
  document.documentElement.classList.add('sin-sesion');  // panel, floors and zoom stay hidden until authenticated
  const f = $('#acceso');
  f.hidden = false;
  if (mensaje) $('#acceso-error').textContent = mensaje;
  f.onsubmit = (ev) => { ev.preventDefault(); token.set($('#acceso-token').value.trim()); f.hidden = true; iniciar(); };
}
let conexion = null;
let refrescoEstado = 0;
function iniciar() {
  if (!token.get()) { login(); return; }
  actividad = actividad || crearActividad({
    raiz: $('.actividad'),
    alAgente: (agente) => { const slug = sesionDeAgente(datos?.sesiones || new Map(), agente); if (slug && PU(slug)) volarA(vistaDePuesto(slug)); },
  });
  conexion = conectar({
    onSnapshot: alSnapshot,
    onEvents: (evs, { replay }) => {
      actividad.agregar(evs, (e) => clasificar(e));
      if (!replay) for (const e of evs) alEvento(e);
      // state changes (tasks, sessions, coordinator) refresh labels/poses promptly; never rebuilds unless layout changed
      if (!replay && evs.some((e) => !e.type.startsWith('message.'))) {
        clearTimeout(refrescoEstado);
        refrescoEstado = setTimeout(() => conexion?.snapshot().catch(() => {}), 400);
      }
    },
    onEstado: (texto, clase) => actividad.estado(texto, clase),
    onAuth: () => { conexion?.cerrar(); token.clear(); login('Token inválido o revocado'); },
  });
}
encuadrar();
arrancar();
iniciar();
