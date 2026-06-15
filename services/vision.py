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

import base64
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Protocol

import requests

from . import api_usage, scoring

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

    def analyze_batch(
        self, assets: list[dict], scene: dict, config: dict
    ) -> dict[int, VisionAnalysis]: ...


def _heuristic_resolution_score(asset: dict, config: dict, reasons: list, flags: list) -> float:
    try:
        target_w = int(str(config.get("resolution") or "1920x1080").split("x", 1)[0])
    except ValueError:
        target_w = 1920
    width = int(asset.get("width") or 0)
    if width >= target_w:
        reasons.append("resolução cobre o alvo")
        return 22.0
    if width >= target_w * 0.66:
        return 11.0
    flags.append("baixa_resolucao")
    return 0.0


def _heuristic_aspect_score(asset: dict, flags: list) -> float:
    width = int(asset.get("width") or 0)
    height = int(asset.get("height") or 0)
    if width and height:
        if height > width:
            flags.append("retrato")  # vertical estoura o enquadramento 16:9
        elif width / height >= 1.4:
            return 8.0  # paisagem ampla, bom para B-roll + avatar
    return 0.0


def _heuristic_type_score(asset: dict, config: dict, flags: list) -> float:
    is_video = asset.get("asset_type") == "video"
    prefer_video = (config.get("asset_type_priority") or "video") == "video"
    if is_video == prefer_video:
        return 12.0
    if not is_video and not config.get("image_fallback"):
        flags.append("tipo_incompativel")
        return -8.0
    return 0.0


