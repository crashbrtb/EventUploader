"""Walks the Journal and learns which name belongs to which tournament type.

The game never sends a tournament's name, only its type. The name is on screen,
in the card title "Your Clanmates' results in <name>". Pairing the two by the
order of the cards would be fragile (the Journal mixes other messages in), so
the pairing is causal instead:

1. read the card's title (OCR);
2. open the card and press "Show details";
3. the game answers with the ranking message, which carries the result id and
   the tournament type - that answer is what the name belongs to;
4. close back to the list and go on.

A name already mapped is not opened again, so after the first pass a run only
opens tournaments it has never seen. The game may keep a ranking it already
fetched and not ask again; reloading the game page (F5) before mapping avoids it.
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

import journal
from browser import Browser, find_template
from calibration import Calibration
from titles import Ocr, extract_tournament_name, parse_page_number

STATUS_MAPPED = "mapeado"
STATUS_NO_ANSWER = "sem resposta do jogo"
STATUS_SKIPPED = "já conhecido"


@dataclass
class MappedCard:
    name: str
    page: int
    card: int
    status: str = STATUS_NO_ANSWER
    result_uid: Optional[str] = None
    tournament_key: Optional[str] = None
    ended_at: Optional[int] = None
    icon_png: Optional[bytes] = None

    @property
    def game_type(self) -> Optional[int]:
        if not self.tournament_key:
            return None
        try:
            return int(self.tournament_key.split(":")[0])
        except ValueError:
            return None


@dataclass
class MapperOptions:
    max_pages: int = 30
    open_journal: bool = True
    reopen_known: bool = False
    answer_timeout: float = 8.0
    step_delay: float = 1.2


def normalize(name: str) -> str:
    return " ".join(name.lower().split())


class MapperStopped(Exception):
    pass


class JournalMapper:
    def __init__(
        self,
        browser: Browser,
        calibration: Calibration,
        ocr: Ocr,
        collector: journal.JournalCollector,
        options: Optional[MapperOptions] = None,
        known_names: Optional[Set[str]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_card: Optional[Callable[[MappedCard], None]] = None,
    ):
        self.browser = browser
        self.calibration = calibration
        self.ocr = ocr
        self.collector = collector
        self.options = options or MapperOptions()
        self.known = {normalize(n) for n in (known_names or set())}
        self.on_log = on_log
        self.on_card = on_card
        self.cards: List[MappedCard] = []
        self._stop = threading.Event()

    # --------------------------------------------------------------- control
    def stop(self) -> None:
        self._stop.set()

    def _check(self) -> None:
        if self._stop.is_set():
            raise MapperStopped()

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def _sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise MapperStopped()

    # ------------------------------------------------------------------- run
    def run(self) -> List[MappedCard]:
        missing = self.calibration.missing_steps()
        if missing:
            raise RuntimeError("Calibração incompleta: " + ", ".join(missing))
        if not self.ocr.available():
            raise RuntimeError(self.ocr.error or "OCR indisponível")

        width, height = self.browser.viewport()
        self.calibration.use_viewport(width, height)

        try:
            self._open_journal()
            previous_signature = None
            for page in range(1, self.options.max_pages + 1):
                self._check()
                titles = self._read_titles()
                signature = "|".join(titles)
                if not any(titles):
                    self._log(f"Página {page}: nenhum título lido. Fim.")
                    break
                if signature == previous_signature:
                    self._log(f"Página {page} igual à anterior: última página alcançada.")
                    break
                previous_signature = signature
                self._log(f"Página {page}: " + " / ".join(t for t in titles if t)[:300])

                for index, text in enumerate(titles):
                    self._check()
                    name = extract_tournament_name(text)
                    if not name:
                        continue
                    self._handle_card(page, index, name)

                if not self._next_page():
                    self._log("A página não mudou ao avançar: fim do Journal.")
                    break
        except MapperStopped:
            self._log("Mapeamento interrompido.")
        return self.cards

    # ------------------------------------------------------------- journal
    def _open_journal(self) -> None:
        if not self.options.open_journal:
            return
        for name in ("journal_button", "events_tab"):
            point = self.calibration.point(name)
            if point is not None:
                self._log(f"Clicando em {name} {point}")
                self.browser.click(*point)
                self._sleep(self.options.step_delay)

    def _read_titles(self) -> List[str]:
        titles = []
        for index in range(self.calibration.cards_per_page):
            region = self.calibration.card_title_region(index)
            image = self.browser.capture(region, scale=2.0) if region else None
            titles.append(self.ocr.read(image))
        return titles

    def _next_page(self) -> bool:
        point = self.calibration.point("next_page")
        if point is None:
            return False
        page_region = self.calibration.region("page_area")
        before_page = parse_page_number(self.ocr.read(self.browser.capture(page_region, scale=2.0), True)) if page_region else None
        before = self.browser.capture(self.calibration.card_title_region(0), scale=1.0)

        self.browser.click(*point)
        self._sleep(self.options.step_delay)

        if page_region:
            after_page = parse_page_number(self.ocr.read(self.browser.capture(page_region, scale=2.0), True))
            if before_page is not None and after_page is not None:
                return after_page != before_page
        after = self.browser.capture(self.calibration.card_title_region(0), scale=1.0)
        if before is None or after is None:
            return True
        return not np.array_equal(before, after)

    # ---------------------------------------------------------------- cards
    def _handle_card(self, page: int, index: int, name: str) -> None:
        card = MappedCard(name=name, page=page, card=index + 1)
        key = normalize(name)
        if key in self.known and not self.options.reopen_known:
            card.status = STATUS_SKIPPED
            self._emit(card)
            return

        card.icon_png = self._icon(index)
        before = set(self.collector.rankings.keys())

        point = self.calibration.card_click_point(index)
        self._log(f"Abrindo \"{name}\" (página {page}, card {index + 1})")
        self.browser.click(*point)
        self._sleep(self.options.step_delay)

        self._press_show_details()
        uid = self._wait_for_answer(before)
        if uid is not None:
            key_type, _, _ = self.collector.rankings[uid]
            entry = self.collector.entries.get(uid)
            card.result_uid = uid
            card.tournament_key = key_type or (entry.tournament_key if entry else None)
            card.ended_at = entry.ended_at if entry else None
            card.status = STATUS_MAPPED
            self.known.add(key)
            self._log(f"  \"{name}\" = tipo {card.tournament_key}")
        else:
            self._log(f"  O jogo não enviou o ranking de \"{name}\" (talvez já estivesse em cache: recarregue o jogo).")

        self._back_to_list(index, name)
        self._emit(card)

    def _emit(self, card: MappedCard) -> None:
        self.cards.append(card)
        if self.on_card:
            self.on_card(card)

    def _icon(self, index: int) -> Optional[bytes]:
        region = self.calibration.card_icon_region(index)
        if region is None or cv2 is None:
            return None
        image = self.browser.capture(region, scale=1.0)
        if image is None:
            return None
        ok, png = cv2.imencode(".png", image)
        return png.tobytes() if ok else None

    def _press_show_details(self) -> None:
        reference = self.calibration.reference("show_details")
        found, score = find_template(self.browser, reference, None, self.calibration.image_scale) if reference is not None else (None, 0.0)
        if found is not None:
            x, y = found[0] + found[2] // 2, found[1] + found[3] // 2
        else:
            center = self.calibration.region_center("show_details")
            if center is None:
                return
            x, y = center
            self._log(f"  Botão Show details não reconhecido (semelhança {score:.2f}); usando a posição calibrada.")
        self.browser.click(x, y)

    def _wait_for_answer(self, before: Set[str]) -> Optional[str]:
        deadline = time.time() + self.options.answer_timeout
        while time.time() < deadline:
            self._check()
            new = set(self.collector.rankings.keys()) - before
            if new:
                # Normally one; if several arrived, the newest is the one just asked for.
                return max(new, key=lambda uid: self.collector.rankings[uid][2])
            self._sleep(0.25)
        return None

    def _back_to_list(self, index: int, name: str) -> None:
        for step in ("details_close", "card_close"):
            point = self.calibration.point(step)
            if point is not None:
                self.browser.click(*point)
            else:
                self.browser.press_key("Escape")
            self._sleep(self.options.step_delay * 0.7)

        # Back on the list, the same card shows the same title again.
        for _attempt in range(2):
            text = self.ocr.read(self.browser.capture(self.calibration.card_title_region(index), scale=2.0))
            if normalize(extract_tournament_name(text) or "") == normalize(name):
                return
            self.browser.press_key("Escape")
            self._sleep(self.options.step_delay)
        raise RuntimeError(
            f"Depois de fechar \"{name}\" o Journal não voltou para a lista. "
            "Confira os passos 'Fechar o ranking' e 'Fechar o card' na calibração."
        )


def catalogue_entries(cards: List[MappedCard]) -> List[dict]:
    """One entry per tournament type, ready for POST /api/v1/tournament-catalog."""
    by_type: Dict[int, MappedCard] = {}
    for card in cards:
        if card.status != STATUS_MAPPED or card.game_type is None:
            continue
        current = by_type.get(card.game_type)
        if current is None or (current.icon_png is None and card.icon_png is not None):
            by_type[card.game_type] = card

    entries = []
    for card in by_type.values():
        entry = {"tournament_key": card.tournament_key, "name": card.name}
        if card.ended_at:
            entry["ended_at"] = journal.Tournament(card.result_uid or "", ended_at=card.ended_at).ended_at_iso
        if card.icon_png:
            entry["image"] = base64.b64encode(card.icon_png).decode("ascii")
        entries.append(entry)
    return entries


def conflicts(cards: List[MappedCard]) -> List[str]:
    """Different names read for one type, or one name for different types."""
    names_by_type: Dict[int, Set[str]] = {}
    types_by_name: Dict[str, Set[int]] = {}
    for card in cards:
        if card.status != STATUS_MAPPED or card.game_type is None:
            continue
        names_by_type.setdefault(card.game_type, set()).add(card.name)
        types_by_name.setdefault(normalize(card.name), set()).add(card.game_type)
    out = [f"Tipo {t}: nomes diferentes lidos ({', '.join(sorted(n))})" for t, n in names_by_type.items() if len(n) > 1]
    out += [f"\"{n}\" apareceu em tipos diferentes ({', '.join(str(t) for t in sorted(ts))})" for n, ts in types_by_name.items() if len(ts) > 1]
    return out
