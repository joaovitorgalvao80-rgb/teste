"""Busca de assets em Pexels e Pixabay (videos e, opcionalmente, imagens).

Retorna metadados ricos para a galeria estilo Pinterest. O download real so
acontece na hora de gerar o ZIP, para nao desperdiciar banda em assets que o
usuario vai rejeitar.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

logger = logging.getLogger("nwrch.assets")

PEXELS_VIDEO_URL = "https://api.pexels.com/v1/videos/search"
PEXELS_IMAGE_URL = "https://api.pexels.com/v1/search"
PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE_URL = "https://pixabay.com/api/"
COVERR_URL = "https://api.coverr.co/videos"
COVERR_CDN = "https://cdn.coverr.co/videos"
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


def _pexels_get(url: str, *, headers: dict, params: dict):
    """GET a Pexels serializado, espacado e com retry/backoff em 401/429."""
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        with _PEXELS_GATE:
            wait = _PEXELS_MIN_INTERVAL - (time.monotonic() - _PEXELS_LAST[0])
            if wait > 0:
                time.sleep(wait)
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
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
    return sorted(pool, key=lambda f: (f.get("width", 0), f.get("height", 0)), reverse=True)[0]


def search_pexels_videos(keyword: str, key: str, max_w: int, per_page: int = 8) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page)
    try:
        resp = _pexels_get(
            PEXELS_VIDEO_URL,
            headers={"Authorization": key},
            params={"query": keyword, "orientation": "landscape", "per_page": per_page},
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
            }
        )
    return out


def search_pixabay_videos(keyword: str, key: str, max_w: int, per_page: int = 8) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page, default=8, minimum=3)
    try:
        resp = requests.get(
            PIXABAY_VIDEO_URL,
            params={"key": key, "q": keyword, "video_type": "film", "per_page": per_page, "safesearch": "true"},
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
        chosen = sorted(preferred or choices, key=lambda c: c.get("width", 0), reverse=True)[0]
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
            }
        )
    return out


def search_pexels_images(keyword: str, key: str, per_page: int = 6) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page, default=6)
    try:
        resp = _pexels_get(
            PEXELS_IMAGE_URL,
            headers={"Authorization": key},
            params={"query": keyword, "orientation": "landscape", "per_page": per_page},
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
            }
        )
    return out


def search_pixabay_images(keyword: str, key: str, per_page: int = 6) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page, default=6, minimum=3)
    try:
        resp = requests.get(
            PIXABAY_IMAGE_URL,
            params={"key": key, "q": keyword, "image_type": "photo", "orientation": "horizontal",
                    "per_page": per_page, "safesearch": "true"},
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
            }
        )
    return out


def search_coverr_videos(keyword: str, key: str, max_w: int, per_page: int = 8) -> list[dict]:
    if not key:
        return []
    per_page = _bounded_per_page(per_page)
    try:
        with _COVERR_GATE:
            resp = requests.get(
                COVERR_URL,
                params={"query": keyword, "page_size": per_page, "api_key": key},
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
            }
        )
    return out


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
) -> list[dict]:
    """Busca todas as keywords de uma cena e devolve candidatos deduplicados.

    media: "video" (so videos), "image" (so imagens) ou "all" (videos + imagens
    se allow_images). Quando media == "image", allow_images e ignorado.
    """
    seen = seen_urls if seen_urls is not None else set()
    results: list[dict] = []
    want_video = media in {"all", "video"}
    want_image = media == "image" or (media == "all" and allow_images)

    # Monta as buscas em ordem deterministica e executa em paralelo;
    # cada provedor ja devolve [] em caso de erro/chave ausente.
    tasks: list[Callable[[], list[dict]]] = []
    for i, kw in enumerate(keywords[:3]):
        if want_video:
            tasks.append(lambda kw=kw: search_pexels_videos(kw, pexels_key, max_w, per_keyword))
            tasks.append(lambda kw=kw: search_pixabay_videos(kw, pixabay_key, max_w, per_keyword))
            # Coverr so na keyword principal (limite ~50 req/hora)
            if coverr_key and i == 0:
                tasks.append(lambda kw=kw: search_coverr_videos(kw, coverr_key, max_w, per_keyword))
        if want_image:
            n = per_keyword if media == "image" else (per_keyword // 2 or 1)
            tasks.append(lambda kw=kw, n=n: search_pexels_images(kw, pexels_key, n))
            tasks.append(lambda kw=kw, n=n: search_pixabay_images(kw, pixabay_key, n))
    if not tasks:
        return results

    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
        batches = list(pool.map(lambda task: task(), tasks))

    # Dedupe sequencial preserva a mesma ordem do fluxo antigo.
    for batch in batches:
        for item in batch:
            url = item["download_url"]
            if url in seen:
                continue
            seen.add(url)
            results.append(item)
    return results
