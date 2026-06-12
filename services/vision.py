"""Análise inteligente de imagem/asset, isolada atrás de uma interface.

Objetivo: dar à curadoria (automática e manual) uma pontuação de compatibilidade
entre o asset e a cena, com justificativa e detecção de problemas, sem acoplar o
resto do sistema a um provedor específico.

Provedores:
  - HeuristicVisionProvider (padrão): 100% offline. Usa os metadados do asset
    (resolução, proporção, tipo, duração) e a relevância textual da keyword vs a
    cena (services/scoring.py). Sempre disponível, determinístico e testável.
  - LLMVisionProvider (opcional): manda a thumbnail para um modelo de visão
    compatível com a API OpenAI (ex.: via OpenRouter) e interpreta a imagem de
    verdade. A chave é injetada pelo chamador — NUNCA fica hardcoded aqui.

Para trocar de provedor basta implementar `analyze()` e registrá-lo em
`get_provider()`. Falha de rede/IA do provedor LLM cai no heurístico.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Protocol

import requests

from . import scoring

logger = logging.getLogger("nwrch.vision")

# Free tiers (Groq/OpenRouter) limitam requisicoes; sem tratar o 429 a visao
# cairia em silencio na heuristica. Tentamos de novo respeitando o backoff.
_VISION_RETRY_STATUSES = {429, 500, 502, 503}
_VISION_MAX_RETRIES = 3
_VISION_BACKOFF = (2.0, 5.0, 10.0)


@dataclass
class VisionAnalysis:
    """Resultado da análise de um asset para uma cena."""
    asset_id: int
    score: float                       # 0-100, compatibilidade com a cena
    verdict: str                       # "ótimo" | "bom" | "fraco" | "descartar"
    relevance: float                   # 0-1, relevância textual (do scoring)
    reasons: list[str] = field(default_factory=list)   # por que serve
    flags: list[str] = field(default_factory=list)     # problemas detectados
    provider: str = "heuristic"

    def to_dict(self) -> dict:
        return asdict(self)


# Flags que sozinhas reprovam o asset (imagem fora do contexto da cena).
_DISCARD_FLAGS = {
    "irrelevante", "conteudo_proibido", "fora_do_tema",
    "pessoa_errada", "ambiente_errado", "tom_incompativel",
}
# Abaixo disso, mesmo sem flag, a imagem nao representa a cena -> descartar.
DISCARD_SCORE = 35.0


def _verdict_for(score: float, flags: list[str]) -> str:
    if _DISCARD_FLAGS & set(flags) or score < DISCARD_SCORE:
        return "descartar"
    if score >= 70:
        return "ótimo"
    if score >= 45:
        return "bom"
    return "fraco"


class VisionProvider(Protocol):
    name: str

    def analyze(self, asset: dict, scene: dict, config: dict) -> VisionAnalysis: ...


class HeuristicVisionProvider:
    """Análise offline a partir de metadados + relevância textual."""

    name = "heuristic"

    def analyze(self, asset: dict, scene: dict, config: dict) -> VisionAnalysis:
        reasons: list[str] = []
        flags: list[str] = []
        relevance = scoring.keyword_relevance(scene, asset)
        score = 40.0 * relevance  # relevância textual domina a base

        # --- relevância / contexto -------------------------------------------
        if relevance >= 0.66:
            reasons.append("keyword bem alinhada ao objetivo visual da cena")
        elif relevance < 0.2:
            flags.append("irrelevante")
            reasons.append("keyword pouco relacionada à cena")

        if scoring.is_generic_keyword(asset.get("keyword", "")):
            flags.append("keyword_generica")
            score -= 8.0

        # --- resolução --------------------------------------------------------
        try:
            target_w = int(str(config.get("resolution") or "1920x1080").split("x", 1)[0])
        except ValueError:
            target_w = 1920
        width = int(asset.get("width") or 0)
        height = int(asset.get("height") or 0)
        if width >= target_w:
            score += 22.0
            reasons.append("resolução cobre o alvo")
        elif width >= target_w * 0.66:
            score += 11.0
        else:
            flags.append("baixa_resolucao")

        # --- proporção / orientação ------------------------------------------
        if width and height:
            if height > width:
                flags.append("retrato")  # vertical estoura o enquadramento 16:9
            elif width / height >= 1.4:
                score += 8.0  # paisagem ampla, bom para B-roll + avatar

        # --- tipo de asset ----------------------------------------------------
        is_video = asset.get("asset_type") == "video"
        prefer_video = (config.get("asset_type_priority") or "video") == "video"
        if is_video == prefer_video:
            score += 12.0
        elif not is_video and not config.get("image_fallback"):
            flags.append("tipo_incompativel")
            score -= 8.0

        # --- cobertura de duração (vídeo) ------------------------------------
        if is_video:
            duration = float(asset.get("duration") or 0)
            scene_duration = float(scene.get("duration") or 0)
            if scene_duration and duration >= scene_duration:
                score += 14.0
                reasons.append("duração cobre a cena")
            elif scene_duration and duration < scene_duration * 0.5:
                flags.append("duracao_curta")

        # asset feito sob medida pelo usuário
        if asset.get("source") == "generated":
            score += 10.0
            reasons.append("imagem gerada sob medida para a cena")

        score = max(0.0, min(100.0, score))
        return VisionAnalysis(
            asset_id=int(asset.get("id") or 0),
            score=round(score, 1),
            verdict=_verdict_for(score, flags),
            relevance=round(relevance, 3),
            reasons=reasons,
            flags=flags,
            provider=self.name,
        )


class LLMVisionProvider:
    """Análise via modelo de visão (API compatível OpenAI, ex.: OpenRouter).

    Interpreta a imagem de fato e compara com a descrição da cena. Cai no
    heurístico em qualquer falha. A chave é recebida no construtor.
    """

    name = "llm-vision"
    DEFAULT_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "openai/gpt-4o-mini"

    def __init__(
        self,
        api_key: str,
        model: str = "",
        url: str = "",
        timeout: int = 60,
        name: str = "",
    ) -> None:
        self.api_key = api_key or ""
        self.model = model or self.DEFAULT_MODEL
        self.url = url or self.DEFAULT_URL
        self.timeout = timeout
        self.name = name or type(self).name
        self._fallback = HeuristicVisionProvider()

    def analyze(self, asset: dict, scene: dict, config: dict) -> VisionAnalysis:
        # Para vídeo usamos o poster (preview_url); o download_url de vídeo é o
        # .mp4 e não serve para um endpoint de visão. Imagens têm os dois.
        thumb = asset.get("preview_url")
        if asset.get("asset_type") != "video":
            thumb = thumb or asset.get("download_url")
        if not self.api_key or not thumb:
            # sem chave ou sem thumbnail analisável: heurística
            return self._fallback.analyze(asset, scene, config)
        try:
            return self._analyze_remote(asset, scene, config, thumb)
        except Exception as exc:  # noqa: BLE001 - fallback intencional
            logger.warning("Vision LLM erro, usando heurística: %s", exc)
            base = self._fallback.analyze(asset, scene, config)
            base.flags.append("vision_indisponivel")
            return base

    def _analyze_remote(self, asset: dict, scene: dict, config: dict, thumb: str) -> VisionAnalysis:
        prompt = (
            "You are a strict B-roll curator for a YouTube video. Look at the IMAGE "
            "and judge how well it ACTUALLY DEPICTS the scene below — by its visible "
            "content, not by hope.\n"
            f'Scene narration (pt-BR): {scene.get("narration", "")}\n'
            f'What the footage should show: {scene.get("visual_goal", "")}\n'
            f'Must NOT show: {", ".join(scene.get("must_not_show") or []) or "-"}\n\n"'
            "Scoring (be harsh): 85-100 = clearly shows it; 60-84 = related/works; "
            "35-59 = weak/tangential; 0-34 = does NOT show it / off-topic / wrong "
            "subject or place. If the visible content is off-topic, score under 35 "
            "and add the flag 'fora_do_tema'.\n"
            'Return JSON only: {"desc": "what the image literally shows, pt-BR, max 8 words", '
            '"score": 0-100, "reasons": ["pt-BR, why it fits or not"], '
            '"flags": ["irrelevante"|"fora_do_tema"|"baixa_qualidade"|"texto_logo"|'
            '"pessoa_errada"|"ambiente_errado"|"tom_incompativel"|"conteudo_proibido"]}'
        )
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": thumb}},
                ],
            }],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        resp = None
        for attempt in range(_VISION_MAX_RETRIES + 1):
            resp = requests.post(self.url, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code not in _VISION_RETRY_STATUSES:
                break
            if attempt < _VISION_MAX_RETRIES:
                # respeita Retry-After do provedor quando presente
                try:
                    wait = float(resp.headers.get("retry-after", ""))
                except (TypeError, ValueError):
                    wait = _VISION_BACKOFF[attempt]
                time.sleep(max(wait, _VISION_BACKOFF[attempt]))
        resp.raise_for_status()
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
        score = max(0.0, min(100.0, float(data.get("score") or 0)))
        flags = [str(f).strip() for f in (data.get("flags") or []) if str(f).strip()]
        reasons = [str(r).strip() for r in (data.get("reasons") or []) if str(r).strip()]
        desc = str(data.get("desc") or "").strip()
        if desc:
            reasons.insert(0, f"mostra: {desc}")
        return VisionAnalysis(
            asset_id=int(asset.get("id") or 0),
            score=round(score, 1),
            verdict=_verdict_for(score, flags),
            relevance=round(scoring.keyword_relevance(scene, asset), 3),
            reasons=reasons or ["avaliado pela IA de visão"],
            flags=flags,
            provider=self.name,
        )


# Visão pelo Groq (chave que o usuário já tem e que funciona): modelo Llama-4
# multimodal, gratuito no free tier e bom para julgar relevância de thumbnail.
GROQ_VISION_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


def get_provider(name: str = "heuristic", **kwargs) -> VisionProvider:
    """Fábrica de provedores. Default heurístico (offline, sem chave)."""
    if name == "groq" and kwargs.get("api_key"):
        return LLMVisionProvider(
            api_key=kwargs.get("api_key", ""),
            model=kwargs.get("model") or GROQ_VISION_MODEL,
            url=GROQ_VISION_URL,
            name="groq-vision",
        )
    if name in {"llm", "llm-vision", "openrouter"} and kwargs.get("api_key"):
        return LLMVisionProvider(
            api_key=kwargs.get("api_key", ""),
            model=kwargs.get("model", ""),
            url=kwargs.get("url", ""),
        )
    return HeuristicVisionProvider()


def analyze_candidates(
    scene: dict,
    assets: list[dict],
    config: dict,
    provider: Optional[VisionProvider] = None,
) -> dict[int, VisionAnalysis]:
    """Analisa todos os candidatos de uma cena. Retorna {asset_id: VisionAnalysis}."""
    provider = provider or HeuristicVisionProvider()
    out: dict[int, VisionAnalysis] = {}
    for asset in assets:
        analysis = provider.analyze(asset, scene, config)
        out[analysis.asset_id] = analysis
    return out
