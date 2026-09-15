"""Drives the game tab: screenshots, clicks and keys through CDP.

Adapted from newchestcouter/core/browser.py, where every detail below was
learned against the real game:

- captures are requested pre-compensated for Windows display scaling, so a
  pixel on the image is the CSS pixel a click needs (at 125% a raw capture is
  1.25x larger and every marked point misses);
- a click moves the pointer first, because the game's canvas ignores a press
  that arrives without hover;
- commands are serialised on a lock, because this is one request/response
  conversation on one socket.

This connection only sends commands. Listening to the game's traffic is
capture.CdpCapture's job, on a second connection to the same tab.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np
import requests
import websocket

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

Region = Tuple[int, int, int, int]  # left, top, width, height (CSS pixels)

KEY_CODES = {
    "Escape": (27, "Escape"),
    "Enter": (13, "Enter"),
}


class BrowserError(RuntimeError):
    pass


class Browser:
    def __init__(self, host: str = "127.0.0.1", port: int = 9222):
        self.host = host
        self.port = port
        self.ws: Optional[websocket.WebSocket] = None
        self.tab_id: Optional[str] = None
        self.dpr = 1.0
        self._msg_id = 0
        self._lock = threading.RLock()

    # --------------------------------------------------------------- connect
    def find_game_tab(self) -> Optional[dict]:
        try:
            tabs = requests.get(f"http://{self.host}:{self.port}/json/list", timeout=3).json()
        except Exception:
            return None
        for tab in tabs:
            if tab.get("type") == "page" and "totalbattle.com" in (tab.get("url") or "").lower():
                return tab
        return None

    def connect(self) -> bool:
        tab = self.find_game_tab()
        if not tab or not tab.get("webSocketDebuggerUrl"):
            return False
        self.disconnect()
        try:
            self.ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        except Exception:
            self.ws = None
            return False
        self.tab_id = tab.get("id")
        self.refresh_dpr()
        return True

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def disconnect(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None

    def refresh_dpr(self) -> float:
        value = self.evaluate("window.devicePixelRatio")
        self.dpr = float(value) if isinstance(value, (int, float)) and value > 0 else 1.0
        return self.dpr

    def focus_tab(self) -> None:
        if self.tab_id:
            try:
                requests.get(f"http://{self.host}:{self.port}/json/activate/{self.tab_id}", timeout=2)
            except Exception:
                pass

    # -------------------------------------------------------------- protocol
    def send(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        with self._lock:
            if self.ws is None and not self.connect():
                raise BrowserError("Sem conexão com a aba do jogo (o Chrome está aberto com depuração?).")
            self._msg_id += 1
            msg_id = self._msg_id
            try:
                self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            except Exception as exc:
                self.ws = None
                raise BrowserError(f"Falha ao enviar {method}: {exc}") from exc

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = self.ws.recv()
                except Exception as exc:
                    self.ws = None
                    raise BrowserError(f"Conexão perdida durante {method}: {exc}") from exc
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if message.get("id") != msg_id:
                    continue
                if "error" in message:
                    raise BrowserError(f"{method}: {message['error'].get('message', message['error'])}")
                return message.get("result", {})

            self.ws = None
            raise BrowserError(f"Sem resposta de {method}.")

    def evaluate(self, expression: str, default: Any = None) -> Any:
        try:
            result = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        except BrowserError:
            return default
        if result.get("exceptionDetails"):
            return default
        return result.get("result", {}).get("value", default)

    # ----------------------------------------------------------------- input
    def viewport(self) -> Tuple[int, int]:
        size = self.evaluate("[window.innerWidth, window.innerHeight]") or [0, 0]
        return int(size[0]), int(size[1])

    def move(self, x: float, y: float) -> None:
        self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(x), "y": float(y), "button": "none", "buttons": 0})

    def click(self, x: float, y: float, delay: float = 0.0) -> None:
        self.move(x, y)
        time.sleep(0.03)
        common = {"x": float(x), "y": float(y), "button": "left", "clickCount": 1}
        self.send("Input.dispatchMouseEvent", dict(common, type="mousePressed", buttons=1))
        time.sleep(0.04)
        self.send("Input.dispatchMouseEvent", dict(common, type="mouseReleased", buttons=0))
        if delay > 0:
            time.sleep(delay)

    def press_key(self, name: str, delay: float = 0.1) -> None:
        code, key = KEY_CODES.get(name, (0, name))
        for event_type in ("keyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", {
                "type": event_type, "key": key, "code": key,
                "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code,
            })
        if delay > 0:
            time.sleep(delay)

    # --------------------------------------------------------------- capture
    def capture(self, region: Optional[Region] = None, scale: float = 1.0) -> Optional[np.ndarray]:
        """The page, or one region of it, as a BGR image `scale` times the CSS size."""
        if cv2 is None:
            raise BrowserError("opencv-python não está instalado (rode install.bat).")
        if region:
            left, top, width, height = (int(v) for v in region)
        else:
            left, top = 0, 0
            width, height = self.viewport()
        if width <= 0 or height <= 0:
            return None

        params: Dict[str, Any] = {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": False,
            "clip": {"x": float(left), "y": float(top), "width": float(width), "height": float(height),
                     "scale": float(scale) / (self.dpr or 1.0)},
        }
        try:
            result = self.send("Page.captureScreenshot", params, timeout=30.0)
        except BrowserError:
            return None
        data = result.get("data")
        if not data:
            return None
        image = cv2.imdecode(np.frombuffer(base64.b64decode(data), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return None
        expected = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        if (image.shape[1], image.shape[0]) != expected:
            image = cv2.resize(image, expected, interpolation=cv2.INTER_AREA)
        return image


def changed_fraction(before: Optional[np.ndarray], after: Optional[np.ndarray], tolerance: int = 12) -> float:
    """How much of the screen changed between two captures, from 0 to 1."""
    if before is None or after is None or cv2 is None:
        return 0.0
    if before.shape != after.shape or before.size == 0:
        return 1.0
    difference = cv2.absdiff(before, after)
    if difference.ndim == 3:
        difference = difference.max(axis=2)
    return float((difference > tolerance).mean())


def find_template(browser: Browser, template: np.ndarray, region: Optional[Region] = None,
                  base_scale: float = 1.0, threshold: float = 0.78) -> Tuple[Optional[Region], float]:
    """Where a reference image is on the page, trying a short ladder of scales."""
    if cv2 is None or template is None or template.size == 0:
        return None, 0.0
    screenshot = browser.capture(region, scale=1.0)
    if screenshot is None:
        return None, 0.0
    haystack = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
    gray_template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if template.ndim == 3 else template

    best: Tuple[Optional[Region], float] = (None, 0.0)
    for factor in (1.0, 0.98, 1.02, 0.95, 1.05, 0.9, 1.1):
        scale = base_scale * factor
        if abs(scale - 1.0) > 0.01:
            size = (max(1, int(round(gray_template.shape[1] * scale))), max(1, int(round(gray_template.shape[0] * scale))))
            candidate = cv2.resize(gray_template, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        else:
            candidate = gray_template
        h, w = candidate.shape[:2]
        if h > haystack.shape[0] or w > haystack.shape[1]:
            continue
        result = np.nan_to_num(cv2.matchTemplate(haystack, candidate, cv2.TM_CCOEFF_NORMED), nan=-1.0)
        _, score, _, location = cv2.minMaxLoc(result)
        if score > best[1]:
            offset_x = region[0] if region else 0
            offset_y = region[1] if region else 0
            best = ((offset_x + location[0], offset_y + location[1], w, h), float(score))
        if score >= threshold:
            break
    return (best[0], best[1]) if best[1] >= threshold else (None, best[1])
