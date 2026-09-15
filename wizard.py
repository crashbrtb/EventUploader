"""Calibration wizard: marking the Journal's controls on a picture of the game.

Modelled on newchestcouter/gui/calibration_wizard.py. The capture comes from
CDP, so a click on it is already a page coordinate - no window position, browser
toolbar or display scaling in between. The magnifier follows the pointer because
the targets are small. A step that marks a button presses it before opening the
next step, so the game arrives at the screen the next step describes.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Tuple

from PIL import Image, ImageTk

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from browser import Browser, changed_fraction, find_template
from calibration import STEPS, STEPS_BY_NAME, Calibration
from titles import Ocr, extract_tournament_name

OK = "#15803d"
WARN = "#b45309"
ERROR = "#b91c1c"
MUTED = "#4b5563"
MAG_SIZE = 150
MAG_ZOOM = 4


class CalibrationWizard(tk.Toplevel):
    def __init__(self, master, browser: Browser, calibration: Calibration, ocr: Ocr,
                 on_done: Optional[Callable[[], None]] = None):
        super().__init__(master)
        self.browser = browser
        self.calibration = calibration
        self.ocr = ocr
        self.on_done = on_done

        self.index = 0
        self.corners: List[Tuple[int, int]] = []
        self.screenshot = None
        self.photo = None
        self.mag_photo = None
        self.display_scale = 1.0
        self.busy = False
        self.results: "queue.Queue[dict]" = queue.Queue()

        self.title("Calibração do Journal")
        self.geometry("1320x820")
        self.minsize(1080, 660)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self.after(200, self.refresh_capture)
        self.after(150, self._drain)

    # -------------------------------------------------------------------- UI
    def _build(self) -> None:
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        left = ttk.Frame(body, width=300)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)
        ttk.Label(left, text="Passos", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(left, text="Clique num passo para refazer só ele.", foreground=MUTED).pack(anchor="w", pady=(0, 6))
        self.steps_list = tk.Listbox(left, activestyle="none", font=("Segoe UI", 10), height=16)
        self.steps_list.pack(fill="both", expand=True)
        self.steps_list.bind("<<ListboxSelect>>", lambda _e: self._on_list())

        cards = ttk.LabelFrame(left, text="Cards por página")
        cards.pack(fill="x", pady=8)
        self.cards_var = tk.IntVar(value=self.calibration.cards_per_page)
        ttk.Spinbox(cards, from_=1, to=20, width=5, textvariable=self.cards_var,
                    command=self._save_cards).pack(side="left", padx=6, pady=4)
        ttk.Label(cards, text="quantos cards cabem numa página", foreground=MUTED).pack(side="left")

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(right)
        header.pack(fill="x")
        titles = ttk.Frame(header)
        titles.pack(side="left", fill="x", expand=True)
        self.lbl_progress = ttk.Label(titles, text="", foreground=MUTED)
        self.lbl_progress.pack(anchor="w")
        self.lbl_title = ttk.Label(titles, text="", font=("Segoe UI", 15, "bold"))
        self.lbl_title.pack(anchor="w")
        self.lbl_instruction = ttk.Label(titles, text="", justify="left", font=("Segoe UI", 10))
        self.lbl_instruction.pack(anchor="w", pady=(4, 4))
        self.magnifier = tk.Canvas(header, width=MAG_SIZE, height=MAG_SIZE, bg="#111", highlightthickness=1)
        self.magnifier.pack(side="right")

        self.canvas = tk.Canvas(right, bg="#111", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, pady=6)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Configure>", lambda _e: self._draw())

        self.lbl_state = ttk.Label(right, text="", wraplength=900, justify="left", font=("Segoe UI", 10, "bold"))
        self.lbl_state.pack(anchor="w", pady=(0, 6))

        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=10, pady=10)
        ttk.Button(footer, text="Fechar", command=self._close).pack(side="left")
        self.chain_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(footer, text="Avançar automaticamente (faz o clique marcado no jogo)",
                        variable=self.chain_var).pack(side="left", padx=12)
        ttk.Button(footer, text="Testar este passo", command=self.test_step).pack(side="right", padx=4)
        ttk.Button(footer, text="Testar leitura dos cards", command=self.test_cards).pack(side="right", padx=4)
        ttk.Button(footer, text="Atualizar captura", command=self.refresh_capture).pack(side="right", padx=4)
        ttk.Button(footer, text="Pular", command=self.skip).pack(side="right", padx=4)
        ttk.Button(footer, text="Refazer", command=self.redo).pack(side="right", padx=4)
        self._show_step()

    def _save_cards(self) -> None:
        try:
            self.calibration.cards_per_page = max(1, int(self.cards_var.get()))
        except (tk.TclError, ValueError):
            return
        self.calibration.save()

    def _set_state(self, text: str, color: str = MUTED) -> None:
        self.lbl_state.configure(text=text, foreground=color)

    # --------------------------------------------------------------- capture
    def refresh_capture(self) -> None:
        self._set_state("Capturando a tela do jogo...")
        self.update_idletasks()
        try:
            image = self.browser.capture(scale=1.0)
        except Exception as exc:
            self._set_state(f"✖ Não foi possível capturar: {exc}", ERROR)
            return
        if image is None or image.size == 0:
            self._set_state("✖ Captura vazia. O jogo está aberto no Chrome com depuração?", ERROR)
            return
        self.screenshot = image
        width, height = self.browser.viewport()
        if width and height:
            self.calibration.set_viewport(width, height)
            self.calibration.use_viewport(width, height)
        self._draw()
        self._set_state(f"Captura de {image.shape[1]}x{image.shape[0]} px. Navegue no jogo e atualize a captura quando a tela do passo estiver visível.")

    def _draw(self) -> None:
        self.canvas.delete("all")
        if self.screenshot is None or cv2 is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 10), max(self.canvas.winfo_height(), 10)
        h, w = self.screenshot.shape[:2]
        self.display_scale = min(cw / w, ch / h, 1.0)
        shown = Image.fromarray(cv2.cvtColor(self.screenshot, cv2.COLOR_BGR2RGB)).resize(
            (max(1, int(w * self.display_scale)), max(1, int(h * self.display_scale))), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(shown)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        step = STEPS[self.index]
        if step.is_rectangle and step.name in self.calibration.regions:
            r = self.calibration.regions[step.name]
            self._outline(r["left"], r["top"], r["left"] + r["width"], r["top"] + r["height"], OK)
        elif step.name in self.calibration.points:
            self._crosshair(*self.calibration.points[step.name], OK)
        if step.name in ("card1_title", "card2_title"):
            # Where the other cards of the page are expected, once both are marked.
            for i in range(self.calibration.cards_per_page):
                region = self.calibration.card_title_region(i)
                if region:
                    self._outline(region[0], region[1], region[0] + region[2], region[1] + region[3], "#60a5fa")
        if len(self.corners) == 1:
            self._crosshair(*self.corners[0], WARN)

    def _to_canvas(self, x: int, y: int) -> Tuple[float, float]:
        return x * self.display_scale, y * self.display_scale

    def _to_page(self, x: int, y: int) -> Tuple[int, int]:
        return int(round(x / self.display_scale)), int(round(y / self.display_scale))

    def _crosshair(self, x: int, y: int, color: str) -> None:
        cx, cy = self._to_canvas(x, y)
        self.canvas.create_line(cx - 9, cy, cx + 9, cy, fill=color, width=2)
        self.canvas.create_line(cx, cy - 9, cx, cy + 9, fill=color, width=2)
        self.canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=color, width=2)

    def _outline(self, x1: int, y1: int, x2: int, y2: int, color: str) -> None:
        ax, ay = self._to_canvas(x1, y1)
        bx, by = self._to_canvas(x2, y2)
        self.canvas.create_rectangle(ax, ay, bx, by, outline=color, width=2)

    def _on_motion(self, event) -> None:
        self.magnifier.delete("all")
        if self.screenshot is None or cv2 is None:
            return
        px, py = self._to_page(event.x, event.y)
        half = MAG_SIZE // (2 * MAG_ZOOM)
        h, w = self.screenshot.shape[:2]
        left, top, right, bottom = max(0, px - half), max(0, py - half), min(w, px + half), min(h, py + half)
        if right - left < 2 or bottom - top < 2:
            return
        crop = cv2.cvtColor(self.screenshot[top:bottom, left:right], cv2.COLOR_BGR2RGB)
        self.mag_photo = ImageTk.PhotoImage(Image.fromarray(crop).resize((MAG_SIZE, MAG_SIZE), Image.NEAREST))
        self.magnifier.create_image(0, 0, anchor="nw", image=self.mag_photo)
        mid = MAG_SIZE // 2
        self.magnifier.create_line(mid, 0, mid, MAG_SIZE, fill="#60a5fa")
        self.magnifier.create_line(0, mid, MAG_SIZE, mid, fill="#60a5fa")
        self.magnifier.create_text(mid, MAG_SIZE - 10, text=f"{px}, {py}", fill="#fff", font=("Consolas", 9))

    # ------------------------------------------------------------------ steps
    def go_to(self, index: int) -> None:
        self.index = max(0, min(index, len(STEPS) - 1))
        self.corners.clear()
        self._show_step()

    def _on_list(self) -> None:
        selection = self.steps_list.curselection()
        if selection and selection[0] != self.index:
            self.go_to(selection[0])

    def _show_step(self) -> None:
        step = STEPS[self.index]
        kind = {"click": "um ponto", "area": "uma área", "region": "uma área com imagem de referência"}[step.type]
        self.lbl_progress.configure(text=f"Passo {self.index + 1} de {len(STEPS)} · {kind}" + (" · opcional" if step.optional else ""))
        self.lbl_title.configure(text=step.title)
        self.lbl_instruction.configure(text=step.instruction)
        done = self.calibration.is_calibrated(step.name)
        target = "no primeiro canto" if step.is_rectangle else "no ponto"
        self._set_state(("Já calibrado: clique de novo para substituir. " if done else "") + f"Clique na captura, {target}.")
        self._update_list()
        self._draw()

    def _update_list(self) -> None:
        self.steps_list.delete(0, "end")
        for position, step in enumerate(STEPS):
            mark = "✔" if self.calibration.is_calibrated(step.name) else ("·" if step.optional else "○")
            self.steps_list.insert("end", f"{'▸' if position == self.index else ' '} {mark} {position + 1:>2}. {step.title}")
            self.steps_list.itemconfig(position, foreground=OK if self.calibration.is_calibrated(step.name) else MUTED)
        self.steps_list.selection_clear(0, "end")
        self.steps_list.selection_set(self.index)

    def _on_click(self, event) -> None:
        if self.busy:
            self._set_state("Aguarde: o jogo ainda está sendo operado pelo passo anterior.", WARN)
            return
        if self.screenshot is None:
            self._set_state("Capture a tela primeiro (Atualizar captura).", WARN)
            return
        step = STEPS[self.index]
        px, py = self._to_page(event.x, event.y)
        h, w = self.screenshot.shape[:2]
        if not (0 <= px < w and 0 <= py < h):
            return
        if not step.is_rectangle:
            self.calibration.set_point(step.name, px, py)
            self.calibration.save()
            self._recorded(f"✔ Ponto salvo em {px}, {py}")
            return
        self.corners.append((px, py))
        if len(self.corners) == 1:
            self._set_state(f"✔ Primeiro canto em {px}, {py}: agora clique no canto oposto.", OK)
            self._draw()
            return
        try:
            warning = self.calibration.set_region(step.name, self.corners[0], self.corners[1], self.screenshot)
            self.calibration.save()
        except ValueError as exc:
            self.corners.clear()
            self._set_state(f"✖ {exc}", ERROR)
            self._draw()
            return
        self.corners.clear()
        if warning:
            self._set_state(warning, WARN)
            self._update_list()
            self._draw()
            return
        region = self.calibration.regions[step.name]
        self._recorded(f"✔ Área de {region['width']}x{region['height']} px salva")

    def _recorded(self, message: str) -> None:
        self._set_state(message, OK)
        self._update_list()
        self._draw()
        if self.chain_var.get():
            self.after(400, self._advance)

    def _advance(self) -> None:
        step = STEPS[self.index]
        last = self.index >= len(STEPS) - 1
        if not step.acts:
            if not last:
                self.refresh_capture()
                self._go_next("Nada para pressionar neste passo.")
            return
        self.busy = True
        self._set_state(f"Pressionando \"{step.title}\" no jogo antes de seguir...")
        threading.Thread(target=self._run_advance, args=(step, last), daemon=True).start()

    def _run_advance(self, step, last: bool) -> None:
        try:
            if step.name == "show_details":
                reference = self.calibration.reference("show_details")
                found, _ = find_template(self.browser, reference, None, self.calibration.image_scale)
                target = (found[0] + found[2] // 2, found[1] + found[3] // 2) if found else self.calibration.action_point(step.name)
            else:
                target = self.calibration.action_point(step.name)
            before = self.browser.capture(scale=1.0)
            self.browser.click(*target, delay=1.3)
            moved = changed_fraction(before, self.browser.capture(scale=1.0))
            if moved < 0.01:
                self._post(f"✖ Cliquei em {target} e a tela NÃO mudou ({moved:.1%}). Refaça o passo ou navegue à mão e atualize a captura.",
                           ERROR, recapture=True)
                return
            self._post(f"✔ Cliquei em {target}; a tela mudou ({moved:.0%}).", OK, recapture=True, advance=not last)
        except Exception as exc:
            self._post(f"✖ Não foi possível executar o passo: {exc}", ERROR)

    def _go_next(self, note: str = "", color: str = MUTED) -> None:
        if self.index >= len(STEPS) - 1:
            return
        self.go_to(self.index + 1)
        if note:
            self._set_state(note + "\n" + self.lbl_state.cget("text"), color)

    def _post(self, message: str, color: str, recapture: bool = False, advance: bool = False) -> None:
        self.results.put({"message": message, "color": color, "recapture": recapture, "advance": advance})

    def _drain(self) -> None:
        while True:
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                break
            self.busy = False
            if result["recapture"]:
                self.refresh_capture()
            if result["advance"]:
                self._go_next(result["message"], result["color"])
            else:
                self._set_state(result["message"], result["color"])
        self.after(150, self._drain)

    # ----------------------------------------------------------------- tests
    def test_step(self) -> None:
        step = STEPS[self.index]
        if not self.calibration.is_calibrated(step.name) or self.busy:
            self._set_state("Este passo ainda não foi calibrado." if not self.busy else "Aguarde.", WARN)
            return
        self.busy = True
        self._set_state("Testando...")
        threading.Thread(target=self._run_test, args=(step,), daemon=True).start()

    def _run_test(self, step) -> None:
        try:
            width, height = self.browser.viewport()
            self.calibration.use_viewport(width, height)
            if step.type == "click":
                point = self.calibration.point(step.name)
                before = self.browser.capture(scale=1.0)
                self.browser.click(*point, delay=1.3)
                moved = changed_fraction(before, self.browser.capture(scale=1.0))
                if moved >= 0.01:
                    self._post(f"✔ Cliquei em {point} e a tela mudou ({moved:.0%}). Abriu o que devia?", OK, recapture=True)
                else:
                    self._post(f"✖ Cliquei em {point} e a tela não mudou ({moved:.1%}): o ponto está errado ou algo cobre o botão.", ERROR, recapture=True)
            elif step.type == "area":
                text = self.ocr.read(self.browser.capture(self.calibration.region(step.name), scale=2.0))
                if not text:
                    self._post(f"✖ Nada lido nesta área. {self.ocr.error}", ERROR)
                else:
                    name = extract_tournament_name(text)
                    extra = f"\nNome do torneio: {name}" if name else ""
                    self._post(f"✔ Lido: {text}{extra}", OK)
            else:
                found, score = find_template(self.browser, self.calibration.reference(step.name), None, self.calibration.image_scale)
                if found:
                    self._post(f"✔ Imagem encontrada em ({found[0]}, {found[1]}) com semelhança {score:.2f}", OK)
                else:
                    self._post(f"✖ Imagem não encontrada agora (melhor semelhança {score:.2f}). Ela está visível?", ERROR)
        except Exception as exc:
            self._post(f"✖ O teste falhou: {exc}", ERROR)

    def test_cards(self) -> None:
        if self.busy:
            return
        if self.calibration.card_pitch() is None:
            self._set_state("Calibre os títulos do 1º e do 2º card primeiro.", WARN)
            return
        self.busy = True
        self._set_state("Lendo os títulos dos cards da página...")

        def run():
            try:
                width, height = self.browser.viewport()
                self.calibration.use_viewport(width, height)
                lines = []
                for i in range(self.calibration.cards_per_page):
                    text = self.ocr.read(self.browser.capture(self.calibration.card_title_region(i), scale=2.0))
                    name = extract_tournament_name(text)
                    lines.append(f"{i + 1}. {text or '(nada lido)'}" + (f"  →  torneio: {name}" if name else ""))
                self._post("\n".join(lines), OK)
            except Exception as exc:
                self._post(f"✖ {exc}", ERROR)
        threading.Thread(target=run, daemon=True).start()

    def redo(self) -> None:
        self.calibration.forget(STEPS[self.index].name)
        self.corners.clear()
        self._show_step()

    def skip(self) -> None:
        self._go_next()

    def _close(self) -> None:
        self._save_cards()
        self.calibration.save()
        if self.on_done:
            self.on_done()
        self.destroy()
