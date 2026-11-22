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


def _post(url: str, admin: str, channel: str, body: str) -> int:
    hdrs = {"Authorization": f"Bearer {admin}", "content-type": "application/json"}
    data = json.dumps({"body": body}).encode()
    req = urllib.request.Request(f"{url}/api/channels/{channel}/messages", data=data, headers=hdrs)  # noqa: S310
    return int(json.loads(urllib.request.urlopen(req, timeout=10).read())["id"])  # noqa: S310 (local test server)


async def realtime_checks(c: CDP, url: str, admin: str, channel: str) -> dict:
    """WebSocket loss -> messages posted meanwhile -> reconnection: all appear once; then a burst of 300 events."""
    r: dict = {}
    await c.js("window.oficina3d.cortarConexion()")
    await asyncio.sleep(0.2)
    r["pill_after_drop"] = await c.js("document.querySelector('.conexion').textContent")
    ids = [_post(url, admin, channel, f"durante el corte {i}") for i in range(5)]
    await asyncio.sleep(6)
    r["pill_after_reconnect"] = await c.js("document.querySelector('.conexion').textContent")
    shown = await c.js("[...document.querySelectorAll('.tarjeta[data-msg]')].map(x => +x.dataset.msg)")
    r["missed_messages_recovered"] = all(i in shown for i in ids)
    evs = await c.js("[...document.querySelectorAll('.tarjeta')].map(x => x.dataset.ev)")
    r["duplicate_cards"] = len(evs) - len(set(evs))
    t0 = time.time()
    burst = [_post(url, admin, channel, f"ráfaga {i}") for i in range(300)]
    r["burst_post_seconds"] = round(time.time() - t0, 1)
    await asyncio.sleep(5)
    t1 = time.time()
    await c.js("1")
    r["page_roundtrip_ms_after_burst"] = round((time.time() - t1) * 1000)
    shown = await c.js("[...document.querySelectorAll('.tarjeta[data-msg]')].map(x => +x.dataset.msg)")
    r["latest_burst_message_shown"] = burst[-1] in shown
    r["js_frame_ms"] = await c.js("({simular: window.oficinaMedida.simular, rotulos: window.oficinaMedida.rotulos, "
                                  "mover: window.oficinaMedida.mover, dibujar_software_gl: window.oficinaMedida.dibujar})")  # fmt: skip
    r["builds_after_burst"] = await c.js("window.oficina3d.resumen().construcciones")
    # Activity Center filter by agent: every visible card must involve that agent (author, mention or subject).
    # pick an agent that appears in the cards currently held (the burst above may have evicted older ones: cap 300)
    agent = await c.js(
        "(() => { const cards = [...document.querySelectorAll('.tarjeta')].map(x => x.textContent);"
        " return [...document.querySelector('[name=agente]').options].map(o => o.value).filter(Boolean)"
        "   .find(a => cards.some(t => t.includes(a))) || ''; })()"
    )
    if agent:
        r["filter_agent"] = agent
        res = await c.js(
            "(() => { const s = document.querySelector('[name=agente]'); s.value = " + json.dumps(agent) + ";"
            " s.dispatchEvent(new Event('input')); const cards = [...document.querySelectorAll('.tarjeta')];"
            " const bad = cards.filter(x => !x.textContent.includes(" + json.dumps(agent) + "))"
            "   .map(x => x.dataset.ev + ' ' + x.querySelector('.titulo').textContent + ' | ' + x.querySelector('.ruta').textContent);"
            " s.value = ''; s.dispatchEvent(new Event('input')); return {n: cards.length, bad: bad.slice(0, 5)}; })()"
        )
        r["filter_cards"], r["filter_mismatches"] = res["n"], res["bad"]
        r["filter_ok"] = res["n"] > 0 and not res["bad"]
    r["ok"] = (r["missed_messages_recovered"] and r["duplicate_cards"] == 0 and r["latest_burst_message_shown"]
               and r["pill_after_reconnect"] == "en vivo" and r.get("filter_ok", True))  # fmt: skip
    return r


def _first_int(text: str) -> int:
    import re

    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else 0


