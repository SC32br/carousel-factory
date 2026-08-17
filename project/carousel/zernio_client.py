"""Публикация через Zernio: один createPost, несколько площадок.

Схема из docs.zernio.com: POST /media/presign → PUT файла по uploadUrl →
publicUrl в mediaItems / customMedia → POST /posts.

Instagram-карусель и Threads проверены в бою. 9 слайдов 4:5 также уходят альбомом Telegram и мультифото Facebook.
TikTok берёт фотосет ИЛИ одно видео. YouTube - одно видео. Pinterest - один пин.
MAX и VK в Zernio нет.
"""
from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

import requests

from . import config

# Сколько элементов уходит на площадку и в каком виде.
# https://docs.zernio.com/guides/media-uploads
CAROUSEL_PLATFORMS = {"instagram", "telegram", "facebook", "threads", "linkedin"}
VIDEO_OR_PHOTOS_PLATFORMS = {"tiktok"}  # нельзя мешать фото и видео
VIDEO_ONLY_PLATFORMS = {"youtube"}
SINGLE_PIN_PLATFORMS = {"pinterest"}

CAPTION_LIMITS = {
    "instagram": 2200,
    "threads": 500,
    "facebook": 63206,
    "telegram": 1024,  # подпись к медиа-альбому
    "tiktok": 2200,
    "youtube": 5000,
    "pinterest": 500,
    "linkedin": 3000,
}

_URL_HINTS = (
    "instagram.com", "t.me/", "telegram.me", "tiktok.com", "youtube.com",
    "youtu.be", "pinterest.com", "facebook.com", "fb.com", "threads.net",
    "linkedin.com",
)


class ZernioError(RuntimeError):
    pass


def _headers(*, request_id: str | None = None) -> dict:
    key = config.require("ZERNIO_API_KEY", config.ZERNIO_API_KEY)
    headers = {"Authorization": f"Bearer {key}"}
    if request_id:
        headers["x-request-id"] = request_id
    return headers


def targets() -> list[tuple[str, str]]:
    if not config.ZERNIO_TARGETS:
        raise ZernioError(
            "В .env пустой ZERNIO_TARGETS (формат platform:accountId через запятую). "
            "Instagram можно задать отдельно через ZERNIO_INSTAGRAM_ACCOUNT_ID."
        )
    return list(config.ZERNIO_TARGETS)


def targets_configured() -> bool:
    return bool(config.ZERNIO_API_KEY and config.ZERNIO_TARGETS)


