"""Хуманайзер (обязательный шаг): чистит тексты карусели от ИИ-штампов.

Два слоя, как в github.com/Horosheff/russian-humanizer:
1) детерминированный сканер маркеров (порт словаря slop_detector.py);
2) редакторский проход Claude по контракту prompts/humanizer.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, llm

HUMANIZER_RULES = (config.PROMPTS_DIR / "humanizer.md").read_text(encoding="utf-8")

# Ядро словаря маркеров из slop_detector (russian-humanizer) + местный список.
SLOP_MARKERS = [
    "важно понимать", "важно отметить", "важно подчеркнуть", "стоит отметить",
    "следует учитывать", "нельзя не упомянуть", "интересно отметить",
    "справедливости ради", "честно говоря", "дело вот в чем", "дело вот в чём",
    "в современном мире", "в быстро меняющемся", "таким образом",
    "в конечном итоге", "меняет правила игры", "давайте поговорим",
    "вот в чем проблема", "вот в чём проблема", "и точка", "просто вдумайтесь",
    "огромное значение", "не делайте ошибки", "синергия",
    "на стероидах", "на минималках", "и тут начинается", "и в этот момент",
    "и вот тут", "и тогда становится понятно", "это не просто",
    "полотно", "мозаика", "симфония", "оазис",
]

_RE_NE_X_A_Y = re.compile(r"\bне\s+[^,.!?]{2,40},\s*а\s+", re.IGNORECASE)
_EM_DASH = "—"


def slop_scan(text: str) -> list[str]:
    """Возвращает список найденных маркеров (пусто = чисто)."""
    low = text.lower()
    hits = [m for m in SLOP_MARKERS if m in low]
    if _EM_DASH in text:
        hits.append("длинное тире U+2014")
    if _RE_NE_X_A_Y.search(text):
        hits.append("конструкция «не X, а Y»")
    return hits


def _copy_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer"},
                        "head": {"type": "string"},
                        "accent_word": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["n", "head", "accent_word", "body"],
                    "additionalProperties": False,
                },
            },
            "cover_sub": {"type": "string"},
            "caption": {"type": "string"},
        },
        "required": ["slides", "cover_sub", "caption"],
        "additionalProperties": False,
    }


def humanize_copy(copy: dict, report_file: Path) -> dict:
    """Редакторский проход: переписывает тексты по контракту хуманайзера.

    Держит структуру и лимиты: та же арка, те же accent_word-правила, head короче лимитов.
    """
    before = json.dumps(copy, ensure_ascii=False)
    result = llm.structured(
        [
            {
                "type": "text",
                "text": (
                    "Ты - редактор-хуманайзер. Ниже JSON текстов карусели (9 слайдов + caption). "
                    "Перепиши их по контракту ниже: смысл и структура те же, заголовки НЕ длиннее "
                    "исходных, accent_word по-прежнему встречается в head дословно, теги <br> сохраняй. "
                    "Если текст уже чистый - верни его без изменений.\n\n"
                    + HUMANIZER_RULES
                    + "\n\nJSON:\n" + before
                ),
            }
        ],
        _copy_schema(),
    )

    # Контроль после чистки: детектор по всем текстам.
    flat = " ".join(
        [s["head"] + " " + s["body"] for s in result["slides"]]
        + [result["cover_sub"], result["caption"]]
    )
    leftovers = slop_scan(flat)
    report_file.write_text(
        json.dumps({"leftover_markers": leftovers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if leftovers:
        raise RuntimeError(f"Хуманайзер не дочистил маркеры: {leftovers}")
    return result
