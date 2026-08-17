"""Шаг 2. Vision-отбор: Claude смотрит пачку пинов и выбирает лучшие по STYLE-DNA."""
from __future__ import annotations

import json
from pathlib import Path

from . import config, llm

_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}

_PROMPT = """Ты - арт-директор Pinterest-контента. Ниже пронумерованные референс-пины.
Оцени каждый от 0 до 10: насколько он годится как СТИЛЬНЫЙ референс для перерисовки
под эстетику «{family}» (сегмент: {segment}).

Критерии (из STYLE-DNA):
+ живой стильный кадр «как из Pinterest»: POV сверху, флэтлей, макро-деталь, кроп, со спины;
+ светлый чистый editorial-свет ИЛИ чистый глянцевый тёмный (не мутный);
+ трендовые детали 2026 (глянец, хром, фактура), один яркий акцент;
− сток-мокап (пустой ноутбук, дежурное фото), CGI/3D-пластик, пересвеченный каталог;
− постановочный портрет «модель смотрит вдаль», полная фигура в рост;
− мутная темнота, выцветший беж-минимал.
"""


_TREND_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "colors": {"type": "array", "items": {"type": "string"}},
        "textures": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["note", "colors", "textures"],
    "additionalProperties": False,
}


def trend_note(pins: list[Path], theme: dict, out_file: Path) -> str:
    """Свежие пины → короткая выжимка трендов для дизайнера (цвета, фактуры, приёмы)."""
    blocks: list[dict] = [{
        "type": "text",
        "text": (
            "Это свежая выдача Pinterest по теме "
            f"«{theme['name']}» (семья: {theme['family']}). Посмотри и опиши, что сейчас "
            "в тренде: цвета, фактуры, свет, композиционные приёмы. "
            "note - 2-3 предложения по-русски для арт-директора карусели; "
            "colors - 3-5 цветов; textures - 3-5 фактур/материалов. "
            "Пиши только то, что реально видно на пинах."
        ),
    }]
    for pin in pins[:8]:
        blocks.append(llm.image_block(pin, max_side=768))

    result = llm.structured(blocks, _TREND_SCHEMA, max_tokens=2000)
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return (f"{result['note']} Цвета: {', '.join(result['colors'])}. "
            f"Фактуры: {', '.join(result['textures'])}.")


def select_best(pins: list[Path], theme: dict, out_file: Path, *, top: int = 3) -> list[Path]:
    blocks: list[dict] = [
        {"type": "text", "text": _PROMPT.format(family=theme["family"], segment=theme["segment"])}
    ]
    for i, pin in enumerate(pins):
        blocks.append({"type": "text", "text": f"Пин №{i}:"})
        blocks.append(llm.image_block(pin, max_side=1024))

    result = llm.structured(blocks, _SCHEMA)
    scores = sorted(result["scores"], key=lambda s: s["score"], reverse=True)
    out_file.write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")

    chosen = [pins[s["index"]] for s in scores[:top] if 0 <= s["index"] < len(pins)]
    if not chosen:
        raise RuntimeError("Vision-отбор не вернул ни одного пина")
    return chosen
