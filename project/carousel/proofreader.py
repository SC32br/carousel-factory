"""Корректор: последняя вычитка текстов перед вёрсткой.

Ловит то, на чём спотыкается генерация: согласование числительных («3 клиентов» →
«3 клиента»), падежи, опечатки, кривые акцент-слова. Работает после хуманайзера -
тот чистит стиль, этот чинит грамматику, не трогая смысл.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import llm

_SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "where": {"type": "string"},
                    "was": {"type": "string"},
                    "now": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["where", "was", "now", "why"],
                "additionalProperties": False,
            },
        },
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
    "required": ["fixes", "slides", "cover_sub", "caption"],
    "additionalProperties": False,
}

_PROMPT = """Ты - корректор русского языка. Вычитай тексты карусели и исправь ТОЛЬКО
грамматику, не трогая смысл, длину и стиль.

Что искать (по опыту брака):
1. Согласование числительных: «теряешь 3 клиентов» → «теряешь 3 клиента»;
   после 2, 3, 4 - родительный единственного («2 клиента»), после 5+ - множественного
   («5 клиентов»), 21/22 - как 1/2 («21 клиент», «22 клиента»).
2. Падежи и управление глаголов: «звонит до клиента» → «звонит клиенту».
3. Опечатки, пропущенные буквы, «ё» не обязательна, но единообразно.
4. Кривые окончания в заголовках и подписи.
5. accent_word обязан встречаться в head ДОСЛОВНО и быть осмысленной единицей:
   не обрывок («слово» из «кодовое слово»), а цельное выражение или число.
   Если акцент - обрывок, подбери правильный кусок из head.

Правила:
- head НЕ должен стать длиннее, чем был (лимиты вёрстки);
- ничего не переписывай ради красоты - только реальные ошибки;
- нет ошибок - верни тексты как есть с пустым fixes;
- в fixes перечисли правки: where (например «слайд 3, head»), was, now, why (кратко).

JSON текстов:
"""


def proofread(copy: dict, report_file: Path) -> dict:
    """Возвращает вычитанные тексты; отчёт о правках пишет в report_file."""
    result = llm.structured(
        [{"type": "text", "text": _PROMPT + json.dumps(copy, ensure_ascii=False)}],
        _SCHEMA,
    )

    # Страховка: акцент обязан быть внутри заголовка - иначе вёрстка не подсветит слово.
    for slide in result["slides"]:
        if slide["accent_word"] and slide["accent_word"] not in slide["head"]:
            original = next((s for s in copy["slides"] if s["n"] == slide["n"]), None)
            slide["accent_word"] = (
                original["accent_word"]
                if original and original["accent_word"] in slide["head"]
                else _longest_word(slide["head"])
            )

    report_file.write_text(
        json.dumps(result["fixes"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {k: result[k] for k in ("slides", "cover_sub", "caption")}


def _longest_word(head: str) -> str:
    words = re.findall(r"[\w₽%+-]+", head.replace("<br>", " "))
    return max(words, key=len) if words else ""
