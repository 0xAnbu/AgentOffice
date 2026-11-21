"""Real-browser check of the dashboards (headless Chromium driven over the DevTools protocol).

    python scripts/visual_check.py --url http://127.0.0.1:8080 --token <monitor token> --out docs/evidence/visual

Logs in (token in sessionStorage, as a human would), waits for live data, records JS errors, reads the scene summary
exposed by window.oficina3d.resumen(), takes screenshots (building, one floor, a close-up, /chat) and writes a JSON
report. Exit code 1 if the page threw errors or the scene is not built from backend data.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

THEME = "dia"


class CDP:
    def __init__(self, ws):
        self.ws, self.n, self.pending, self.events = ws, 0, {}, []

    async def pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            else:
                self.events.append(m)

    async def call(self, method, **params):
        self.n += 1
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        r = await asyncio.wait_for(fut, 60)
        if "error" in r:
            raise RuntimeError(r["error"])
        return r.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    async def shot(self, path: Path):
        r = await self.call("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(r["data"]))


async def run(url: str, token: str, out: Path, wait: float) -> int:
    out.mkdir(parents=True, exist_ok=True)
    prof = tempfile.mkdtemp(prefix="cdp-")
    port = 9333
    chrome = subprocess.Popen([shutil.which("chromium") or "chromium", "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
                               "--window-size=1600,1000", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--no-first-run", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(100):
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json").read())
                page = next(t for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            c = CDP(ws)
            c.pump_task = asyncio.create_task(c.pump())  # referenced for the whole session
            for d in ("Page", "Runtime", "Log", "Network"):
                await c.call(f"{d}.enable")
            await c.call("Page.navigate", url=url + "/?tema=" + THEME)
            await asyncio.sleep(1.5)
            await c.js(f"sessionStorage.setItem('agentoffice_token', {json.dumps(token)}); location.reload(); 1")
            await asyncio.sleep(wait)
            report = {
                "url": url,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "scene": await c.js("window.oficina3d && window.oficina3d.resumen()"),
            }
            report["conexion"] = await c.js("document.querySelector('.conexion')?.textContent")
            report["tarjetas"] = await c.js("document.querySelectorAll('.tarjeta').length")
            report["login_visible"] = await c.js("!document.querySelector('#acceso').hidden")
            await c.shot(out / "edificio.png")
            await c.js("window.oficina3d.irAPiso(0)")
            await asyncio.sleep(2.5)
            await c.shot(out / "planta-1.png")
            slug = await c.js("(window.oficina3d.resumen().sesiones.find(s => s.visible) || {}).id")
            if slug:
                await c.js(f"window.oficina3d.irAPuesto({json.dumps(slug)})")
                await asyncio.sleep(2.5)
                await c.shot(out / "agente.png")
            report["scene_after"] = await c.js("window.oficina3d.resumen()")
            await c.call("Page.navigate", url=url + "/chat")
            await asyncio.sleep(4)
            report["chat"] = await c.js(
                "({canvas: !!document.querySelector('canvas'), mensajes: document.querySelectorAll('#feed li').length, title: document.title})"
            )
            await c.shot(out / "chat.png")
            errs = []
            for e in c.events:
                if e.get("method") == "Runtime.exceptionThrown":
                    d = e["params"]["exceptionDetails"]
                    errs.append((d.get("exception") or {}).get("description") or d.get("text"))
                elif e.get("method") == "Runtime.consoleAPICalled" and e["params"]["type"] == "error":
                    errs.append(" ".join(str(a.get("value", a.get("description", ""))) for a in e["params"]["args"]))
                elif e.get("method") == "Log.entryAdded" and e["params"]["entry"]["level"] == "error":
                    errs.append(e["params"]["entry"]["text"])
            report["errors"] = errs
            report["http_errors"] = sorted(
                {
                    f"{e['params']['response']['status']} {e['params']['response']['url']}"
                    for e in c.events
                    if e.get("method") == "Network.responseReceived" and e["params"]["response"]["status"] >= 400
                }
            )
            (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
            print(
                json.dumps(
                    {k: report[k] for k in ("conexion", "tarjetas", "errors", "http_errors", "chat")},
                    ensure_ascii=False,
                    indent=1,
                )
            )
            sc = report["scene"] or {}
            print(
                "floors:",
                [(p["key"], p["personas"]) for p in sc.get("pisos", [])],
                "builds:",
                (report["scene_after"] or {}).get("construcciones"),
            )
            ok = not errs and sc.get("pisos") and not report["login_visible"]
            return 0 if ok else 1
    finally:
        chrome.terminate()
        shutil.rmtree(prof, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", default="docs/evidence/visual")
    ap.add_argument("--wait", type=float, default=12)
    a = ap.parse_args()
    raise SystemExit(asyncio.run(run(a.url.rstrip("/"), a.token, Path(a.out), a.wait)))


if __name__ == "__main__":
    main()
