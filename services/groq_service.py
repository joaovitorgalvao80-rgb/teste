"""Geracao de intencao visual + keywords por cena usando Groq.

A IA recebe cada bloco (com timestamp e narracao) e devolve, para cada cena:
  visual_goal, keywords (ingles), must_show, must_not_show, asset_type, overlay_text.

Se a chave Groq nao estiver disponivel ou a chamada falhar, usamos um
fallback heuristico (PT->EN) para nao travar o fluxo no MVP.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests

from . import api_usage, scoring
from .project_config import DEFAULT_LANGUAGE, language_name, language_whisper_code
from .script_parser import remove_accents

logger = logging.getLogger("nwrch.groq")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
CONTENT_TYPE_JSON = "application/json"
BRIEF_BATCH_SIZE = 40  # cenas por chamada; roteiros longos estouram o contexto num prompt unico
GROQ_MODELS = [
    ("llama-3.3-70b-versatile", "Llama 3.3 70B — melhor qualidade (padrão)"),
    ("llama-3.1-8b-instant",    "Llama 3.1 8B Instant — mais rápido"),
    ("openai/gpt-oss-120b",     "GPT-OSS 120B — raciocínio forte"),
    ("openai/gpt-oss-20b",      "GPT-OSS 20B — rápido e barato"),
]
# Modelos removidos da API da Groq; redireciona para o padrao em vez de falhar.
DECOMMISSIONED_MODELS = {"mixtral-8x7b-32768", "gemma2-9b-it", "llama3-70b-8192", "llama3-8b-8192"}


def _tracked_post(operation: str, *args, **kwargs):
    import time

    start = time.monotonic()
    try:
        resp = requests.post(*args, **kwargs)
        api_usage.record(
            "groq",
            operation,
            status_code=resp.status_code,
            ok=resp.status_code < 400,
            latency_ms=api_usage.elapsed_ms(start),
        )
        return resp
    except Exception as exc:
        api_usage.record(
            "groq",
            operation,
            ok=False,
            latency_ms=api_usage.elapsed_ms(start),
            detail=type(exc).__name__,
        )
        raise


def resolve_model(model: str) -> str:
    model = (model or "").strip()
    if not model or model in DECOMMISSIONED_MODELS:
        return DEFAULT_MODEL
    return model

def _overlay_from(text: str) -> str:
    clean = remove_accents(text)
    words = re.sub(r"[^A-Za-z0-9\s]", " ", clean).split()
    words = [w for w in words if len(w) > 2][:5]
    return " ".join(words).upper()


# Fillers de fallback por zona narrativa: menos genéricos que um único default.
_ZONE_FALLBACK = {
    "GANCHO": ["close up detail", "real situation"],
    "CTA": ["person taking action", "hands working"],
    "DESENVOLVIMENTO": ["close up detail", "real environment"],
}

_SEARCH_TRANSLATIONS = {
    "agua": "water",
    "parada": "stagnant",
    "quintal": "backyard",
    "casa": "home",
    "rua": "street",
    "cidade": "city",
    "mosquito": "mosquito",
    "dengue": "mosquito",
    "larva": "larvae",
    "larvas": "larvae",
    "ovo": "eggs",
    "ovos": "eggs",
    "chuva": "rain",
    "balde": "bucket",
    "pneu": "tire",
    "vaso": "plant pot",
    "planta": "plant",
    "crianca": "child",
    "criancas": "children",
    "idoso": "elderly person",
    "idosos": "elderly people",
    "maos": "hands",
    "mao": "hand",
    "pessoa": "person",
    "pessoas": "people",
    "familia": "family",
    "dinheiro": "money",
    "mercado": "market",
    "saude": "health",
    "medico": "doctor",
    "hospital": "hospital",
    "celular": "smartphone",
    "telefone": "smartphone",
    "computador": "computer",
    "documento": "document",
}

_BAD_SEARCH_WORDS = {
    "abstract", "background", "concept", "conceptual", "cinematic", "documentary",
    "footage", "generic", "image", "moment", "scene", "shot", "stock", "video",
    "visual", "view",
}


def _translated_tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", remove_accents(str(text or "")).lower())
    out: list[str] = []
    for token in raw:
        if len(token) < 3 or token.isdigit():
            continue
        mapped = _SEARCH_TRANSLATIONS.get(token, token)
        for part in mapped.split():
            if part and part not in out and part not in _BAD_SEARCH_WORDS:
                out.append(part)
    return out


def _clean_query_phrase(phrase: str) -> str:
    tokens = _translated_tokens(phrase)
    tokens = [t for t in tokens if t not in _BAD_SEARCH_WORDS]
    return " ".join(tokens[:4]).strip()


def _fallback_queries(scene: dict) -> list[str]:
    text = " ".join(
        str(scene.get(k) or "")
        for k in ("visual_target", "visual_goal", "narration")
    )
    tokens = _translated_tokens(text)
    queries: list[str] = []

    def add(words: list[str]) -> None:
        phrase = " ".join(w for w in words if w)[:80].strip()
        if phrase and phrase not in queries:
            queries.append(phrase)

    must = _translated_tokens(" ".join(scene.get("must_show") or []))
    if must:
        add(must[:4])
    if "mosquito" in tokens and "water" in tokens:
        add(["mosquito", "stagnant", "water"])
        add(["mosquito", "larvae", "water"])
        add(["standing", "water", "backyard"])
    add(tokens[:4])
    add(tokens[1:5])
    for fb in _ZONE_FALLBACK.get(scene.get("zone", ""), ["close up detail", "real environment"]):
        add(_translated_tokens(fb))
    return queries[:5]


def _sanitize_query_ladder(raw: list[str], scene: dict, fallback: list[str]) -> list[str]:
    out: list[str] = []
    for phrase in raw or []:
        clean = _clean_query_phrase(str(phrase))
        if not clean:
            continue
        parts = clean.split()
        if len(parts) == 1 and parts[0] in _BAD_SEARCH_WORDS:
            continue
        if clean not in out:
            out.append(clean)
    for phrase in fallback or _fallback_queries(scene):
        clean = _clean_query_phrase(str(phrase))
        if clean and clean not in out:
            out.append(clean)
    return out[:5]


def normalized_scene_queries(scene: dict) -> list[str]:
    """Escada buscavel e saneada para cenas novas ou antigas."""
    raw = scene.get("query_ladder") or scene.get("keywords") or []
    if isinstance(raw, str):
        raw = [raw]
    return _sanitize_query_ladder(raw, scene, _fallback_queries(scene))


def classify_scene_editorial(scene: dict) -> dict:
    text = str(scene.get("narration") or "")
    tokens = scoring.normalize_tokens(text, min_len=4)
    has_number = any(ch.isdigit() for ch in text)
    concrete = {
        "mosquito", "agua", "water", "quintal", "bucket", "larva", "larvas",
        "casa", "street", "rua", "plant", "animal", "person", "hands",
        "food", "map", "chart", "document", "machine", "car", "city",
    }
    bridge = {"agora", "entao", "mas", "conclusao", "resumo", "importante", "obrigado"}
    if len(tokens) <= 4 or tokens <= bridge:
        return {"screen_mode": "avatar_only", "visual_need": 0.18, "visual_strategy": "none"}
    if has_number or tokens & concrete:
        return {"screen_mode": "broll", "visual_need": 0.82, "visual_strategy": "literal"}
    return {"screen_mode": "optional_broll", "visual_need": 0.45, "visual_strategy": "evidence"}


def fallback_scene_brief(scene: dict, avatar_safe_area: str) -> dict:
    """Brief determinístico (sem IA), usado só quando a Groq falha.

    Sem tradução PT->EN confiável offline, usa os tokens significativos da
    narração (já sem stopwords) e fillers por zona narrativa — evitando despejar
    palavras de ligação em português numa API de busca em inglês. O caminho
    normal é a Groq (generate_briefs), que produz keywords concretas em inglês.
    """
    text = scene.get("narration", "")
    keywords: list[str] = []
    # tokens significativos (sem stopwords PT/EN), não só "len > 4"
    tokens = [t for t in scoring.normalize_tokens(text, min_len=4) if not t.isdigit()]
    if tokens:
        keywords.append(" ".join(sorted(tokens)[:4]))
    for fb in _ZONE_FALLBACK.get(scene.get("zone", ""), ["close up detail", "real environment"]):
        if fb not in keywords:
            keywords.append(fb)
    editorial = classify_scene_editorial(scene)
    visual_target = keywords[0] if keywords else f"editorial evidence for {text}"[:80]
    query_ladder = _sanitize_query_ladder(keywords, scene, _fallback_queries(scene))
    return {
        "scene_id": scene["scene_id"],
        "visual_goal": f"Concrete editorial B-roll for: {text}"[:240],
        "screen_mode": editorial["screen_mode"],
        "visual_need": editorial["visual_need"],
        "visual_strategy": editorial["visual_strategy"],
        "visual_target": visual_target,
        "keywords": query_ladder[:3],
        "query_ladder": query_ladder,
        "must_show": [],
        "must_not_show": ["corporate stock", "watermark", "text inside footage"],
        "asset_type": "video",
        "overlay_text": _overlay_from(text),
        "avatar_safe_area": avatar_safe_area,
    }


def infer_video_theme(
    scenes: list[dict], groq_key: str, model: str = DEFAULT_MODEL, language: str = DEFAULT_LANGUAGE
) -> str:
    """Resume o ASSUNTO do vídeo inteiro numa frase curta em inglês.

    Esse tema vira a âncora de contexto: tanto a geração de keywords quanto a IA
    de visão usam ele para rejeitar imagens que só batem superficialmente com uma
    palavra mas estão fora do tema (ex.: ovo de galinha num vídeo sobre dengue).
    Determinístico no fallback (sem chave/erro): junta os tokens mais frequentes.
    """
    narration = " ".join(str(s.get("narration") or "") for s in scenes).strip()
    if not narration:
        return ""
    if groq_key:
        try:
            resp = _tracked_post(
                "infer_theme",
                GROQ_URL,
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": CONTENT_TYPE_JSON},
                json={
                    "model": resolve_model(model),
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"Read this {language_name(language)} video script and state its MAIN SUBJECT "
                            "in ONE short English sentence (max 14 words). Be concrete and specific "
                            "(the actual topic, place, domain), not generic.\n"
                            'Return JSON only: {"theme": "..."}\n\nSCRIPT:\n' + narration[:6000]
                        ),
                    }],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
                timeout=60,
            )
            if resp.status_code < 400:
                theme = str(json.loads(resp.json()["choices"][0]["message"]["content"]).get("theme") or "").strip()
                if theme:
                    return theme[:160]
        except Exception as exc:  # noqa: BLE001 - fallback intencional
            logger.warning("infer_video_theme erro, usando fallback: %s", exc)
    # fallback determinístico: palavras significativas MAIS FREQUENTES.
    # normalize_tokens devolve um set (sem frequência), então contamos as
    # ocorrências no texto cru, mantendo só os tokens válidos (sem stopwords).
    from collections import Counter
    valid = {t for t in scoring.normalize_tokens(narration, min_len=4) if not t.isdigit()}
    words = re.findall(r"[a-z0-9]+", remove_accents(narration).lower())
    counts = Counter(w for w in words if w in valid)
    common = [w for w, _ in counts.most_common(6)]
    return (" ".join(common)).strip()


def _build_prompt(
    scenes: list[dict],
    style: str,
    avatar_safe_area: str,
    safe_ratio: float,
    video_theme: str = "",
    language: str = DEFAULT_LANGUAGE,
) -> str:
    lang_name = language_name(language)
    blocks = []
    for s in scenes:
        blocks.append(
            f'{s["scene_id"]} [{s["start_time"]:.1f}s-{s["end_time"]:.1f}s]: {s.get("narration","")}'
        )
    joined = "\n".join(blocks)
    theme_line = (
        f"\nThe WHOLE video is about: {video_theme}. Every scene must stay strictly ON THIS topic. "
        "Never let a keyword drift to a superficially-similar but off-topic subject "
        "(e.g. if the video is about mosquito eggs, NEVER search generic 'eggs' that returns chicken eggs).\n"
        if video_theme else ""
    )
    return f"""You are a senior YouTube editor planning a 100% B-roll video from a {lang_name} script.
{theme_line}
For EACH scene below, produce concrete visual search intent for Pexels/Pixabay.

