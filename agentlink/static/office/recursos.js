// Ownership of GPU resources across rebuilds of the building.
//
// `scene.remove()` does not free anything on the GPU: geometries, materials and textures stay resident until
// `dispose()` is called. Every rebuild (a desk appears or disappears) used to leave the previous building behind
// (~330 geometries and ~110 textures each time). The rule here:
//   - resources created while building belong to that build and are freed when it is replaced;
//   - resources marked with `fijar()` are shared between builds/floors (cached geometries, the floor textures,
//     the contact shadow and halo) and are never freed;
//   - textures created through texturas.js while building are recorded even if no material references them at
//     the moment (e.g. the night version of the city board), so they are freed too.
export const fijos = new WeakSet();
export function fijar(x) { fijos.add(x); return x; }

let actual = null;
/** Start recording the textures created from now on into `conjunto` (null stops). Returns the previous one. */
export function recolectar(conjunto) { const antes = actual; actual = conjunto; return antes; }
export function registrar(t) { if (actual && !fijos.has(t)) actual.add(t); return t; }

/** Free everything owned by the objects under `raices` plus the recorded `texturas`, except shared ones and `vivos`. */
export function liberar(raices, texturas = new Set(), vivos = new Set()) {
  const geos = new Set(), mats = new Set(), texs = new Set(texturas);
  let instancias = 0;
  for (const raiz of raices) {
    raiz.traverse((o) => {
      if (o.geometry) geos.add(o.geometry);
      const m = o.material;
      for (const x of Array.isArray(m) ? m : m ? [m] : []) mats.add(x);
      if (o.isInstancedMesh) { o.dispose(); instancias++; }  // frees the per-instance buffers
    });
  }
  for (const m of mats) {
    for (const v of Object.values(m)) if (v && v.isTexture) texs.add(v);
    if (m.uniforms) for (const u of Object.values(m.uniforms)) if (u?.value?.isTexture) texs.add(u.value);
  }
  const libre = (x) => !fijos.has(x) && !vivos.has(x);
  let g = 0, ma = 0, t = 0;
  for (const x of geos) if (libre(x)) { x.dispose(); g++; }
  for (const x of mats) if (libre(x)) { x.dispose(); ma++; }
  for (const x of texs) if (libre(x)) { x.dispose(); t++; }
  return { geometrias: g, materiales: ma, texturas: t, instancias };
}
