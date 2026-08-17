"""QA слайдов новой схемы: текст рисует нейросеть, значит проверяем именно текст.

Что ловим: покорёженную кириллицу, отсебятину и лишние надписи, обрезанные буквы,
логотипы, нечитаемый контраст. Плюс общие правила GUARDRAILS.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, llm

_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "text_seen": {"type": "string"},
                    "text_ok": {"type": "boolean"},
                    "problems": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["n", "text_seen", "text_ok", "problems"],
                "additionalProperties": False,
            },
        },
        "passed": {"type": "boolean"},
        "verdict": {"type": "string"},
    },
    "required": ["slides", "passed", "verdict"],
    "additionalProperties": False,
}

_PROMPT = """Ты строгий QA перед публикацией. Проверь слайды карусели.
Сомневаешься - бракуй: перегенерить дешевле, чем публиковать брак.

Для каждого слайда:
- text_seen: перепиши ДОСЛОВНО текст, который видишь на картинке;
- text_ok: true, только если текст совпадает с ожидаемым (ниже) и все буквы целые,
  не искажены, не обрезаны краями, читаются с первого взгляда;
- problems: перечисли проблемы из списка, если есть:
  «искажённые буквы», «текст не совпадает», «лишние надписи», «текст обрезан»,
  «логотип или бренд», «плохой контраст», «предмет перекрывает текст»,
  «английские слова», «номера страниц».

Ожидаемые заголовки:
{heads}

passed: true, только если ВСЕ слайды в порядке.
verdict: одна фраза по-русски - что не так или «всё чисто».

{extra}"""


def check_slides(slides: list[Path], heads: list[str], report_file: Path) -> dict:
    heads_txt = "\n".join(f"  слайд {i + 1}: «{h}»" for i, h in enumerate(heads))
    extra = config.extra_rules("Визуал")
    blocks: list[dict] = [{"type": "text", "text": _PROMPT.format(
        heads=heads_txt,
        extra=("Выученные правила:\n" + extra) if extra else "")}]
    for i, path in enumerate(slides):
        blocks.append({"type": "text", "text": f"Слайд {i + 1}:"})
        blocks.append(llm.image_block(path, max_side=1000))

    result = llm.structured(blocks, _SCHEMA, max_tokens=8000)
    report_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def failed_slides(report: dict) -> list[int]:
    return [s["n"] for s in report["slides"] if not s["text_ok"] or s["problems"]]
