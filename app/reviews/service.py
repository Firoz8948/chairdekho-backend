"""Google Business reviews via Places API (New), cached in memory.

Google bills per Place Details call, so results are reused for
GOOGLE_REVIEWS_CACHE_HOURS and failures back off instead of retrying per request.
"""

import asyncio
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

PLACES_BASE = "https://places.googleapis.com/v1"
DETAILS_FIELDS = "displayName,rating,userRatingCount,reviews,googleMapsUri"
FAILURE_RETRY_SECONDS = 600

_cache: dict = {"data": None, "expires_at": 0.0}
_resolved_place_id: str | None = None
_lock = asyncio.Lock()


def _write_review_url(place_id: str | None) -> str | None:
    if settings.GOOGLE_REVIEW_URL:
        return settings.GOOGLE_REVIEW_URL
    if place_id:
        return f"https://search.google.com/local/writereview?placeid={place_id}"
    return None


def _empty_payload(place_id: str | None = None) -> dict:
    return {
        "configured": bool(settings.GOOGLE_PLACES_API_KEY),
        "name": None,
        "rating": None,
        "review_count": None,
        "maps_url": None,
        "review_url": _write_review_url(place_id or settings.GOOGLE_PLACE_ID or None),
        "reviews": [],
    }


def _serialize_review(raw: dict) -> dict:
    author = raw.get("authorAttribution") or {}
    text = (raw.get("text") or raw.get("originalText") or {}).get("text") or ""
    return {
        "author": author.get("displayName") or "Google user",
        "author_url": author.get("uri"),
        "author_photo": author.get("photoUri"),
        "rating": raw.get("rating"),
        "text": text.strip(),
        "relative_time": raw.get("relativePublishTimeDescription"),
        "published_at": raw.get("publishTime"),
    }


async def _resolve_place_id(client: httpx.AsyncClient) -> str | None:
    global _resolved_place_id
    if settings.GOOGLE_PLACE_ID:
        return settings.GOOGLE_PLACE_ID
    if _resolved_place_id:
        return _resolved_place_id
    if not settings.GOOGLE_PLACE_QUERY:
        return None

    resp = await client.post(
        f"{PLACES_BASE}/places:searchText",
        headers={"X-Goog-FieldMask": "places.id,places.displayName"},
        json={"textQuery": settings.GOOGLE_PLACE_QUERY, "pageSize": 1},
    )
    resp.raise_for_status()
    places = resp.json().get("places") or []
    if places:
        _resolved_place_id = places[0].get("id")
        logger.info(
            "Google place resolved: %s (%s)",
            (places[0].get("displayName") or {}).get("text"),
            _resolved_place_id,
        )
    return _resolved_place_id


async def _fetch_from_google() -> dict:
    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY},
    ) as client:
        place_id = await _resolve_place_id(client)
        if not place_id:
            logger.warning("Google reviews: no place found for GOOGLE_PLACE_QUERY")
            return _empty_payload()

        resp = await client.get(
            f"{PLACES_BASE}/places/{place_id}",
            params={"languageCode": "en"},
            headers={"X-Goog-FieldMask": DETAILS_FIELDS},
        )
        resp.raise_for_status()
        body = resp.json()

    reviews = [_serialize_review(r) for r in body.get("reviews") or []]
    return {
        "configured": True,
        "name": (body.get("displayName") or {}).get("text"),
        "rating": body.get("rating"),
        "review_count": body.get("userRatingCount"),
        "maps_url": body.get("googleMapsUri"),
        "review_url": _write_review_url(place_id),
        "reviews": [r for r in reviews if r["text"]],
    }


async def get_google_reviews() -> dict:
    if not settings.GOOGLE_PLACES_API_KEY:
        return _empty_payload()

    now = time.time()
    if _cache["data"] is not None and now < _cache["expires_at"]:
        return _cache["data"]

    async with _lock:
        now = time.time()
        if _cache["data"] is not None and now < _cache["expires_at"]:
            return _cache["data"]
        try:
            data = await _fetch_from_google()
            _cache["data"] = data
            _cache["expires_at"] = now + settings.GOOGLE_REVIEWS_CACHE_HOURS * 3600
        except httpx.HTTPStatusError as exc:
            # Body carries Google's reason (e.g. API not enabled); it never echoes the key
            logger.warning(
                "Google reviews fetch failed: %s %s",
                exc.response.status_code,
                exc.response.text[:300],
            )
            _cache["data"] = _cache["data"] or _empty_payload()
            _cache["expires_at"] = now + FAILURE_RETRY_SECONDS
        except httpx.HTTPError as exc:
            logger.warning("Google reviews fetch failed: %s", type(exc).__name__)
            _cache["data"] = _cache["data"] or _empty_payload()
            _cache["expires_at"] = now + FAILURE_RETRY_SECONDS
        return _cache["data"]
