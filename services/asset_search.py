"""Busca de assets em Pexels e Pixabay (videos e, opcionalmente, imagens).

Retorna metadados ricos para a galeria estilo Pinterest. O download real so
acontece na hora de gerar o ZIP, para nao desperdiciar banda em assets que o
usuario vai rejeitar.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from . import api_usage, scoring

logger = logging.getLogger("nwrch.assets")

PEXELS_VIDEO_URL = "https://api.pexels.com/v1/videos/search"
PEXELS_IMAGE_URL = "https://api.pexels.com/v1/search"
PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE_URL = "https://pixabay.com/api/"
COVERR_URL = "https://api.coverr.co/videos"
COVERR_CDN = "https://cdn.coverr.co/videos"
OPENVERSE_URL = "https://api.openverse.org/v1/images/"
WIKIMEDIA_URL = "https://commons.wikimedia.org/w/api.php"
# Wikimedia exige um User-Agent identificavel (politica de etiqueta da API).
WIKIMEDIA_UA = "NWRCH-Studio/1.0 (b-roll curation; contact via app)"
REQUEST_TIMEOUT = 25

# Coverr tem limite apertado (~50 req/hora): serializa e usamos so a keyword
# principal por cena (ver search_scene). A URL do MP4 e deterministica a partir
# do base_filename, entao NAO gastamos request extra para baixar.
_COVERR_GATE = threading.Semaphore(1)

# A Pexels rejeita rajadas concorrentes/seguidas com 401 "Invalid API key"
# (nao 429). search_scene dispara ate 8 requests em paralelo, entao a maioria
# das chamadas Pexels falhava silenciosamente (cada provedor devolve [] em erro),
# degradando a curadoria sem avisar. Aqui serializamos as chamadas Pexels, damos
# um intervalo minimo entre elas e tentamos de novo com backoff no transiente.
# Chaves muito limitadas (free tier estressado) ainda podem cair para Pixabay,
# mas agora isso fica registrado em log em vez de sumir em silencio.
_PEXELS_GATE = threading.Semaphore(1)
_PEXELS_MIN_INTERVAL = float(os.getenv("PEXELS_MIN_INTERVAL", "0.34"))
_PEXELS_LAST = [0.0]
_RETRY_STATUSES = {401, 429}
_MAX_RETRIES = 2
_RETRY_BACKOFF = (1.5, 3.0)


def _tracked_get(provider: str, operation: str, url: str, **kwargs):
    start = time.monotonic()
    try:
        resp = requests.get(url, **kwargs)
        api_usage.record(
            provider,
            operation,
            status_code=resp.status_code,
            ok=resp.status_code < 400,
            latency_ms=api_usage.elapsed_ms(start),
        )
        return resp
    except Exception as exc:
        api_usage.record(
            provider,
            operation,
            ok=False,
            latency_ms=api_usage.elapsed_ms(start),
            detail=type(exc).__name__,
        )
        raise


def _pexels_get(url: str, *, headers: dict, params: dict, operation: str = "request"):
    """GET a Pexels serializado, espacado e com retry/backoff em 401/429."""
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        with _PEXELS_GATE:
            wait = _PEXELS_MIN_INTERVAL - (time.monotonic() - _PEXELS_LAST[0])
            if wait > 0:
                time.sleep(wait)
            resp = _tracked_get("pexels", operation, url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            _PEXELS_LAST[0] = time.monotonic()
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_BACKOFF[attempt])
    logger.warning(
        "Pexels respondeu %s apos %d tentativas (chave possivelmente limitada/invalida); "
        "caindo para os demais provedores nesta busca.",
        resp.status_code if resp is not None else "erro", _MAX_RETRIES + 1,
    )
    return resp


def _bounded_per_page(value: int, default: int = 8, minimum: int = 1) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, 80))


def _with_query_param(url: str, key: str, value: str) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    return urlunparse(parts._replace(query=urlencode(query)))


def _best_pexels_video_file(video: dict, max_w: int) -> Optional[dict]:
    files = video.get("video_files", [])
    landscape = [f for f in files if f.get("width", 0) >= f.get("height", 0) and f.get("link")]
    if not landscape:
        return None
    preferred = [f for f in landscape if f.get("width", 0) <= max_w]
    pool = preferred or landscape
    return max(pool, key=lambda f: (f.get("width", 0), f.get("height", 0)))


def search_pexels_videos(keyword: str, key: str, max_w: int, per_page: int = 8, page: int = 1) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page)
    try:
        resp = _pexels_get(
            PEXELS_VIDEO_URL,
            headers={"Authorization": key},
            params={"query": keyword, "orientation": "landscape", "per_page": per_page, "page": max(1, int(page or 1))},
            operation="video_search",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pexels video erro: %s", exc)
        return []
    if resp.status_code >= 400:
        logger.warning("Pexels video HTTP %s: %s", resp.status_code, resp.text[:160])
        return []
    out = []
    for v in resp.json().get("videos", []):
        chosen = _best_pexels_video_file(v, max_w)
        if not chosen:
            continue
        picture = v.get("image", "")
        out.append(
            {
                "source": "pexels",
                "source_id": v.get("id", ""),
                "asset_type": "video",
                "preview_url": picture,
                "download_url": chosen["link"],
                "page_url": v.get("url", ""),
                "width": chosen.get("width", 0),
                "height": chosen.get("height", 0),
                "duration": v.get("duration", 0),
                "keyword": keyword,
                "author": (v.get("user") or {}).get("name", ""),
                "author_url": (v.get("user") or {}).get("url", ""),
                "provider_payload": {
                    "url": v.get("url", ""),
                    "image": picture,
                },
            }
        )
    return out


def search_pixabay_videos(keyword: str, key: str, max_w: int, per_page: int = 8, page: int = 1) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page, default=8, minimum=3)
    try:
        resp = _tracked_get(
            "pixabay",
            "video_search",
            PIXABAY_VIDEO_URL,
            params={"key": key, "q": keyword, "video_type": "film", "per_page": per_page,
                    "page": max(1, int(page or 1)), "safesearch": "true"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pixabay video erro: %s", exc)
        return []
    if resp.status_code >= 400:
        logger.warning("Pixabay video HTTP %s: %s", resp.status_code, resp.text[:160])
        return []
    out = []
    for hit in resp.json().get("hits", []):
        videos = hit.get("videos", {})
        choices = [videos.get(k) for k in ["large", "medium", "small", "tiny"] if videos.get(k)]
        choices = [c for c in choices if c.get("url") and c.get("width", 0) >= c.get("height", 0)]
        if not choices:
            continue
        preferred = [c for c in choices if c.get("width", 0) <= max_w]
        chosen = max(preferred or choices, key=lambda c: c.get("width", 0))
        # thumbnail do pixabay
        thumb = (videos.get("tiny") or {}).get("thumbnail", "") or (videos.get("small") or {}).get("thumbnail", "")
        out.append(
            {
                "source": "pixabay",
                "source_id": hit.get("id", ""),
                "asset_type": "video",
                "preview_url": thumb,
                "download_url": _with_query_param(chosen["url"], "download", "1"),
                "page_url": hit.get("pageURL", ""),
                "width": chosen.get("width", 0),
                "height": chosen.get("height", 0),
                "duration": hit.get("duration", 0),
                "keyword": keyword,
                "author": hit.get("user", ""),
                "author_url": f"https://pixabay.com/users/{hit.get('user','')}-{hit.get('user_id','')}/",
                "provider_payload": {
                    "tags": hit.get("tags", ""),
                    "page_url": hit.get("pageURL", ""),
                },
            }
        )
    return out


def search_pexels_images(keyword: str, key: str, per_page: int = 6, page: int = 1) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page, default=6)
    try:
        resp = _pexels_get(
            PEXELS_IMAGE_URL,
            headers={"Authorization": key},
            params={"query": keyword, "orientation": "landscape", "per_page": per_page, "page": max(1, int(page or 1))},
            operation="image_search",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pexels image erro: %s", exc)
        return []
    if resp.status_code >= 400:
        return []
    out = []
    for p in resp.json().get("photos", []):
        src = p.get("src", {})
        out.append(
            {
                "source": "pexels",
                "source_id": p.get("id", ""),
                "asset_type": "image",
                "preview_url": src.get("medium", src.get("small", "")),
                "download_url": src.get("large2x") or src.get("large") or src.get("original", ""),
                "page_url": p.get("url", ""),
                "width": p.get("width", 0),
                "height": p.get("height", 0),
                "duration": 0,
                "keyword": keyword,
                "author": p.get("photographer", ""),
                "author_url": p.get("photographer_url", ""),
                "provider_payload": {
                    "alt": p.get("alt", ""),
                    "url": p.get("url", ""),
                },
            }
        )
    return out


def search_pixabay_images(keyword: str, key: str, per_page: int = 6, page: int = 1) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page, default=6, minimum=3)
    try:
        resp = _tracked_get(
            "pixabay",
            "image_search",
            PIXABAY_IMAGE_URL,
            params={"key": key, "q": keyword, "image_type": "photo", "orientation": "horizontal",
                    "per_page": per_page, "page": max(1, int(page or 1)), "safesearch": "true"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pixabay image erro: %s", exc)
        return []
    if resp.status_code >= 400:
        return []
    out = []
    for hit in resp.json().get("hits", []):
        out.append(
            {
                "source": "pixabay",
                "source_id": hit.get("id", ""),
                "asset_type": "image",
                "preview_url": hit.get("webformatURL", hit.get("previewURL", "")),
                "download_url": hit.get("largeImageURL") or hit.get("webformatURL", ""),
                "page_url": hit.get("pageURL", ""),
                "width": hit.get("imageWidth", 0),
                "height": hit.get("imageHeight", 0),
                "duration": 0,
                "keyword": keyword,
                "author": hit.get("user", ""),
                "author_url": f"https://pixabay.com/users/{hit.get('user','')}-{hit.get('user_id','')}/",
                "provider_payload": {
                    "tags": hit.get("tags", ""),
                    "page_url": hit.get("pageURL", ""),
                },
            }
        )
    return out


def search_coverr_videos(keyword: str, key: str, per_page: int = 8, page: int = 1) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page)
    try:
        with _COVERR_GATE:
            resp = _tracked_get(
                "coverr",
                "video_search",
                COVERR_URL,
                params={"query": keyword, "page_size": per_page, "page": max(1, int(page or 1)), "api_key": key},
                timeout=REQUEST_TIMEOUT,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Coverr erro: %s", exc)
        return []
    if resp.status_code >= 400:
        logger.warning("Coverr HTTP %s: %s", resp.status_code, resp.text[:160])
        return []
    out = []
    for hit in resp.json().get("hits", []):
        if hit.get("is_vertical"):
            continue  # queremos paisagem 16:9
        bf = hit.get("base_filename")
        if not bf:
            continue
        out.append(
            {
                "source": "coverr",
                "source_id": str(hit.get("id", "")),
                "asset_type": "video",
                "preview_url": hit.get("thumbnail") or hit.get("poster", ""),
                # URL deterministica do MP4 (sem request extra)
                "download_url": f"{COVERR_CDN}/{bf}/1080p.mp4",
                "page_url": f"https://coverr.co/videos/{hit.get('slug', '')}",
                "width": int(hit.get("max_width") or 1920),
                "height": int(hit.get("max_height") or 1080),
                "duration": float(hit.get("duration") or 0),
                "keyword": keyword,
                "author": "Coverr",
                "author_url": "https://coverr.co",
                "provider_payload": {
                    "title": hit.get("title", ""),
                    "slug": hit.get("slug", ""),
                    "tags": hit.get("tags", ""),
                },
            }
        )
    return out


def search_openverse_images(keyword: str, per_page: int = 5) -> list[dict]:
    """Imagens CC do Openverse (agregador: Flickr, museus, etc.). Sem chave
    (uso anonimo, com rate limit). Fallback dirigido para cenas com pool fraco."""
    per_page = _bounded_per_page(per_page, default=5, minimum=1)
    try:
        resp = _tracked_get(
            "openverse",
            "image_search",
            OPENVERSE_URL,
            params={"q": keyword, "page_size": per_page, "aspect_ratio": "wide",
                    "mature": "false", "license_type": "all"},
            headers={"User-Agent": WIKIMEDIA_UA},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Openverse erro: %s", exc)
        return []
    if resp.status_code >= 400:
        logger.warning("Openverse HTTP %s: %s", resp.status_code, resp.text[:160])
        return []
    out = []
    for hit in resp.json().get("results", []):
        url = hit.get("url")
        width, height = int(hit.get("width") or 0), int(hit.get("height") or 0)
        if not url or (width and height and height > width):
            continue  # so paisagem
        out.append(
            {
                "source": "openverse",
                "source_id": str(hit.get("id", "")),
                "asset_type": "image",
                "preview_url": hit.get("thumbnail") or url,
                "download_url": url,
                "page_url": hit.get("foreign_landing_url", ""),
                "width": width,
                "height": height,
                "duration": 0,
                "keyword": keyword,
                "author": hit.get("creator", "") or "",
                "author_url": hit.get("creator_url", "") or "",
                "provider_payload": {
                    "title": hit.get("title", ""),
                    "tags": hit.get("tags", []),
                },
            }
        )
    return out


def _wikimedia_item(page: dict, keyword: str) -> Optional[dict]:
    info = (page.get("imageinfo") or [{}])[0]
    mime = str(info.get("mime") or "")
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        return None  # pula SVG/PDF/audio/video
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    if width and height and height > width:
        return None  # so paisagem
    meta = info.get("extmetadata") or {}
    artist = str((meta.get("Artist") or {}).get("value") or "")
    artist = re.sub(r"<[^>]+>", "", artist).strip()[:120]
    return {
        "source": "wikimedia",
        "source_id": str(page.get("pageid", "")),
        "asset_type": "image",
        "preview_url": info.get("thumburl") or info.get("url", ""),
        "download_url": info.get("thumburl") or info.get("url", ""),
        "page_url": info.get("descriptionurl", ""),
        "width": int(info.get("thumbwidth") or width),
        "height": int(info.get("thumbheight") or height),
        "duration": 0,
        "keyword": keyword,
        "author": artist or "Wikimedia Commons",
        "author_url": info.get("descriptionurl", ""),
        "provider_payload": {
            "title": (meta.get("ObjectName") or {}).get("value", ""),
            "description": (meta.get("ImageDescription") or {}).get("value", ""),
        },
    }


def search_wikimedia_images(keyword: str, max_w: int, per_page: int = 5) -> list[dict]:
    """Fotos do Wikimedia Commons. Excelente para assuntos factuais/cientificos
    (especies, fenomenos) que os bancos de stock nao cobrem. Sem chave."""
    per_page = _bounded_per_page(per_page, default=5, minimum=1)
    iiurlwidth = max(640, min(int(max_w or 1280), 1920))
    try:
        resp = _tracked_get(
            "wikimedia",
            "image_search",
            WIKIMEDIA_URL,
            params={
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": keyword, "gsrnamespace": 6, "gsrlimit": per_page,
                "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
                "iiurlwidth": iiurlwidth,
            },
            headers={"User-Agent": WIKIMEDIA_UA},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wikimedia erro: %s", exc)
        return []
    if resp.status_code >= 400:
        logger.warning("Wikimedia HTTP %s: %s", resp.status_code, resp.text[:160])
        return []
    pages = (resp.json().get("query") or {}).get("pages") or {}
    out = []
    for page in pages.values():
        item = _wikimedia_item(page, keyword)
        if item:
            out.append(item)
    return out


def _collect_provider_tasks(
    keywords: list[str],
    pexels_key: str,
    pixabay_key: str,
    coverr_key: str,
    max_w: int,
    per_keyword: int,
    want_video: bool,
    want_image: bool,
    media: str,
    page: int = 1,
) -> list[Callable[[], list[dict]]]:
    """Monta as buscas mainstream em ordem deterministica (executadas em paralelo)."""
    tasks: list[Callable[[], list[dict]]] = []
    for i, kw in enumerate(keywords[:3]):
        if want_video:
            tasks.append(lambda kw=kw, page=page: search_pexels_videos(kw, pexels_key, max_w, per_keyword, page=page))
            tasks.append(lambda kw=kw, page=page: search_pixabay_videos(kw, pixabay_key, max_w, per_keyword, page=page))
            # Coverr so na keyword principal (limite ~50 req/hora)
            if coverr_key and i == 0:
                tasks.append(lambda kw=kw, page=page: search_coverr_videos(kw, coverr_key, per_keyword, page=page))
        if want_image:
            n = per_keyword if media == "image" else (per_keyword // 2 or 1)
            tasks.append(lambda kw=kw, n=n, page=page: search_pexels_images(kw, pexels_key, n, page=page))
            tasks.append(lambda kw=kw, n=n, page=page: search_pixabay_images(kw, pixabay_key, n, page=page))
    return tasks


def _collect_extra_image_tasks(keywords: list[str], max_w: int) -> list[Callable[[], list[dict]]]:
    """Fallback dirigido: Wikimedia Commons + Openverse para pool fraco."""
    extra_tasks: list[Callable[[], list[dict]]] = []
    for kw in keywords[:2]:
        extra_tasks.append(lambda kw=kw: search_wikimedia_images(kw, max_w, 4))
        extra_tasks.append(lambda kw=kw: search_openverse_images(kw, 4))
    return extra_tasks


def _query_role(index: int) -> str:
    if index == 0:
        return "primary"
    if index == 1:
        return "alternative"
    if index == 2:
        return "evidence"
    return "fallback"


def _context_floor(context: dict) -> float:
    return 0.55 if context.get("risks") else 0.33


def _is_contextual_enough(scene: Optional[dict], asset: dict) -> bool:
    if not scene:
        return True
    context = scoring.context_analysis(scene, asset)
    return float(context["context_score"]) >= _context_floor(context)


def _good_context_count(scene: Optional[dict], assets: list[dict]) -> int:
    if not scene:
        return len(assets)
    return sum(1 for asset in assets if _is_contextual_enough(scene, asset))


def _sort_and_trim_by_context(scene: Optional[dict], assets: list[dict], per_keyword: int) -> list[dict]:
    """Promote context-matching candidates and hide obvious false positives.

    Stock APIs often return many pretty but unrelated hits for a keyword. When
    we have at least one plausible candidate, keep the pool focused; when every
    result is weak, return the sorted weak pool so the user can still recover
    manually instead of seeing an empty scene.
    """
    if not scene or not assets:
        return assets
    annotated = []
    for idx, asset in enumerate(assets):
        context = scoring.context_analysis(scene, asset)
        score = float(context["context_score"])
        risks = context.get("risks") or []
        acceptable = score >= _context_floor(context)
        annotated.append((acceptable, score, bool(risks), idx, asset))

    accepted = [row for row in annotated if row[0]]
    if accepted:
        weak_clean = [row for row in annotated if not row[0] and not row[2]]
        keep = accepted + weak_clean[:max(0, max(4, per_keyword) - len(accepted))]
    elif scoring.requires_visual_evidence(scene):
        return []
    else:
        keep = annotated

    keep.sort(key=lambda row: (row[0], row[1], not row[2], -row[3]), reverse=True)
    return [row[4] for row in keep]


def search_scene(
    keywords: list[str],
    pexels_key: str,
    pixabay_key: str,
    max_w: int,
    per_keyword: int = 8,
    allow_images: bool = False,
    seen_urls: Optional[set] = None,
    media: str = "all",
    coverr_key: str = "",
    extra_image_banks: bool = False,
    page: int = 1,
    query_role_prefix: str = "",
    scene: Optional[dict] = None,
) -> list[dict]:
    """Busca todas as keywords de uma cena e devolve candidatos deduplicados.

    media: "video" (so videos), "image" (so imagens) ou "all" (videos + imagens
    se allow_images). Quando media == "image", allow_images e ignorado.

    extra_image_banks: habilita Wikimedia Commons + Openverse como FALLBACK
    DIRIGIDO — so sao consultados quando o pool mainstream da cena vem fraco
    (poucos candidatos), para nao inchar o pool nem pressionar a visao.
    """
    seen = seen_urls if seen_urls is not None else set()
    results: list[dict] = []
    want_video = media in {"all", "video"}
    want_image = media == "image" or (media == "all" and allow_images)

    def _absorb(batches: list[list[dict]], role: str = "", query: str = "") -> None:
        for batch in batches:
            for item in batch:
                url = item["download_url"]
                if url in seen:
                    continue
                seen.add(url)
                if query_role_prefix:
                    item["query_role"] = f"{query_role_prefix}_{role}" if role else query_role_prefix
                else:
                    item.setdefault("query_role", role)
                item.setdefault("query_text", query or item.get("keyword", ""))
                results.append(item)

    # Busca em escada: tenta a query principal primeiro e so avanca enquanto o
    # pool ainda estiver fraco. Cada provedor ja devolve [] em erro/chave ausente.
    min_good = max(6, per_keyword)
    max_raw = 40
    clean_keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    for idx, kw in enumerate(clean_keywords[:5]):
        role = _query_role(idx)
        tasks = _collect_provider_tasks(
            [kw], pexels_key, pixabay_key, coverr_key, max_w, per_keyword, want_video, want_image, media, page=page
        )
        if tasks:
            with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
                _absorb(list(pool.map(lambda task: task(), tasks)), role=role, query=kw)
        if _good_context_count(scene, results) >= min_good:
            break
        if len(results) >= max_raw:
            break

    # Fallback dirigido: so consulta bancos extras (Wikimedia/Openverse) quando o
    # pool mainstream veio fraco. Mantem o pool enxuto e a visao dentro do limite.
    if extra_image_banks and keywords and _good_context_count(scene, results) < max(4, per_keyword):
        extra_tasks = _collect_extra_image_tasks(keywords, max_w)
        with ThreadPoolExecutor(max_workers=min(4, len(extra_tasks))) as pool:
            _absorb(list(pool.map(lambda task: task(), extra_tasks)), role="fallback", query=keywords[0])

    return _sort_and_trim_by_context(scene, results[:max_raw], per_keyword)
