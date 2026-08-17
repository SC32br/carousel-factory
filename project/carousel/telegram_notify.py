"""Шаг 8. Превью в Telegram на подтверждение (если бот настроен)."""
from __future__ import annotations

import json
from pathlib import Path

import requests

from . import config


def enabled() -> bool:
    return bool(config.TG_BOT_TOKEN and config.TG_CHAT_IDS)


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/{method}"


# Одна сессия на весь процесс: keep-alive убирает TLS-рукопожатие на каждом запросе.
_session = requests.Session()


def call(method: str, *, files: dict | None = None, request_timeout: int = 60, **data) -> dict:
    """Универсальный вызов Bot API; возвращает json-ответ (ok/description/result).

    request_timeout - сетевой таймаут клиента. ВСЁ остальное уходит в тело запроса,
    включая `timeout` для getUpdates (это длина long-poll на стороне Telegram -
    раньше он терялся, и бот молотил API короткими опросами).
    Неудачи логируем всегда - молчащий бот без следа в логе искать невозможно."""
    try:
        r = _session.post(_api(method), data=data, files=files, timeout=request_timeout)
        result = r.json()
    except (requests.RequestException, ValueError) as exc:
        result = {"ok": False, "description": str(exc)}
    if not result.get("ok") and method != "getUpdates":
        print(f"[tg] {method} → {result.get('description')} "
              f"(chat={data.get('chat_id')})", flush=True)
    return result


def review_keyboard(run_id: str) -> str:
    return json.dumps({"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"pub:{run_id}"},
        {"text": "🔧 Доработать", "callback_data": f"fix:{run_id}"},
    ]]})


def send_review_request(run_id: str, media: list[Path], caption: str) -> None:
    """Альбом + текст поста с кнопками «Опубликовать / Доработать» - всем получателям."""
    if not enabled():
        return
    send_preview(media, "")  # альбом; пустой текст не шлётся
    for chat_id in config.TG_CHAT_IDS:
        resp = call(
            "sendMessage", chat_id=chat_id,
            text=f"Карусель за {run_id} готова.\n\nТекст поста:\n{caption}",
            reply_markup=review_keyboard(run_id),
        )
        if not resp.get("ok"):
            print(f"[tg] кнопки {chat_id}: {resp.get('description')}")


def send_text(text: str) -> None:
    """Сообщение всем получателям. Ошибки печатаем, конвейер не роняем."""
    if not enabled():
        return
    for chat_id in config.TG_CHAT_IDS:
        try:
            r = requests.post(_api("sendMessage"), data={"chat_id": chat_id, "text": text}, timeout=30)
            if not r.json().get("ok"):
                print(f"[tg] {chat_id}: {r.json().get('description')}")
        except requests.RequestException as exc:
            print(f"[tg] {chat_id}: {exc}")


def send_preview(slides: list[Path], text: str) -> None:
    """Альбом до 10 слайдов + сообщение - всем получателям."""
    if not enabled():
        return
    for chat_id in config.TG_CHAT_IDS:
        try:
            media, files = [], {}
            for i, path in enumerate(slides[:10]):
                key = f"slide{i}"
                kind = "video" if path.suffix.lower() in {".mp4", ".mov"} else "photo"
                media.append({"type": kind, "media": f"attach://{key}"})
                files[key] = path.open("rb")
            r = requests.post(
                _api("sendMediaGroup"),
                data={"chat_id": chat_id, "media": json.dumps(media)},
                files=files,
                timeout=180,
            )
            for fh in files.values():
                fh.close()
            if not r.json().get("ok"):
                print(f"[tg] альбом {chat_id}: {r.json().get('description')}")
            if text:
                requests.post(_api("sendMessage"), data={"chat_id": chat_id, "text": text}, timeout=30)
        except requests.RequestException as exc:
            print(f"[tg] превью {chat_id}: {exc}")
