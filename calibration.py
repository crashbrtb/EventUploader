"""Where the Journal's controls are - learned by clicking a picture of the game.

Same model as newchestcouter/core/calibration.py:

click   a single point to click
area    a rectangle to read text from
region  a rectangle AND a picture of what was inside it, searched for on
        screen at run time (the "Show details" button moves with the text above it)

Positions are stored with the page size they were recorded at and rescaled if
the window changes size. The steps are in the order of one pass through the
Journal, so calibrating means walking through it once.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, NamedTuple, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from settings import config_dir

Point = Tuple[int, int]
Region = Tuple[int, int, int, int]

MIN_REFERENCE_CONTRAST = 10.0
DEFAULT_CARDS_PER_PAGE = 5


class Step(NamedTuple):
    name: str
    type: str          # click | area | region
    title: str
    instruction: str
    optional: bool = False
    acts: bool = False  # pressing it takes the game to the screen of the next step

    @property
    def is_rectangle(self) -> bool:
        return self.type in ("area", "region")

    @property
    def keeps_reference(self) -> bool:
        return self.type == "region"


STEPS: List[Step] = [
    Step("journal_button", "click", "Botão do Journal",
         "Na tela principal do jogo, clique no botão que abre o JOURNAL (Diário).\n"
         "Pule se preferir abrir o Journal à mão antes de mapear.",
         optional=True, acts=True),
    Step("events_tab", "click", "Aba de eventos",
         "No Journal, clique na aba onde aparecem os resultados dos torneios\n"
         "(\"Your Clanmates' results in ...\"). Pule se o Journal já abre nela.",
         optional=True, acts=True),
    Step("card1_title", "area", "Título do 1º card",
         "Marque a área do TÍTULO do PRIMEIRO card da lista (onde aparece\n"
         "\"Your Clanmates' results in <evento>\"). Pegue a linha inteira, com folga.\n"
         "Clique em dois cantos opostos."),
    Step("card2_title", "area", "Título do 2º card",
         "Marque a mesma área, agora no SEGUNDO card. A distância entre os dois\n"
         "é usada para achar os outros cards da página."),
    Step("card_icon", "area", "Ícone do 1º card",
         "Marque o ÍCONE do primeiro card, justo. Ele vira a imagem do torneio\n"
         "no catálogo do site quando o torneio ainda não tem imagem.",
         optional=True),
    Step("page_area", "area", "Número da página",
         "Marque onde aparece o NÚMERO DA PÁGINA, entre as setas.\n"
         "Serve para saber quando a última página foi alcançada.",
         optional=True),
    Step("next_page", "click", "Próxima página",
         "Clique na seta que vai para a PRÓXIMA página do Journal.\n"
         "Ela não é pressionada agora: o resto da calibração continua na página 1."),
    Step("card_open", "click", "Abrir o 1º card",
         "Clique no PRIMEIRO card para abri-lo (de preferência um resultado de torneio).\n"
         "O assistente abre o card antes do próximo passo.",
         acts=True),
    Step("show_details", "region", "Botão Show details",
         "Com o card aberto, marque só o botão \"SHOW DETAILS\", justo.\n"
         "A imagem dele é procurada na tela, porque a posição muda de um torneio para outro.",
         acts=True),
    Step("details_close", "click", "Fechar o ranking",
         "Com o ranking aberto, clique no X (ou voltar) que FECHA o ranking.",
         acts=True),
    Step("card_close", "click", "Fechar o card",
         "Clique no X que fecha o card e volta à lista do Journal.\n"
         "Pule se a tecla Esc já faz isso no jogo.",
         optional=True, acts=True),
]

STEPS_BY_NAME = {step.name: step for step in STEPS}


class Calibration:
    def __init__(self, path: Optional[str] = None):
        base = config_dir()
        self.path = path or str(base / "calibracao_journal.json")
        self.refs_dir = os.path.join(os.path.dirname(self.path), "calibracao_refs")
        self.points: Dict[str, Point] = {}
        self.regions: Dict[str, Dict[str, int]] = {}
        self.viewport: Tuple[int, int] = (0, 0)
        self.step_viewports: Dict[str, Tuple[int, int]] = {}
        self.cards_per_page = DEFAULT_CARDS_PER_PAGE
        self._current_viewport: Tuple[int, int] = (0, 0)
        self.load()

    # ----------------------------------------------------------- persistence
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        self.points = {k: (int(v[0]), int(v[1])) for k, v in (data.get("points") or {}).items()}
        self.regions = data.get("regions") or {}
        self.step_viewports = {k: (int(v[0]), int(v[1])) for k, v in (data.get("step_viewports") or {}).items()}
        stored = data.get("viewport") or {}
        self.viewport = (int(stored.get("width", 0)), int(stored.get("height", 0)))
        self.cards_per_page = int(data.get("cards_per_page") or DEFAULT_CARDS_PER_PAGE)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({
                "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
                "points": {k: list(v) for k, v in self.points.items()},
                "regions": self.regions,
                "step_viewports": {k: list(v) for k, v in self.step_viewports.items()},
                "cards_per_page": self.cards_per_page,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, fh, indent=2, ensure_ascii=False)

    # ----------------------------------------------------------------- state
    def is_calibrated(self, name: str) -> bool:
        step = STEPS_BY_NAME.get(name)
        if step is None:
            return False
        return name in (self.regions if step.is_rectangle else self.points)

    def missing_steps(self, include_optional: bool = False) -> List[str]:
        return [s.name for s in STEPS if (include_optional or not s.optional) and not self.is_calibrated(s.name)]

    @property
    def complete(self) -> bool:
        return not self.missing_steps()

    # --------------------------------------------------------------- scaling
    def use_viewport(self, width: int, height: int) -> None:
        self._current_viewport = (int(width), int(height))

    def _factors(self, name: Optional[str] = None) -> Tuple[float, float]:
        calibrated = self.step_viewports.get(name, self.viewport) if name else self.viewport
        current = self._current_viewport
        if not (calibrated[0] and calibrated[1] and current[0] and current[1]):
            return 1.0, 1.0
        return current[0] / calibrated[0], current[1] / calibrated[1]

    @property
    def image_scale(self) -> float:
        sx, sy = self._factors()
        return min(sx, sy)

    def point(self, name: str) -> Optional[Point]:
        raw = self.points.get(name)
        if not raw:
            return None
        sx, sy = self._factors(name)
        return int(round(raw[0] * sx)), int(round(raw[1] * sy))

    def region(self, name: str) -> Optional[Region]:
        raw = self.regions.get(name)
        if not raw:
            return None
        sx, sy = self._factors(name)
        return (int(round(raw["left"] * sx)), int(round(raw["top"] * sy)),
                max(1, int(round(raw["width"] * sx))), max(1, int(round(raw["height"] * sy))))

    def region_center(self, name: str) -> Optional[Point]:
        region = self.region(name)
        return (region[0] + region[2] // 2, region[1] + region[3] // 2) if region else None

    def action_point(self, name: str) -> Optional[Point]:
        step = STEPS_BY_NAME.get(name)
        if step is None:
            return None
        return self.region_center(name) if step.is_rectangle else self.point(name)

    # ------------------------------------------------------------ the cards
    def card_pitch(self) -> Optional[int]:
        first, second = self.region("card1_title"), self.region("card2_title")
        if not first or not second:
            return None
        pitch = second[1] - first[1]
        return pitch if pitch > 5 else None

    def card_title_region(self, index: int) -> Optional[Region]:
        first, pitch = self.region("card1_title"), self.card_pitch()
        if not first or pitch is None:
            return None
        return first[0], first[1] + pitch * index, first[2], first[3]

    def card_icon_region(self, index: int) -> Optional[Region]:
        icon, pitch = self.region("card_icon"), self.card_pitch()
        if not icon or pitch is None:
            return None
        return icon[0], icon[1] + pitch * index, icon[2], icon[3]

    def card_click_point(self, index: int) -> Optional[Point]:
        """Where to click card `index`: the calibrated click, moved down by the pitch."""
        base, pitch = self.point("card_open"), self.card_pitch()
        if base is None:
            base = self.region_center("card1_title")
        if base is None or pitch is None:
            return None
        return base[0], base[1] + pitch * index

    # ------------------------------------------------------------ recording
    def ref_path(self, name: str) -> str:
        return os.path.join(self.refs_dir, f"{name}.png")

    def reference(self, name: str) -> Optional[np.ndarray]:
        path = self.ref_path(name)
        if cv2 is None or not os.path.exists(path):
            return None
        return cv2.imread(path, cv2.IMREAD_COLOR)

    def set_viewport(self, width: int, height: int) -> None:
        self.viewport = (int(width), int(height))

    def set_point(self, name: str, x: int, y: int) -> None:
        self.points[name] = (int(x), int(y))
        if self.viewport[0]:
            self.step_viewports[name] = self.viewport

    def set_region(self, name: str, corner_a: Point, corner_b: Point, image: Optional[np.ndarray] = None) -> Optional[str]:
        left, right = sorted((int(corner_a[0]), int(corner_b[0])))
        top, bottom = sorted((int(corner_a[1]), int(corner_b[1])))
        if right - left < 6 or bottom - top < 6:
            raise ValueError("área pequena demais: marque dois cantos opostos")

        warning = None
        step = STEPS_BY_NAME.get(name)
        if step is not None and step.keeps_reference:
            if image is None or cv2 is None:
                raise ValueError("não foi possível recortar a imagem de referência")
            crop = image[top:bottom, left:right]
            if crop.size == 0:
                raise ValueError("a área está fora da captura")
            os.makedirs(self.refs_dir, exist_ok=True)
            cv2.imwrite(self.ref_path(name), crop)
            if float(np.std(crop)) < MIN_REFERENCE_CONTRAST:
                warning = ("⚠ esta área quase não tem detalhe: uma imagem lisa é \"encontrada\" em qualquer lugar. "
                           "Marque algo com texto ou borda - o próprio rótulo do botão.")

        self.regions[name] = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        if self.viewport[0]:
            self.step_viewports[name] = self.viewport
        return warning

    def forget(self, name: str) -> None:
        self.points.pop(name, None)
        self.regions.pop(name, None)
        self.step_viewports.pop(name, None)
        if os.path.exists(self.ref_path(name)):
            try:
                os.remove(self.ref_path(name))
            except OSError:
                pass
        self.save()
