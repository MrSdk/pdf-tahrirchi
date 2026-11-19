"""PDF tahrirlash yadrosi: matn/rasm elementlarini topish va o'zgartirish (PyMuPDF)."""
import io
import math
from collections import Counter, defaultdict

import pymupdf
from PIL import Image

from . import fonts

TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE
MIN_SHRINK = 0.6          # shrift eng ko'pi bilan shu nisbatgacha kichrayadi
MAX_IMAGE_SIDE = 3000     # qo'yiladigan rasmning eng katta tomoni (piksel)


class EditError(Exception):
    pass


# ---------------------------------------------------------------- ranglar

def int_to_hex(c: int) -> str:
    return "#%06x" % (c & 0xFFFFFF)


def hex_to_rgb(h: str):
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    try:
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in rgb)


# ---------------------------------------------------------------- matn elementlari

def text_items(page, pno: int):
    """Sahifadagi matnni tahrirlash birliklariga ajratadi.

    Birlik — bir qatordagi, bir xil uslubdagi va orasida katta bo'shliq bo'lmagan
    belgilar ketma-ketligi. Shunda «Nomi:» va uning qiymati alohida element bo'ladi.
    """
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    items = []

    def push(seg):
        if seg and seg["text"].strip():
            seg["text"] = seg["text"].rstrip()
            seg.pop("key")
            seg.pop("last_x")
            seg["id"] = f"p{pno}t{len(items)}"
            seg["bbox"] = [round(v, 2) for v in seg["bbox"]]
            items.append(seg)

    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            dx, dy = line.get("dir", (1, 0))
            if abs(dx - 1) > 0.03 or abs(dy) > 0.03:
                continue  # qiya yoki vertikal matn — hozircha tahrirlanmaydi
            seg = None
            for span in line["spans"]:
                size = span["size"]
                if size < 1:
                    continue
                fname = span["font"]
                flags = span["flags"]
                bold = fonts.is_bold(fname, flags)
                italic = fonts.is_italic(fname, flags)
                key = (fname, round(size, 1), span["color"], bold, italic)
                sy0, sy1 = span["bbox"][1], span["bbox"][3]
                pending_space = False
                for ch in span["chars"]:
                    c = ch["c"]
                    if c.isspace() or c == " ":
                        pending_space = True
                        continue
                    cx0, _, cx1, _ = ch["bbox"]
                    gap = cx0 - seg["last_x"] if seg else 0
                    if seg is None or seg["key"] != key or gap > 1.0 * size or gap < -0.6 * size:
                        push(seg)
                        seg = {
                            "key": key, "last_x": cx1, "page": pno, "source": "pdf",
                            "text": c, "bbox": [cx0, sy0, cx1, sy1],
                            "origin": [round(ch["origin"][0], 2), round(ch["origin"][1], 2)],
                            "size": round(size, 2), "font": fonts.clean_name(fname),
                            "family": fonts.guess_family(fname, flags),
                            "bold": bold, "italic": italic, "color": int_to_hex(span["color"]),
                        }
                    else:
                        if pending_space or gap > 0.18 * size:
                            seg["text"] += " "
                        seg["text"] += c
                        seg["last_x"] = cx1
                        seg["bbox"][2] = max(seg["bbox"][2], cx1)
                        seg["bbox"][1] = min(seg["bbox"][1], sy0)
                        seg["bbox"][3] = max(seg["bbox"][3], sy1)
                    pending_space = False
            push(seg)

    _guess_alignment(items, page.rect.width)
    return items


def _rows(items):
    """Elementlarni ko'rinadigan qatorlarga guruhlaydi (yuqoridan pastga, chapdan o'ngga)."""
    rows = []
    for it in sorted(items, key=lambda i: (i["origin"][1], i["bbox"][0])):
        for row in rows:
            ref = row[0]
            if abs(ref["origin"][1] - it["origin"][1]) <= 0.45 * max(ref["size"], it["size"]):
                row.append(it)
                break
        else:
            rows.append([it])
    for row in rows:
        row.sort(key=lambda i: i["bbox"][0])
    rows.sort(key=lambda r: (min(i["origin"][1] for i in r), r[0]["bbox"][0]))
    return rows


