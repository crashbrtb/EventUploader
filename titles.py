"""Reading the Journal: OCR of card titles and the tournament name inside them.

Tesseract only. The titles are short English (or Portuguese) sentences in a
large font, which it reads well once the image is prepared the way
newchestcouter learned to: grey, inverted when the text is light on dark, and
captured by Chrome at twice the size instead of enlarged afterwards.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

try:
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None

TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Tesseract-OCR\tesseract.exe"),
)

# "Your Clanmates' results in the Rise of the Ancients tournament" and the
# misreadings OCR produces around the apostrophe and the doubled letters.
_CLANMATES = re.compile(
    r"your\s*cla[nm]{1,2}\s*-?\s*mates?\s*['’`´]?\s*s?\s*results?\s*(?:in\s*the|in|inthe)\s+(.+)",
    re.IGNORECASE,
)
_CLANMATES_PT = re.compile(
    r"resultados?\s*d[oae]s?\s*(?:membros\s*do\s*cl[aã]|companheiros(?:\s*de\s*cl[aã])?)\s*(?:n[oae]s?|em)\s+(.+)",
    re.IGNORECASE,
)
_TRAILING = re.compile(r"(?:\s*\d{1,2}[:.]\d{2}.*$)|(?:\s*\d+\s*(?:days?|hours?|minutes?)\s*ago.*$)|(?:\s*yesterday.*$)", re.IGNORECASE)


def extract_tournament_name(text: str) -> Optional[str]:
    """The tournament name in a clan results title, or None if it is not one."""
    if not text:
        return None
    clean = " ".join(text.replace("\n", " ").split())
    for pattern in (_CLANMATES, _CLANMATES_PT):
        match = pattern.search(clean)
        if match:
            name = _TRAILING.sub("", match.group(1)).strip(" .,:;-|")
            # OCR sometimes glues a stray character from the card border.
            name = re.sub(r"^[^\w]+|[^\w)]+$", "", name)
            return name or None
    return None


def parse_page_number(text: str) -> Optional[int]:
    digits = re.findall(r"\d+", text or "")
    return int(digits[0]) if digits else None


class Ocr:
    CONFIG = "--psm 7 --oem 1"

    def __init__(self, lang: str = "eng"):
        self.lang = lang
        self._ready: Optional[bool] = None
        self.error = ""

    def available(self) -> bool:
        if self._ready is not None:
            return self._ready
        if pytesseract is None or cv2 is None:
            self.error = "pytesseract/opencv não instalados (rode install.bat)"
            self._ready = False
            return False
        try:
            pytesseract.get_tesseract_version()
            self._ready = True
        except Exception:
            for path in TESSERACT_PATHS:
                if path and os.path.exists(path):
                    pytesseract.pytesseract.tesseract_cmd = path
                    try:
                        pytesseract.get_tesseract_version()
                        self._ready = True
                        break
                    except Exception:
                        continue
            else:
                self._ready = False
                self.error = "Tesseract não encontrado. Instale em https://github.com/UB-Mannheim/tesseract/wiki"
        if self._ready:
            langs = set(pytesseract.get_languages(config=""))
            if self.lang not in langs:
                self.lang = "por" if "por" in langs else next(iter(langs - {"osd"}), "eng")
        return bool(self._ready)

    @staticmethod
    def prepare(image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        if gray.mean() < 128:
            gray = cv2.bitwise_not(gray)
        return gray

    def read(self, image: Optional[np.ndarray], single_line: bool = False) -> str:
        if image is None or image.size == 0 or not self.available():
            return ""
        config = self.CONFIG if single_line else "--psm 6 --oem 1"
        try:
            text = pytesseract.image_to_string(self.prepare(image), lang=self.lang, config=config)
        except Exception as exc:
            self.error = str(exc)
            return ""
        return " ".join(text.split())
