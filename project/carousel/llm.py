"""Все вызовы LLM через kie.ai (OpenAI-совместимый формат, ключ один на весь проект).

Endpoint: https://api.kie.ai/<модель>/v1/chat/completions - модель зашита в URL.
Проверено 15.08: gemini-3-pro - текст, vision (base64 data-uri), response_format json_schema.
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
from pathlib import Path

import requests
from PIL import Image

from . import config


class LlmError(RuntimeError):
    pass


def image_block(path: Path, *, max_side: int = 1280) -> dict:
    """Файл → content-блок image_url (даунскейл + JPEG - экономим токены)."""
    img = Image.open(path).convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


def chat(content_blocks: list[dict], *, schema: dict | None = None,
         max_tokens: int = 8000, model: str | None = None) -> str:
    """Запрос к kie-чату с резервными моделями.

    У kie бывает «no user can use» (код 524) - это НЕ наш ключ и не баланс, а пустой
    пул провайдера под конкретную модель. Лечится ожиданием или переходом на резервную.
    """
    models = [model] if model else [config.LLM_MODEL, *config.LLM_FALLBACK_MODELS]
    errors = []
    for i, name in enumerate(models):
        try:
            return _chat_once(content_blocks, schema=schema, max_tokens=max_tokens, model=name)
        except LlmError as exc:
            errors.append(f"{name}: {exc}")
            if i + 1 < len(models):
                print(f"[llm] {name} недоступна, перехожу на {models[i + 1]}", flush=True)
    raise LlmError("все модели недоступны - " + " | ".join(errors))


def _chat_once(content_blocks: list[dict], *, schema: dict | None = None,
               max_tokens: int = 8000, model: str) -> str:
    """Один запрос к конкретной модели. content_blocks - {"type":"text"|"image_url",...}."""
    config.require("KIE_API_KEY", config.KIE_API_KEY)
    if model.startswith("gpt-5"):
        return _chat_codex(content_blocks, schema=schema, model=model)
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content_blocks}],
    }
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema},
        }

    url = f"https://api.kie.ai/{model}/v1/chat/completions"
    last_err = ""
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {config.KIE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=180,
            )
            payload = resp.json()
            if resp.status_code < 400 and payload.get("choices"):
                return payload["choices"][0]["message"]["content"]
            last_err = f"HTTP {resp.status_code}: {str(payload)[:200]}"
        except (requests.RequestException, ValueError) as exc:
            last_err = str(exc)[:200]
        time.sleep(min(20, 5 * attempt))
    raise LlmError(f"kie-чат ({model}) не ответил за 3 попытки: {last_err}")


def _chat_codex(content_blocks: list[dict], *, schema: dict | None, model: str) -> str:
    """Модели gpt-5.6-* живут на /codex/v1/responses: другой формат запроса и ответа.

    Картинки этот эндпоинт не принимает - на vision-задачах он в резерв не годится.
    """
    texts = [b["text"] for b in content_blocks if b.get("type") == "text"]
    if len(texts) != len(content_blocks):
        raise LlmError(f"{model}: эндпоинт не принимает картинки")
    prompt = "\n\n".join(texts)
    if schema is not None:
        prompt += ("\n\nВерни СТРОГО валидный JSON по схеме ниже, без markdown-обёртки "
                   "и пояснений:\n" + json.dumps(schema, ensure_ascii=False))
    try:
        resp = requests.post(
            "https://api.kie.ai/codex/v1/responses",
            headers={"Authorization": f"Bearer {config.KIE_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": model, "input": prompt, "stream": False},
            timeout=300,
        )
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise LlmError(f"{model}: {str(exc)[:200]}") from exc
    if payload.get("code") and payload.get("code") != 200:
        raise LlmError(f"{model}: {str(payload)[:200]}")
    text = "".join(
        c.get("text", "")
        for item in payload.get("output") or []
        if item.get("type") == "message"
        for c in item.get("content", [])
    )
    if not text.strip():
        raise LlmError(f"{model}: пустой ответ")
    return text


def transcribe_voice(audio_path: Path) -> str:
    """Голосовое из Telegram (ogg/opus) → текст, через native-endpoint Gemini на kie."""
    config.require("KIE_API_KEY", config.KIE_API_KEY)
    data = base64.standard_b64encode(Path(audio_path).read_bytes()).decode("ascii")
    url = f"https://api.kie.ai/gemini/v1/models/{config.LLM_MODEL}:generateContent"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {config.KIE_API_KEY}", "Content-Type": "application/json"},
        json={"contents": [{"parts": [
            {"inline_data": {"mime_type": "audio/ogg", "data": data}},
            {"text": "Расшифруй голосовое сообщение дословно, по-русски. Верни только текст."},
        ]}]},
        timeout=180,
    )
    payload = resp.json()
    try:
        return payload["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as exc:
        raise LlmError(f"Не удалось распознать голос: {str(payload)[:200]}") from exc


def structured(content_blocks: list[dict], schema: dict, *, max_tokens: int = 8000) -> dict:
    """Запрос с JSON-схемой → провалидированный dict."""
    text = chat(content_blocks, schema=schema, max_tokens=max_tokens)
    # На случай, если модель обернёт JSON в ```json-забор.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LlmError(f"Модель вернула не-JSON: {text[:300]}") from exc
