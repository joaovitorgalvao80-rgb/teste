"""Detecção automática de chaves de API a partir de texto/.txt/kaggle.json.

Funções puras extraídas de app.py: dado o conteúdo de um arquivo, descobrem
qual valor pertence a qual provedor (por rótulo ou por formato conhecido).
"""
from __future__ import annotations

import re
from typing import Optional

MAX_KEYS_FILE_BYTES = 64 * 1024

# Formatos conhecidos de cada provedor; usados quando a linha não tem rótulo.
_KEY_GUESS_PATTERNS = [
    ("groq", re.compile(r"^gsk_[A-Za-z0-9_-]{20,}$")),
    ("kaggle_token", re.compile(r"^KGAT[A-Za-z0-9_-]{10,}$", re.IGNORECASE)),
    ("pixabay", re.compile(r"^\d{6,10}-[0-9a-f]{20,40}$", re.IGNORECASE)),
    ("kaggle_token", re.compile(r"^[0-9a-f]{32}$")),
    ("pexels", re.compile(r"^[A-Za-z0-9]{45,60}$")),
]

KEY_FIELD_LABELS = {
    "pexels": "Pexels",
    "pixabay": "Pixabay",
    "groq": "Groq",
    "kaggle_username": "Kaggle username",
    "kaggle_token": "Kaggle token",
}


def _key_field_from_label(label: str) -> Optional[str]:
    low = label.lower()
    if "pexels" in low:
        return "pexels"
    if "pixabay" in low:
        return "pixabay"
    if "groq" in low:
        return "groq"
    if "kaggle" in low:
        return "kaggle_username" if "user" in low else "kaggle_token"
    if low.strip() in {"username", "user"}:
        return "kaggle_username"
    return None


def detect_api_keys(text: str) -> dict[str, str]:
    """Lê um .txt (ou kaggle.json) e descobre qual chave pertence a qual API.

    Aceita linhas rotuladas ("pexels: CHAVE", "groq = CHAVE"), o kaggle.json
    oficial e chaves soltas reconhecidas pelo formato (gsk_, KGAT, etc).
    """
    detected: dict[str, str] = {}

    # kaggle.json oficial: {"username": "...", "key": "..."}
    m_user = re.search(r'"username"\s*:\s*"([^"\s]+)"', text)
    m_key = re.search(r'"key"\s*:\s*"([^"\s]+)"', text)
    if m_user and m_key:
        detected["kaggle_username"] = m_user.group(1)
        detected["kaggle_token"] = m_key.group(1)

    for raw_line in text.splitlines():
        line = raw_line.strip().strip(",;")
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        match = re.match(r"^[-*\s]*([A-Za-z _\-]{2,40}?)\s*[:=]\s*(\S+)\s*$", line)
        if match:
            field = _key_field_from_label(match.group(1))
            value = match.group(2).strip().strip('"').strip("'")
            min_len = 3 if field == "kaggle_username" else 8
            if field and len(value) >= min_len:
                detected.setdefault(field, value)
                continue
        for token in re.split(r"[\s,;]+", line):
            token = token.strip().strip('"').strip("'")
            if not token:
                continue
            for field, pattern in _KEY_GUESS_PATTERNS:
                if field not in detected and pattern.match(token):
                    detected[field] = token
                    break
    return detected
