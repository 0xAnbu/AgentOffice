"""Rendering cost and GPU-resource check of the 3D office in headless Chromium.

    python scripts/render_check.py [--repo <checkout to serve>] [--out docs/evidence/render-<ts>.json] [--no-cpu]

Starts its own server (temporary SQLite database) from `--repo` (default: this checkout), so the same procedure
can be run against another commit for a before/after comparison. Scenarios:

  cpu      CPU% and memory (PSS) of every Chromium process, 0 agents and 12 working agents, still camera,
           adaptive loop vs `?ahorro=0` (the previous fixed 30 fps) at the same quality, in a tiny window (see
           --window); plus memory with default settings on an emulated 2x screen. Linux only (/proc).
  rebuild  sessions appear one by one (each adds a desk and rebuilds the building); afterwards the page must hold
           no more geometries/textures than a fresh page opened on the same final layout.
  rates    frame-rate tiers: idle, camera flight, after an event, rest after 60 s without anything.

WebGL runs on SwiftShader (software) here: absolute CPU figures are not those of a real GPU; ratios are.
Exit code 1 if a resource leak, a wrong frame-rate tier or a JS error is found.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from visual_check import CDP  # noqa: E402

from agentlink.local import Api  # noqa: E402

TICK = os.sysconf("SC_CLK_TCK")
SPECS = ["coordination", "qa", "devops", "backend", "frontend"]
METRICS = """(() => { const m = window.oficinaMedida || {}, o = window.oficina3d, i = o && o.renderer.info;
  return { fps_real: m.real, fps_objetivo: m.fps, calidad: m.calidad, dpr: o && o.renderer.getPixelRatio(),
    llamadas: m.llamadas, triangulos: m.triangulos, geometrias: i && i.memory.geometries, texturas: i && i.memory.textures,
    construcciones: o && o.resumen().construcciones, personas: o && o.resumen().pisos.reduce((a, p) => a + p.personas, 0),
    liberado: m.liberado || null }; })()"""


# ---------------------------------------------------------------------------------------------- processes
def _procs(tag: str) -> dict[int, str]:
    out = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if tag in cmd:
            kind = next(
                (k for k in ("gpu-process", "renderer", "utility", "zygote") if f"--type={k}" in cmd), "browser"
            )
            out[int(pid)] = kind
    return out


def _ticks(pid: int) -> int:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])
    except OSError:
        return 0


def _pss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def sample(tag: str, seconds: float) -> dict:
    ps = _procs(tag)
    t0 = {p: _ticks(p) for p in ps}
    time.sleep(seconds)
    res: dict[str, dict[str, float]] = {}
    for p, kind in ps.items():
        r = res.setdefault(kind, {"cpu_pct": 0.0, "pss_mb": 0.0})
        r["cpu_pct"] += (_ticks(p) - t0[p]) / TICK / seconds * 100
        r["pss_mb"] += _pss_mb(p)
    res["TOTAL"] = {k: sum(v[k] for v in res.values()) for k in ("cpu_pct", "pss_mb")}
    return {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in res.items()}


# ---------------------------------------------------------------------------------------------- browser
class Browser:
    def __init__(self, size: str = "1600,1000", scale: float = 1) -> None:
        self.prof = tempfile.mkdtemp(prefix="render-check-")
        self.port = 9500 + os.getpid() % 400
        self.proc = subprocess.Popen(
            [shutil.which("chromium") or "chromium", "--headless=new", f"--remote-debugging-port={self.port}",
             f"--user-data-dir={self.prof}", f"--window-size={size}", f"--force-device-scale-factor={scale}", "--use-angle=swiftshader",
             "--enable-unsafe-swiftshader", "--no-first-run", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip

    async def open(self, url: str, token: str, query: str, settle: float) -> CDP:
        for _ in range(100):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json").read())
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.2)
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000)
        c = CDP(self.ws)
        c.t = asyncio.create_task(c.pump())
        for d in ("Page", "Runtime"):
            await c.call(f"{d}.enable")
        await c.call("Page.navigate", url=url + "/")
        await asyncio.sleep(1)
        await c.js(
            f"sessionStorage.setItem('agentoffice_token', {json.dumps(token)}); location.href = '/' + {json.dumps(query)}; 1"
        )
        await asyncio.sleep(settle)
        return c

    def errors(self, c: CDP) -> list[str]:
        return [
            e["params"]["exceptionDetails"].get("exception", {}).get("description", "")[:200]
            for e in c.events
            if e.get("method") == "Runtime.exceptionThrown"
        ]

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.prof, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- office
class Office:
    def __init__(self, repo: Path, work: Path) -> None:
        self.url = f"http://127.0.0.1:{8600 + os.getpid() % 300}"
        self.admin = "al_" + secrets.token_urlsafe(32)
        env = {**os.environ, "AGENTLINK_ADMIN_TOKEN": self.admin, "AGENTLINK_DB_PATH": str(work / "render.db"),
               "AGENTLINK_MODE": "testing", "AGENTLINK_WRITE_RATE": "0"}  # fmt: skip
        py = repo / ".venv/bin/python"
        self.srv = subprocess.Popen(
            [str(py if py.exists() else sys.executable), "-m", "agentlink.serve", "--port", self.url.rsplit(":", 1)[1],
             "--env-file", str(work / "none.env")],
            env=env, cwd=repo, stdout=(work / "server.log").open("w"), stderr=subprocess.STDOUT,
        )  # fmt: skip
        self.api = Api(self.url, self.admin)
        for _ in range(100):
            try:
                self.api.call("GET", "/api/health")
                break
            except Exception:
                time.sleep(0.2)
        self.api.call("POST", "/api/projects", {"name": "demo", "repo": "acme/demo"})
        self.api.call("POST", "/api/channels", {"name": "demo-dev", "project": "demo"})
        self.tokens = {}
        for o in range(5):
            for s in SPECS:
                n = f"p{o}-{s[:4]}"
                body = {"name": n, "owner": f"dev0{o}", "specialization": s, "projects": ["demo"]}
                self.tokens[n] = self.api.call("POST", "/api/admin/agents", body)["token"]
        self.monitor = self.api.call("POST", "/api/admin/agents", {"name": "wall", "role": "monitor"})["token"]
        self.live: dict[str, tuple[Api, str]] = {}
        self.stop = threading.Event()
        threading.Thread(target=self._beat, daemon=True).start()

    def _beat(self) -> None:
        while not self.stop.wait(10):
            for api, sid in list(self.live.values()):
                try:
                    api.call("POST", f"/api/sessions/{sid}/heartbeat", {"state": "executing", "progress": True})
                except Exception:  # noqa: S112
                    continue

    def start(self, name: str) -> None:
        api = Api(self.url, self.tokens[name])
        sid = api.call("POST", "/api/sessions", {"project": "demo", "runtime": "synthetic"})["id"]
        api.call("POST", f"/api/sessions/{sid}/heartbeat", {"state": "executing", "progress": True})
        self.live[name] = (api, sid)

    def end(self, name: str) -> None:
        api, sid = self.live.pop(name)
        api.call("POST", f"/api/sessions/{sid}/end", {})

    def close(self) -> None:
        self.stop.set()
        self.srv.terminate()


async def cpu_case(office: Office, query: str, seconds: float, size: str, scale: float = 1) -> dict:
    b = Browser(size, scale)
    try:
        c = await b.open(office.url, office.monitor, query, settle=15)
        n0 = await c.js("window.oficinaMedida.cuadros || 0")
        proc = await asyncio.to_thread(sample, b.prof, seconds)
        drawn = (await c.js("window.oficinaMedida.cuadros || 0")) - n0
        page = await c.js(METRICS)
        page["cuadros_dibujados_por_s"] = (
            round(drawn / seconds, 1) if drawn else None
        )  # None: counter absent (old code)
        return {
            "query": query,
            "ventana": size,
            "escala": scale,
            "procesos": proc,
            "pagina": page,
            "errores": b.errors(c),
        }
    finally:
        await b.close()


async def rebuild_case(office: Office, cycles: int) -> dict:
    b = Browser()
    try:
        c = await b.open(office.url, office.monitor, "?calidad=baja&dpr=1", settle=12)
        rows = [await c.js(METRICS)]
        for i in range(cycles):
            name = f"p{i % 5}-fron"
            office.start(name)
            await asyncio.sleep(5)
            office.end(name)
            await asyncio.sleep(5)
            rows.append(await c.js(METRICS))
        # day/night switches must not accumulate resources either (the first night builds its sky/environment once)
        tema = "(t => { document.querySelector(`.tema button[data-tema='${t}']`).click(); return 1; })"
        for t in ("noche", "dia"):
            await c.js(f"{tema}('{t}')")
            await asyncio.sleep(1.5)
        antes = await c.js(METRICS)
        for t in ("noche", "dia") * 3:
            await c.js(f"{tema}('{t}')")
            await asyncio.sleep(1.5)
        despues = await c.js(METRICS)
        temas = {
            "geometrias": despues["geometrias"] - antes["geometrias"],
            "texturas": despues["texturas"] - antes["texturas"],
        }
        errors = b.errors(c)
    finally:
        await b.close()
    # Reference: a fresh page on the same final layout. Whatever the rebuilt page holds beyond it is leaked.
    b = Browser()
    try:
        c = await b.open(office.url, office.monitor, "?calidad=baja&dpr=1", settle=12)
        fresh = await c.js(METRICS)
        errors += b.errors(c)
    finally:
        await b.close()
    first, last = rows[0], rows[-1]
    return {
        "filas": rows,
        "pagina_nueva_mismo_plano": fresh,
        "errores": errors,
        "construcciones": last["construcciones"] - first["construcciones"],
        "geometrias_de_mas": last["geometrias"] - fresh["geometrias"],
        "texturas_de_mas": last["texturas"] - fresh["texturas"],
        "crecimiento_con_6_cambios_de_tema": temas,
    }


async def rates_case(office: Office, channel: str) -> dict:
    b = Browser()
    try:
        c = await b.open(office.url, office.monitor, "?calidad=baja&dpr=1", settle=12)
        r: dict = {"reposo_inicial": await c.js("window.oficinaMedida.fps")}
        # a real drag on the canvas (rotates the camera through OrbitControls)
        mouse = "Input.dispatchMouseEvent"
        await c.call(mouse, type="mousePressed", x=800, y=500, button="left", buttons=1, clickCount=1)
        for i in range(12):
            await c.call(mouse, type="mouseMoved", x=800 + i * 15, y=500, button="left", buttons=1)
            await asyncio.sleep(0.03)
        r["camara_arrastrada"] = await c.js("window.oficinaMedida.fps")
        await c.call(mouse, type="mouseReleased", x=980, y=500, button="left", buttons=0, clickCount=1)
        # OrbitControls damping advances one step per drawn frame: with software WebGL (~1 frame/s) the camera keeps
        # gliding (and the loop stays at full rate) for ~100 s; on a GPU it settles in ~1 s. Stop the residue.
        await c.js(
            "(k => { k.enableDamping = false; k.update(); k.enableDamping = true; return 1; })(window.oficina3d.controles)"
        )
        await asyncio.sleep(5)
        office.api.call("POST", f"/api/channels/{channel}/messages", {"body": "render check event"})
        await asyncio.sleep(0.6)
        r["tras_un_evento"] = await c.js("window.oficinaMedida.fps")
        await asyncio.sleep(4)
        r["ambiente"] = await c.js("window.oficinaMedida.fps")
        await asyncio.sleep(62)
        r["sin_nada_60s"] = await c.js("window.oficinaMedida.fps")
        r["errores"] = b.errors(c)
        return r
    finally:
        await b.close()


async def main_async(args: argparse.Namespace) -> int:
    work = Path(tempfile.mkdtemp(prefix="render-office-"))
    office = Office(Path(args.repo).resolve(), work)
    report: dict = {"repo_commit": subprocess.run(["git", "-C", args.repo, "rev-parse", "--short", "HEAD"],
                                                  capture_output=True, text=True).stdout.strip(),
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "webgl": "SwiftShader (software)"}  # fmt: skip
    try:
        if args.cpu:
            cases = []
            loop = ("?calidad=baja&dpr=1&ahorro=0", "?calidad=baja&dpr=1")  # same quality: only the loop differs
            for q in loop:
                cases.append({"agentes": 0, **await cpu_case(office, q, args.seconds, args.window)})
            for n in list(office.tokens)[:12]:
                office.start(n)
            for q in loop:
                cases.append({"agentes": 12, **await cpu_case(office, q, args.seconds, args.window)})
            # memory with default settings on an emulated 2x (Retina) screen: pixel ratio, textures, shadow map
            cases.append({"agentes": 12, **await cpu_case(office, "", args.seconds, "1600,1000", 2)})
            for n in list(office.live):
                office.end(n)
            report["cpu"] = cases
            for x in cases:
                print(f"cpu agentes={x['agentes']} x{x['escala']} {x['query'] or 'default':34} total={x['procesos']['TOTAL']} "
                      f"fps={x['pagina']['fps_objetivo']} dibujados/s={x['pagina']['cuadros_dibujados_por_s']}", flush=True)  # fmt: skip
        report["rebuild"] = await rebuild_case(office, args.cycles)
        print("rebuild:", json.dumps({k: v for k, v in report["rebuild"].items() if k != "filas"}), flush=True)
        if args.rates:
            report["rates"] = await rates_case(office, "demo-dev")
            print("rates:", json.dumps(report["rates"]), flush=True)
    finally:
        office.close()
        shutil.rmtree(work, ignore_errors=True)
    rb = report["rebuild"]
    problems = []
    if rb["construcciones"] < args.cycles:
        problems.append(f"expected >= {args.cycles} rebuilds, saw {rb['construcciones']}")
    if rb["geometrias_de_mas"] > 3 or rb["texturas_de_mas"] > 3:
        problems.append(
            f"rebuilt page holds {rb['geometrias_de_mas']} geometries and {rb['texturas_de_mas']} textures more than a fresh page"
        )
    if any(rb["crecimiento_con_6_cambios_de_tema"].values()):
        problems.append(f"day/night switches accumulate resources: {rb['crecimiento_con_6_cambios_de_tema']}")
    rt = report.get("rates")
    if rt:
        want = {"camara_arrastrada": 60, "tras_un_evento": 30, "ambiente": 8, "sin_nada_60s": 2}
        problems += [f"rate {k}: {rt[k]} != {v}" for k, v in want.items() if rt[k] != v]
    errs = rb["errores"] + (rt or {}).get("errores", []) + [e for x in report.get("cpu", []) for e in x["errores"]]
    problems += [f"JS error: {e}" for e in errs]
    report["problemas"] = problems
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    print("OK" if not problems else "PROBLEMS:\n  " + "\n  ".join(problems))
    return 1 if problems else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(HERE.parent), help="checkout whose server and UI are measured")
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--seconds", type=float, default=20, help="CPU sampling window per case")
    ap.add_argument("--cycles", type=int, default=5, help="session appear/end cycles for the rebuild check")
    ap.add_argument(
        "--window",
        default="200,120",
        help="browser size for the loop CPU cases: tiny so software WebGL can reach the requested rate "
        "(it draws ~1 frame/s at 1600x1000 and ~4 at 320x200, below any target, which would hide the difference)",
    )
    ap.add_argument("--no-cpu", dest="cpu", action="store_false", help="skip the CPU/memory cases")
    ap.add_argument("--no-rates", dest="rates", action="store_false", help="skip the frame-rate tiers (needs ~90 s)")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
