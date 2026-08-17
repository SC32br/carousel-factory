"""Шаг 1. Парсинг свежих пинов Pinterest по запросу темы (gallery-dl + cookies.txt)."""
from __future__ import annotations

import random
import subprocess
from pathlib import Path

from . import config

GALLERY_DL = str(config.PROJECT_ROOT / ".venv" / "bin" / "gallery-dl")


def parse_pins(queries: list[str], out_dir: Path, *, count: int | None = None) -> list[Path]:
    """Тянет `count` пинов по одному из запросов темы (запрос выбирается случайно -
    ротация из PINTEREST-ZAPROSY.md, чтобы не долбить один и тот же)."""
    count = count or config.PINS_PER_RUN
    query = random.choice(queries)
    out_dir.mkdir(parents=True, exist_ok=True)
    url = "https://www.pinterest.com/search/pins/?q=" + query.replace(" ", "+")

    cmd = [
        GALLERY_DL,
        "--range", f"1-{count}",
        "-D", str(out_dir),  # плоская папка, без вложенности pinterest/…
        url,
    ]
    if config.PINTEREST_COOKIES_FILE.is_file():
        cmd[1:1] = ["--cookies", str(config.PINTEREST_COOKIES_FILE)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    images = sorted(
        p for p in out_dir.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if not images:
        raise RuntimeError(
            f"gallery-dl не спарсил ни одного пина по запросу «{query}».\n"
            f"stderr: {result.stderr[-800:]}\n"
            "Частая причина - протухшие куки Pinterest (secrets/pinterest_cookies.txt)."
        )
    (out_dir / "_query.txt").write_text(query, encoding="utf-8")
    return images