Return ONE JSON object only, shape:
{{
  "scenes": [
    {{
      "scene_id": "scene_001",
      "screen_mode": "avatar_only | broll | hybrid | optional_broll",
      "visual_need": 0.0,
      "visual_strategy": "literal | evidence | environment | symbolic | none",
      "visual_target": "short concrete visual target",
      "visual_goal": "what footage should appear here, concrete, in English",
      "query_ladder": ["primary searchable phrase", "alternative phrase", "evidence/context phrase"],
      "keywords": ["same as first three query_ladder items"],
      "must_show": ["concrete element", "concrete element"],
      "must_not_show": ["thing to reject", "thing to reject"],
      "asset_type": "video",
      "overlay_text": "{lang_name} uppercase, max 5 words, may be empty"
    }}
  ]
}}

Keyword strategy (CRITICAL — bad keywords cause irrelevant footage):
- Provide exactly 3 English search phrases, ORDERED by strategy:
  1) PRIMARY: the most concrete, literal depiction of the scene's main subject/action.
  2) SEMANTIC ALTERNATIVE: a different but equally on-topic angle (other object,
     environment or point of view of the same idea).
  3) SAFE FALLBACK: a broader but still on-theme phrase that is likely to return
     results even if the first two are too niche.
- 2 to 4 words each. Concrete and filmable. NEVER single generic words like
  "background", "business", "concept", "abstract", "people", "nature".
