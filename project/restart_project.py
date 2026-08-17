#!/usr/bin/env python3
"""Полный перезапуск: гасим прогоны, поднимаем бота, пишем отчёт в Telegram.

Без systemd. В Docker контейнер с restart: unless-stopped поднимется сам
после выхода бота. На хосте скрипт заново запускает botd.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from carousel import config, kie_client  # noqa: E402
from carousel import telegram_notify as tg  # noqa: E402

PYTHON = sys.executable


def sh(*args: str, timeout: int = 60) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()


def pids_of(pattern: str) -> list[str]:
    return [p for p in sh("pgrep", "-f", pattern).split() if p]


def main() -> None:
    import datetime as dt

    started = time.monotonic()
    t0 = dt.datetime.now()
    chat_id = sys.argv[1] if len(sys.argv) > 1 else (config.TG_CHAT_IDS[0] if config.TG_CHAT_IDS else "")
    report: list[str] = ["Полный перезапуск проекта"]

    runs = pids_of("run_daily.py")
    for pid in runs:
        subprocess.run(["kill", "-TERM", pid], capture_output=True)
    if runs:
        time.sleep(5)
        for pid in pids_of("run_daily.py"):
            subprocess.run(["kill", "-KILL", pid], capture_output=True)
    report.append(f"• прогоны конвейера: остановлено {len(runs)}"
                  + (f" ({', '.join(runs)})" if runs else ""))

    stale = list(config.RUNS_DIR.glob("*/failure.json")) + list(config.RUNS_DIR.glob("*.tmp"))
    for f in stale:
        f.unlink(missing_ok=True)
    report.append(f"• временные пометки: убрано {len(stale)}")

    in_docker = Path("/.dockerenv").is_file()
    old_pid = str(os.getppid())
    report.append(f"• среда: {'docker' if in_docker else 'хост'}, родительский pid {old_pid}")

    imports = subprocess.run(
        [PYTHON, "-c",
         "import run_daily, botd; from carousel import director, fixic, humanizer, llm, qa_gate, "
         "redraw, render, select_pins, copywriter, cover_live, zernio_client, pins, kie_client"],
        cwd=config.PROJECT_ROOT, capture_output=True, text=True, timeout=120,
    )
    report.append(
        "• модули конвейера: все загружаются"
        if imports.returncode == 0
        else "• модули конвейера: ОШИБКА: " + imports.stderr.strip()[-200:]
    )
    ok = imports.returncode == 0

    tg_ok = tg.call("getMe").get("ok")
    report.append(f"• связь с Telegram: {'есть' if tg_ok else 'НЕТ'}")

    credits = kie_client.get_credits(config.KIE_API_KEY) if config.KIE_API_KEY else None
    report.append(f"• кредиты kie: {credits:.0f}" if credits is not None else "• кредиты kie: не отвечает")

    left = pids_of("run_daily.py")
    report.append(f"• зависших процессов: {len(left)}" + (f"  {', '.join(left)}" if left else " ок"))
    ok = ok and not left

    free_gb = __import__("shutil").disk_usage(config.PROJECT_ROOT).free / 2**30
    report.append(f"• свободно на диске: {free_gb:.0f} ГБ")

    elapsed = time.monotonic() - started
    report.append(f"• заняло: {elapsed:.1f} сек ({t0:%H:%M:%S} → {dt.datetime.now():%H:%M:%S})")
    report.insert(1, "Всё поднялось, проект работает" if ok else "Есть проблемы, смотри список")
    if chat_id:
        tg.call("sendMessage", chat_id=chat_id, text="\n".join(report))
    print("\n".join(report))

    flag = config.RUNS_DIR / ".restart-notify"
    if chat_id:
        flag.write_text(f"{chat_id}\n{os.getpid()}", encoding="utf-8")
    if in_docker:
        # Гасим бота (часто pid 1). Compose restart: unless-stopped поднимет контейнер.
        bot_pids = [pid for pid in pids_of("botd.py") if pid != str(os.getpid())]
        for pid in bot_pids:
            subprocess.run(["kill", "-TERM", pid], capture_output=True)
    else:
        subprocess.Popen([PYTHON, "-u", "botd.py"], cwd=config.PROJECT_ROOT, start_new_session=True)


if __name__ == "__main__":
    main()