def _guess_alignment(items, page_w):
    if not items:
        return
    left = min(i["bbox"][0] for i in items)
    for row in _rows(items):
        for it in row:
            it["align"] = "left"
        if len(row) == 1:
            it = row[0]
            cx = (it["bbox"][0] + it["bbox"][2]) / 2
            if abs(cx - page_w / 2) < 4 and it["bbox"][0] > left + 8:
                it["align"] = "center"


def rows_for_prompt(items):
    return _rows(items)


# ---------------------------------------------------------------- rasm elementlari

def image_items(page, pno: int):
    out = []
    page_area = abs(page.rect) or 1
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    for n, info in enumerate(infos):
        r = pymupdf.Rect(info["bbox"]) & page.rect
        if r.is_empty or r.width < 4 or r.height < 4:
            continue
        out.append({
            "id": f"p{pno}i{n}", "page": pno, "xref": info.get("xref", 0),
            "bbox": [round(v, 2) for v in r],
            "px": [info.get("width", 0), info.get("height", 0)],
            "background": abs(r) > 0.8 * page_area,
        })
    return out


def pages_using_xref(doc, xref: int):
    res = []
    for pno in range(doc.page_count):
        try:
            if any(im[0] == xref for im in doc.get_page_images(pno, full=True)):
                res.append(pno)
        except Exception:
            pass
    return res


# ---------------------------------------------------------------- rang namunasi

def _region_image(page, rect):
    rect = pymupdf.Rect(rect) & page.rect
    if rect.is_empty:
        return None, 1
    area = max(abs(rect), 1)
    scale = max(0.3, min(2.0, math.sqrt(150_000 / area)))
    pm = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False,
                         colorspace=pymupdf.csRGB)
    return Image.frombytes("RGB", (pm.width, pm.height), pm.samples), scale


def sample_background(page, rect):
    """To'rtburchak atrofidagi (bo'lmasa — ichidagi) eng ko'p uchraydigan rang."""
    rect = pymupdf.Rect(rect)
    pad = 3
    outer = pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad) & page.rect
    im, scale = _region_image(page, outer)
    if im is None:
        return (255, 255, 255)
    w, h = im.size
    ix0 = (rect.x0 - outer.x0) * scale
    iy0 = (rect.y0 - outer.y0) * scale
    ix1 = w - (outer.x1 - rect.x1) * scale
    iy1 = h - (outer.y1 - rect.y1) * scale
    px = im.load()
    ring, inner = Counter(), Counter()
    sums = defaultdict(lambda: [0, 0, 0, 0])
    for y in range(h):
        inside_y = iy0 <= y < iy1
        for x in range(w):
            c = px[x, y]
            k = (c[0] >> 3, c[1] >> 3, c[2] >> 3)
            (inner if (inside_y and ix0 <= x < ix1) else ring)[k] += 1
            s = sums[k]
            s[0] += c[0]; s[1] += c[1]; s[2] += c[2]; s[3] += 1
    counter = ring if sum(ring.values()) >= 8 else inner
    k = counter.most_common(1)[0][0]
    s = sums[k]
    return (s[0] / s[3], s[1] / s[3], s[2] / s[3])


