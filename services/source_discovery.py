"""Lightweight source discovery for rejected-scene rescue.

This module only orchestrates APIs that return candidate pages/images. It does
not scrape video, run ML, or perform heavy media analysis inside Railway.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests

from . import api_usage

logger = logging.getLogger("nwrch.source_discovery")

EXA_SEARCH_URL = "https://api.exa.ai/search"
FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
DEFAULT_TIMEOUT = 25
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
CONTENT_TYPE_JSON = "application/json"


def _env_enabled(name: str, default: str = "1") -> bool:
    raw = os.getenv(name, default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def discovery_enabled() -> bool:
    return _env_enabled("DEEP_RESEARCH_ENABLED", "1")


def _timeout() -> int:
    return _env_int("SOURCE_DISCOVERY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT, 5, 60)


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host.replace("www.", "")


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_image(url: str) -> bool:
    return urlparse(url).path.lower().endswith(IMAGE_EXTENSIONS)


def _post_json(provider: str, operation: str, url: str, *, headers: dict, payload: dict) -> dict:
    start = time.monotonic()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_timeout())
        api_usage.record(
            provider,
            operation,
            status_code=resp.status_code,
            ok=resp.status_code < 400,
            latency_ms=api_usage.elapsed_ms(start),
        )
        if resp.status_code >= 400:
            logger.warning("%s %s HTTP %s: %s", provider, operation, resp.status_code, resp.text[:180])
            return {}
        try:
            return resp.json() or {}
        except ValueError:
            api_usage.record(provider, operation, ok=False, detail="invalid_json")
            return {}
    except Exception as exc:  # noqa: BLE001
        api_usage.record(
            provider,
            operation,
            ok=False,
            latency_ms=api_usage.elapsed_ms(start),
            detail=type(exc).__name__,
        )
        logger.warning("%s %s failed: %s", provider, operation, exc)
        return {}


def _scene_terms(scene: dict) -> list[str]:
    terms: list[str] = []
    visual_goal = str(scene.get("visual_goal") or "").strip()
    if visual_goal:
        terms.append(visual_goal)
    for kw in scene.get("keywords") or []:
        kw = str(kw or "").strip()
        if kw and kw not in terms:
            terms.append(kw)
    if not terms:
        narration = re.sub(r"\s+", " ", str(scene.get("narration") or "")).strip()
        if narration:
            terms.append(narration[:140])
    capped: list[str] = []
    for term in terms:
        if len(capped) >= 4:
            break
        capped.append(term)
    return capped


def _query_for_scene(scene: dict) -> str:
    terms = _scene_terms(scene)
    if not terms:
        return ""
    query = " | ".join(terms)
    return f"{query} photo OR documentary still OR stock footage"


def _safe_payload(payload: dict) -> dict:
    keep = {
        "title",
        "url",
        "imageUrl",
        "image_url",
        "source",
        "description",
        "score",
        "width",
        "height",
    }
    return {k: v for k, v in payload.items() if k in keep and v not in (None, "")}


def _asset_from_image(
    *,
    image_url: str,
    page_url: str,
    keyword: str,
    provider: str,
    discovery_provider: str,
    payload: dict,
    scrape_url: str = "",
    scrape_status: str = "",
    confidence: float = 0.55,
) -> Optional[dict]:
    if not _is_http_url(image_url):
        return None
    author = payload.get("author") or payload.get("source") or _domain(page_url or image_url)
    title = payload.get("title") or payload.get("description") or ""
    return {
        "source": provider,
        "source_id": payload.get("id") or image_url,
        "asset_type": "image",
        "preview_url": image_url,
        "download_url": image_url,
        "page_url": page_url or payload.get("url") or image_url,
        "width": int(payload.get("width") or 0),
        "height": int(payload.get("height") or 0),
        "duration": 0,
        "keyword": keyword,
        "author": str(author or ""),
        "author_url": page_url or payload.get("url") or "",
        "license": payload.get("license") or "review_required",
        "license_url": payload.get("license_url") or "",
        "attribution": str(title or author or ""),
        "discovery_provider": discovery_provider,
        "scrape_url": scrape_url,
        "scrape_status": scrape_status,
        "provider_payload": _safe_payload(payload),
        "confidence": confidence,
    }


def _dedupe_assets(assets: Iterable[dict], seen_urls: set[str], limit: int) -> list[dict]:
    out: list[dict] = []
    for asset in assets:
        url = asset.get("download_url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        out.append(asset)
        if len(out) >= limit:
            break
    return out


def _split_firecrawl_results(root) -> tuple[list[dict], list[dict]]:
    image_items: list[dict] = []
    page_items: list[dict] = []
    if isinstance(root, dict):
        image_items.extend(root.get("images") or root.get("imageResults") or [])
        page_items.extend(root.get("web") or root.get("results") or [])
    elif isinstance(root, list):
        page_items.extend(root)
    return image_items, page_items


def _page_image_item(item: dict) -> Optional[dict]:
    image = item.get("imageUrl") or item.get("image_url") or item.get("image")
    if not image:
        return None
    return {**item, "imageUrl": image}


def _firecrawl_search(query: str, key: str, limit: int) -> tuple[list[dict], list[dict]]:
    if not key:
        return [], []
    payload = {
        "query": query,
        "limit": min(max(limit, 1), 10),
        "sources": ["web", "images"],
    }
    data = _post_json(
        "firecrawl",
        "search",
        FIRECRAWL_SEARCH_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": CONTENT_TYPE_JSON},
        payload=payload,
    )
    root = data.get("data") or data.get("results") or data
    image_items, page_items = _split_firecrawl_results(root)
    for item in page_items:
        if not isinstance(item, dict):
            continue
        image_item = _page_image_item(item)
        if image_item:
            image_items.append(image_item)
    return [i for i in image_items if isinstance(i, dict)], [p for p in page_items if isinstance(p, dict)]


def _scrape_root(data: dict) -> dict:
    root = data.get("data") if isinstance(data, dict) else {}
    if isinstance(root, dict):
        return root
    return data if isinstance(data, dict) else {}


def _image_entries_from_scrape(images: list, page_url: str) -> list[dict]:
    out: list[dict] = []
    for image in images:
        if isinstance(image, str):
            out.append({"imageUrl": image, "url": page_url})
            continue
        if isinstance(image, dict):
            out.append({**image, "url": image.get("url") or page_url})
    return out


def _link_url(link) -> str:
    if isinstance(link, str):
        return link
    if isinstance(link, dict):
        return str(link.get("url") or "")
    return ""


def _image_entries_from_links(links: list, page_url: str) -> list[dict]:
    out: list[dict] = []
    for link in links:
        raw = _link_url(link)
        if raw and _looks_like_image(raw):
            out.append({"imageUrl": raw, "url": page_url})
    return out


def _firecrawl_scrape_images(url: str, key: str) -> list[dict]:
    if not key or not _is_http_url(url):
        return []
    payload = {"url": url, "formats": ["links", "images"]}
    data = _post_json(
        "firecrawl",
        "scrape",
        FIRECRAWL_SCRAPE_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": CONTENT_TYPE_JSON},
        payload=payload,
    )
    root = _scrape_root(data)
    images = root.get("images") or root.get("imageUrls") or []
    links = root.get("links") or []
    return [
        *_image_entries_from_scrape(images, url),
        *_image_entries_from_links(links, url),
    ]


def _exa_search(query: str, key: str, limit: int) -> list[dict]:
    if not key:
        return []
    payload = {
        "query": query,
        "type": os.getenv("EXA_SEARCH_TYPE", "auto"),
        "numResults": min(max(limit, 1), 10),
        "contents": {"highlights": True},
    }
    data = _post_json(
        "exa",
        "search",
        EXA_SEARCH_URL,
        headers={"x-api-key": key, "Content-Type": CONTENT_TYPE_JSON},
        payload=payload,
    )
    results = data.get("results") if isinstance(data, dict) else []
    return [r for r in (results or []) if isinstance(r, dict)]


def _keyword_for_scene(scene: dict, query: str) -> str:
    keywords = [str(k).strip() for k in (scene.get("keywords") or []) if str(k).strip()]
    return keywords[0] if keywords else query


def _assets_from_firecrawl_images(items: list[dict], keyword: str) -> list[dict]:
    candidates: list[dict] = []
    for item in items:
        image = item.get("imageUrl") or item.get("image_url") or item.get("image")
        asset = _asset_from_image(
            image_url=image,
            page_url=item.get("url") or item.get("pageUrl") or "",
            keyword=keyword,
            provider="firecrawl",
            discovery_provider="firecrawl",
            payload=item,
            confidence=0.62,
        )
        if asset:
            candidates.append(asset)
    return candidates


def _assets_from_exa_image_pages(items: list[dict], keyword: str) -> list[dict]:
    candidates: list[dict] = []
    for item in items:
        url = item.get("url") or ""
        if not _looks_like_image(url):
            continue
        asset = _asset_from_image(
            image_url=url,
            page_url=url,
            keyword=keyword,
            provider="exa",
            discovery_provider="exa",
            payload=item,
            confidence=0.48,
        )
        if asset:
            candidates.append(asset)
    return candidates


def _unique_page_urls(items: list[dict]) -> list[str]:
    pages: list[str] = []
    for item in items:
        url = item.get("url") if isinstance(item, dict) else ""
        if url and _is_http_url(url) and url not in pages:
            pages.append(url)
    return pages


def _assets_from_scraped_pages(
    pages: list[str],
    exa_pages: list[dict],
    firecrawl_key: str,
    keyword: str,
    limit: int,
    existing_count: int,
) -> list[dict]:
    candidates: list[dict] = []
    exa_urls = {str(p.get("url") or "") for p in exa_pages}
    scrape_max = _env_int("FIRECRAWL_SCRAPE_MAX_PAGES", 3, 0, 8)
    for page_url in pages[:scrape_max]:
        if existing_count + len(candidates) >= limit:
            break
        for item in _firecrawl_scrape_images(page_url, firecrawl_key):
            image = item.get("imageUrl") or item.get("image_url")
            asset = _asset_from_image(
                image_url=image,
                page_url=page_url,
                keyword=keyword,
                provider="firecrawl",
                discovery_provider="exa+firecrawl" if page_url in exa_urls else "firecrawl",
                payload=item,
                scrape_url=page_url,
                scrape_status="scraped",
                confidence=0.58,
            )
            if asset:
                candidates.append(asset)
    return candidates


def discover_scene_assets(
    scene: dict,
    keys: dict,
    max_w: int,
    limit: int = 8,
    existing_urls: Optional[set[str]] = None,
) -> list[dict]:
    """Return normalized image assets for a rejected scene rescue pass."""
    del max_w  # kept for interface symmetry with asset_search.search_scene
    if not discovery_enabled():
        return []
    exa_key = keys.get("exa") or ""
    firecrawl_key = keys.get("firecrawl") or ""
    if not (exa_key or firecrawl_key):
        return []
    query = _query_for_scene(scene)
    if not query:
        return []
    seen = set(existing_urls or set())
    limit = min(max(int(limit or 1), 1), _env_int("DEEP_RESEARCH_MAX_RESULTS_PER_SCENE", 8, 1, 20))
    keyword = _keyword_for_scene(scene, query)

    candidates: list[dict] = []
    fire_images, fire_pages = _firecrawl_search(query, firecrawl_key, limit)
    candidates.extend(_assets_from_firecrawl_images(fire_images, keyword))

    exa_pages = _exa_search(query, exa_key, limit) if len(candidates) < limit else []
    candidates.extend(_assets_from_exa_image_pages(exa_pages, keyword))

    pages = _unique_page_urls([*exa_pages, *fire_pages])
    candidates.extend(_assets_from_scraped_pages(pages, exa_pages, firecrawl_key, keyword, limit, len(candidates)))

    return _dedupe_assets(candidates, seen, limit)
