"""PDF Tahrirchi — veb-server (FastAPI)."""
import json
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import quote

import pymupdf
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import ai, fonts, ocr, pdfcore, store

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(_app):
    os.makedirs(os.path.join(store.DATA_DIR, "sessions"), exist_ok=True)
    store.cleanup_old()
    yield


app = FastAPI(title="PDF Tahrirchi", docs_url=None, redoc_url=None, lifespan=lifespan)


def _session(sid: str) -> store.Session:
    try:
        return store.Session(sid)
    except store.StoreError as e:
        raise HTTPException(404, str(e))


def _check_base(s: store.Session, base):
    if base is None or int(base) != s.cur:
        raise HTTPException(409, "Hujjat boshqa joyda o'zgargan. Sahifa yangilandi — qayta urinib ko'ring.")


def _page_items(s: store.Session, doc, pno: int, with_ocr: bool, run_ocr: bool):
    page = doc[pno]
    texts = pdfcore.text_items(page, pno)
    images = pdfcore.image_items(page, pno)
    ocr_items, ocr_done = [], False
    if with_ocr:
        cached = s.load_ocr(pno)
        if cached is None and run_ocr:
            cached = ocr.ocr_page(page, pno, texts)
            s.save_ocr(pno, cached)
        if cached is not None:
            ocr_items, ocr_done = cached, True
    return page, texts, images, ocr_items, ocr_done


# ------------------------------------------------------------------ umumiy

@app.get("/api/config")
def config():
    return {
        "fonts": fonts.available_families(),
        "ocr_langs": ocr.langs().split("+") if ocr.langs() else [],
        "max_upload_mb": MAX_UPLOAD_MB,
    }


@app.post("/api/upload")
def upload(file: UploadFile = File(...)):
    data = file.file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Fayl juda katta (chegarasi {MAX_UPLOAD_MB} MB).")
    try:
        s = store.Session.create(data, file.filename or "hujjat.pdf")
    except store.StoreError as e:
        raise HTTPException(400, str(e))
    return s.info()


@app.get("/api/doc/{sid}")
def doc_info(sid: str):
    return _session(sid).info()