def _heuristic_duration_score(asset: dict, scene: dict, reasons: list, flags: list) -> float:
    if asset.get("asset_type") != "video":
        return 0.0
    duration = float(asset.get("duration") or 0)
    scene_duration = float(scene.get("duration") or 0)
    if scene_duration and duration >= scene_duration:
        reasons.append("duração cobre a cena")
        return 14.0
    if scene_duration and duration < scene_duration * 0.5:
        flags.append("duracao_curta")
    return 0.0


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

        score += _heuristic_resolution_score(asset, config, reasons, flags)
        score += _heuristic_aspect_score(asset, flags)
        score += _heuristic_type_score(asset, config, flags)
        score += _heuristic_duration_score(asset, scene, reasons, flags)

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

    def analyze_batch(self, assets: list[dict], scene: dict, config: dict) -> dict[int, VisionAnalysis]:
        return {a["id"]: self.analyze(a, scene, config) for a in assets}


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

    @staticmethod
    def _thumb_for(asset: dict) -> str:
        # Para vídeo usamos o poster (preview_url); o download_url de vídeo é o
        # .mp4 e não serve para um endpoint de visão. Imagens têm os dois.
        thumb = asset.get("preview_url")
        if asset.get("asset_type") != "video":
            thumb = thumb or asset.get("download_url")
        return thumb or ""

    def analyze(self, asset: dict, scene: dict, config: dict) -> VisionAnalysis:
        thumb = self._thumb_for(asset)
        if not self.api_key or not thumb:
            # sem chave ou sem thumbnail analisável: heurística
            return self._fallback.analyze(asset, scene, config)
        try:
            return self._analyze_remote(asset, scene, thumb)
        except Exception as exc:  # noqa: BLE001 - fallback intencional
            logger.warning("Vision LLM erro, usando heurística: %s", exc)
            base = self._fallback.analyze(asset, scene, config)
            base.flags.append("vision_indisponivel")
            return base

    @staticmethod
    def _theme_block(scene: dict) -> str:
        theme = str(scene.get("video_theme") or "").strip()
        if not theme:
            return ""
        return (
            f"The WHOLE video is about: {theme}. Judge every image INSIDE this topic. "
            "An image that only superficially matches a word but is OFF this topic must "
            "score under 35 with flag 'fora_do_tema' (e.g. chicken eggs in a video about "
            "mosquito eggs = fora_do_tema; a city street in a video about farming = fora_do_tema).\n"
        )

    @classmethod
    def _prompt(cls, scene: dict) -> str:
        return (
            "You are a strict B-roll curator for a YouTube video. Look at the IMAGE "
            "and judge how well it ACTUALLY DEPICTS the scene below — by its visible "
            "content, not by hope.\n"
            + cls._theme_block(scene)
            + f'Scene narration (pt-BR): {scene.get("narration", "")}\n'
            f'What the footage should show: {scene.get("visual_goal", "")}\n'
            f'Must NOT show: {", ".join(scene.get("must_not_show") or []) or "-"}\n\n'
            "Scoring (be harsh): 85-100 = clearly shows it; 60-84 = related/works; "
            "35-59 = weak/tangential; 0-34 = does NOT show it / off-topic / wrong "
            "subject or place. If the visible content is off-topic, score under 35 "
            "and add the flag 'fora_do_tema'.\n"
            'Return JSON only: {"desc": "what the image literally shows, pt-BR, max 8 words", '
            '"score": 0-100, "reasons": ["pt-BR, why it fits or not"], '
            '"flags": ["irrelevante"|"fora_do_tema"|"baixa_qualidade"|"texto_logo"|'
            '"pessoa_errada"|"ambiente_errado"|"tom_incompativel"|"conteudo_proibido"]}'
        )

    def _post_with_retry(self, payload: dict):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        resp = None
        for attempt in range(_VISION_MAX_RETRIES + 1):
            start = time.monotonic()
            resp = requests.post(self.url, headers=headers, json=payload, timeout=self.timeout)
            api_usage.record(
                self.name,
                "vision_analyze",
                status_code=resp.status_code,
                ok=resp.status_code < 400,
                latency_ms=api_usage.elapsed_ms(start),
            )
            if resp.status_code not in _VISION_RETRY_STATUSES:
                break
            if attempt < _VISION_MAX_RETRIES:
                try:
                    wait = float(resp.headers.get("retry-after", ""))
                except (TypeError, ValueError):
                    wait = _VISION_BACKOFF[attempt]
                time.sleep(max(wait, _VISION_BACKOFF[attempt]))
        resp.raise_for_status()
        return resp

    def _parse(self, asset: dict, scene: dict, content: str) -> VisionAnalysis:
        data = json.loads(content)
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

    def _analyze_remote(self, asset: dict, scene: dict, thumb: str) -> VisionAnalysis:
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self._prompt(scene)},
                    {"type": "image_url", "image_url": {"url": thumb}},
                ],
            }],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        resp = self._post_with_retry(payload)
        return self._parse(asset, scene, resp.json()["choices"][0]["message"]["content"])

    # --- contact-sheet: julga varias candidatas numa unica chamada -----------
    @classmethod
    def _batch_prompt(cls, scene: dict, n: int) -> str:
        return (
            "You are a STRICT B-roll curator for a YouTube video.\n"
            + cls._theme_block(scene)
            + f"Below are {n} candidate images for ONE scene, in order (image 1 first).\n"
            f'Scene narration (pt-BR): {scene.get("narration", "")}\n'
            f'What the footage should show: {scene.get("visual_goal", "")}\n'
            f'Must NOT show: {", ".join(scene.get("must_not_show") or []) or "-"}\n\n'
            "For EACH image judge how well it ACTUALLY depicts the scene WITHIN the video topic. "
            "Be harsh: 85-100 clearly shows it; 60-84 related/works; 35-59 weak/tangential; "
            "0-34 off-topic / wrong subject or place (add flag 'fora_do_tema').\n"
            f'Return JSON only, exactly {n} items in the same order: '
            '{"items":[{"n":1,"desc":"pt-BR max 8 words","score":0-100,'
            '"reasons":["pt-BR"],"flags":["irrelevante"|"fora_do_tema"|"baixa_qualidade"|'
            '"texto_logo"|"pessoa_errada"|"ambiente_errado"|"tom_incompativel"|"conteudo_proibido"]}]}'
        )

    def _parse_item(self, asset: dict, scene: dict, item: dict) -> VisionAnalysis:
        score = max(0.0, min(100.0, float(item.get("score") or 0)))
        flags = [str(f).strip() for f in (item.get("flags") or []) if str(f).strip()]
        reasons = [str(r).strip() for r in (item.get("reasons") or []) if str(r).strip()]
        desc = str(item.get("desc") or "").strip()
        if desc:
            reasons.insert(0, f"mostra: {desc}")
        return VisionAnalysis(
            asset_id=int(asset.get("id") or 0),
            score=round(score, 1),
            verdict=_verdict_for(score, flags),
            relevance=round(scoring.keyword_relevance(scene, asset), 3),
            reasons=reasons or ["avaliado pela IA de visão (lote)"],
            flags=flags,
            provider=self.name,
        )

    def analyze_batch(self, assets: list[dict], scene: dict, config: dict) -> dict[int, VisionAnalysis]:
        """Julga ate N candidatas de uma cena numa UNICA chamada (contact-sheet).

        Manda o contexto da cena + o tema do video + as N thumbnails numeradas e
        recebe um veredito por imagem. Compara as candidatas entre si dentro do
        tema -> mais rigor e ~1 chamada/cena (em vez de N). Qualquer falha cai no
        analyze() individual para nao deixar a cena sem analise.
        """
        usable = [(a, self._thumb_for(a)) for a in assets]
        usable = [(a, t) for a, t in usable if t]
        if not self.api_key or len(usable) < 2:
            return {a["id"]: self.analyze(a, scene, config) for a in assets}
        try:
            content = [{"type": "text", "text": self._batch_prompt(scene, len(usable))}]
            for _, thumb in usable:
                content.append({"type": "image_url", "image_url": {"url": thumb}})
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            }
            resp = self._post_with_retry(payload)
            data = json.loads(resp.json()["choices"][0]["message"]["content"])
            items = data.get("items") or []
            out: dict[int, VisionAnalysis] = {}
            for idx, (asset, _) in enumerate(usable):
                item = items[idx] if idx < len(items) else {}
                out[asset["id"]] = self._parse_item(asset, scene, item)
            # candidatas sem thumbnail caem na heuristica
            for asset in assets:
                if asset["id"] not in out:
                    out[asset["id"]] = self._fallback.analyze(asset, scene, config)
            return out
        except Exception as exc:  # noqa: BLE001 - fallback intencional
            logger.warning("Vision batch erro, caindo no individual: %s", exc)
            return {a["id"]: self.analyze(a, scene, config) for a in assets}


