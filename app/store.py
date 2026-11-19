"""Yuklangan hujjatlar va ularning versiyalari (ortga/oldinga qaytarish uchun)."""
import json
import os
import re
import shutil
import threading
import time
import uuid

import pymupdf

DATA_DIR = os.environ.get("DATA_DIR", "/data")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "40"))
SESSION_TTL_DAYS = float(os.environ.get("SESSION_TTL_DAYS", "7"))

_locks = {}
_locks_guard = threading.Lock()


class StoreError(Exception):
    pass


def lock(sid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(sid, threading.Lock())


def cleanup_old():
    root = os.path.join(DATA_DIR, "sessions")
    if not os.path.isdir(root):
        return
    limit = time.time() - SESSION_TTL_DAYS * 86400
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if os.path.getmtime(os.path.join(p, "meta.json")) < limit:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            shutil.rmtree(p, ignore_errors=True)


class Session:
    def __init__(self, sid: str):
        if not re.fullmatch(r"[a-f0-9]{16}", sid or ""):
            raise StoreError("Hujjat topilmadi.")
        self.id = sid
        self.dir = os.path.join(DATA_DIR, "sessions", sid)
        try:
            with open(os.path.join(self.dir, "meta.json"), encoding="utf-8") as f:
                self.meta = json.load(f)
        except OSError:
            raise StoreError("Hujjat topilmadi (eskirgan bo'lishi mumkin). Qayta yuklang.")

    # --- yaratish
    @classmethod
    def create(cls, data: bytes, name: str):
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception:
            raise StoreError("Bu fayl PDF emas yoki buzilgan.")
        if doc.needs_pass and not doc.authenticate(""):
            raise StoreError("PDF parol bilan himoyalangan. Avval parolini olib tashlang.")
        if doc.page_count == 0:
            raise StoreError("PDF'da sahifa yo'q.")
        for page in doc:
            if page.rotation:
                page.remove_rotation()
        sid = uuid.uuid4().hex[:16]
        d = os.path.join(DATA_DIR, "sessions", sid)
        os.makedirs(d, exist_ok=True)
        doc.save(os.path.join(d, "v0.pdf"), garbage=1, deflate=True,
                 encryption=pymupdf.PDF_ENCRYPT_NONE)
        meta = {
            "name": os.path.basename(name or "hujjat.pdf"),
            "cur": 0, "top": 0, "low": 0,
            "pages": [{"w": round(p.rect.width, 2), "h": round(p.rect.height, 2)} for p in doc],
        }
        doc.close()
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
        return cls(sid)

    # --- yordamchilar
    def _save_meta(self):
        tmp = os.path.join(self.dir, "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.meta, f)
        os.replace(tmp, os.path.join(self.dir, "meta.json"))

    @property
    def cur(self) -> int:
        return self.meta["cur"]

    def pdf_path(self, v=None) -> str:
        return os.path.join(self.dir, f"v{self.cur if v is None else int(v)}.pdf")

    def open(self, v=None):
        p = self.pdf_path(v)
        if not os.path.exists(p):
            raise StoreError("Bu versiya endi mavjud emas.")
        return pymupdf.open(p)

    def info(self) -> dict:
        m = self.meta
        return {
            "id": self.id, "name": m["name"], "version": m["cur"], "pages": m["pages"],
            "can_undo": m["cur"] > m["low"], "can_redo": m["cur"] < m["top"],
        }

    # --- OCR keshi
    def ocr_path(self, pno: int, v=None) -> str:
        return os.path.join(self.dir, f"ocr_v{self.cur if v is None else int(v)}_p{int(pno)}.json")

    def load_ocr(self, pno: int, v=None):
        try:
            with open(self.ocr_path(pno, v), encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return None

    def save_ocr(self, pno: int, items, v=None):
        with open(self.ocr_path(pno, v), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)

    def _remove_version(self, v: int):
        for name in os.listdir(self.dir):
            if name == f"v{v}.pdf" or name.startswith(f"ocr_v{v}_"):
                try:
                    os.remove(os.path.join(self.dir, name))
                except OSError:
                    pass

    # --- versiyalar
    def commit(self, doc, affected=()):
        """O'zgartirilgan hujjatni yangi versiya sifatida saqlaydi."""
        m = self.meta
        old, new = m["cur"], m["cur"] + 1
        for v in range(new, m["top"] + 1):   # «oldinga» tarmog'i bekor bo'ladi
            self._remove_version(v)
        tmp = os.path.join(self.dir, f"v{new}.tmp")
        doc.save(tmp, garbage=1, deflate=True)
        os.replace(tmp, self.pdf_path(new))
        # o'zgarmagan sahifalarning OCR natijasi yangi versiyaga ko'chiriladi
        affected = set(affected)
        prefix = f"ocr_v{old}_p"
        for name in os.listdir(self.dir):
            if name.startswith(prefix):
                pno = int(name[len(prefix):-5])
                if pno not in affected:
                    shutil.copyfile(os.path.join(self.dir, name), self.ocr_path(pno, new))
        m["cur"] = m["top"] = new
        while m["top"] - m["low"] > MAX_HISTORY:
            self._remove_version(m["low"])
            m["low"] += 1
        self._save_meta()

    def undo(self):
        if self.meta["cur"] > self.meta["low"]:
            self.meta["cur"] -= 1
            self._save_meta()

    def redo(self):
        if self.meta["cur"] < self.meta["top"]:
            self.meta["cur"] += 1
            self._save_meta()
