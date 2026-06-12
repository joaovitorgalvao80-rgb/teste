"""Relevância textual e sinais de qualidade para curadoria de assets.

Módulo determinístico e offline: não chama rede nem IA. Serve de base de
pontuação tanto para a seleção automática quanto para os badges da seleção
manual, e é a fundação sobre a qual o adapter de visão (services/vision.py)
constrói uma análise mais rica.

A ideia central é medir o quanto a *keyword que trouxe* um asset corresponde
ao conceito visual da cena (visual_goal + keywords + must_show), penalizando
termos genéricos de banco de imagem e colisões com must_not_show.
"""
from __future__ import annotations

import re
import unicodedata

# Stopwords PT+EN suficientes para limpar narração/objetivo visual sem
# depender de bibliotecas externas. Mantido enxuto de propósito.
_STOPWORDS = {
    # português
    "a", "o", "os", "as", "um", "uma", "uns", "umas", "de", "do", "da", "dos",
    "das", "e", "ou", "que", "se", "por", "para", "com", "sem", "no", "na",
    "nos", "nas", "em", "ao", "aos", "à", "às", "the", "of", "isso", "isto",
    "esse", "essa", "este", "esta", "ele", "ela", "eles", "elas", "voce",
    "voces", "seu", "sua", "seus", "suas", "meu", "minha", "como", "quando",
    "porque", "porem", "mas", "mais", "menos", "muito", "muita", "ja", "nao",
    "sim", "tambem", "entao", "assim", "ser", "estar", "ter", "fazer", "vai",
    "foi", "era", "sao", "tem", "pode", "todo", "toda", "todos", "todas",
    "cada", "aqui", "ali", "la", "onde", "qual", "quais",
    # inglês (queries vêm em inglês)
    "and", "or", "to", "in", "on", "of", "for", "with", "without", "a", "an",
    "is", "are", "be", "the", "this", "that", "these", "those", "it", "as",
    "at", "by", "from", "into", "over", "up", "down", "out",
}

# Termos genéricos de banco de imagem: trazem resultado bonito porém
# desconectado da cena. Usado para penalizar keyword fraca.
_GENERIC_TERMS = {
    "background", "backgrounds", "abstract", "wallpaper", "texture", "pattern",
    "business", "corporate", "office", "teamwork", "success", "motivation",
    "concept", "lifestyle", "people", "person", "man", "woman", "happy",
    "nature", "beautiful", "modern", "technology", "digital", "generic",
    "stock", "footage", "video", "image", "scene", "view", "shot", "cinematic",
    "4k", "hd",
}


def remove_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch)
    )


def normalize_tokens(text: str, min_len: int = 3) -> set[str]:
    """Tokens significativos: sem acento, minúsculos, sem stopword nem ruído."""
    clean = re.sub(r"[^a-z0-9\s]", " ", remove_accents(text).lower())
    return {
        tok for tok in clean.split()
        if len(tok) >= min_len and tok not in _STOPWORDS
    }


def is_generic_keyword(keyword: str) -> bool:
    """True quando a keyword é só termos genéricos de banco (sem âncora concreta)."""
    tokens = normalize_tokens(keyword, min_len=2)
    if not tokens:
        return True
    concrete = tokens - _GENERIC_TERMS
    return not concrete


def scene_concept_tokens(scene: dict) -> set[str]:
    """Saco de tokens que descreve o conceito visual da cena.

    Prioriza os campos em inglês (visual_goal, keywords, must_show), que casam
    melhor com a keyword (também inglês) que trouxe o asset; inclui a narração
    (pt-BR) como reforço fraco.
    """
    tokens: set[str] = set()
    tokens |= normalize_tokens(scene.get("visual_goal", ""))
    for kw in scene.get("keywords") or []:
        tokens |= normalize_tokens(str(kw))
    for item in scene.get("must_show") or []:
        tokens |= normalize_tokens(str(item))
    tokens |= normalize_tokens(scene.get("narration", ""))
    return tokens


def keyword_relevance(scene: dict, asset: dict) -> float:
    """Relevância textual em [0,1] entre a keyword do asset e o conceito da cena.

    1.0  = a keyword do asset está totalmente contida no conceito da cena.
    0.0  = nenhuma sobreposição (provável imagem fora de contexto).
    Penaliza colisão com must_not_show e keyword genérica.
    """
    kw_tokens = normalize_tokens(asset.get("keyword", ""))
    if not kw_tokens:
        return 0.0
    concept = scene_concept_tokens(scene)
    if not concept:
        return 0.0

    overlap = kw_tokens & concept
    base = len(overlap) / len(kw_tokens)

    # bônus se a keyword bate exatamente com a keyword principal da cena
    scene_keywords = [str(k).strip().lower() for k in (scene.get("keywords") or [])]
    asset_kw = str(asset.get("keyword", "")).strip().lower()
    if scene_keywords and asset_kw == scene_keywords[0]:
        base = min(1.0, base + 0.25)

    # penaliza se a keyword toca algo proibido pela cena
    forbidden: set[str] = set()
    for item in scene.get("must_not_show") or []:
        forbidden |= normalize_tokens(str(item))
    if kw_tokens & forbidden:
        base -= 0.4

    # keyword puramente genérica nunca deve marcar alta relevância
    if is_generic_keyword(asset.get("keyword", "")):
        base = min(base, 0.2)

    return max(0.0, min(1.0, base))


def relevance_label(score: float) -> str:
    """Rótulo curto pt-BR para badge de UI a partir de uma relevância [0,1]."""
    if score >= 0.66:
        return "alta"
    if score >= 0.33:
        return "média"
    return "baixa"


def asset_signature(asset: dict) -> tuple[str, str]:
    """Identidade do asset para deduplicar/diversificar entre cenas."""
    return (str(asset.get("source", "")), str(asset.get("source_id", "")))
