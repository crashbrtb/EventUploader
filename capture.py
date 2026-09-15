"""Grava as respostas que o jogo recebe, lendo a rede do Chrome via CDP.

Duas fontes, porque nao se sabe ainda por onde o ranking chega:

1. `Network.*` do CDP: toda resposta HTTP de um host do Total Battle tem o corpo
   pedido com `Network.getResponseBody`, e todo frame de WebSocket e guardado.
   Funciona com o jogo ja aberto, sem recarregar.
2. Hook na pagina (opcional): envolve `fetch`/`XMLHttpRequest` e devolve os
   bytes por `Runtime.addBinding`. E o metodo que o mercs usa; so e necessario
   se a fonte 1 nao trouxer nada, e costuma exigir F5 no jogo.

O que NUNCA e gravado: o corpo das requisicoes. A sessao do jogo viaja dentro
dele, e esta gravacao vai ser enviada para analise.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import requests
import websocket

import tbcodec

BINDING_NAME = "__tbEventRank"
MAX_BODY_BYTES = 8 * 1024 * 1024

_STATIC_EXT = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|map|woff2?|ttf|otf|mp3|ogg|wav|mp4|webm|"
    r"wasm|data|unityweb|bundle|br|gz|html?)(\?|$)",
    re.I,
)

PAGE_HOOK = """
(function () {
  if (window.__TB_EVENTRANK_HOOK__) return;
  window.__TB_EVENTRANK_HOOK__ = true;
  var CHUNK = 262144, seq = 0;
  function raw(s) { try { window.__BINDING__(s); } catch (e) {} }
  function b64(buf) {
    var u = new Uint8Array(buf), s = '', C = 0x8000;
    for (var i = 0; i < u.length; i += C) s += String.fromCharCode.apply(null, u.subarray(i, i + C));
    return btoa(s);
  }
  function send(url, payload) {
    var id = ++seq, parts = Math.ceil(payload.length / CHUNK) || 1;
    for (var i = 0; i < parts; i++) {
      raw(JSON.stringify({ id: id, part: i, parts: parts, url: url, b64: payload.substr(i * CHUNK, CHUNK) }));
    }
  }
  function isGame(u) { return typeof u === 'string' && /totalbattle\\.com|rubens-/i.test(u); }
  var of = window.fetch;
  if (of) {
    window.fetch = function (input, init) {
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      var p = of.apply(this, arguments);
      if (isGame(url)) {
        p.then(function (res) {
          try { res.clone().arrayBuffer().then(function (ab) { send(url, b64(ab)); }); } catch (e) {}
          return res;
        });
      }
      return p;
    };
  }
  var X = window.XMLHttpRequest, xo = X.prototype.open, xs = X.prototype.send;
  X.prototype.open = function (m, u) { this.__tbU = u; return xo.apply(this, arguments); };
  X.prototype.send = function () {
    var u = this.__tbU || '';
    if (isGame(u)) {
      this.addEventListener('load', function () {
        try {
          var r = this.response;
          if (r instanceof ArrayBuffer) send(u, b64(r));
          else if (typeof r === 'string') send(u, btoa(unescape(encodeURIComponent(r))));
        } catch (e) {}
      });
    }
    return xs.apply(this, arguments);
  };
})();
""".replace("window.__BINDING__", BINDING_NAME)


def is_game_url(url: str) -> bool:
    if not url or "totalbattle" not in url.lower() and "rubens-" not in url.lower():
        return False
    return not _STATIC_EXT.search(urlsplit(url).path or "")


def safe_url(url: str) -> str:
    """URL sem query string: parametros podem carregar token de sessao."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def routes_of(data: bytes) -> list:
    try:
        return [f.route for f in tbcodec.iter_frames(data) if f.route is not None][:50]
    except Exception:
        return []


