"""Transforma roteiro/transcricao em blocos visuais (cenas).

Aceita dois formatos de entrada:

1. Transcricao com timestamps (preferido):
   [00:00.0 - 00:04.2] Voce sabia que existe uma forma barata de controlar mosquito?
   [00:04.2 - 00:08.0] O problema e que quase ninguem fala disso.

2. Roteiro em texto corrido (fallback): dividido por frases e fatiado em
   blocos de duracao media configuravel.
"""
from __future__ import annotations

import re
import unicodedata

# [00:00.0 - 00:04.2] texto / [00:00 - 00:04] texto / [1:02.5 - 1:08] texto
# minutos com ate 3 digitos: transcricoes de videos longos passam de 99 min
TIMESTAMP_RE = re.compile(
    r"\[\s*(?P<start>\d{1,3}:\d{2}(?:[.,]\d+)?)\s*"
    "[-\u2013\u2014]"
    r"\s*(?P<end>\d{1,3}:\d{2}(?:[.,]\d+)?)\s*\]\s*(?P<text>.*)"
)


def remove_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def _to_seconds(stamp: str) -> float:
    stamp = stamp.replace(",", ".")
    minutes, rest = stamp.split(":")
    return int(minutes) * 60 + float(rest)


def has_timestamps(text: str) -> bool:
    return any(TIMESTAMP_RE.match(line.strip()) for line in text.splitlines())


def _zone_for(idx: int, total: int) -> str:
    """Heuristica simples de zona narrativa para o guia visual.

    Primeira cena e sempre GANCHO, ultima e CTA; o miolo e DESENVOLVIMENTO.
    Em videos mais longos, as 2 primeiras contam como gancho.
    """
    if total <= 1:
        return "GANCHO"
    if idx == 1:
        return "GANCHO"
    if idx == total:
        return "CTA"
    if total >= 8 and idx == 2:
        return "GANCHO"
    return "DESENVOLVIMENTO"


def parse_timestamped(text: str) -> list[dict]:
    scenes: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    matched = [TIMESTAMP_RE.match(ln) for ln in lines]
    total = sum(1 for m in matched if m)
    idx = 0
    for m in matched:
        if not m:
            continue
        idx += 1
        start = _to_seconds(m.group("start"))
        end = _to_seconds(m.group("end"))
        if end < start:
            end = start
        scenes.append(
            {
                "scene_id": f"scene_{idx:03d}",
                "idx": idx,
                "zone": _zone_for(idx, total),
                "start_time": round(start, 3),
                "end_time": round(end, 3),
                "duration": round(max(0.1, end - start), 3),
                "narration": m.group("text").strip(),
            }
        )
    return scenes


def _chunk_words(sentence: str, target_words: int) -> list[str]:
    """Quebra uma frase longa em pedaços de no máximo `target_words` palavras."""
    words = sentence.split()
    if len(words) <= target_words:
        return [sentence]
    return [" ".join(words[i : i + target_words]) for i in range(0, len(words), target_words)]


def _split_sentences(text: str, target_words: int = 16) -> list[str]:
    parts: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            sentence = sentence.strip()
            if sentence:
                parts.extend(_chunk_words(sentence, target_words))
    return [p for p in parts if p]


def parse_plain(text: str, scene_duration: float = 4.0) -> list[dict]:
    scenes: list[dict] = []
    sentences = _split_sentences(text)
    total = len(sentences)
    start = 0.0
    for idx, sentence in enumerate(sentences, 1):
        end = start + scene_duration
        scenes.append(
            {
                "scene_id": f"scene_{idx:03d}",
                "idx": idx,
                "zone": _zone_for(idx, total),
                "start_time": round(start, 3),
                "end_time": round(end, 3),
                "duration": round(scene_duration, 3),
                "narration": sentence,
            }
        )
        start = end
    return scenes


def parse_script(text: str, scene_duration: float = 4.0) -> list[dict]:
    """Detecta o formato e devolve a lista de cenas base (sem keywords ainda)."""
    text = text.strip()
    if not text:
        return []
    if has_timestamps(text):
        return parse_timestamped(text)
    return parse_plain(text, scene_duration)


def assign_parts(scenes: list[dict], target_seconds: float) -> int:
    """Divide as cenas em partes de ~target_seconds, cortando em fronteira de cena.

    Atribui scene['part'] (1-based) in-place e retorna o numero de partes.
    Usado no modo video longo: cada parte vira um pacote + render proprio.
    """
    if not scenes:
        return 0
    target_seconds = max(30.0, float(target_seconds or 150))
    part = 1
    part_start = float(scenes[0].get("start_time") or 0.0)
    for scene in scenes:
        start = float(scene.get("start_time") or 0.0)
        end = float(scene.get("end_time") or start)
        if end - part_start > target_seconds and start > part_start:
            part += 1
            part_start = start
        scene["part"] = part
    return part
