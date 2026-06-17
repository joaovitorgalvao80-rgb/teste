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

import json
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


# Papéis das keywords por estratégia de busca (ordem = prioridade):
#   primary     -> depiction concreta e literal do assunto principal
#   alternative -> ângulo semântico diferente, igualmente no tema
#   fallback    -> consulta mais ampla, ainda no tema (rede de segurança)
def asset_context_tokens(asset: dict) -> set[str]:
    """Tokens offline usados como pista fraca do asset."""
    tokens: set[str] = set()
    for field in ("keyword", "source_id", "page_url", "author", "attribution"):
        tokens |= normalize_tokens(str(asset.get(field) or ""))
    tokens |= asset_visual_tokens(asset)
    return tokens


def asset_visual_tokens(asset: dict) -> set[str]:
    """Tokens vindos de metadados do resultado, nao da query que buscou o asset."""
    tokens: set[str] = set()
    for field in ("page_url", "author", "attribution"):
        tokens |= normalize_tokens(str(asset.get(field) or ""))
    payload = asset.get("provider_payload_json") or asset.get("provider_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {"raw": payload}
    if isinstance(payload, dict):
        for key in ("tags", "title", "alt", "description", "slug", "url", "page_url", "raw"):
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                value = " ".join(str(v) for v in value)
            tokens |= normalize_tokens(str(value or ""))
    return tokens


def _phrase_match(tokens: set[str], phrase: str) -> bool:
    phrase_tokens = normalize_tokens(str(phrase))
    return bool(phrase_tokens) and phrase_tokens <= tokens


_MOSQUITO_ALIASES = {"mosquito", "mosquitoes", "larvae", "larva", "insect", "aedes", "dengue"}
_MOSQUITO_FALSE_POSITIVES = {
    "bird", "birds", "duck", "ducks", "swan", "swans", "goose", "geese",
    "chicken", "chickens", "rooster", "egg", "eggs", "yolk", "fried",
    "flower", "flowers", "dog", "dogs", "cat", "cats", "child", "children",
    "girl", "boy", "woman", "man",
}


def context_risks(scene: dict, asset: dict) -> list[str]:
    visual = asset_visual_tokens(asset)
    risks: list[str] = []
    for item in scene.get("must_not_show") or []:
        if normalize_tokens(str(item)) & visual:
            risks.append(str(item))
    concept = scene_concept_tokens(scene)
    if concept & {"mosquito", "dengue", "aedes"}:
        false_hits = sorted(_MOSQUITO_FALSE_POSITIVES & visual)
        has_mosquito_signal = bool(_MOSQUITO_ALIASES & visual)
        if false_hits and not has_mosquito_signal:
            risks.append("falso positivo: " + ", ".join(false_hits[:3]))
    return risks


def context_analysis(scene: dict, asset: dict) -> dict:
    """Score de contexto em [0,1] com matched/missing/risks auditaveis."""
    visual_tokens = asset_visual_tokens(asset)
    must = [str(item).strip() for item in (scene.get("must_show") or []) if str(item).strip()]

    matched = [item for item in must if _phrase_match(visual_tokens, item)]
    missing = [item for item in must if item not in matched]
    risks = context_risks(scene, asset)

    keyword_score = keyword_relevance(scene, asset)
    if must:
        must_score = len(matched) / len(must)
        # A query que trouxe o asset e so uma pista; nao prova que o asset mostra
        # aquilo. Sem metadado visual confirmando must_show, capamos forte.
        score = 0.80 * must_score + 0.20 * keyword_score
        if not matched:
            score = min(score, 0.22)
        elif missing:
            score = min(score, 0.55)
    else:
        score = 0.55 * keyword_score
        if visual_tokens and scene_concept_tokens(scene) & visual_tokens:
            score += 0.25

    generic = is_generic_keyword(asset.get("keyword", ""))
    if risks:
        score -= 0.65
    if generic:
        score = min(score, 0.25)

    return {
        "context_score": round(max(0.0, min(1.0, score)), 3),
        "matched": matched,
        "missing": missing,
        "risks": risks + (["keyword_generica"] if generic else []),
    }


def context_relevance(scene: dict, asset: dict) -> float:
    return float(context_analysis(scene, asset)["context_score"])


ROLE_PRIMARY = "primary"
ROLE_ALTERNATIVE = "alternative"
ROLE_FALLBACK = "fallback"
KEYWORD_ROLES = [ROLE_PRIMARY, ROLE_ALTERNATIVE, ROLE_FALLBACK]
ROLE_LABELS_PT = {ROLE_PRIMARY: "principal", ROLE_ALTERNATIVE: "alternativa", ROLE_FALLBACK: "reserva"}


def assign_roles(keywords: list) -> list[str]:
    """Deriva o papel de cada keyword pela posição (1ª principal, 2ª alternativa,
    o resto reserva). Determinístico e alinhado à ordem produzida pela IA."""
    roles: list[str] = []
    for i in range(len(keywords or [])):
        roles.append(KEYWORD_ROLES[i] if i < len(KEYWORD_ROLES) else ROLE_FALLBACK)
    return roles


def keyword_role(scene: dict, query: str) -> str:
    """Papel da `query` dentro da cena (usa keyword_roles persistido ou posição)."""
    keywords = [str(k).strip().lower() for k in (scene.get("keywords") or [])]
    q = str(query or "").strip().lower()
    if q not in keywords:
        return ROLE_FALLBACK
    idx = keywords.index(q)
    roles = scene.get("keyword_roles") or assign_roles(keywords)
    if idx < len(roles) and roles[idx] in KEYWORD_ROLES:
        return roles[idx]
    return KEYWORD_ROLES[idx] if idx < len(KEYWORD_ROLES) else ROLE_FALLBACK


def keyword_relevance(scene: dict, asset: dict) -> float:
    """Relevância textual em [0,1] entre a keyword do asset e o conceito da cena.

    1.0  = a keyword do asset está totalmente contida no conceito da cena.
    0.0  = nenhuma sobreposição (provável imagem fora de contexto).
    Penaliza colisão com must_not_show, keyword genérica e match por reserva.
    """
    kw_tokens = normalize_tokens(asset.get("keyword", ""))
    if not kw_tokens:
        return 0.0
    concept = scene_concept_tokens(scene)
    if not concept:
        return 0.0

    overlap = kw_tokens & concept
    base = len(overlap) / len(kw_tokens)

    # bônus/penalidade pelo papel da keyword que trouxe o asset: a principal é a
    # mais precisa; a reserva é ampla de propósito, então vale um pouco menos.
    role = keyword_role(scene, asset.get("keyword", ""))
    if role == ROLE_PRIMARY:
        base = min(1.0, base + 0.25)
    elif role == ROLE_FALLBACK:
        base *= 0.9

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
