"""Шаг 6. Авто-QA vision-гейт: модель смотрит фото и решает PASS / брак."""
from __future__ import annotations

import json
from pathlib import Path

from . import config, llm

_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "fails": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["rule", "detail"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["passed", "fails"],
    "additionalProperties": False,
}


def check_photo(photo: Path, report_file: Path) -> dict:
    """Прогоняет фото по чек-листу GUARDRAILS. Пишет отчёт, возвращает результат."""
    result = llm.structured(
        [
            {
                "type": "text",
                "text": (
                    "Ты - строгий QA-контролёр перед автопостингом. Проверь фото по чек-листу. "
                    "Сомневаешься - бракуй (лучше перегенерить, чем опубликовать брак).\n\n"
                    + config.QA_CHECKLIST
                    + ("\nВыученные правила:\n" + config.extra_rules("Визуал")
                       if config.extra_rules("Визуал") else "")
                ),
            },
            llm.image_block(photo, max_side=1568),
        ],
        _SCHEMA,
        max_tokens=4000,
    )
    report_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def negative_prompt_addon(fails: list[dict]) -> str:
    """Усиленный негатив для регенерации: что именно чинить по провалившимся правилам."""
    fixes = {
        "brands": "absolutely NO logos, NO brand names, NO readable text anywhere",
        "anatomy": "exactly five fingers per hand, natural proportions, no extra or fused limbs",
        "face_figure": "faceless framing only: crop above the nose, back view, hands or detail shot; NO full face, NO full body",
        "light": "bright clean luminous natural light OR clean dark glossy editorial, NOT murky, NOT underexposed",
        "style": "looks like a real candid Pinterest photo, NOT CGI, NOT staged studio portrait",
        "no_brands_scene": "no logos, no foreign neon signs, no fake-luxury cliches unless the theme asks",
    }
    keys = {f["rule"].split()[0].strip(".").lower() for f in fails}
    addons = [v for k, v in fixes.items() if any(k in key for key in keys)]
    return (" STRICT FIX REQUIRED: " + "; ".join(addons) + ".") if addons else ""
