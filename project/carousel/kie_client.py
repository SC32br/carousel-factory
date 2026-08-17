"""Клиент kie.ai (перерисовка фото и видео-луп). Адаптирован из hyperion-reels (MIT)."""
from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import requests

API_ROOT = "https://api.kie.ai/api/v1/jobs"
FILE_UPLOAD_ROOT = "https://kieai.redpandaai.co"


class KieApiError(RuntimeError):
    """Ошибка kie.ai, безопасная для лога (ключ не раскрывается)."""


def get_credits(api_key: str) -> float | None:
    """Остаток кредитов на аккаунте kie.ai (None, если не удалось узнать)."""
    try:
        resp = requests.get(
            "https://api.kie.ai/api/v1/chat/credit",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        data = resp.json()
        return float(data["data"]) if data.get("code") == 200 else None
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


class KieClient:
    def __init__(self, api_key: str):
        if not api_key.strip():
            raise ValueError("KIE_API_KEY пуст")
        self._api_key = api_key.strip()

    def _request_json(self, request: Request) -> dict:
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise KieApiError(f"kie.ai HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KieApiError("kie.ai не ответил корректно") from exc
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise KieApiError(f"kie.ai отклонил задачу: {payload.get('msg') or payload.get('code')}")
        return payload

    def create_task(self, model: str, input_data: dict) -> str:
        body = json.dumps({"model": model, "input": input_data}, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{API_ROOT}/createTask",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        task_id = self._request_json(request).get("data", {}).get("taskId")
        if not isinstance(task_id, str) or not task_id:
            raise KieApiError("kie.ai не вернул taskId")
        return task_id

    def wait_for_result(self, task_id: str, *, timeout_sec: int = 600, poll_sec: float = 4.0) -> list[str]:
        deadline = time.monotonic() + timeout_sec
        delay = max(1.0, poll_sec)
        while time.monotonic() < deadline:
            request = Request(
                f"{API_ROOT}/recordInfo?{urlencode({'taskId': task_id})}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            record = self._request_json(request).get("data") or {}
            state = record.get("state")
            if state == "success":
                result = json.loads(record.get("resultJson") or "{}")
                urls = result.get("resultUrls", [])
                if isinstance(urls, list) and urls:
                    return urls
                raise KieApiError("Задача kie.ai завершилась без resultUrls")
            if state == "fail":
                raise KieApiError(
                    f"kie.ai task fail: {record.get('failCode') or ''} {record.get('failMsg') or ''}".strip()
                )
            time.sleep(delay)
            delay = min(15.0, delay * 1.5)
        raise KieApiError(f"Истёк таймаут ожидания задачи kie.ai {task_id}")

    def upload_file(self, local_path: Path, *, upload_path: str = "carousel") -> str:
        """Локальный файл → HTTPS URL на CDN kie.ai (нужен для image-to-image и video)."""
        path = Path(local_path)
        if not path.is_file():
            raise KieApiError(f"Файл для upload не найден: {path}")
        mime, _ = mimetypes.guess_type(path.name)
        try:
            with path.open("rb") as fh:
                response = requests.post(
                    f"{FILE_UPLOAD_ROOT}/api/file-stream-upload",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": (path.name, fh, mime or "application/octet-stream")},
                    data={"uploadPath": upload_path, "fileName": path.name},
                    timeout=300,
                )
        except requests.RequestException as exc:
            raise KieApiError("Не удалось загрузить файл на CDN kie.ai") from exc
        if response.status_code >= 400:
            raise KieApiError(f"kie.ai upload HTTP {response.status_code}")
        payload = response.json()
        data = payload.get("data") or {}
        url = data.get("fileUrl") or data.get("downloadUrl")
        if not isinstance(url, str) or not url.startswith("http"):
            raise KieApiError(f"kie.ai upload не вернул fileUrl: {payload.get('msg')}")
        return url

    def redraw_image(
        self, model: str, prompt: str, source_url: str,
        *, aspect_ratio: str = "4:5", resolution: str = "2K",
    ) -> list[str]:
        """Редроу пина: image-to-image по промту (сюжет тот же, кадр новый).

        Спека nano-banana-pro: docs.kie.ai/market/google/pro-image-to-image
        (поле image_input, aspect_ratio 4:5 поддерживается)."""
        task_id = self.create_task(
            model,
            {
                "prompt": prompt,
                "image_input": [source_url],
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "output_format": "png",
            },
        )
        return self.wait_for_result(task_id)

    def animate_image(self, model: str, prompt: str, image_url: str) -> list[str]:
        """Живая обложка через Veo3 API (отдельный endpoint, не jobs).

        Спека: docs.kie.ai/veo3-api/quickstart - POST /api/v1/veo/generate,
        поллинг GET /api/v1/veo/record-info. Кроп под 4:5 делаем потом ffmpeg-ом."""
        body = json.dumps(
            {
                "prompt": prompt,
                "model": model,
                "imageUrls": [image_url],
                "aspect_ratio": "9:16",
            }
        ).encode("utf-8")

        # Постановка задачи с ретраем: veo иногда отдаёт 5xx на старте.
        task_id = None
        last_err = ""
        for attempt in (1, 2, 3):
            request = Request(
                "https://api.kie.ai/api/v1/veo/generate",
                data=body,
                method="POST",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
            )
            try:
                task_id = self._request_json(request).get("data", {}).get("taskId")
                if task_id:
                    break
            except KieApiError as exc:
                last_err = str(exc)
                time.sleep(10 * attempt)
        if not task_id:
            raise KieApiError(f"veo/generate не вернул taskId: {last_err}")

        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            req = Request(
                f"https://api.kie.ai/api/v1/veo/record-info?{urlencode({'taskId': task_id})}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            data = self._request_json(req).get("data") or {}
            flag = data.get("successFlag")
            if flag == 1:
                resp = data.get("response") or {}
                urls = resp.get("resultUrls") or json.loads(data.get("resultUrls") or "[]")
                if urls:
                    return urls
                raise KieApiError("veo завершился без resultUrls")
            if flag in (2, 3):
                raise KieApiError(f"veo task fail: {data.get('errorMessage') or flag}")
            time.sleep(15)
        raise KieApiError(f"Истёк таймаут ожидания veo task {task_id}")

    def download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        request = Request(url, headers={"User-Agent": "carousel-factory"})
        try:
            with urlopen(request, timeout=120) as response:
                target.write_bytes(response.read())
        except (HTTPError, URLError, TimeoutError) as exc:
            raise KieApiError(f"Не удалось скачать результат kie.ai: {url}") from exc
