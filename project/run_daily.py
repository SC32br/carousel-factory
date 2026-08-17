#!/usr/bin/env python3
"""Оркестратор дневного прогона: тема → пины → редроу → тексты → слайды → QA → пост.

Запуск (из папки project/):
  .venv/bin/python run_daily.py run              - собрать карусель за сегодня
  .venv/bin/python run_daily.py run --no-publish - собрать, но не постить (для теста)
  .venv/bin/python run_daily.py publish 2026-08-15 - опубликовать готовый прогон
Каждый шаг пишет результат в runs/ГГГГ-ММ-ДД/ - повторный запуск продолжает с места сбоя.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

from carousel import config, copywriter, designer, master, pins, select_pins, slide_qa
from carousel import telegram_notify, zernio_client

THEMES = json.loads((config.PROJECT_ROOT / "themes.json").read_text(encoding="utf-8"))
QA_MAX_ATTEMPTS = 3  # редроу + до 2 регенераций с усиленным негативом


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def cleanup_old_runs() -> None:
    cutoff = dt.date.today() - dt.timedelta(days=config.KEEP_RUNS_DAYS)
    for run_dir in config.RUNS_DIR.glob("20??-??-??"):
        try:
            if dt.date.fromisoformat(run_dir.name) < cutoff:
                shutil.rmtree(run_dir, ignore_errors=True)
                log(f"чищу старый прогон {run_dir.name}")
        except ValueError:
            continue


def step_master_with_qa(design: dict, heads: list[str], run_dir: Path,
                        slides_dir: Path) -> list[Path]:
    """Генерация мастера → нарезка → QA текста. Брак = перегенерация (до 3 попыток)."""
    if (slides_dir / "slide-09.png").is_file() and (run_dir / "slides-qa.json").is_file():
        report = json.loads((run_dir / "slides-qa.json").read_text(encoding="utf-8"))
        if report.get("passed"):
            return sorted(slides_dir.glob("slide-*.png"))

    prompt = designer.build_master_prompt(design, heads)
    for attempt in range(1, QA_MAX_ATTEMPTS + 1):
        log(f"шаг 5: генерю мастер-кадр 3:4@4K, попытка {attempt}…")
        master_png = run_dir / f"master-{attempt}.png"
        master.generate_master(prompt, master_png)

        cut = master.check_cut_lines(master_png, run_dir / f"cutlines-{attempt}.json")
        if not cut["passed"]:
            log(f"линии реза светлые {cut['white_ratio']} - перегенерирую")
            prompt += ("\nВАЖНО: панели стыкуются вплотную, между ними НЕТ белых линий, "
                       "рамок, полей и разделителей.")
            continue

        log("шаг 6: режу 3×3 → 9 слайдов 1080×1350…")
        slides = master.slice_master(master_png, slides_dir)

        log("шаг 6б: QA текста на слайдах…")
        report = slide_qa.check_slides(slides, heads, run_dir / "slides-qa.json")
        if report["passed"]:
            log(f"QA слайдов: PASS на попытке {attempt}")
            (run_dir / "master.png").write_bytes(master_png.read_bytes())
            return slides

        bad = slide_qa.failed_slides(report)
        problems = {p for s in report["slides"] for p in s["problems"]}
        log(f"QA слайдов: брак на {bad} ({', '.join(problems)}) - перегенерирую")
        prompt += ("\nИСПРАВЬ ОБЯЗАТЕЛЬНО: " + "; ".join(problems)
                   + ". Тексты панелей писать дословно, буквы целые, ничего лишнего.")

    raise RuntimeError(
        f"QA забраковал все {QA_MAX_ATTEMPTS} мастер-кадров - смотри slides-qa.json в {run_dir}"
    )


def step_redraw_with_qa(pin_candidates: list[Path], theme: dict, run_dir: Path) -> Path:
    """Редроу с QA-гейтом: провал → усиленный негатив → следующая попытка/пин."""
    photo = run_dir / "cover-bg.png"
    attempt = 0
    for pin in pin_candidates:
        base_prompt_extra = ""
        for _ in range(QA_MAX_ATTEMPTS):
            attempt += 1
            log(f"редроу: попытка {attempt} (пин {pin.name})")
            prompt = redraw.build_redraw_prompt(pin, theme) + base_prompt_extra
            (run_dir / f"redraw-{attempt}.prompt.txt").write_text(prompt, encoding="utf-8")

            kie = redraw.KieClient(config.require("KIE_API_KEY", config.KIE_API_KEY))
            source_url = kie.upload_file(pin, upload_path="carousel/pins")
            urls = kie.redraw_image(config.KIE_IMAGE_MODEL, prompt, source_url, aspect_ratio="4:5")
            candidate = run_dir / f"redraw-{attempt}.png"
            kie.download(urls[0], candidate)

            report = qa_gate.check_photo(candidate, run_dir / f"qa-{attempt}.json")
            if report["passed"]:
                shutil.copy(candidate, photo)
                log(f"QA: PASS на попытке {attempt}")
                return photo
            fails = ", ".join(f["rule"] for f in report["fails"])
            log(f"QA: брак ({fails}) - регенерирую")
            base_prompt_extra = qa_gate.negative_prompt_addon(report["fails"])
    raise RuntimeError(
        f"QA-гейт забраковал все {attempt} попыток редроу - смотри qa-*.json в {run_dir}"
    )


def build(run_dir: Path, *, publish: bool, theme: dict | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    theme = theme or THEMES[str(dt.date.today().weekday())]
    log(f"тема: {theme['name']} (сегмент: {theme['segment']})")
    (run_dir / "theme.json").write_text(json.dumps(theme, ensure_ascii=False), encoding="utf-8")

    # 1-2. Пины: источник свежих трендов для дизайнера (не референс генерации)
    pins_note_file = run_dir / "pins-note.txt"
    if not pins_note_file.is_file():
        try:
            log("шаг 1: парсю свежие пины (тренды)…")
            all_pins = pins.parse_pins(theme["queries"], run_dir / "pins")
            log(f"шаг 2: vision-разбор {len(all_pins)} пинов…")
            note = select_pins.trend_note(all_pins, theme, run_dir / "pins-scores.json")
        except Exception as exc:  # noqa: BLE001 - тренды приятны, но не обязательны
            log(f"пины недоступны ({str(exc)[:120]}) - иду без них")
            note = ""
        pins_note_file.write_text(note, encoding="utf-8")
    pins_note = pins_note_file.read_text(encoding="utf-8")

    # 3. Копирайт (тексты 9 слайдов + подпись)
    copy_file = run_dir / "copy.json"
    if not copy_file.is_file():
        log("шаг 3: пишу тексты 9 слайдов…")
        copywriter.write_copy(theme, copy_file)
    copy = json.loads(copy_file.read_text(encoding="utf-8"))
    heads = [s["head"].replace("<br>", " ") for s in copy["slides"]]

    # 4. Дизайнер: предмет и сцена для каждой панели
    design_file = run_dir / "design.json"
    if not design_file.is_file():
        log("шаг 4: дизайнер придумывает 9 сцен…")
        designer.design_panels(theme, heads, design_file, pins_note=pins_note)
    design = json.loads(design_file.read_text(encoding="utf-8"))

    # 5-6. Мастер-кадр 3:4@4K → нарезка 3×3 → 9 слайдов 1080×1350, с QA текста
    slides_dir = run_dir / "slides"
    media = step_master_with_qa(design, heads, run_dir, slides_dir)

    # 7. Живая обложка: оживляем ПЕРВЫЙ слайд (звук эмбиента, без речи).
    # Косяк видео не роняет карусель - откатываемся на статичный слайд.
    if config.LIVE_COVER:
        cover_mp4 = run_dir / "cover-live.mp4"
        if not cover_mp4.is_file():
            from carousel import cover_live, fixic

            for attempt in (1, 2):
                try:
                    log(f"шаг 7: оживляю первый слайд (grok + звук), попытка {attempt}…")
                    cover_live.animate_slide(media[0], design, cover_mp4)
                    break
                except Exception as exc:  # noqa: BLE001 - видео опционально
                    log(f"живая обложка не вышла: {str(exc)[:200]}")
                    cover_mp4.unlink(missing_ok=True)
                    if attempt == 2:
                        fixic.record_incident("живая обложка", str(exc))
                        telegram_notify.send_text(
                            "ℹ️ Живая обложка сегодня не получилась "
                            f"({str(exc)[:150]}) - карусель со статичным первым слайдом, "
                            "остальное в порядке."
                        )
        if cover_mp4.is_file():
            media = [cover_mp4] + media[1:]

    log(f"собрано: {len(media)} элементов карусели в {run_dir}")

    # 8-9. Превью с кнопками «Опубликовать / Доработать» (решение за человеком в Telegram),
    # либо автопост без человека, если явно включён AUTO_PUBLISH=1.
    if publish and config.AUTO_PUBLISH:
        do_publish(run_dir)
    else:
        telegram_notify.send_review_request(run_dir.name, media, copy["caption"])
        log(f"готово, жду кнопку в Telegram (резерв: run_daily.py publish {run_dir.name})")
    (run_dir / "failure.json").unlink(missing_ok=True)


def handle_failure(run_dir: Path, cmd: list[str], error_text: str) -> None:
    """Авто-фиксик при падении: инцидент → диагноз → уведомление → план ретрая."""
    from carousel import fixic

    run_dir.mkdir(parents=True, exist_ok=True)
    inc_id = fixic.record_incident(f"прогон {run_dir.name}", error_text)
    try:
        verdict = fixic.classify_failure(error_text)
    except Exception:  # даже диагност упал - считаем транзиентом
        verdict = {"kind": "transient", "diagnosis": "не удалось поставить диагноз (LLM недоступен)",
                   "advice": "повторю прогон"}

    fail_file = run_dir / "failure.json"
    attempts = 0
    if fail_file.is_file():
        attempts = json.loads(fail_file.read_text()).get("attempts", 0)
    fail_file.write_text(json.dumps({
        "inc": inc_id, "kind": verdict["kind"], "attempts": attempts, "cmd": cmd,
        "retry_at": (dt.datetime.now() + dt.timedelta(minutes=10)).timestamp()
        if verdict["kind"] == "transient" and attempts < 2 else None,
    }, ensure_ascii=False), encoding="utf-8")

    plan = {
        "transient": f"Повторю сам через 10 минут (попытка {attempts + 1} из 3)."
        if attempts < 2 else "Три попытки исчерпаны - нужен взгляд человека.",
        "access": f"Сам не починю, нужен человек: {verdict['advice']}",
        "logic": f"Похоже на баг. Что чинить: {verdict['advice']} (детали в runs/incidents.md, {inc_id})",
    }[verdict["kind"]]
    telegram_notify.send_text(
        f"⚠ Прогон {run_dir.name} упал.\nДиагноз фиксика: {verdict['diagnosis']}\n{plan}"
    )
    log(f"падение: {verdict['kind']} - {verdict['diagnosis']}")


def next_extra_run_dir() -> Path:
    today = dt.date.today().isoformat()
    n = 1
    while (config.RUNS_DIR / f"{today}-extra{n}").exists():
        n += 1
    return config.RUNS_DIR / f"{today}-extra{n}"


def do_publish(run_dir: Path) -> dict | None:
    copy = json.loads((run_dir / "copy.json").read_text(encoding="utf-8"))
    slides_dir = run_dir / "slides"
    media = sorted(slides_dir.glob("slide-*.png"))
    cover_mp4 = run_dir / "cover-live.mp4"
    if cover_mp4.is_file():
        media = [cover_mp4] + media[1:]
    if (run_dir / "published.json").is_file():
        log("этот прогон уже опубликован - выходим, дубль не постим")
        return json.loads((run_dir / "published.json").read_text(encoding="utf-8"))
    log(f"публикую карусель ({len(media)} элементов) через zernio…")
    result = zernio_client.publish_carousel(media, copy["caption"])
    (run_dir / "published.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"опубликовано ✔ {result.get('post_url') or ''}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="собрать карусель за сегодня")
    p_run.add_argument("--no-publish", action="store_true", help="только собрать, не постить")
    p_pub = sub.add_parser("publish", help="опубликовать готовый прогон")
    p_pub.add_argument("date", help="дата прогона, напр. 2026-08-15")
    p_cus = sub.add_parser("custom", help="внеплановый прогон по брифу директора")
    p_cus.add_argument("brief", help="путь к brief.json (name/segment/family/queries)")
    args = parser.parse_args()

    if args.cmd == "publish":
        run_dir = config.RUNS_DIR / args.date
        if not run_dir.is_dir():
            sys.exit(f"Нет прогона {run_dir}")
        do_publish(run_dir)
        return

    if args.cmd == "custom":
        theme = json.loads(Path(args.brief).read_text(encoding="utf-8"))
        run_dir = next_extra_run_dir()
        cmd = ["custom", args.brief]
        try:
            build(run_dir, publish=False, theme=theme)
        except Exception:
            import traceback

            handle_failure(run_dir, cmd, traceback.format_exc())
            sys.exit(1)
        return

    cleanup_old_runs()
    run_dir = config.RUNS_DIR / dt.date.today().isoformat()
    try:
        build(run_dir, publish=not getattr(args, "no_publish", False))
    except Exception:
        import traceback

        handle_failure(run_dir, ["run"], traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