def upload_media(path: Path) -> str:
    """Файл → публичный URL через presigned upload zernio."""
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "application/octet-stream"
    resp = requests.post(
        f"{config.ZERNIO_API_URL}/media/presign",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"filename": path.name, "contentType": mime},
        timeout=60,
    )
    if resp.status_code >= 400:
        raise ZernioError(f"zernio presign HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    upload_url, public_url = data.get("uploadUrl"), data.get("publicUrl")
    if not upload_url or not public_url:
        raise ZernioError(f"zernio presign без uploadUrl/publicUrl: {data}")

    with path.open("rb") as fh:
        put = requests.put(upload_url, data=fh, headers={"Content-Type": mime}, timeout=600)
    if put.status_code >= 400:
        raise ZernioError(f"zernio upload HTTP {put.status_code}")
    return public_url


def check_caption(caption: str, *, limit: int = 2200) -> str:
    """Последний рубеж: в подписи нет HTML-тегов (Instagram напечатает их как текст)."""
    tags = re.findall(r"<[^>]+>", caption)
    if tags:
        raise ZernioError(
            f"В подписи остались теги разметки {tags[:3]} - площадка напечатает их как текст. "
            "Публикация остановлена."
        )
    if len(caption) > limit:
        raise ZernioError(f"Подпись длиннее лимита: {len(caption)} > {limit}")
    if not caption.strip():
        raise ZernioError("Пустая подпись к посту")
    return caption


def _item(path: Path, url: str) -> dict:
    kind = "video" if path.suffix.lower() in {".mp4", ".mov"} else "image"
    return {"type": kind, "url": url}


def media_for_platform(platform: str, items: list[dict]) -> list[dict]:
    """Нарезка общего набора слайдов под ограничения площадки."""
    images = [m for m in items if m["type"] == "image"]
    videos = [m for m in items if m["type"] == "video"]
    if platform in {"instagram", "telegram", "facebook"}:
        return items[:10]
    if platform == "threads":
        return (images or items)[:10]
    if platform == "linkedin":
        return (images or items)[:20]
    if platform == "tiktok":
        if videos:
            return videos[:1]
        return images[:10]
    if platform == "youtube":
        return videos[:1]
    if platform == "pinterest":
        return (images[:1] or videos[:1])
    return items[:1]


def _tiktok_settings(items: list[dict], caption: str) -> dict:
    settings = {
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "allow_comment": True,
        "content_preview_confirmed": True,
        "express_consent_given": True,
    }
    if items and all(m["type"] == "image" for m in items):
        settings.update({
            "media_type": "photo",
            "photo_cover_index": 0,
            "description": caption[:4000],
            "auto_add_music": True,
        })
    else:
        settings.update({"allow_duet": True, "allow_stitch": True})
    return settings


def _platform_entry(
    platform: str,
    account_id: str,
    default_items: list[dict],
    custom_items: list[dict],
    caption: str,
) -> dict:
    entry: dict = {"platform": platform, "accountId": account_id}
    if custom_items != default_items:
        entry["customMedia"] = custom_items
    limit = CAPTION_LIMITS.get(platform, 2200)
    if len(caption) > limit:
        entry["customContent"] = caption[:limit]
    if platform == "pinterest":
        specific: dict = {}
        title = caption.split("\n", 1)[0][:100]
        if title:
            specific["title"] = title
        if config.PINTEREST_BOARD_ID:
            specific["boardId"] = config.PINTEREST_BOARD_ID
        if config.PINTEREST_LINK:
            specific["link"] = config.PINTEREST_LINK
        if specific:
            entry["platformSpecificData"] = specific
    if platform == "youtube":
        title = caption.split("\n", 1)[0][:100] or "Shorts"
        entry.setdefault("platformSpecificData", {})["title"] = title
    return entry


def publish(media_files: list[Path], caption: str) -> dict:
    """Один createPost на все цели из ZERNIO_TARGETS.

    Instagram / Telegram / Facebook получают mediaItems карусели (2-10).
    Остальные площадки - customMedia: TikTok видео или фотосет, YouTube одно
    видео, Pinterest первый кадр. Площадку без подходящего медиа пропускаем.
    """
    dest = targets()
    check_caption(caption, limit=max(CAPTION_LIMITS.get(p, 2200) for p, _ in dest))
    if not 1 <= len(media_files) <= 10:
        raise ZernioError(f"Ожидалось 1-10 файлов, получено {len(media_files)}")

    uploaded: list[dict] = []
    for path in media_files:
        uploaded.append(_item(path, upload_media(path)))

    default_items = uploaded[:10]
    platforms: list[dict] = []
    skipped: list[str] = []
    body_extra: dict = {}

    for platform, account_id in dest:
        adapted = media_for_platform(platform, uploaded)
        if not adapted:
            skipped.append(platform)
            continue
        if platform in {"instagram", "telegram"} and not (2 <= len(adapted) <= 10):
            skipped.append(f"{platform} (нужно 2-10 элементов, есть {len(adapted)})")
            continue
        platforms.append(_platform_entry(platform, account_id, default_items, adapted, caption))
        if platform == "tiktok":
            body_extra["tiktokSettings"] = _tiktok_settings(adapted, caption)

    if not platforms:
        raise ZernioError(
            "Ни одна площадка из ZERNIO_TARGETS не получила подходящее медиа. "
            f"Пропущено: {', '.join(skipped) or '-'}. "
            "YouTube и видео-TikTok ждут cover-live.mp4 (LIVE_COVER=1)."
        )

    payload = {
        "content": caption,
        "mediaItems": default_items,
        "platforms": platforms,
        "publishNow": True,
    }
    payload.update(body_extra)

    resp = requests.post(
        f"{config.ZERNIO_API_URL}/posts",
        headers={**_headers(request_id=str(uuid.uuid4())), "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if resp.status_code >= 400:
        raise ZernioError(f"zernio post HTTP {resp.status_code}: {resp.text[:500]}")
    result = resp.json()

    post_id = _find_first(result, {"id", "postId", "post_id", "_id"})
    urls = _find_post_urls(result)
    if post_id and not urls:
        extra = wait_for_published(str(post_id))
        if extra:
            urls.append(extra)
    result["post_url"] = urls[0] if urls else None
    result["post_urls"] = urls
    result["skipped_platforms"] = skipped
    result["published_platforms"] = [p["platform"] for p in platforms]
    return result


def publish_carousel(media_files: list[Path], caption: str) -> dict:
    """Старое имя. Теперь постит все цели из ZERNIO_TARGETS, не только Instagram."""
    return publish(media_files, caption)


def _find_first(data, keys: set[str]):
    """Первое значение по любому из ключей в глубину (формат ответа в доках плавает)."""
    if isinstance(data, dict):
        for k, v in data.items():
            if k in keys and isinstance(v, (str, int)):
                return v
        for v in data.values():
            found = _find_first(v, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_first(item, keys)
            if found is not None:
                return found
    return None


def _find_post_urls(data) -> list[str]:
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and any(h in node for h in _URL_HINTS):
            if node not in found:
                found.append(node)

    walk(data)
    return found


def wait_for_published(post_id: str, *, timeout_sec: int = 240) -> str | None:
    """Поллит статус поста, пока Zernio не отдаст ссылку (или до таймаута)."""
    import time

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            resp = requests.get(
                f"{config.ZERNIO_API_URL}/posts/{post_id}",
                headers=_headers(),
                timeout=30,
            )
            if resp.status_code < 400:
                data = resp.json()
                urls = _find_post_urls(data)
                if urls:
                    return urls[0]
                status = str(_find_first(data, {"status", "state"}) or "").lower()
                if status in {"failed", "error"}:
                    raise ZernioError(f"zernio: публикация не удалась (status={status})")
        except requests.RequestException:
            pass
        time.sleep(12)
    return None