class NvidiaVisionProvider(LLMVisionProvider):
    """Visão via NVIDIA NIM (build.nvidia.com). Os VLMs da NVIDIA exigem a imagem
    em base64 embutida (<img src="data:..."/>), nao a URL remota. Baixamos a
    thumbnail, encodamos e mandamos inline. Cai na heuristica em falha/limite."""

    name = "nvidia-vision"
    NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
    NVIDIA_MODEL = "meta/llama-3.2-11b-vision-instruct"
    MAX_IMG_BYTES = 180 * 1024  # limite pratico do endpoint para imagem inline

    def __init__(self, api_key: str, model: str = "", timeout: int = 60) -> None:
        super().__init__(api_key=api_key, model=model or self.NVIDIA_MODEL,
                         url=self.NVIDIA_URL, timeout=timeout, name=self.name)

    def _analyze_remote(self, asset: dict, scene: dict, thumb: str) -> VisionAnalysis:
        start = time.monotonic()
        img_resp = requests.get(thumb, timeout=self.timeout)
        api_usage.record(
            "asset-thumbnail",
            "nvidia_thumbnail_fetch",
            status_code=img_resp.status_code,
            ok=img_resp.status_code < 400,
            latency_ms=api_usage.elapsed_ms(start),
        )
        img_resp.raise_for_status()
        img = img_resp.content
        if not img or len(img) > self.MAX_IMG_BYTES:
            raise RuntimeError(f"thumbnail invalida/grande para NVIDIA ({len(img)} bytes)")
        b64 = base64.b64encode(img).decode()
        content = self._prompt(scene) + f' <img src="data:image/jpeg;base64,{b64}" />'
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
            "max_tokens": 300,
        }
        resp = self._post_with_retry(payload)
        raw = resp.json()["choices"][0]["message"]["content"]
        # NVIDIA nem sempre respeita JSON puro; extrai o objeto do texto.
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        return self._parse(asset, scene, raw)

    def analyze_batch(self, assets: list[dict], scene: dict, config: dict) -> dict[int, VisionAnalysis]:
        # O endpoint da NVIDIA limita a imagem inline (base64); um contact-sheet
        # com varias imagens estoura o contexto. Mantemos o lane individual.
        return {a["id"]: self.analyze(a, scene, config) for a in assets}


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
    if name == "nvidia" and kwargs.get("api_key"):
        return NvidiaVisionProvider(api_key=kwargs.get("api_key", ""), model=kwargs.get("model", ""))
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
