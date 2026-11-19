"""Shriftlarni tanlash: PDF ichidagi shrift nomiga eng mos bepul shriftni topadi."""
import functools
import os
import re

import pymupdf

FONT_DIRS = [
    os.path.join(os.path.dirname(__file__), "fonts"),
    "/usr/share/fonts",
    "/usr/local/share/fonts",
]

# oila -> (oddiy, qalin, kursiv, qalin kursiv) fayl nomlari (kengaytmasiz)
FAMILIES = {
    "dejavu-sans": ("DejaVuSans", "DejaVuSans-Bold", "DejaVuSans-Oblique", "DejaVuSans-BoldOblique"),
    "dejavu-sans-cond": (
        "DejaVuSansCondensed", "DejaVuSansCondensed-Bold",
        "DejaVuSansCondensed-Oblique", "DejaVuSansCondensed-BoldOblique",
    ),
    "dejavu-serif": ("DejaVuSerif", "DejaVuSerif-Bold", "DejaVuSerif-Italic", "DejaVuSerif-BoldItalic"),
    "dejavu-serif-cond": (
        "DejaVuSerifCondensed", "DejaVuSerifCondensed-Bold",
        "DejaVuSerifCondensed-Italic", "DejaVuSerifCondensed-BoldItalic",
    ),
    "dejavu-mono": (
        "DejaVuSansMono", "DejaVuSansMono-Bold", "DejaVuSansMono-Oblique", "DejaVuSansMono-BoldOblique",
    ),
    "liberation-sans": (
        "LiberationSans-Regular", "LiberationSans-Bold", "LiberationSans-Italic", "LiberationSans-BoldItalic",
    ),
    "liberation-serif": (
        "LiberationSerif-Regular", "LiberationSerif-Bold", "LiberationSerif-Italic", "LiberationSerif-BoldItalic",
    ),
    "liberation-mono": (
        "LiberationMono-Regular", "LiberationMono-Bold", "LiberationMono-Italic", "LiberationMono-BoldItalic",
    ),
    "carlito": ("Carlito-Regular", "Carlito-Bold", "Carlito-Italic", "Carlito-BoldItalic"),
    "caladea": ("Caladea-Regular", "Caladea-Bold", "Caladea-Italic", "Caladea-BoldItalic"),
}

# Interfeysda ko'rsatiladigan nomlar
LABELS = {
    "liberation-sans": "Arial uslubi",
    "liberation-serif": "Times uslubi",
    "carlito": "Calibri uslubi",
    "caladea": "Cambria uslubi",
    "dejavu-sans": "DejaVu Sans",
    "dejavu-sans-cond": "DejaVu Sans (tor)",
    "dejavu-serif": "DejaVu Serif",
    "dejavu-serif-cond": "DejaVu Serif (tor)",
    "liberation-mono": "Courier uslubi",
    "dejavu-mono": "DejaVu Mono",
}

# Belgi topilmasa o'tiladigan zaxira (DejaVu deyarli barcha lotin/kirill belgilarini qamraydi)
FALLBACK = {
    "liberation-sans": "dejavu-sans",
    "carlito": "dejavu-sans",
    "dejavu-sans-cond": "dejavu-sans",
    "liberation-serif": "dejavu-serif",
    "caladea": "dejavu-serif",
    "dejavu-serif-cond": "dejavu-serif",
    "liberation-mono": "dejavu-mono",
    "dejavu-serif": "dejavu-sans",
    "dejavu-mono": "dejavu-sans",
}

_NAME_RULES = [
    (r"dejavusanscondensed", "dejavu-sans-cond"),
    (r"dejavuserifcondensed", "dejavu-serif-cond"),
    (r"dejavusansmono", "dejavu-mono"),
    (r"dejavuserif", "dejavu-serif"),
    (r"dejavu", "dejavu-sans"),
    (r"calibri|carlito", "carlito"),
    (r"cambria|caladea", "caladea"),
    (r"courier|consolas|menlo|monaco|mono|typewriter", "liberation-mono"),
    (r"times|liberationserif|tinos|nimbusrom|georgia|garamond|palatino|bookman|century|minion|serif(?!less)|pts?erif|notoserif|charter",
     "liberation-serif"),
    (r"arial|helvet|liberationsans|arimo|nimbussan|verdana|tahoma|segoe|roboto|opensans|notosans|inter|lato|montserrat|ptsans|ubuntu|sans",
     "liberation-sans"),
]


@functools.lru_cache(maxsize=1)
def _index():
    idx = {}
    for root in FONT_DIRS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                base, ext = os.path.splitext(f)
                if ext.lower() in (".ttf", ".otf"):
                    idx.setdefault(base.lower(), os.path.join(dirpath, f))
    return idx


def font_path(family: str, bold: bool, italic: bool):
    names = FAMILIES.get(family)
    if not names:
        return None
    order = [(2 if italic else 0) + (1 if bold else 0)]
    order += [1 if bold else 0, 0]  # kursiv yo'q bo'lsa — to'g'ri shakli
    for i in order:
        p = _index().get(names[i].lower())
        if p:
            return p
    return None


def available_families():
    return [
        {"id": fam, "label": LABELS[fam]}
        for fam in LABELS
        if font_path(fam, False, False)
    ]


def clean_name(fontname: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", fontname or "")


def guess_family(fontname: str, flags: int = 0) -> str:
    n = re.sub(r"[^a-z0-9]", "", clean_name(fontname).lower())
    for pat, fam in _NAME_RULES:
        if re.search(pat, n):
            return fam
    if flags & 8:
        return "liberation-mono"
    if flags & 4:
        return "liberation-serif"
    return "liberation-sans"


def is_bold(fontname: str, flags: int) -> bool:
    n = clean_name(fontname).lower()
    return bool(flags & 16) or bool(re.search(r"bold|black|heavy|semibold|demi", n))


def is_italic(fontname: str, flags: int) -> bool:
    n = clean_name(fontname).lower()
    return bool(flags & 2) or bool(re.search(r"italic|oblique", n))


@functools.lru_cache(maxsize=64)
def _load(path: str):
    return pymupdf.Font(fontfile=path)


def get_font(family: str, bold: bool, italic: bool, text: str = ""):
    """Matndagi barcha belgilarni chiza oladigan shriftni qaytaradi: (Font, oila)."""
    tried = set()
    fam = family if family in FAMILIES else "liberation-sans"
    while fam and fam not in tried:
        tried.add(fam)
        p = font_path(fam, bold, italic)
        if p:
            f = _load(p)
            if all(ch.isspace() or f.has_glyph(ord(ch)) for ch in text):
                return f, fam
        fam = FALLBACK.get(fam)
    for fam in ("dejavu-sans", "liberation-sans"):
        p = font_path(fam, bold, italic)
        if p:
            return _load(p), fam
    return pymupdf.Font("hebo" if bold else "helv"), "builtin"