async def chat_scroll_checks(c: CDP, url: str, admin: str, channel: str) -> dict:
    """/chat scroll QoS: follow new messages only while the reader is at the bottom; otherwise keep the view and offer
    a floating button with the count of new messages; clicking it jumps down and resumes following."""
    sc = "document.querySelector('section.feed')"
    at_bottom = f"({sc}.scrollHeight - {sc}.scrollTop - {sc}.clientHeight < 48)"
    # wait for the initial page of the feed: loading a view puts the reader at the end, by design
    for _ in range(60):
        if await c.js("document.querySelectorAll('#feed li').length > 0"):
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(1)
    r: dict = {"starts_at_bottom": await c.js(at_bottom)}
    # The office may have other traffic: judge by what the reader sees, not by exact positions.
    top_seen = (
        f"(() => {{ const s = {sc}.getBoundingClientRect(); const li = [...document.querySelectorAll('#feed li')]"
        ".find(x => x.getBoundingClientRect().bottom > s.top + 1); return li ? [li.id, Math.round(li.getBoundingClientRect().top)] : null; })()"
    )
    await c.js(f"{sc}.scrollTop = 0; 1")
    await asyncio.sleep(0.5)
    before = await c.js(top_seen)
    for i in range(2):
        _post(url, admin, channel, f"scroll test {i}")
    await asyncio.sleep(3)
    after = await c.js(top_seen)
    r["view_kept_while_reading_history"] = bool(
        before and after and before[0] == after[0] and abs(before[1] - after[1]) < 4
    )
    r["button_text"] = await c.js("(b => b.hidden ? null : b.textContent)(document.getElementById('to-bottom'))")
    await c.js("document.getElementById('to-bottom').click(); 1")
    await asyncio.sleep(0.5)
    r["button_jumps_to_bottom"] = await c.js(f"{at_bottom} && document.getElementById('to-bottom').hidden")
    _post(url, admin, channel, "scroll test after resume")
    await asyncio.sleep(3)
    r["follows_again"] = await c.js(f"{at_bottom} && document.getElementById('to-bottom').hidden")
    r["last_visible_is_newest"] = await c.js(  # our message is not left below the view (later traffic may follow)
        f"(() => {{ const s = {sc}.getBoundingClientRect(); const li = [...document.querySelectorAll('#feed li')]"
        ".find(x => x.querySelector('.body')?.textContent.includes('after resume'));"
        " if (!li) return false; return li.getBoundingClientRect().top < s.bottom; })()"  # shown or already scrolled past
    )
    r["ok"] = bool(r["view_kept_while_reading_history"] and r["button_text"] and _first_int(r["button_text"]) >= 2
                   and r["button_jumps_to_bottom"] and r["follows_again"] and r["last_visible_is_newest"])  # fmt: skip
    return r


async def run(url: str, token: str, out: Path, wait: float, admin: str | None = None, channel: str = "demo-dev") -> int:
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
            anon = await c.js(
                "(() => { const shown = s => { const e = document.querySelector(s);"
                " return !!e && getComputedStyle(e).display !== 'none'; };"
                " return {login: shown('#acceso'), panel: shown('.actividad'), floors: shown('.plantas'), zoom: shown('.zoom')}; })()"
            )
            await c.shot(out / "sin-sesion.png")
            await c.js(f"sessionStorage.setItem('agentoffice_token', {json.dumps(token)}); location.reload(); 1")
            await asyncio.sleep(wait)
            report = {
                "url": url,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "scene": await c.js("window.oficina3d && window.oficina3d.resumen()"),
            }
            report["anonymous"] = anon
            report["anonymous_ok"] = bool(
                anon and anon["login"] and not (anon["panel"] or anon["floors"] or anon["zoom"])
            )
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
            if admin:
                report["realtime"] = await realtime_checks(c, url, admin, channel)
                print("realtime:", json.dumps(report["realtime"], ensure_ascii=False))
            await c.call("Page.navigate", url=url + "/chat")
            await asyncio.sleep(4)
            if admin:
                report["chat_scroll"] = await chat_scroll_checks(c, url, admin, channel)
                print("chat_scroll:", json.dumps(report["chat_scroll"], ensure_ascii=False))
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
                    {k: report[k] for k in ("anonymous", "conexion", "tarjetas", "errors", "http_errors", "chat")},
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
            ok = (
                not errs
                and sc.get("pisos")
                and not report["login_visible"]
                and report.get("realtime", {"ok": True})["ok"]
                and report["anonymous_ok"]
                and report.get("chat_scroll", {"ok": True})["ok"]
            )
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
    ap.add_argument(
        "--admin-token", help="also run the WebSocket-loss and 300-event burst checks (posts test messages)"
    )
    ap.add_argument("--channel", default="demo-dev")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(run(a.url.rstrip("/"), a.token, Path(a.out), a.wait, a.admin_token, a.channel)))


if __name__ == "__main__":
    main()
