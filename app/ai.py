"""AI yordamchi: foydalanuvchi ko'rsatmasini Ollama orqali o'zgarishlar ro'yxatiga aylantiradi.

Model PDF'ga tegmaydi — u faqat «qaysi element qaysi matnga almashsin» degan taklif beradi.
O'zgarishlarni foydalanuvchi tasdiqlaganidan keyin pdfcore qo'llaydi.
"""
import json
import os
import re

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "2m")
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
NUM_THREAD = int(os.environ.get("OLLAMA_NUM_THREAD", "0"))
TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "600"))
BATCH_CHARS = int(os.environ.get("AI_BATCH_CHARS", "3500"))
LABEL_SUFFIX = ":"   # an element ending with this is treated as a label of its right neighbour

_CANDIDATES = [u.strip().rstrip("/") for u in OLLAMA_URL.split(",") if u.strip()] + [
    "http://host.docker.internal:11434",
    "http://172.17.0.1:11434",
    "http://127.0.0.1:11434",
]
_active = {"url": None}


class AiError(Exception):
    pass


SYSTEM = """You are a precise assistant that edits the text of a PDF document.

You receive:
1. ELEMENTS - the text elements of the PDF. Each element has a numeric id in square brackets. Elements that are on the same visual row are separated by " | ". Rows are listed top to bottom.
2. INSTRUCTION - what the user wants changed. It is usually written in Uzbek (Latin or Cyrillic) or Russian.

Decide which elements must change and give the complete new text for each of them.

Rules:
- A label (for example "Maktab nomi:") and its value are often SEPARATE elements, on the same row or on the next row. When the user writes "Maktab nomi: 122-sonli", change the VALUE element and leave the label element untouched.
- If the label and the value are inside ONE element (for example "[7] Sinfi: 4A"), return the full new text of that element and keep the label: "Sinfi: 4B".
- "new_text" replaces the whole text of the element. Parts the user did not ask to change must stay exactly as they are (spelling, case, punctuation, trailing symbols).
- Change only what the user asked for. Never translate, fix spelling or reformat anything else.
- Copy the user's new values exactly as written, in the same script (Latin or Cyrillic).
- If the user asks to replace a word or name everywhere, include every element that contains it.
- If the user asks to delete a text, use an empty string as new_text.
- If no element matches the instruction, return an empty list.

Answer with JSON only, in this form: {"changes":[{"id":<number>,"new_text":"<text>"}]}

Example
ELEMENTS:
[1] GUVOHNOMA
[2] Maktab nomi: | [3] 45-sonli umumta'lim maktabi
[4] Sinfi: 7A
[5] O'quvchi: | [6] Karimov Anvar;
INSTRUCTION:
1. Maktab nomi: 122-sonli umumta'lim maktabi
2. sinfi: 4B
3. o'quvchi Valiyev Sardor
Answer:
{"changes":[{"id":3,"new_text":"122-sonli umumta'lim maktabi"},{"id":4,"new_text":"Sinfi: 4B"},{"id":6,"new_text":"Valiyev Sardor;"}]}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "new_text": {"type": "string"}},
                "required": ["id", "new_text"],
            },
        }
    },
    "required": ["changes"],
}


def status() -> dict:
    """Ollama'ga ulanishni tekshiradi va o'rnatilgan modellarni qaytaradi."""
    order = ([_active["url"]] if _active["url"] else []) + [u for u in _CANDIDATES if u != _active["url"]]
    for url in order:
        try:
            r = httpx.get(url + "/api/tags", timeout=2.5)
            r.raise_for_status()
            models = sorted(m["name"] for m in r.json().get("models", []))
        except Exception:
            continue
        _active["url"] = url
        default = DEFAULT_MODEL if DEFAULT_MODEL in models else (models[0] if models else None)
        return {"ok": True, "url": url, "models": models, "default": default}
    _active["url"] = None
    return {"ok": False, "url": None, "models": [], "default": None, "tried": _CANDIDATES}


def _url() -> str:
    if not _active["url"]:
        st = status()
        if not st["ok"]:
            raise AiError("Ollama'ga ulanib bo'lmadi.")
    return _active["url"]


def unload(model: str):
    """Modelni xotiradan darhol chiqaradi (RAM bo'shaydi)."""
    try:
        httpx.post(_url() + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=10)
    except Exception:
        pass


def batches(page_chars: dict) -> list:
    """Sahifalarni model bir yo'la o'qiy oladigan guruhlarga bo'ladi."""
    out, cur, size = [], [], 0
    for pno in sorted(page_chars):
        n = page_chars[pno]
        if n <= 0:
            continue
        if cur and size + n > BATCH_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append(pno)
        size += n
    if cur:
        out.append(cur)
    return out


def _parse(content: str) -> list:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    changes = data.get("changes") if isinstance(data, dict) else data
    return changes if isinstance(changes, list) else []


def _drop_copied_label(new_text: str, old_text: str, left_text) -> str:
    """Remove a label the model copied into a value element.

    Small models often answer "Sana: 20-dekabr" for the value element even though
    "Sana:" is a separate element to its left, so the label would be printed twice.
    Only a left neighbour ending with LABEL_SUFFIX counts as a label, the copied
    label must be followed by the suffix too, and nothing is removed when the
    original value itself started with the label (label and value in one element).
    """
    label = " ".join((left_text or "").split())
    if not label.endswith(LABEL_SUFFIX):
        return new_text
    name = label[:-len(LABEL_SUFFIX)].strip()
    if not name or old_text.casefold().startswith(name.casefold()):
        return new_text
    m = re.match(rf"{re.escape(name)}\s*{re.escape(LABEL_SUFFIX)}\s*", new_text, re.IGNORECASE)
    if not m or m.end() == len(new_text):
        return new_text
    return new_text[m.end():]


def plan(model: str, instruction: str, rows_by_page: dict) -> list:
    """rows_by_page: {sahifa: [[element, ...] qatorlar]} -> takliflar ro'yxati."""
    num_to_item, left_of, lines, n = {}, {}, [], 0
    for pno in sorted(rows_by_page):
        if len(rows_by_page) > 1:
            lines.append(f"--- page {pno + 1} ---")
        for row in rows_by_page[pno]:
            parts, prev = [], None
            for it in row:
                n += 1
                num_to_item[n] = it
                if prev is not None:
                    left_of[n] = prev["text"]
                prev = it
                parts.append(f"[{n}] {it['text']}")
            lines.append(" | ".join(parts))
    if not num_to_item:
        return []

    options = {"temperature": 0, "num_ctx": NUM_CTX}
    if NUM_THREAD > 0:
        options["num_thread"] = NUM_THREAD
    payload = {
        "model": model, "stream": False, "format": SCHEMA, "keep_alive": KEEP_ALIVE,
        "options": options,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "ELEMENTS:\n" + "\n".join(lines)
                                        + "\n\nINSTRUCTION:\n" + instruction.strip() + "\n\nAnswer:"},
        ],
    }
    try:
        r = httpx.post(_url() + "/api/chat", json=payload, timeout=TIMEOUT)
    except httpx.TimeoutException:
        raise AiError("Model javob bermadi (vaqt tugadi). Kichikroq model tanlang yoki qayta urining.")
    except httpx.HTTPError as e:
        _active["url"] = None
        raise AiError(f"Ollama bilan aloqa uzildi: {e.__class__.__name__}")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", "")
        except Exception:
            msg = r.text[:200]
        raise AiError(f"Ollama xatosi: {msg}")
    content = (r.json().get("message") or {}).get("content", "")

    proposals, seen = [], set()
    for ch in _parse(content):
        try:
            num = int(ch.get("id"))
            new = " ".join(str(ch.get("new_text", "")).split())
        except (TypeError, ValueError, AttributeError):
            continue
        it = num_to_item.get(num)
        if not it or it["id"] in seen:
            continue
        new = _drop_copied_label(new, it["text"], left_of.get(num))
        if new == it["text"]:
            continue
        seen.add(it["id"])
        proposals.append({"id": it["id"], "page": it["page"], "old": it["text"], "new": new,
                          "bbox": it["bbox"], "source": it["source"]})
    return proposals