@app.get("/api/doc/{sid}/v/{v}/page/{pno}.png")
def page_png(sid: str, v: int, pno: int, scale: float = Query(1.5)):
    s = _session(sid)
    scale = max(0.2, min(4.0, scale))
    try:
        doc = s.open(v)
    except store.StoreError as e:
        raise HTTPException(404, str(e))
    try:
        if not 0 <= pno < doc.page_count:
            raise HTTPException(404, "Bunday sahifa yo'q.")
        pm = doc[pno].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False,
                                 colorspace=pymupdf.csRGB)
        data = pm.tobytes("png")
    finally:
        doc.close()
    return Response(data, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=86400, immutable"})


@app.get("/api/doc/{sid}/page/{pno}/items")
def page_items(sid: str, pno: int, ocr_on: int = Query(0, alias="ocr")):
    with store.lock(sid):
        s = _session(sid)
        doc = s.open()
        try:
            if not 0 <= pno < doc.page_count:
                raise HTTPException(404, "Bunday sahifa yo'q.")
            try:
                page, texts, images, ocr_items, ocr_done = _page_items(s, doc, pno, bool(ocr_on), True)
            except ocr.OcrError as e:
                raise HTTPException(500, str(e))
            scanned = not texts and any(
                abs(pymupdf.Rect(im["bbox"])) > 0.5 * abs(page.rect) for im in images)
            return {"version": s.cur, "page": pno, "texts": texts + ocr_items, "images": images,
                    "scanned": scanned, "ocr": ocr_done}
        finally:
            doc.close()


# ------------------------------------------------------------------ tahrirlash

def _commit(s: store.Session, doc, ops):
    try:
        affected, warns = pdfcore.apply_ops(doc, ops, ocr_lookup=s.load_ocr)
    except pdfcore.EditError as e:
        raise HTTPException(400, str(e))
    s.commit(doc, affected)
    return {**s.info(), "affected": sorted(affected), "warnings": warns}


@app.post("/api/doc/{sid}/edit")
def edit(sid: str, body: dict = Body(...)):
    ops = body.get("ops") or []
    if not isinstance(ops, list) or not ops:
        raise HTTPException(400, "O'zgarishlar ko'rsatilmagan.")
    allowed = {"text", "erase", "add_text", "image_delete"}
    if any(not isinstance(op, dict) or op.get("type") not in allowed for op in ops):
        raise HTTPException(400, "Noto'g'ri amal.")
    with store.lock(sid):
        s = _session(sid)
        _check_base(s, body.get("base"))
        doc = s.open()
        try:
            return _commit(s, doc, ops)
        finally:
            doc.close()


@app.post("/api/doc/{sid}/image")
def image(sid: str, base: int = Form(...), page: int = Form(...), action: str = Form(...),
          bbox: str = Form(...), xref: int = Form(0), fit: str = Form("cover"),
          file: UploadFile = File(...)):
    if action not in ("replace", "add"):
        raise HTTPException(400, "Noto'g'ri amal.")
    data = file.file.read(60 * 1024 * 1024 + 1)
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(413, "Rasm juda katta (chegarasi 60 MB).")
    try:
        box = json.loads(bbox)
    except json.JSONDecodeError:
        raise HTTPException(400, "Hudud noto'g'ri berilgan.")
    op = {"type": "image_replace" if action == "replace" else "add_image", "page": page,
          "bbox": box, "xref": xref, "fit": fit, "data": data}
    with store.lock(sid):
        s = _session(sid)
        _check_base(s, base)
        doc = s.open()
        try:
            return _commit(s, doc, [op])
        finally:
            doc.close()


@app.post("/api/doc/{sid}/undo")
def undo(sid: str):
    with store.lock(sid):
        s = _session(sid)
        s.undo()
        return s.info()


@app.post("/api/doc/{sid}/redo")
def redo(sid: str):
    with store.lock(sid):
        s = _session(sid)
        s.redo()
        return s.info()


@app.get("/api/doc/{sid}/download")
def download(sid: str):
    s = _session(sid)
    with store.lock(sid):
        doc = s.open()
        try:
            data = None
            try:
                # shriftlardan faqat ishlatilgan belgilar qoladi (fayl ancha kichrayadi);
                # matn o'zgarib qolsa, xavfsiz yo'l — kichraytirilmagan nusxa beriladi
                before = [p.get_text() for p in doc]
                small = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
                small.subset_fonts()
                out = small.tobytes(garbage=3, deflate=True)
                small.close()
                check = pymupdf.open(stream=out, filetype="pdf")
                if [p.get_text() for p in check] == before:
                    data = out
                check.close()
            except Exception:
                data = None
            if data is None:
                data = doc.tobytes(garbage=3, deflate=True)
        finally:
            doc.close()
    base = re.sub(r"\.pdf$", "", s.meta["name"], flags=re.I)
    name = f"{base}-tahrirlangan.pdf"
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "hujjat.pdf"
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store",
    })


# ------------------------------------------------------------------ AI

@app.get("/api/ai/status")
def ai_status():
    return ai.status()


@app.post("/api/ai/unload")
def ai_unload(body: dict = Body(...)):
    if body.get("model"):
        ai.unload(str(body["model"]))
    return {"ok": True}


@app.get("/api/doc/{sid}/ai/batches")
def ai_batches(sid: str):
    with store.lock(sid):
        s = _session(sid)
        doc = s.open()
        try:
            chars = {}
            for pno in range(doc.page_count):
                _p, texts, _i, ocr_items, _d = _page_items(s, doc, pno, True, False)
                chars[pno] = sum(len(t["text"]) + 8 for t in texts + ocr_items)
        finally:
            doc.close()
    return {"version": s.cur, "batches": ai.batches(chars),
            "empty": [p for p, n in chars.items() if n == 0]}


@app.post("/api/doc/{sid}/ai/plan")
def ai_plan(sid: str, body: dict = Body(...)):
    instruction = str(body.get("instruction") or "").strip()
    model = str(body.get("model") or "").strip()
    pages = body.get("pages") or []
    if not instruction:
        raise HTTPException(400, "Ko'rsatma yozilmagan.")
    if not model:
        raise HTTPException(400, "Model tanlanmagan.")
    with store.lock(sid):
        s = _session(sid)
        _check_base(s, body.get("base"))
        doc = s.open()
        try:
            rows = {}
            for pno in pages:
                pno = int(pno)
                if not 0 <= pno < doc.page_count:
                    continue
                _p, texts, _i, ocr_items, _d = _page_items(s, doc, pno, True, False)
                if texts or ocr_items:
                    rows[pno] = pdfcore.rows_for_prompt(texts + ocr_items)
        finally:
            doc.close()
    try:
        proposals = ai.plan(model, instruction, rows)
    except ai.AiError as e:
        raise HTTPException(502, str(e))
    return {"version": s.cur, "proposals": proposals}


# ------------------------------------------------------------------ statik sahifa

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
