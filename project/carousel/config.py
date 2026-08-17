"""Загрузка настроек из .env (подписи полей в .env.example в корне репозитория)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
load_dotenv(REPO_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env", override=True)

RUNS_DIR = PROJECT_ROOT / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
PROMPTS_DIR = PROJECT_ROOT / "prompts"
SECRETS_DIR = PROJECT_ROOT / "secrets"

KIE_API_KEY = os.getenv("KIE_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3-pro").strip()
LLM_FALLBACK_MODELS = [m.strip() for m in
                       os.getenv("LLM_FALLBACK_MODELS", "gemini-2.5-pro,gemini-3-flash").split(",")
                       if m.strip()]
KIE_IMAGE_MODEL = os.getenv("KIE_IMAGE_MODEL", "nano-banana-pro").strip()
KIE_VIDEO_MODEL = os.getenv("KIE_VIDEO_MODEL", "veo3_fast").strip()
LIVE_COVER = os.getenv("LIVE_COVER", "0").strip() == "1"

PINTEREST_COOKIES_FILE = PROJECT_ROOT / os.getenv(
    "PINTEREST_COOKIES_FILE", "secrets/pinterest_cookies.txt"
)
PINS_PER_RUN = int(os.getenv("PINS_PER_RUN", "8"))

ZERNIO_API_KEY = os.getenv("ZERNIO_API_KEY", "").strip()
ZERNIO_API_URL = os.getenv("ZERNIO_API_URL", "https://zernio.com/api/v1").rstrip("/")
# Совместимость со старым .env: один Instagram-аккаунт.
ZERNIO_INSTAGRAM_ACCOUNT_ID = os.getenv("ZERNIO_INSTAGRAM_ACCOUNT_ID", "").strip()

CTA_WORD = os.getenv("CTA_WORD", "СТАРТ").strip() or "СТАРТ"
SERIES_NAME = os.getenv("SERIES_NAME", "КАРУСЕЛЬ").strip() or "КАРУСЕЛЬ"
PINTEREST_BOARD_ID = os.getenv("PINTEREST_BOARD_ID", "").strip()
PINTEREST_LINK = os.getenv("PINTEREST_LINK", "").strip()

AUTO_PUBLISH = os.getenv("AUTO_PUBLISH", "0").strip() == "1"

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_IDS = [c.strip() for c in os.getenv("TG_CHAT_IDS", "").split(",") if c.strip()]
# Кто вправе нажать «Опубликовать». Пусто = любой чат из TG_CHAT_IDS.
TG_PUBLISH_CHAT_ID = os.getenv("TG_PUBLISH_CHAT_ID", "").strip()

EXTRA_RUNS_PER_DAY = int(os.getenv("EXTRA_RUNS_PER_DAY", "3"))
KEEP_RUNS_DAYS = int(os.getenv("KEEP_RUNS_DAYS", "14"))


def _default_chrome() -> str:
    for candidate in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if Path(candidate).is_file():
            return candidate
    return "/usr/bin/chromium"


CHROME_BIN = os.getenv("CHROME_BIN", "").strip() or _default_chrome()


def _parse_zernio_targets() -> list[tuple[str, str]]:
    """Список (platform, accountId) из ZERNIO_TARGETS=telegram:id,youtube:id.

    Instagram остаётся опциональной целью: либо в этом списке, либо через
    ZERNIO_INSTAGRAM_ACCOUNT_ID (старое имя переменной).
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    raw = os.getenv("ZERNIO_TARGETS", "").strip()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            continue
        platform, account_id = part.split(":", 1)
        platform, account_id = platform.strip().lower(), account_id.strip()
        if not platform or not account_id or platform in seen:
            continue
        out.append((platform, account_id))
        seen.add(platform)
    if ZERNIO_INSTAGRAM_ACCOUNT_ID and "instagram" not in seen:
        out.append(("instagram", ZERNIO_INSTAGRAM_ACCOUNT_ID))
    return out


ZERNIO_TARGETS = _parse_zernio_targets()

STYLE_TAIL = (PROMPTS_DIR / "style_tail.txt").read_text(encoding="utf-8").strip()
QA_CHECKLIST = (PROMPTS_DIR / "qa_checklist.md").read_text(encoding="utf-8")


def extra_rules(section: str) -> str:
    """Выученные правила фиксика («Тексты» или «Визуал»), читаются на каждом вызове."""
    path = PROMPTS_DIR / "extra_rules.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    marker = f"## {section}"
    if marker not in text:
        return ""
    chunk = text.split(marker, 1)[1].split("\n## ", 1)[0]
    rules = [l for l in chunk.splitlines() if l.strip().startswith("- ")]
    return "\n".join(rules)


class ConfigError(RuntimeError):
    """Не заполнена обязательная настройка в .env."""


def require(name: str, value: str) -> str:
    if not value:
        raise ConfigError(
            f"В .env не заполнен {name} - открой .env.example, там подписано, где его взять."
        )
    return value
