"""Шаг 4. Копирайт 9 слайдов: арка + лимиты символов."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, humanizer, llm, proofreader

# Роль и лимит на заголовок каждого слайда - таблица из доков.
_ARC = [
    (1, "Хук: боль/цифра/ошибка, читается за 2 секунды", 50),
    (2, "Проблема", 45),
    (3, "Скрытая цена проблемы, в рублях", 45),
    (4, "Механизм: как это решается", 45),
    (5, "Пруф/цифра (главное число слайда - в поле accent_word)", 45),
    (6, "Шаги 1-2-3", 45),
    (7, "Чек-лист «сохрани» (ради сохранений)", 45),
    (8, "Итог/правило", 45),
    (9, "CTA: ОДНО действие - написать кодовое слово из брифа в директ или комментарий", 40),
]

_SCHEMA = {
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

_PROMPT = """Напиши тексты карусели из 9 слайдов. Ниша и оффер берутся из темы дня,
не выдумывай чужой бренд.

Аудитория (сегмент): {segment}. Тема дня: {theme}.
Серия: «{series}». Кодовое слово CTA: «{cta}».

Голос: плотный, конкретный, с цифрами (рубли, минуты, штуки). Без канцелярита
и без инфоцыганского пафоса.

Жёсткая структура (одна мысль = один слайд):
{arc}

Конкретика:
- где уместно, ставь проверяемое число. «Теряешь деньги» плохо; «5 заявок мимо =
  12 500 ₽ за неделю» годится;
- считай на примерах ниши: средний чек × число упущенных клиентов;
- CTA на слайде 9 - строго кодовое слово «{cta}» в директ или комментарий,
  без размытых «забери схему» и «узнай подробности».

Правила:
- head - заголовок слайда, не длиннее лимита в скобках (символы с пробелами);
- accent_word - одно слово или число из head, которое выделяется цветом (обязательно
  встречается в head дословно);
- body - 1-2 короткие строки пояснения (до 90 символов);
- слайд 9: head - призыв, body - что человек получит. Слово «{cta}» в body не пиши,
  оно будет на отдельной плашке;
- cover_sub - подзаголовок для обложки (до 60 символов), продолжает хук слайда 1;
- caption - подпись к посту (до 800 символов). Первая строка - короткий хук
  (до 60 символов), затем пустая строка, затем 2-3 абзаца,
  CTA «напиши {cta} в директ», в конце 5-8 нишевых хэштегов на русском.
  В caption никаких тегов: абзацы разделяй настоящими переносами строк (\\n\\n),
  тег <br> запрещён - площадка напечатает его как текст.
- Всё на русском. Переносы строк в head (и только там) ставь тегом <br>.

Язык плотный, без ИИ-штампов. Запрещено: «не X, а Y», «это не просто…»,
длинное тире U+2014, канцелярит («важно понимать», «позволяет», «является»), размытые
прилагательные (качественный, эффективный, мощный), рваные серии «без-без-без»,
псевдостатистика «большинство/каждый». Цифры вместо абстракций."""


_TAG = re.compile(r"<[^>]+>")


def _sanitize(result: dict) -> None:
    """Теги <br> нужны только в заголовках слайдов (их рисует HTML-вёрстка).

    В подписи поста это обычный текст: теги напечатались бы дословно.
    Поэтому в caption и cover_sub переводим <br> в настоящие переносы, теги вырезаем.
    """
    caption = result["caption"].replace("<br><br>", "\n\n").replace("<br>", "\n")
    result["caption"] = _TAG.sub("", caption).strip()
    result["cover_sub"] = _TAG.sub(" ", result["cover_sub"].replace("<br>", " ")).strip()
    for slide in result["slides"]:
        # В body тегов тоже быть не должно: вёрстка кладёт его одним абзацем.
        slide["body"] = _TAG.sub(" ", slide["body"].replace("<br>", " ")).strip()
        # В head допустим только <br>; прочие теги убираем.
        slide["head"] = re.sub(r"<(?!br\s*/?>)[^>]+>", "", slide["head"]).strip()


def _check_limits(result: dict) -> None:
    """Жёсткая проверка: превышение лимитов = ошибка конвейера, а не «ну ладно»."""
    limits = {n: lim for n, _, lim in _ARC}
    for slide in result["slides"]:
        head_plain = slide["head"].replace("<br>", " ")
        if len(head_plain) > limits.get(slide["n"], 50) + 10:
            raise RuntimeError(
                f"Слайд {slide['n']}: заголовок длиннее лимита ({len(head_plain)}): {head_plain!r}"
            )
    if len(result["slides"]) != 9:
        raise RuntimeError(f"Копирайтер вернул {len(result['slides'])} слайдов вместо 9")


def write_copy(theme: dict, out_file: Path, extra_note: str = "") -> dict:
    arc = "\n".join(f"  {n}. {role} (лимит {lim})" for n, role, lim in _ARC)
    prompt = _PROMPT.format(segment=theme["segment"], theme=theme["name"], arc=arc,
                           series=config.SERIES_NAME, cta=config.CTA_WORD)

    learned = config.extra_rules("Тексты")
    if learned:
        prompt += "\n\nВыученные правила (обязательны):\n" + learned
    if extra_note:
        prompt += "\n\nЗамечания к прошлой версии (учесть обязательно):\n" + extra_note

    result = llm.structured([{"type": "text", "text": prompt}], _SCHEMA)
    _sanitize(result)
    _check_limits(result)

    # Обязательный шаг: хуманайзер чистит ИИ-штампы (контракт prompts/humanizer.md).
    result = humanizer.humanize_copy(result, out_file.with_name("humanizer-report.json"))
    _sanitize(result)
    _check_limits(result)

    # Последняя вычитка: грамматика и согласование числительных («3 клиентов» → «3 клиента»).
    result = proofreader.proofread(result, out_file.with_name("proofread-report.json"))
    _sanitize(result)
    _check_limits(result)

    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
