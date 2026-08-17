"""Директор: приём внеплановой идеи поста в Telegram → бриф → запуск конвейера.

Роутинг решает НЕ он (это делает бот по состояниям/кнопкам) - директор только
разбирает свободное сообщение уже в своей зоне: новая идея / вопрос о статусе.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import config, llm

_FAMILIES = [
    "светлый editorial", "тёмный editorial", "макро-деталь", "POV стол",
]

_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["new_post", "new_rule", "status", "maybe_feedback", "chat"]},
        "reply": {"type": "string"},
        "ready": {"type": "boolean"},
        "rule": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["Тексты", "Визуал"]},
                "rule": {"type": "string"},
            },
            "required": ["section", "rule"],
            "additionalProperties": False,
        },
        "brief": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "segment": {"type": "string"},
                "angle": {"type": "string"},
                "family": {"type": "string", "enum": _FAMILIES},
                "queries": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "segment", "angle", "family", "queries"],
            "additionalProperties": False,
        },
    },
    "required": ["kind", "reply", "ready", "brief", "rule"],
    "additionalProperties": False,
}

_PROMPT = """Ты директор конвейера каруселей. Тебе пишут в Telegram. Разбери диалог:

- kind=new_post - человек хочет карусель на свою тему. Собери бриф:
  name (тема коротко), segment (кто аудитория),
  angle (боль/угол подачи, одной фразой), family (тип визуала из списка),
  queries (2-3 англоязычных запроса для стильных референсов на Pinterest:
  «luxury manicure aesthetic», «quiet luxury desk aesthetic», не иллюстрация боли).
  ready=true если брифа хватает. Если не хватает одной ключевой детали - задай в reply
  ровно один короткий вопрос и ready=false. Анкету не устраивай: чего-то нет - реши сам.
- kind=new_rule - постоянное пожелание ко всем будущим каруселям
  («больше не показывай кофе в кадре», «в текстах не обращайся на ты»).
  Сформулируй в rule: section «Тексты» или «Визуал», rule - короткая формулировка.
  В reply повтори правило своими словами.
- kind=status - спрашивают про статус/расписание/что готово. В reply ничего,
  статус подставит бот.
- kind=maybe_feedback - похоже на замечание к уже собранной карусели.
- kind=chat - приветствие: ответь коротко и одной фразой скажи, что умеешь
  (принять идею поста, показать статус).

reply по-русски, коротко, без канцелярита.
При ready=true в reply резюме брифа в 1-2 строки (его покажут с кнопкой запуска)."""


_DEFAULT_FAMILY = {
    "предприниматель": "POV стол",
    "мастер салона красоты": "светлый editorial",
    "локальный бизнес": "макро-деталь",
}
_DEFAULT_QUERIES = ["quiet luxury aesthetic", "editorial still life", "macro detail aesthetic"]


def intake(dialog: list[dict]) -> dict:
    """dialog: [{"role": "user"|"director", "text": ...}] - история короткого диалога."""
    convo = "\n".join(f"{'Директор' if m['role'] == 'director' else 'Человек'}: {m['text']}" for m in dialog)
    result = llm.structured(
        [{"type": "text", "text": _PROMPT + "\n\nДИАЛОГ:\n" + convo}],
        _SCHEMA,
        max_tokens=2000,
    )
    # Прокси kie не всегда силово соблюдает enum - валидируем руками с фолбэком.
    brief = result.get("brief") or {}
    if brief.get("family") not in _FAMILIES:
        brief["family"] = _DEFAULT_FAMILY.get(brief.get("segment", ""), "светлый editorial")
    if not brief.get("queries"):
        brief["queries"] = _DEFAULT_QUERIES
    brief["queries"] = [q + " aesthetic" if "aesthetic" not in q.lower() else q
                        for q in brief["queries"][:3]]
    result["brief"] = brief
    return result


def extras_today() -> int:
    today = dt.date.today().isoformat()
    return len(list(config.RUNS_DIR.glob(f"{today}-extra*")))


def save_brief(brief: dict) -> Path:
    briefs_dir = config.RUNS_DIR / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path = briefs_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def status_text() -> str:
    lines = ["📊 Статус конвейера:"]
    runs = sorted(
        (d for d in config.RUNS_DIR.glob("20??-??-??*") if d.is_dir()), reverse=True
    )[:4]
    if not runs:
        lines.append("прогонов ещё не было")
    for run in runs:
        if (run / "published.json").is_file():
            state = "✔ опубликована"
        elif (run / "failure.json").is_file():
            state = "⚠ упала, чинится"
        elif (run / "slides" / "slide-01.png").is_file():
            state = "готова, ждёт кнопку"
        else:
            state = "в работе"
        lines.append(f"• {run.name}: {state}")
    lines.append(f"Внеплановых сегодня: {extras_today()} из {config.EXTRA_RUNS_PER_DAY}")
    return "\n".join(lines)
