"""Busca de assets em Pexels e Pixabay (videos e, opcionalmente, imagens).

Retorna metadados ricos para a galeria estilo Pinterest. O download real so
acontece na hora de gerar o ZIP, para nao desperdiciar banda em assets que o
usuario vai rejeitar.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

PEXELS_VIDEO_URL = "https://api.pexels.com/v1/videos/search"
PEXELS_IMAGE_URL = "https://api.pexels.com/v1/search"
PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE_URL = "https://pixabay.com/api/"
REQUEST_TIMEOUT = 25


def _bounded_per_page(value: int, default: int = 8) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 80))


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
        resp = requests.get(
            PEXELS_VIDEO_URL,
            headers={"Authorization": key},
            params={"query": keyword, "orientation": "landscape", "per_page": per_page},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Pexels video] erro: {exc}")
        return []
    if resp.status_code >= 400:
        print(f"[Pexels video] HTTP {resp.status_code}: {resp.text[:160]}")
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
    per_page = _bounded_per_page(per_page, default=8)
    try:
        resp = requests.get(
            PIXABAY_VIDEO_URL,
            params={"key": key, "q": keyword, "video_type": "film", "per_page": per_page, "safesearch": "true"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Pixabay video] erro: {exc}")
        return []
    if resp.status_code >= 400:
        print(f"[Pixabay video] HTTP {resp.status_code}: {resp.text[:160]}")
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
        resp = requests.get(
            PEXELS_IMAGE_URL,
            headers={"Authorization": key},
            params={"query": keyword, "orientation": "landscape", "per_page": per_page},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Pexels image] erro: {exc}")
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
    per_page = _bounded_per_page(per_page, default=6)
    try:
        resp = requests.get(
            PIXABAY_IMAGE_URL,
            params={"key": key, "q": keyword, "image_type": "photo", "orientation": "horizontal",
                    "per_page": per_page, "safesearch": "true"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Pixabay image] erro: {exc}")
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


def search_scene(
    keywords: list[str],
    pexels_key: str,
    pixabay_key: str,
    max_w: int,
    per_keyword: int = 8,
    allow_images: bool = False,
    seen_urls: Optional[set] = None,
    media: str = "all",
) -> list[dict]:
    """Busca todas as keywords de uma cena e devolve candidatos deduplicados.

    media: "video" (so videos), "image" (so imagens) ou "all" (videos + imagens
    se allow_images). Quando media == "image", allow_images e ignorado.
    """
    seen = seen_urls if seen_urls is not None else set()
    results: list[dict] = []
    want_video = media in {"all", "video"}
    want_image = media == "image" or (media == "all" and allow_images)
    for kw in keywords[:3]:
        batch: list[dict] = []
        if want_video:
            batch += search_pexels_videos(kw, pexels_key, max_w, per_keyword)
            batch += search_pixabay_videos(kw, pixabay_key, max_w, per_keyword)
        if want_image:
            n = per_keyword if media == "image" else (per_keyword // 2 or 1)
            batch += search_pexels_images(kw, pexels_key, n)
            batch += search_pixabay_images(kw, pixabay_key, n)
        for item in batch:
            url = item["download_url"]
            if url in seen:
                continue
            seen.add(url)
            results.append(item)
    return results