class CdpCapture:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9222,
        out_dir: Optional[str] = "gravacoes",
        on_status: Optional[Callable[[str], None]] = None,
        on_record: Optional[Callable[[dict], None]] = None,
        use_page_hook: bool = False,
    ):
        self.host = host
        self.port = port
        self.out_dir = out_dir
        self.on_status = on_status
        self.on_record = on_record
        self.use_page_hook = use_page_hook

        self.ws: Optional[websocket.WebSocketApp] = None
        self.lock = threading.Lock()
        self.msg_id = 5000
        self.is_running = False
        self.path: Optional[str] = None
        self.fh = None

        self.responses: Dict[tuple, dict] = {}   # (session, requestId) -> meta
        self.body_calls: Dict[int, dict] = {}     # id do comando -> meta
        self.websockets: Dict[tuple, str] = {}    # (session, requestId) -> url
        self.hook_parts: Dict[Any, dict] = {}
        self.recent: Dict[str, float] = {}
        self.counter = 0
        self.stats = {"RESP": 0, "WS_IN": 0, "HOOK": 0, "REQ": 0, "ignorados": 0}

    # ------------------------------------------------------------------ infra
    def _status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _cmd(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> Optional[int]:
        if not self.ws:
            return None
        with self.lock:
            self.msg_id += 1
            cmd_id = self.msg_id
        msg: Dict[str, Any] = {"id": cmd_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        try:
            self.ws.send(json.dumps(msg))
            return cmd_id
        except Exception:
            return None

    def cdp_available(self) -> bool:
        try:
            return requests.get(f"http://{self.host}:{self.port}/json/version", timeout=1.5).status_code == 200
        except Exception:
            return False

    def find_game_tab(self) -> Optional[dict]:
        try:
            tabs = requests.get(f"http://{self.host}:{self.port}/json/list", timeout=3).json()
        except Exception:
            return None
        for t in tabs:
            if t.get("type") == "page" and "totalbattle.com" in (t.get("url") or "").lower():
                return t
        return None

    def _install(self, session_id: Optional[str] = None) -> None:
        self._cmd("Network.enable", {
            "maxResourceBufferSize": 64 * 1024 * 1024,
            "maxTotalBufferSize": 256 * 1024 * 1024,
        }, session_id)
        if self.use_page_hook:
            self._cmd("Runtime.enable", None, session_id)
            self._cmd("Runtime.addBinding", {"name": BINDING_NAME}, session_id)
            self._cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PAGE_HOOK}, session_id)
            self._cmd("Runtime.evaluate", {"expression": PAGE_HOOK}, session_id)

    # -------------------------------------------------------------- gravacao
    def _duplicate(self, data: bytes) -> bool:
        """O mesmo corpo pode chegar pelo CDP e pelo hook."""
        now = time.time()
        for key, seen in list(self.recent.items()):
            if now - seen > 15:
                self.recent.pop(key, None)
        key = hashlib.md5(data).hexdigest()
        if key in self.recent:
            return True
        self.recent[key] = now
        return False

    def _record(self, kind: str, url: str, data: bytes) -> None:
        if not data or len(data) > MAX_BODY_BYTES:
            self.stats["ignorados"] += 1
            return
        if self._duplicate(data):
            return
        with self.lock:
            self.counter += 1
            idx = self.counter
        self.stats[kind] = self.stats.get(kind, 0) + 1
        record = {
            "idx": idx,
            "kind": kind,
            "ts": time.time(),
            "url": safe_url(url),
            "size": len(data),
            "routes": routes_of(data),
            "b64": base64.b64encode(data).decode("ascii"),
        }
        self._write(record)
        if self.on_record:
            try:
                self.on_record(record)
            except Exception:
                pass

    def _note_request(self, url: str, data: bytes) -> None:
        """So a rota da requisicao e guardada; o corpo (com a sessao) e descartado."""
        self.stats["REQ"] += 1
        routes = routes_of(data)
        if routes:
            self._write({"idx": 0, "kind": "REQ", "ts": time.time(), "url": safe_url(url), "routes": routes})

    def _write(self, record: dict) -> None:
        if self.fh:
            try:
                self.fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.fh.flush()
            except Exception:
                pass

    # -------------------------------------------------------------- mensagens
    def _on_message(self, _ws, message: str) -> None:
        try:
            data = json.loads(message)
        except ValueError:
            return

        method = data.get("method", "")
        params = data.get("params", {})
        sid = data.get("sessionId")

        if method == "Target.attachedToTarget":
            child = params.get("sessionId")
            if child and params.get("targetInfo", {}).get("type") in ("page", "iframe", "worker"):
                self._install(child)
                self._cmd("Runtime.runIfWaitingForDebugger", None, child)
            return

        if method == "Network.requestWillBeSent":
            req = params.get("request", {})
            url = req.get("url", "")
            if is_game_url(url):
                for entry in req.get("postDataEntries") or []:
                    raw = entry.get("bytes")
                    if raw:
                        try:
                            self._note_request(url, base64.b64decode(raw))
                        except ValueError:
                            pass
            return

        if method == "Network.responseReceived":
            url = params.get("response", {}).get("url", "")
            if is_game_url(url):
                self.responses[(sid, params.get("requestId"))] = {"url": url, "sid": sid}
            return

        if method == "Network.loadingFinished":
            meta = self.responses.pop((sid, params.get("requestId")), None)
            if meta:
                cmd = self._cmd("Network.getResponseBody", {"requestId": params.get("requestId")}, sid)
                if cmd is not None:
                    self.body_calls[cmd] = meta
            return

        if method == "Network.loadingFailed":
            self.responses.pop((sid, params.get("requestId")), None)
            return

        if method == "Network.webSocketCreated":
            self.websockets[(sid, params.get("requestId"))] = params.get("url", "")
            return

        if method == "Network.webSocketFrameReceived":
            url = self.websockets.get((sid, params.get("requestId")), "websocket")
            resp = params.get("response", {})
            payload = resp.get("payloadData", "")
            if payload:
                try:
                    body = base64.b64decode(payload) if resp.get("opcode") == 2 else payload.encode("utf-8")
                except ValueError:
                    return
                self._record("WS_IN", url, body)
            return

        if method == "Runtime.bindingCalled" and params.get("name") == BINDING_NAME:
            self._on_hook(params.get("payload", ""))
            return

        if method == "Runtime.executionContextCreated" and self.use_page_hook:
            self._cmd("Runtime.evaluate", {"expression": PAGE_HOOK}, sid)
            return

        if "id" in data and data["id"] in self.body_calls:
            meta = self.body_calls.pop(data["id"])
            result = data.get("result") or {}
            body = result.get("body")
            if body is None:
                return
            try:
                raw = base64.b64decode(body) if result.get("base64Encoded") else body.encode("utf-8")
            except ValueError:
                return
            self._record("RESP", meta["url"], raw)

    def _on_hook(self, payload: str) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        buf = self.hook_parts.setdefault(msg.get("id"), {"chunks": {}, "url": msg.get("url", "")})
        buf["chunks"][msg.get("part", 0)] = msg.get("b64", "")
        if len(buf["chunks"]) < msg.get("parts", 1):
            return
        self.hook_parts.pop(msg.get("id"), None)
        try:
            raw = base64.b64decode("".join(buf["chunks"][i] for i in sorted(buf["chunks"])))
        except ValueError:
            return
        self._record("HOOK", buf["url"], raw)

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> Optional[str]:
        if self.is_running:
            return self.path
        if not self.cdp_available():
            self._status(f"Chrome nao responde na porta {self.port}. Use 'Abrir Chrome' ou abra com --remote-debugging-port={self.port}.")
            return None
        tab = self.find_game_tab()
        if not tab or not tab.get("webSocketDebuggerUrl"):
            self._status("Nenhuma aba do Total Battle aberta nesse Chrome.")
            return None

        # Without an output folder nothing is written to disk: the uploader only
        # needs the records passed to on_record while it runs.
        if self.out_dir:
            os.makedirs(self.out_dir, exist_ok=True)
            self.path = os.path.join(self.out_dir, f"gravacao_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
            self.fh = open(self.path, "w", encoding="utf-8")
        else:
            self.path = None
            self.fh = None
        self.stats = {"RESP": 0, "WS_IN": 0, "HOOK": 0, "REQ": 0, "ignorados": 0}
        self.responses.clear()
        self.body_calls.clear()
        self.hook_parts.clear()
        self.recent.clear()
        self.counter = 0

        def on_open(_ws):
            self._cmd("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            self._install()
            self._status("Gravando. Abra o Journal e o 'Show details' do evento agora.")

        def on_close(*_):
            if self.is_running:
                self._status("Conexao com o Chrome caiu. Pare e grave de novo.")

        self.ws = websocket.WebSocketApp(
            tab["webSocketDebuggerUrl"],
            on_open=on_open,
            on_message=self._on_message,
            on_error=lambda *_: None,
            on_close=on_close,
        )
        self.is_running = True
        threading.Thread(target=lambda: self.ws.run_forever(suppress_origin=True), daemon=True).start()
        return self.path or "memory"

    def stop(self) -> Optional[str]:
        if not self.is_running:
            return None
        self.is_running = False
        # Da tempo para os corpos pedidos por ultimo chegarem.
        deadline = time.time() + 2.0
        while self.body_calls and time.time() < deadline:
            time.sleep(0.1)
        try:
            if self.ws:
                self._cmd("Network.disable")
                self.ws.close()
        except Exception:
            pass
        self.ws = None
        if self.fh:
            self.fh.close()
            self.fh = None
        return self.path


def load_recording(path: str) -> list:
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("b64"):
                records.append(rec)
    return records
