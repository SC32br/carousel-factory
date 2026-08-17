"""Мастер-кадр и нарезка: одна генерация 3:4@4K → 9 слайдов 1080×1350 (4:5).

Панель мастера 3:4 = 0.75, у карусели Instagram/альбома нижняя граница 0.8,
поэтому после нарезки берём центральный кроп 4:5 и увеличиваем до 1080×1350.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from . import config
from .kie_client import KieClient

SLIDE_W, SLIDE_H = 1080, 1350
IG_RATIO = SLIDE_W / SLIDE_H  # 0.8 - нижняя граница Instagram


def generate_master(prompt: str, out_png: Path, *, aspect: str = "3:4",
                    resolution: str = "4K") -> Path:
    """Одна задача GPT Image 2 → мастер-кадр с девятью панелями.

    4K обязателен: на 2K панель выходит 581×778 и после апскейла до 1080 текст мылит
    (замер резкости 230 против 512 у 4K, проверено 17.08).
    """
    kie = KieClient(config.require("KIE_API_KEY", config.KIE_API_KEY))
    out_png.with_suffix(".prompt.txt").write_text(prompt, encoding="utf-8")
    task_id = kie.create_task(
        config.KIE_IMAGE_MODEL,
        {"prompt": prompt, "aspect_ratio": aspect, "resolution": resolution},
    )
    urls = kie.wait_for_result(task_id, timeout_sec=900)
    kie.download(urls[0], out_png)
    return out_png


def slice_master(master_png: Path, out_dir: Path) -> list[Path]:
    """Мастер → 9 файлов slide-01..09.png (1080×1350), нумерация слева направо, сверху вниз."""
    out_dir.mkdir(parents=True, exist_ok=True)
    im = Image.open(master_png).convert("RGB")
    w, h = im.size
    cw, ch = w // 3, h // 3
    if cw < 700:
        raise RuntimeError(
            f"Мастер слишком мелкий ({w}×{h}): панель {cw}×{ch}, слайды выйдут мыльными. "
            "Нужна генерация в 4K."
        )

    crop_h = int(round(cw / IG_RATIO))  # высота центрального кропа 4:5
    if crop_h > ch:  # мастер уже «шире» 4:5 - режем по ширине
        crop_h = ch
    top_pad = (ch - crop_h) // 2

    slides = []
    for idx in range(9):
        row, col = divmod(idx, 3)
        panel = im.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
        panel = panel.crop((0, top_pad, cw, top_pad + crop_h))
        panel = panel.resize((SLIDE_W, SLIDE_H), Image.LANCZOS)
        path = out_dir / f"slide-{idx + 1:02d}.png"
        panel.save(path, optimize=True)
        slides.append(path)

    (out_dir / "slice-manifest.json").write_text(json.dumps({
        "master": master_png.name, "master_size": [w, h], "panel": [cw, ch],
        "crop_height": crop_h, "slides": [s.name for s in slides],
        "slide_size": [SLIDE_W, SLIDE_H],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return slides


def check_cut_lines(master_png: Path, report_file: Path) -> dict:
    """Проверка линий реза: на 1/3 и 2/3 не должно быть светлых полос-разделителей.

    Светлая линия = генератор нарисовал рамки, и слайды получат белые края.
    """
    im = Image.open(master_png).convert("L")
    w, h = im.size
    import numpy as np

    arr = np.asarray(im, dtype=float)
    bright = 235
    checks = {}
    for name, pos, axis in [("вертикаль 1/3", w // 3, "v"), ("вертикаль 2/3", 2 * w // 3, "v"),
                            ("горизонталь 1/3", h // 3, "h"), ("горизонталь 2/3", 2 * h // 3, "h")]:
        band = (arr[:, max(0, pos - 4):pos + 4] if axis == "v"
                else arr[max(0, pos - 4):pos + 4, :])
        checks[name] = round(float((band > bright).mean()), 4)

    worst = max(checks.values())
    report = {"white_ratio": checks, "passed": worst <= 0.20}
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