def sample_ink(page, rect, bg):
    """To'rtburchak ichidagi matn (siyoh) rangi — fondan eng uzoq piksellar o'rtachasi."""
    im, _ = _region_image(page, rect)
    if im is None:
        return (0, 0, 0)
    px = list(im.getdata())
    dist = [(abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]), c) for c in px]
    dist = [d for d in dist if d[0] > 90]
    if len(dist) < 5:
        return (0, 0, 0)
    dist.sort(key=lambda d: -d[0])
    top = dist[: max(5, len(dist) // 3)]
    n = len(top)
    return tuple(sum(d[1][i] for d in top) / n for i in range(3))


# ---------------------------------------------------------------- matn yozish

def _write(page, text, font, size, x, baseline, color, hscale=1.0, word_gap=0.0):
    tw = pymupdf.TextWriter(page.rect)
    if word_gap:
        cx = x
        space = font.text_length(" ", fontsize=size)
        for word in text.split(" "):
            if word:
                tw.append((cx, baseline), word, font=font, fontsize=size)
                cx += font.text_length(word, fontsize=size)
            cx += space + word_gap
    else:
        tw.append((x, baseline), text, font=font, fontsize=size)
    morph = None
    if abs(hscale - 1.0) > 0.005:
        morph = (pymupdf.Point(x, baseline), pymupdf.Matrix(hscale, 1))
    tw.write_text(page, color=color, morph=morph)


def place_text(page, text, *, x0, x1, baseline, size, family, bold, italic, color,
               align="left", left_limit=None, right_limit=None, orig_text=None, warns=None):
    """Bir qatorli matnni berilgan joyga yozadi; sig'masa shriftni kichraytiradi."""
    text = " ".join(text.split())
    if not text:
        return
    font, used = fonts.get_font(family, bold, italic, text)
    nat = font.text_length(text, fontsize=size)
    width0 = max(x1 - x0, 0.1)
    hscale, word_gap = 1.0, 0.0

    if orig_text:
        o_nat = font.text_length(" ".join(orig_text.split()), fontsize=size)
        ratio = width0 / o_nat if o_nat > 0 else 1.0
        if ratio < 0.97:
            # asl shrift tanlangan shriftdan torroq — yangi matnni gorizontal siqamiz
            hscale = max(0.8, ratio)
        elif ratio > 1.004 and orig_text.count(" ") >= 3 and align == "left":
            # asl qator ikki chetga tekislangan — yangisi ham xuddi shu kenglikni egallaydi
            spaces = text.count(" ")
            if spaces and 0.85 * width0 <= nat <= width0:
                word_gap = (width0 - nat) / spaces
            elif width0 < nat <= 1.08 * width0:
                hscale = width0 / nat

    width = nat * hscale + (word_gap * text.count(" "))
    if align == "center":
        cx = (x0 + x1) / 2
        lim_l = left_limit if left_limit is not None else 0
        lim_r = right_limit if right_limit is not None else page.rect.width
        avail = 2 * min(cx - lim_l, lim_r - cx)
    elif align == "right":
        avail = x1 - (left_limit if left_limit is not None else 0)
    else:
        avail = (right_limit if right_limit is not None else page.rect.width) - x0
    avail = max(avail, width0)

    if width > avail + 0.5:
        k = max(MIN_SHRINK, avail / width)
        size *= k
        width *= k
        word_gap *= k
        if warns is not None:
            if width > avail + 0.5:
                warns.append(f"«{text[:40]}» joyga sig'madi — matnni qisqartiring yoki shriftni kichraytiring.")
            else:
                warns.append(f"«{text[:40]}» sig'ishi uchun shrift {round(k * 100)}% ga kichraytirildi.")

    if align == "center":
        x = (x0 + x1) / 2 - width / 2
    elif align == "right":
        x = x1 - width
    else:
        x = x0
    _write(page, text, font, size, x, baseline, color, hscale, word_gap)
    if used == "builtin" and warns is not None:
        warns.append("Mos shrift topilmadi — standart shrift ishlatildi.")


def _limits(item, items, page_w):
    """Element o'ngga/chapga qayergacha kengayishi mumkinligini aniqlaydi."""
    x0, _, x1, _ = item["bbox"]
    base = item["origin"][1]
    gap = 0.3 * item["size"]
    all_left = min(i["bbox"][0] for i in items)
    all_right = max(i["bbox"][2] for i in items)
    left, right = min(all_left, x0), max(all_right, x1, page_w - all_left)
    right = min(right, page_w - 4)
    for o in items:
        if o is item or abs(o["origin"][1] - base) > 0.45 * max(o["size"], item["size"]):
            continue
        if o["bbox"][0] >= x1 - 0.5:
            right = min(right, o["bbox"][0] - gap)
        elif o["bbox"][2] <= x0 + 0.5:
            left = max(left, o["bbox"][2] + gap)
    return left, right


def _ocr_metrics(item):
    """OCR qatori uchun shrift o'lchami va tayanch chizig'ini chamalaydi."""
    x0, y0, x1, y1 = item["bbox"]
    h = y1 - y0
    t = item["text"]
    desc = any(c in "gjpqyQ,;()[]{}|/уфрцщдҳқғў" for c in t)
    asc = any(c.isupper() or c.isdigit() or c in "bdfhklt()[]{}|/бвфўЎ" for c in t)
    top = 0.74 if asc else 0.53
    bottom = 0.22 if desc else 0.0
    size = h / (top + bottom)
    return size, y1 - bottom * size


# ---------------------------------------------------------------- rasm tayyorlash

def prepare_image(data: bytes, target_ratio=None, fit="cover") -> bytes:
    """Yuklangan rasmni PDF'ga qo'yishga tayyorlaydi (nisbatni moslaydi, hajmini cheklaydi)."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise EditError("Rasm faylini o'qib bo'lmadi. PNG, JPG yoki WEBP yuklang.")
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")
    if max(im.size) > MAX_IMAGE_SIDE:
        k = MAX_IMAGE_SIDE / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)

    if target_ratio and fit in ("cover", "contain"):
        w, h = im.size
        ratio = w / h
        if abs(ratio - target_ratio) > 0.01:
            if fit == "cover":
                if ratio > target_ratio:
                    nw = round(h * target_ratio)
                    off = (w - nw) // 2
                    im = im.crop((off, 0, off + nw, h))
                else:
                    nh = round(w / target_ratio)
                    off = (h - nh) // 2
                    im = im.crop((0, off, w, off + nh))
            else:
                if ratio > target_ratio:
                    cw, ch = w, round(w / target_ratio)
                else:
                    cw, ch = round(h * target_ratio), h
                canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
                canvas.paste(im, ((cw - w) // 2, (ch - h) // 2))
                im, has_alpha = canvas, True

    buf = io.BytesIO()
    if has_alpha:
        im.save(buf, "PNG", optimize=True)
    else:
        im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------- amallarni qo'llash

def _band(item):
    """Matnni o'chirish uchun tor tasma: qo'shni qatorlarga tegmaydi."""
    x0, _, x1, _ = item["bbox"]
    oy, sz = item["origin"][1], item["size"]
    return pymupdf.Rect(x0 + 0.3, oy - 0.35 * sz, x1 - 0.3, oy - 0.15 * sz)


def _clip_rect(bbox, page):
    try:
        r = pymupdf.Rect([float(v) for v in bbox])
    except Exception:
        raise EditError("Hudud noto'g'ri berilgan.")
    r.normalize()
    r &= page.rect
    if r.is_empty or r.width < 1 or r.height < 1:
        raise EditError("Hudud juda kichik.")
    return r


def _style(op, item=None):
    g = lambda k, d: op.get(k) if op.get(k) is not None else (item.get(k) if item else d)  # noqa: E731
    return {
        "size": float(g("size", 12)),
        "family": g("family", "liberation-sans"),
        "bold": bool(g("bold", False)),
        "italic": bool(g("italic", False)),
        "color": hex_to_rgb(g("color", "#000000")),
        "align": g("align", "left") or "left",
    }


def apply_ops(doc, ops, ocr_lookup=None):
    """Amallar ro'yxatini hujjatga qo'llaydi. Qaytaradi: (o'zgargan sahifalar, ogohlantirishlar)."""
    affected, warns = set(), []
    by_page = defaultdict(list)
    for op in ops:
        try:
            pno = int(op["page"])
        except Exception:
            raise EditError("Sahifa raqami ko'rsatilmagan.")
        if not 0 <= pno < doc.page_count:
            raise EditError("Bunday sahifa yo'q.")
        by_page[pno].append(op)

    for pno, pops in by_page.items():
        page = doc[pno]
        items = text_items(page, pno)
        index = {it["id"]: it for it in items}
        ocr_index = None
        redact = False

        # 1-bosqich: o'chiriladigan matnlarni belgilash (barchasi bir yo'la o'chiriladi)
        for op in pops:
            kind = op.get("type")
            if kind == "text":
                item = index.get(op.get("id"))
                if item is None and ocr_lookup:
                    if ocr_index is None:
                        ocr_index = {it["id"]: it for it in (ocr_lookup(pno) or [])}
                    item = ocr_index.get(op.get("id"))
                if item is None:
                    raise EditError("Element topilmadi — sahifani yangilab, qayta urinib ko'ring.")
                op["_item"] = item
                if item["source"] == "pdf":
                    page.add_redact_annot(_band(item))
                    redact = True
            elif kind == "erase":
                r = _clip_rect(op.get("bbox"), page)
                op["_rect"] = r
                if any(pymupdf.Rect(it["bbox"]).intersects(r) for it in items):
                    page.add_redact_annot(r)
                    redact = True
        if redact:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)

        # 2-bosqich: yangi kontentni yozish
        for op in pops:
            kind = op.get("type")
            if kind == "text":
                item = op["_item"]
                st = _style(op, item)
                new = str(op.get("text", ""))
                if item["source"] == "pdf":
                    left, right = _limits(item, items, page.rect.width)
                    place_text(page, new, x0=item["bbox"][0], x1=item["bbox"][2],
                               baseline=item["origin"][1], left_limit=left, right_limit=right,
                               orig_text=item["text"] if op.get("size") is None and op.get("family") is None else None,
                               warns=warns, **st)
                else:
                    r = pymupdf.Rect(item["bbox"])
                    bg = sample_background(page, r)
                    ink = sample_ink(page, r, bg)
                    size, baseline = _ocr_metrics(item)
                    if op.get("size") is not None:
                        size = float(op["size"])
                    if op.get("color") is None:
                        st["color"] = tuple(v / 255 for v in ink)
                    st["size"] = size
                    cover = pymupdf.Rect(r.x0 - 1.2, r.y0 - 1.0, r.x1 + 1.2, r.y1 + 1.0) & page.rect
                    page.draw_rect(cover, color=None, fill=tuple(v / 255 for v in bg), width=0)
                    place_text(page, new, x0=r.x0, x1=r.x1, baseline=baseline,
                               right_limit=page.rect.width - 4, left_limit=4, warns=warns, **st)

            elif kind == "erase":
                r = op["_rect"]
                fill = op.get("fill")
                if fill:
                    rgb = sample_background(page, r) if fill == "auto" else tuple(v * 255 for v in hex_to_rgb(fill))
                    page.draw_rect(r, color=None, fill=tuple(v / 255 for v in rgb), width=0)

            elif kind == "add_text":
                r = _clip_rect(op.get("bbox"), page)
                st = _style(op)
                lines = [ln for ln in str(op.get("text", "")).split("\n")]
                y = r.y0 + st["size"] * 0.95
                for ln in lines:
                    if ln.strip():
                        place_text(page, ln, x0=r.x0, x1=r.x1, baseline=y, left_limit=4,
                                   right_limit=page.rect.width - 4, warns=warns, **st)
                    y += st["size"] * 1.25

            elif kind == "image_replace":
                xref = int(op.get("xref") or 0)
                r = _clip_rect(op.get("bbox"), page)
                data = prepare_image(op["data"], r.width / r.height, op.get("fit", "cover"))
                if xref > 0:
                    used = pages_using_xref(doc, xref)
                    page.replace_image(xref, stream=data)
                    affected.update(used)
                    if len(used) > 1:
                        warns.append(f"Bu rasm {len(used)} ta sahifada ishlatilgan — barchasida almashtirildi.")
                else:
                    page.insert_image(r, stream=data, keep_proportion=False)

            elif kind == "image_delete":
                xref = int(op.get("xref") or 0)
                if xref <= 0:
                    raise EditError("Bu rasmni o'chirib bo'lmaydi — «Hudud» rejimida ustini yoping.")
                used = pages_using_xref(doc, xref)
                page.delete_image(xref)
                affected.update(used)
                if len(used) > 1:
                    warns.append(f"Bu rasm {len(used)} ta sahifada ishlatilgan — barchasidan olib tashlandi.")

            elif kind == "add_image":
                r = _clip_rect(op.get("bbox"), page)
                data = prepare_image(op["data"])
                page.insert_image(r, stream=data, keep_proportion=True)

            else:
                raise EditError(f"Noma'lum amal: {kind}")
        affected.add(pno)

    return affected, warns
