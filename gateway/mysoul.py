"""Busca nos arquivos do projeto MySoul do Atomic Chat. Não copia os livros."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import unicodedata
import urllib.error
import urllib.request
from collections import Counter

import numpy as np

def _default_db() -> str:
    chosen = os.environ.get("MYSOUL_DB", "").strip()
    if chosen:
        return chosen
    root = os.path.join(os.environ.get("APPDATA", ""), "Atomic Chat", "data", "db")
    if os.path.isdir(root):
        found = [
            os.path.join(root, name)
            for name in os.listdir(root)
            if name.startswith("project_") and name.endswith(".db")
        ]
        found.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        if found:
            return found[0]
    return os.path.join(root, "project.db")


DB_PATH = _default_db()

_STOP = {
    "a", "o", "os", "as", "de", "da", "do", "das", "dos", "e", "em", "um", "uma",
    "uns", "umas", "para", "por", "com", "sem", "que", "se", "na", "no", "nas",
    "nos", "ao", "aos", "ou", "como", "mais", "muito", "ja", "the", "of", "and",
    "to", "for", "in", "on", "at", "is", "it", "this", "that", "from", "with",
    "uma", "nao", "sim", "seu", "sua", "ele", "ela", "eles", "voce", "voces",
}

EMBED = os.environ.get("MYSOUL_EMBED", "http://127.0.0.1:3971/v1/embeddings")
_lock = threading.Lock()
_docs: list[dict] | None = None
_matrix: np.ndarray | None = None
_idf: dict[str, float] = {}
_meta = {"files": 0, "chunks": 0, "ok": False}


def fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]{3,}", fold(text).lower())
    return [word for word in words if word not in _STOP]


def stats() -> dict:
    _ensure()
    return dict(_meta)


def _vector(blob: bytes) -> np.ndarray | None:
    if not blob or len(blob) != 1536:
        return None
    vec = np.frombuffer(blob, dtype=np.float32).copy()
    if vec.shape != (384,) or not np.isfinite(vec).all():
        return None
    norm = float(np.linalg.norm(vec))
    if norm <= 0:
        return None
    vec /= norm
    return vec


def _ensure() -> None:
    global _docs, _idf, _matrix
    if _docs is not None:
        return
    with _lock:
        if _docs is not None:
            return
        if not os.path.isfile(DB_PATH):
            _docs = []
            _matrix = np.zeros((0, 384), dtype=np.float32)
            _meta.update(files=0, chunks=0, ok=False)
            print("mysoul: base nao encontrada")
            return
        uri = "file:" + DB_PATH.replace("\\", "/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=8)
        try:
            rows = connection.execute(
                "SELECT c.text, f.name, c.embedding FROM chunks c JOIN files f ON f.id = c.file_id"
            ).fetchall()
            files = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            connection.close()
        docs = []
        vectors = []
        df: Counter[str] = Counter()
        for text, name, blob in rows:
            body = (text or "").strip()
            if not body:
                continue
            tokens = _tokens(body)
            if not tokens:
                continue
            bag = Counter(tokens)
            docs.append({"name": name or "arquivo", "text": body[:900], "bag": bag, "folded": fold(body).lower()})
            vectors.append(_vector(blob))
            df.update(bag.keys())
        count = len(docs) or 1
        _idf = {word: math.log((count + 1) / (freq + 1)) + 1.0 for word, freq in df.items()}
        _docs = docs
        if vectors and all(item is not None for item in vectors):
            _matrix = np.stack(vectors).astype(np.float32)
        else:
            _matrix = None
        _meta.update(files=int(files), chunks=len(docs), ok=True)
        print(f"mysoul: {files} arquivos, {len(docs)} trechos")


def _embed(question: str) -> np.ndarray | None:
    body = json.dumps({"model": "sentence-transformer-mini", "input": question[:500]}).encode("utf-8")
    request = urllib.request.Request(
        EMBED,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        raw = data["data"][0]["embedding"]
    except (OSError, urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TypeError):
        return None
    vec = np.asarray(raw, dtype=np.float32)
    if vec.shape != (384,) or not np.isfinite(vec).all():
        return None
    norm = float(np.linalg.norm(vec))
    if norm <= 0:
        return None
    return vec / norm


def _keyword_scores(query: list[str]) -> list[float]:
    scores = []
    folded = " ".join(query)
    for doc in _docs or []:
        score = 0.0
        for word in query:
            found = doc["bag"].get(word, 0)
            if found:
                score += _idf.get(word, 1.0) * (1.0 + math.log(found))
            elif len(word) >= 5 and word in doc["folded"]:
                score += 0.4
        if folded and folded in doc["folded"]:
            score += 2.0
        scores.append(score)
    return scores


def _pack(order: list[int], limit: int) -> str:
    parts = []
    total = 0
    seen: set[int] = set()
    for index in order:
        if index in seen or index < 0 or index >= len(_docs or []):
            continue
        seen.add(index)
        doc = _docs[index]
        piece = f"[{doc['name']}] {doc['text']}"
        if total + len(piece) > 3200:
            break
        parts.append(piece)
        total += len(piece)
        if len(parts) >= limit:
            break
    return "\n---\n".join(parts)


def retrieve(question: str, limit: int = 4) -> str:
    _ensure()
    if not _docs:
        return ""
    query = _tokens(question)
    lexical = _keyword_scores(query) if query else [0.0] * len(_docs)
    vector = _embed(question) if question.strip() else None
    if vector is not None and _matrix is not None and len(_matrix) == len(_docs):
        semantic = (_matrix @ vector).tolist()
        scored = [float(semantic[i]) + min(lexical[i], 3.0) * 0.05 for i in range(len(_docs))]
    else:
        scored = lexical
    order = sorted(range(len(_docs)), key=lambda i: scored[i], reverse=True)
    if not order or scored[order[0]] <= 0:
        return ""
    return _pack(order, limit)
