"""Фиксик: разбирает замечание к карусели, предлагает правки, по подтверждению чинит.

Цикл: замечание → анализ → предложение (что перегнать + какие правила выучить)
→ человек жмёт «Применить» → бэкап правил → правка prompts/extra_rules.md
→ перегенерация → новое превью. Кнопка «Откатить» возвращает бэкап.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from . import config, llm

HISTORY_DIR = config.PROMPTS_DIR / ".history"
INCIDENTS = config.RUNS_DIR / "incidents.md"  # локальный лог, runs/ в .gitignore

_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "redo_copy": {"type": "boolean"},
        "redo_photo": {"type": "boolean"},
        "note_for_regen": {"type": "string"},
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": ["Тексты", "Визуал"]},
                    "rule": {"type": "string"},
                },
                "required": ["section", "rule"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "redo_copy", "redo_photo", "note_for_regen", "rules"],
    "additionalProperties": False,
}


def analyze(feedback: str, run_dir: Path) -> dict:
    """Замечание редактора → план: что перегнать и какие правила выучить навсегда."""
    copy = json.loads((run_dir / "copy.json").read_text(encoding="utf-8"))
    blocks: list[dict] = [
        {
            "type": "text",
            "text": (
                "Ты фиксик конвейера каруселей. Редактор посмотрел карусель "
                "и оставила замечание. Разбери его:\n"
                "1) analysis - что именно не так, своими словами, коротко;\n"
                "2) redo_copy - надо ли перегенерировать ТЕКСТЫ слайдов;\n"
                "3) redo_photo - надо ли перегенерировать ФОТО обложки;\n"
                "4) note_for_regen - конкретная инструкция для перегенерации (что изменить);\n"
                "5) rules - 0-3 ПОСТОЯННЫХ правила из этого замечания (чтобы брак не повторялся "
                "во всех будущих каруселях), каждое в секцию «Тексты» или «Визуал». "
                "Правило формулируй обобщённо, а не про конкретный слайд.\n\n"
                f"ЗАМЕЧАНИЕ: {feedback}\n\n"
                f"Тексты карусели (для контекста): {json.dumps(copy, ensure_ascii=False)[:3000]}\n"
                "Фото обложки приложено."
            ),
        }
    ]
    cover = run_dir / "cover-bg.png"
    if cover.is_file():
        blocks.append(llm.image_block(cover, max_side=1024))
    return llm.structured(blocks, _SCHEMA)


def proposal_text(plan: dict) -> str:
    lines = ["🔧 Предложение фиксика:", "", f"Диагноз: {plan['analysis']}", ""]
    todo = []
    if plan["redo_copy"]:
        todo.append("перегенерирую тексты")
    if plan["redo_photo"]:
        todo.append("перегенерирую фото обложки")
    lines.append("Сейчас: " + (", ".join(todo) if todo else "только правки правил") +
                 (f" - с учётом: {plan['note_for_regen']}" if plan["note_for_regen"] else ""))
    if plan["rules"]:
        lines.append("")
        lines.append("Правила навсегда (для всех будущих каруселей):")
        for r in plan["rules"]:
            lines.append(f"• [{r['section']}] {r['rule']}")
    return "\n".join(lines)


def apply_rules(plan: dict, feedback: str) -> str | None:
    """Бэкап + запись правил в extra_rules.md и INCIDENTS.md. Возвращает id бэкапа."""
    if not plan["rules"]:
        return None
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = HISTORY_DIR / ts
    backup.mkdir(parents=True, exist_ok=True)
    rules_file = config.PROMPTS_DIR / "extra_rules.md"
    shutil.copy(rules_file, backup / "extra_rules.md")

    text = rules_file.read_text(encoding="utf-8")
    today = dt.date.today().isoformat()
    for r in plan["rules"]:
        marker = f"## {r['section']}"
        text = text.replace(marker, marker + f"\n- [{today}] {r['rule']}", 1)
    rules_file.write_text(text, encoding="utf-8")

    INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
    with INCIDENTS.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n## INC-{ts}\n- Замечание: {feedback}\n"
            + (f"- Диагноз: {plan['analysis']}\n" if plan.get("analysis") else "")
            + "".join(f"- Правило [{r['section']}]: {r['rule']}\n" for r in plan["rules"])
            + f"- Бэкап для отката: prompts/.history/{ts}/\n"
        )
    return ts


_FAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["transient", "access", "logic"]},
        "diagnosis": {"type": "string"},
        "advice": {"type": "string"},
    },
    "required": ["kind", "diagnosis", "advice"],
    "additionalProperties": False,
}


def record_incident(stage: str, error_text: str) -> str:
    """Падение конвейера → запись в runs/incidents.md. Возвращает id инцидента."""
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
    with INCIDENTS.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## INC-{ts} (авто, {stage})\n```\n{error_text[-1500:]}\n```\n- status: open\n")
    return ts


# Известные сбои, которые НЕ требуют человека. Определяем строкой, а не догадками LLM:
# 16.08 фиксик принял пустой пул провайдера kie за «протухли ключи» и зря дёрнул людей.
_TRANSIENT_MARKERS = {
    "no user can use": "У kie временно кончился пул провайдера под эту модель "
                       "(ключи и баланс в порядке). Пробую резервную модель и повторяю.",
    "все модели недоступны": "Ни основная, ни резервные модели kie сейчас не отвечают. "
                             "Похоже на аварию у провайдера - повторю позже.",
    "timed out": "Таймаут сети при обращении к внешнему API. Повторю.",
    "timeout": "Таймаут сети при обращении к внешнему API. Повторю.",
    "connection": "Сеть моргнула на запросе к внешнему API. Повторю.",
    "http 500": "Внешний API ответил 500. Повторю.",
    "http 502": "Внешний API ответил 502. Повторю.",
    "http 503": "Внешний API ответил 503. Повторю.",
    "http 429": "Уперлись в лимит запросов внешнего API. Повторю позже.",
}


def classify_failure(error_text: str) -> dict:
    """Диагноз падения: transient (ретраим сами) / access (нужен человек) / logic (баг)."""
    low = error_text.lower()
    for marker, diagnosis in _TRANSIENT_MARKERS.items():
        if marker in low:
            return {"kind": "transient", "diagnosis": diagnosis, "advice": "повторный прогон"}
    return llm.structured(
        [{
            "type": "text",
            "text": (
                "Ты - фиксик конвейера. Прогон упал, вот хвост трейсбека/лога. Классифицируй:\n"
                "- transient: сетевой сбой, 5xx/таймаут внешнего API (kie, telegram) - лечится повтором;\n"
                "- access: протухшие ключи/куки/права - нужен человек; в advice напиши, ЧТО обновить и как. "
                "ВАЖНО: ставь access ТОЛЬКО при явном 401/403/«invalid key»/«unauthorized»/«insufficient balance». "
                "Ошибки вида «нет свободных аккаунтов», 5xx, таймауты - это transient, а не access;\n"
                "- logic: баг кода или промпта - в advice опиши, где именно и что чинить.\n"
                "diagnosis - одна-две фразы по-русски, для сообщения в Telegram.\n\n"
                + error_text[-2500:]
            ),
        }],
        _FAIL_SCHEMA,
        max_tokens=1500,
    )


def rollback(backup_id: str) -> bool:
    src = HISTORY_DIR / backup_id / "extra_rules.md"
    if not src.is_file():
        return False
    shutil.copy(src, config.PROMPTS_DIR / "extra_rules.md")
    INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
    with INCIDENTS.open("a", encoding="utf-8") as fh:
        fh.write(f"\n- ОТКАТ правок {backup_id} ({dt.datetime.now():%Y-%m-%d %H:%M})\n")
    return True


def list_rules() -> list[dict]:
    """Все выученные правила: [{'section', 'rule', 'line'}] - для показа и удаления."""
    text = (config.PROMPTS_DIR / "extra_rules.md").read_text(encoding="utf-8")
    rules, section = [], ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.strip().startswith("- ") and section in {"Тексты", "Визуал"}:
            rules.append({"section": section, "rule": line.strip()[2:], "line": line})
    return rules


def remove_rule(line: str) -> bool:
    """Удаление одного правила (с бэкапом - откат через rollback)."""
    rules_file = config.PROMPTS_DIR / "extra_rules.md"
    text = rules_file.read_text(encoding="utf-8")
    if line not in text:
        return False
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = HISTORY_DIR / ts
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy(rules_file, backup / "extra_rules.md")
    rules_file.write_text(text.replace(line + "\n", "", 1), encoding="utf-8")
    INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
    with INCIDENTS.open("a", encoding="utf-8") as fh:
        fh.write(f"\n- УДАЛЕНО правило «{line.strip()[2:]}» ({dt.datetime.now():%Y-%m-%d %H:%M}), "
                 f"бэкап {ts}\n")
    return True


def regenerate(run_dir: Path, plan: dict) -> list[Path]:
    """Перегенерация по плану. Возвращает список медиа для нового превью."""
    from . import copywriter, cover_live, qa_gate, redraw, render

    theme = json.loads((run_dir / "theme.json").read_text(encoding="utf-8"))
    note = plan["note_for_regen"]

    if plan["redo_photo"]:
        import run_daily  # переиспользуем цикл редроу-с-QA

        chosen = [Path(p) for p in json.loads((run_dir / "pins-selected.json").read_text())]
        (run_dir / "cover-bg.png").unlink(missing_ok=True)
        for old in run_dir.glob("redraw-*.png"):
            old.unlink()
        # note подмешается через extra_rules (правила уже применены) + прямой инструкцией
        redraw_prompt_note = note
        photo = None
        for pin in chosen:
            prompt = redraw.build_redraw_prompt(pin, theme, extra_note=redraw_prompt_note)
            kie = redraw.KieClient(config.require("KIE_API_KEY", config.KIE_API_KEY))
            url = kie.upload_file(pin, upload_path="carousel/pins")
            urls = kie.redraw_image(config.KIE_IMAGE_MODEL, prompt, url, aspect_ratio="4:5")
            candidate = run_dir / "redraw-fix.png"
            kie.download(urls[0], candidate)
            report = qa_gate.check_photo(candidate, run_dir / "qa-fix.json")
            if report["passed"]:
                shutil.copy(candidate, run_dir / "cover-bg.png")
                photo = candidate
                break
        if photo is None:
            raise RuntimeError("Перегон фото не прошёл QA - нужен ещё один заход")

    if plan["redo_copy"]:
        (run_dir / "copy.json").unlink(missing_ok=True)
        copywriter.write_copy(theme, run_dir / "copy.json", extra_note=note)

    # Перевёрстка (всегда: тексты или фон могли смениться)
    copy = json.loads((run_dir / "copy.json").read_text(encoding="utf-8"))
    slides_dir = run_dir / "slides"
    hook = copy["slides"][0]
    render.render_cover(run_dir / "cover-bg.png", hook["head"], hook["accent_word"],
                        copy["cover_sub"], slides_dir / "slide-01.png")
    for slide in copy["slides"][1:]:
        render.render_card(slide, 9, slides_dir / f"slide-{slide['n']:02d}.png")

    media = sorted(slides_dir.glob("slide-*.png"))

    # Живая обложка: пересобрать оверлей; veo дёргаем заново только если фон сменился
    raw = run_dir / "cover-raw.mp4"
    live = run_dir / "cover-live.mp4"
    if live.exists() or raw.exists():
        overlay = run_dir / "cover-overlay.png"
        render.render_cover_overlay(hook["head"], hook["accent_word"], copy["cover_sub"], overlay)
        if plan["redo_photo"] or not raw.exists():
            cover_live.make_live_cover(run_dir / "cover-bg.png", overlay, live)
        else:
            cover_live.assemble(raw, overlay, live)
        media = [live] + media[1:]
    return media