- If the narration is METAPHORICAL or idiomatic, search for the REAL underlying
  meaning, not the literal words (e.g. "the economy is heating up" -> "stock
  market trading floor", NOT "fire"). Put the intended meaning in visual_goal.
- Match the right footage TYPE to the idea: a person/action, an environment, a
  close-up object, or an abstract/conceptual B-roll — pick what fits the scene.
- Visual style requested: {style}.
- Avatar safe area is on the {avatar_safe_area} (~{safe_ratio*100:.0f}% of width). Keep main action and text away from it.
- must_not_show should always include watermark / text inside footage / generic corporate stock.
- Return one scene object per input scene, same scene_id, same order.
- Use avatar_only for greetings, transitions, conclusions, short abstract claims, and lines with no natural footage.
- Use optional_broll for hard literal scenes; prefer evidence/context queries instead of impossible scientific wording.

SCENES:
{joined}
"""


def _fetch_briefs_from_groq(
    scenes: list[dict],
    groq_key: str,
    style: str,
    avatar_safe_area: str,
    safe_ratio: float,
    model: str,
    video_theme: str,
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, dict]:
    """Chama a Groq em lotes e devolve {scene_id: brief_bruto} para as cenas cobertas."""
    by_id: dict[str, dict] = {}
    # em lotes: roteiros longos (100+ cenas) nao cabem numa unica resposta JSON
    for start in range(0, len(scenes), BRIEF_BATCH_SIZE):
        chunk = scenes[start:start + BRIEF_BATCH_SIZE]
        try:
            resp = _tracked_post(
                "generate_briefs",
                GROQ_URL,
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": CONTENT_TYPE_JSON},
                json={
                    "model": resolve_model(model),
                    "messages": [
                        {"role": "user", "content": _build_prompt(chunk, style, avatar_safe_area, safe_ratio, video_theme, language)}
                    ],
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
                timeout=180,
            )
            if resp.status_code < 400:
                content = resp.json()["choices"][0]["message"]["content"]
                data = json.loads(content)
                for item in data.get("scenes", []):
                    sid = item.get("scene_id")
                    if sid:
                        by_id[sid] = item
            else:
                logger.warning("Groq HTTP %s: %s", resp.status_code, resp.text[:300])
        except Exception as exc:  # noqa: BLE001 - fallback intencional
            logger.warning("Groq erro, usando fallback para o lote %s+: %s", start, exc)
    return by_id


def _merge_brief(scene: dict, ai: dict, fb: dict, avatar_safe_area: str) -> dict:
    """Combina o brief da IA com o fallback, garantindo campos completos."""
    query_ladder = ai.get("query_ladder") or ai.get("keywords") or fb["query_ladder"]
    if isinstance(query_ladder, str):
        query_ladder = [query_ladder]
    query_ladder = _sanitize_query_ladder(query_ladder, {**scene, **ai}, fb["query_ladder"])
    keywords = ai.get("keywords") or query_ladder[:3] or fb["keywords"]
    if isinstance(keywords, str):
        keywords = [keywords]
    keywords = _sanitize_query_ladder(keywords, {**scene, **ai}, query_ladder)[:3]
    screen_mode = str(ai.get("screen_mode") or fb["screen_mode"]).strip()
    if screen_mode not in {"avatar_only", "broll", "hybrid", "optional_broll"}:
        screen_mode = fb["screen_mode"]
    strategy = str(ai.get("visual_strategy") or fb["visual_strategy"]).strip()
    if strategy not in {"literal", "evidence", "environment", "symbolic", "none"}:
        strategy = fb["visual_strategy"]
    try:
        visual_need = max(0.0, min(1.0, float(ai.get("visual_need", fb["visual_need"]))))
    except (TypeError, ValueError):
        visual_need = fb["visual_need"]
    return {
        "scene_id": scene["scene_id"],
        "visual_goal": (ai.get("visual_goal") or fb["visual_goal"]).strip(),
        "screen_mode": screen_mode,
        "visual_need": visual_need,
        "visual_strategy": strategy,
        "visual_target": str(ai.get("visual_target") or fb["visual_target"]).strip()[:160],
        "keywords": keywords or query_ladder[:3] or fb["keywords"],
        "query_ladder": query_ladder,
        "must_show": [str(k).strip() for k in (ai.get("must_show") or ai.get("must_have") or []) if str(k).strip()],
        "must_not_show": [str(k).strip() for k in (ai.get("must_not_show") or ai.get("avoid") or fb["must_not_show"]) if str(k).strip()],
        "asset_type": ai.get("asset_type") or "video",
        "overlay_text": str(ai.get("overlay_text") or "").upper()[:60],
        "avatar_safe_area": avatar_safe_area,
    }


def generate_briefs(
    scenes: list[dict],
    groq_key: str,
    style: str,
    avatar_safe_area: str,
    safe_ratio: float = 0.30,
    model: str = DEFAULT_MODEL,
    video_theme: str = "",
    language: str = DEFAULT_LANGUAGE,
) -> list[dict]:
    """Devolve uma lista de briefs (1 por cena), sempre completa.

    Usa Groq quando ha chave; cai no fallback heuristico cena a cena para
    qualquer cena que a IA nao tenha coberto. `video_theme` ancora as keywords
    no assunto do video inteiro (evita drift de tema).
    """
    by_id = (
        _fetch_briefs_from_groq(scenes, groq_key, style, avatar_safe_area, safe_ratio, model, video_theme, language)
        if groq_key else {}
    )

    briefs: list[dict] = []
    for scene in scenes:
        ai = by_id.get(scene["scene_id"])
        fb = fallback_scene_brief(scene, avatar_safe_area)
        briefs.append(_merge_brief(scene, ai, fb, avatar_safe_area) if ai else fb)
    return briefs


def _fmt_stamp(seconds: float) -> str:
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m:02d}:{s:04.1f}"


def transcribe_audio(
    audio_bytes: bytes, filename: str, groq_key: str, language: str = DEFAULT_LANGUAGE
) -> str:
    """Transcreve áudio via Groq Whisper e retorna o roteiro no formato de timestamps.

    Retorna string pronta pra colar no campo de roteiro:
        [00:00.0 - 00:04.2] Você sabia que existe uma forma...
        [00:04.2 - 00:08.0] O problema é que quase ninguém...
    """
    if not groq_key:
        raise ValueError("Chave Groq necessária para transcrição de áudio.")

    resp = _tracked_post(
        "transcribe_audio",
        GROQ_TRANSCRIBE_URL,
        headers={"Authorization": f"Bearer {groq_key}"},
        files={"file": (filename, audio_bytes, "application/octet-stream")},
        data={
            "model": "whisper-large-v3-turbo",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
            "language": language_whisper_code(language),
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    segments = data.get("segments") or []
    if not segments:
        # fallback: retorna o texto completo sem timestamps
        return data.get("text", "").strip()

    lines = []
    for seg in segments:
        start = float(seg.get("start", 0))
        end = float(seg.get("end", start))
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"[{_fmt_stamp(start)} - {_fmt_stamp(end)}] {text}")
    return "\n".join(lines)


def regenerate_keywords(
    narration: str,
    visual_goal: str,
    groq_key: str,
    style: str,
    model: str = DEFAULT_MODEL,
    language: str = DEFAULT_LANGUAGE,
    rejected_assets: Optional[list[dict]] = None,
) -> list[str]:
    """Gera um novo conjunto de keywords para uma unica cena (botao 'gerar novas keywords')."""
    if groq_key:
        try:
            rejected_lines = []
            for item in rejected_assets or []:
                reason = str(item.get("rejection_reason") or item.get("reason") or "").strip()
                keyword = str(item.get("keyword") or "").strip()
                source = str(item.get("source") or "").strip()
                bit = " / ".join(p for p in [keyword, source, reason] if p)
                if bit:
                    rejected_lines.append("- " + bit[:140])
            rejected_block = (
                "\nPreviously rejected results to avoid repeating:\n" + "\n".join(rejected_lines[:8]) + "\n"
                if rejected_lines else ""
            )
            prompt = (
                "Generate 3 FRESH English video search phrases for Pexels/Pixabay for this scene, "
                "different from obvious literal terms (the previous results were rejected).\n"
                "Order them: 1) most concrete primary, 2) a different semantic angle, "
                "3) a broader safe fallback. 2-4 words each, no generic single words "
                "(background/business/concept). If the narration is metaphorical, search the "
                "real underlying meaning, not the literal words.\n"
                f"Visual style: {style}.\n"
                f"Narration ({language_name(language)}): {narration}\n"
                f"Visual goal: {visual_goal}\n"
                f"{rejected_block}"
                'Return JSON only: {"keywords": ["...", "...", "..."]}'
            )
            resp = _tracked_post(
                "regenerate_keywords",
                GROQ_URL,
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": CONTENT_TYPE_JSON},
                json={
                    "model": resolve_model(model),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.6,
                    "response_format": {"type": "json_object"},
                },
                timeout=90,
            )
            if resp.status_code < 400:
                data = json.loads(resp.json()["choices"][0]["message"]["content"])
                kws = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
                if kws:
                    scene = {"narration": narration, "visual_goal": visual_goal, "must_show": []}
                    return _sanitize_query_ladder(kws, scene, _fallback_queries(scene))[:3]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Groq regenerate erro: %s", exc)
    return fallback_scene_brief({"scene_id": "x", "narration": narration}, "right")["keywords"]
