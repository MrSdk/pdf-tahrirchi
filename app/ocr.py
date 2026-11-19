"""OCR (Tesseract): skanerlangan sahifa yoki rasm ichidagi matnni topadi."""
import csv
import functools
import io
import os
import subprocess
import tempfile

import pymupdf

OCR_LANGS = os.environ.get("OCR_LANGS", "uzb+uzb_cyrl+rus+eng")
OCR_DPI = int(os.environ.get("OCR_DPI", "300"))
OCR_TIMEOUT = int(os.environ.get("OCR_TIMEOUT", "180"))


class OcrError(Exception):
    pass


@functools.lru_cache(maxsize=1)
def available_langs():
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in (out.stdout + out.stderr).splitlines()[1:] if ln.strip()]


def langs() -> str:
    have = set(available_langs())
    use = [lg for lg in OCR_LANGS.split("+") if lg in have]
    return "+".join(use)


def ocr_page(page, pno: int, pdf_items=()):
    """Sahifani OCR qiladi; PDF'ning o'z matni bilan ustma-ust tushgan qatorlar tashlab yuboriladi."""
    lg = langs()
    if not lg:
        raise OcrError("OCR (Tesseract) o'rnatilmagan yoki til paketlari topilmadi.")
    scale = OCR_DPI / 72
    # juda katta sahifalarda xotirani tejash
    longest = max(page.rect.width, page.rect.height) * scale
    if longest > 5000:
        scale *= 5000 / longest
    pm = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, colorspace=pymupdf.csRGB)
    with tempfile.TemporaryDirectory() as td:
        img = os.path.join(td, "page.png")
        pm.save(img)
        try:
            res = subprocess.run(
                ["tesseract", img, "stdout", "-l", lg, "--psm", "3", "tsv"],
                capture_output=True, text=True, timeout=OCR_TIMEOUT,
                env={**os.environ, "OMP_THREAD_LIMIT": os.environ.get("OCR_THREADS", "2")},
            )
        except subprocess.TimeoutExpired:
            raise OcrError("OCR juda uzoq davom etdi va to'xtatildi.")
        except OSError:
            raise OcrError("Tesseract ishga tushmadi.")
    if res.returncode != 0:
        raise OcrError("OCR xatosi: " + (res.stderr or "").strip()[:200])

    lines = {}
    reader = csv.DictReader(io.StringIO(res.stdout), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        try:
            if int(row["level"]) != 5:
                continue
            text = (row.get("text") or "").strip()
            conf = float(row["conf"])
            if not text or conf < 0:
                continue
            x, y = int(row["left"]), int(row["top"])
            w, h = int(row["width"]), int(row["height"])
        except (KeyError, ValueError, TypeError):
            continue
        key = (row["page_num"], row["block_num"], row["par_num"], row["line_num"])
        lines.setdefault(key, []).append((x, y, x + w, y + h, text, conf))

    pdf_rects = [pymupdf.Rect(it["bbox"]) for it in pdf_items]
    items = []
    for words in lines.values():
        words.sort(key=lambda t: t[0])
        line_h = sorted(t[3] - t[1] for t in words)[len(words) // 2]
        segs, cur = [], [words[0]]
        for wd in words[1:]:
            if wd[0] - cur[-1][2] > 1.6 * line_h:   # katta bo'shliq — alohida element
                segs.append(cur)
                cur = [wd]
            else:
                cur.append(wd)
        segs.append(cur)
        for seg in segs:
            conf = sum(t[5] for t in seg) / len(seg)
            text = " ".join(t[4] for t in seg)
            if conf < 45 or len(text.strip(" .,-_|'\"")) < 2:
                continue
            r = pymupdf.Rect(min(t[0] for t in seg) / scale, min(t[1] for t in seg) / scale,
                             max(t[2] for t in seg) / scale, max(t[3] for t in seg) / scale)
            if r.height < 3 or r.width < 3:
                continue
            area = abs(r) or 1
            if any(abs(r & pr) > 0.3 * area for pr in pdf_rects if r.intersects(pr)):
                continue
            items.append({
                "id": f"p{pno}o{len(items)}", "page": pno, "source": "ocr", "text": text,
                "bbox": [round(v, 2) for v in r], "origin": [round(r.x0, 2), round(r.y1, 2)],
                "size": round(r.height / 0.9, 1), "family": "liberation-sans",
                "bold": False, "italic": False, "color": None, "align": "left",
                "conf": round(conf),
            })
    return items
