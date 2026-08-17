#!/usr/bin/env python3
"""Демон Telegram-бота. Роутинг детерминированный - по кнопкам и состояниям, LLM его не решает.

Таблица маршрутов:
  кнопка pub:/fix:/apply:/cancel:/rb:/go:/fb:/np:  → зашитое действие (фиксик/публикация/запуск)
  текст или голос, состояние «feedback»            → фиксик (замечание к прогону run_id)
  текст или голос, состояние «director» или пусто  → директор (интейк/статус)
Состояния сгорают через 2 часа. Права: «Опубликовать» - только TG_PUBLISH_CHAT_ID
(если задан; иначе любой чат из TG_CHAT_IDS).
Плюс фоновая забота: ретрай упавших transient-прогонов (failure.json).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from carousel import config, director, fixic, kie_client, llm
from carousel import telegram_notify as tg
from carousel import zernio_client

STATE_FILE = config.RUNS_DIR / "bot-state.json"
RESTART_FLAG = config.RUNS_DIR / ".restart-notify"
STATE_TTL = 2 * 3600
PYTHON = sys.executable
# Слова, по которым (без участия LLM) предлагаем перезапуск - чтобы не путать с «перегенерируй»
RESTART_WORDS = ("перезапусти", "перезагрузи", "рестарт", "restart", "переподними")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_FILE.is_file():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if "chats" in state:
            return state
    return {"offset": 0, "chats": {}, "proposals": {}, "briefs": {}, "next_id": 1}


_state_lock = threading.Lock()


def save_state(state: dict) -> None:
    """Атомарная запись: обработчики работают в потоках, файл рвать нельзя."""
    with _state_lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)


def chat_state(state: dict, chat_id) -> dict | None:
    """Состояние чата с учётом сгорания по TTL."""
    entry = state["chats"].get(str(chat_id))
    if entry and time.time() > entry.get("expires", 0):
        state["chats"].pop(str(chat_id), None)
        return {"_expired": entry}
    return entry


def set_chat_state(state: dict, chat_id, mode: str, **extra) -> None:
    state["chats"][str(chat_id)] = {"mode": mode, "expires": time.time() + STATE_TTL, **extra}
    save_state(state)


def known_chat(chat_id) -> bool:
    return str(chat_id) in config.TG_CHAT_IDS


def new_id(state: dict) -> str:
    pid = str(state["next_id"])
    state["next_id"] += 1
    return pid


# ---------- действия ----------

def do_publish_run(run_id: str) -> str:
    import run_daily

    if not zernio_client.targets_configured():
        return ("⚠ Zernio ещё не подключён: в .env пустые ZERNIO_API_KEY / "
                "ZERNIO_TARGETS. Как появятся ключи - кнопка заработает.")
    run_dir = config.RUNS_DIR / run_id
    if not run_dir.is_dir():
        return f"Не нашёл прогон {run_id}"
    try:
        result = run_daily.do_publish(run_dir)
        url = (result or {}).get("post_url")
        return (f"✔ Карусель {run_id} опубликована"
                + (f"\n{url}" if url else "\n(ссылку zernio не вернул - проверь аккаунт)"))
    except Exception as exc:  # noqa: BLE001 - причина уходит человеку в чат
        return f"⚠ Публикация не прошла: {str(exc)[:300]}"


def launch_run(cmd_args: list[str], label: str) -> None:
    """Запуск конвейера отдельным процессом, лог в runs/."""
    logfile = (config.RUNS_DIR / f"launch-{dt.datetime.now():%Y%m%d-%H%M%S}.log").open("w")
    subprocess.Popen([PYTHON, "-u", "run_daily.py", *cmd_args],
                     cwd=config.PROJECT_ROOT, stdout=logfile, stderr=subprocess.STDOUT)
    log(f"запущен прогон: {label}")


def message_text(msg: dict) -> str | None:
    """Текст сообщения; голосовые расшифровываем."""
    if msg.get("text"):
        return msg["text"].strip()
    voice = msg.get("voice") or msg.get("audio")
    if voice:
        info = tg.call("getFile", file_id=voice["file_id"])
        if not info.get("ok"):
            return None
        url = f"https://api.telegram.org/file/bot{config.TG_BOT_TOKEN}/{info['result']['file_path']}"
        import requests

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as fh:
            fh.write(requests.get(url, timeout=120).content)
            path = Path(fh.name)
        try:
            return llm.transcribe_voice(path)
        finally:
            path.unlink(missing_ok=True)
    return None


def retry_failed_runs() -> None:
    """Транзиентные падения: перезапуск по failure.json (авто-фиксик, без человека)."""
    for fail_file in config.RUNS_DIR.glob("*/failure.json"):
        try:
            info = json.loads(fail_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        retry_at = info.get("retry_at")
        if not retry_at or time.time() < retry_at:
            continue
        info["attempts"] = info.get("attempts", 0) + 1
        info["retry_at"] = None
        fail_file.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        tg.send_text(f"🔄 Повторяю упавший прогон {fail_file.parent.name} (попытка {info['attempts'] + 1})")
        launch_run(info.get("cmd", ["run"]), f"ретрай {fail_file.parent.name}")


# ---------- обработчики ----------

def handle_callback(state: dict, cq: dict) -> None:
    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    mid = cq["message"]["message_id"]

    def answer(text: str = "", alert: bool = False):
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=text, show_alert=alert)

    def drop_buttons():
        tg.call("editMessageReplyMarkup", chat_id=chat_id, message_id=mid, reply_markup="")

    log(f"кнопка {data!r} от {chat_id}")
    if not known_chat(chat_id):
        answer("Нет доступа")
        return

    if data.startswith("pub:"):
        run_id = data[4:]
        if config.TG_PUBLISH_CHAT_ID and str(chat_id) != config.TG_PUBLISH_CHAT_ID:
            answer("Публикацию подтверждает владелец аккаунта", alert=True)
            return
        answer("Публикую…")
        drop_buttons()
        result = do_publish_run(run_id)
        for cid in config.TG_CHAT_IDS:
            tg.call("sendMessage", chat_id=cid, text=result)

    elif data.startswith("fix:"):
        run_id = data[4:]
        set_chat_state(state, chat_id, "feedback", run_id=run_id)
        answer()
        tg.call("sendMessage", chat_id=chat_id,
                text=f"Ок. Напиши текстом или голосовым, что не так в карусели {run_id}.")

    elif data.startswith(("apply:", "cancel:")):
        action, pid = data.split(":", 1)
        prop = state["proposals"].pop(pid, None)
        brief = state["briefs"].pop(pid, None)
        save_state(state)
        drop_buttons()
        if action == "cancel":
            answer("Отменено")
            return
        if not prop:
            answer("Уже неактуально" if not brief else "Это был бриф - нажми Погнали", alert=True)
            return
        answer("Применяю…")
        try:
            backup_id = fixic.apply_rules(prop["plan"], prop["feedback"])
            tg.call("sendMessage", chat_id=chat_id,
                    text="Правки принял, перегенерирую - новое превью через несколько минут…")
            media = fixic.regenerate(config.RUNS_DIR / prop["run_id"], prop["plan"])
            copy = json.loads((config.RUNS_DIR / prop["run_id"] / "copy.json").read_text(encoding="utf-8"))
            tg.send_review_request(prop["run_id"], media, copy["caption"])
            if backup_id:
                tg.call("sendMessage", chat_id=chat_id,
                        text=f"Выученные правила записаны (бэкап {backup_id}).",
                        reply_markup=json.dumps({"inline_keyboard": [[
                            {"text": "↩ Откатить правила", "callback_data": f"rb:{backup_id}"}]]}))
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            tg.call("sendMessage", chat_id=chat_id, text=f"⚠ Доработка споткнулась: {str(exc)[:300]}")

    elif data.startswith("rb:"):
        ok = fixic.rollback(data[3:])
        answer("Откатил ✔" if ok else "Бэкап не найден", alert=not ok)
        if ok:
            drop_buttons()

    elif data.startswith("go:"):
        pid = data[3:]
        brief = state["briefs"].pop(pid, None)
        save_state(state)
        drop_buttons()
        if not brief:
            answer("Бриф устарел - расскажи идею ещё раз", alert=True)
            return
        if director.extras_today() >= config.EXTRA_RUNS_PER_DAY and not brief.get("force"):
            brief["force"] = True
            npid = new_id(state)
            state["briefs"][npid] = brief
            save_state(state)
            answer()
            tg.call("sendMessage", chat_id=chat_id,
                    text=f"Сегодня уже {director.extras_today()} внеплановых из "
                         f"{config.EXTRA_RUNS_PER_DAY}. Точно запускаем ещё одну?",
                    reply_markup=json.dumps({"inline_keyboard": [[
                        {"text": "Да, запускай", "callback_data": f"go:{npid}"},
                        {"text": "Отмена", "callback_data": f"cancel:{npid}"}]]}))
            return
        answer("Запускаю 🚀")
        brief_path = director.save_brief({k: v for k, v in brief.items() if k != "force"})
        launch_run(["custom", str(brief_path)], f"внеплановый: {brief['name']}")
        tg.call("sendMessage", chat_id=chat_id,
                text="🚀 Конвейер пошёл: пины → редроу → тексты → QA. Превью придёт через 5-10 минут.")

    elif data.startswith("fb:"):  # уточнение «это было замечание»
        run_id = data[3:]
        set_chat_state(state, chat_id, "feedback", run_id=run_id)
        drop_buttons()
        answer()
        tg.call("sendMessage", chat_id=chat_id,
                text="Понял, это замечание. Повтори его одним сообщением - отдам фиксику.")

    elif data.startswith("rst:"):  # перезапуск (только по явной кнопке)
        drop_buttons()
        if data == "rst:no":
            answer("Отменено")
            return
        if data == "rst:full":
            answer("Запускаю полный перезапуск…")
            tg.call("sendMessage", chat_id=chat_id,
                    text="🏗 Полный перезапуск проекта: гашу процессы, поднимаю бота, "
                         "проверяю всё по списку…")
            log("полный перезапуск проекта (по кнопке)")
            # Отдельным процессом: он переживёт перезапуск бота и сам пришлёт отчёт
            subprocess.Popen([PYTHON, "-u", "restart_project.py", str(chat_id)],
                             cwd=config.PROJECT_ROOT, start_new_session=True,
                             stdout=(config.RUNS_DIR / "restart.log").open("a"),
                             stderr=subprocess.STDOUT)
            return

        hard = data == "rst:hard"
        answer("Перезапускаюсь…")
        if hard:
            killed = running_runs()
            for pid, _ in killed:
                subprocess.run(["kill", "-TERM", pid], capture_output=True)
            # Оборванный прогон помечаем, чтобы он не считался «упавшим сам по себе»
            for fail in config.RUNS_DIR.glob("*/failure.json"):
                fail.unlink(missing_ok=True)
            tg.call("sendMessage", chat_id=chat_id, text=(
                (f"🧨 Оборвал прогонов: {len(killed)} "
                 f"({', '.join(f'PID {p}' for p, _ in killed)}). "
                 if killed else "🧨 Идущих прогонов не было. ")
                + "Перезапускаю бота…"))
            log(f"жёсткий перезапуск, оборвано прогонов: {len(killed)}")
        else:
            tg.call("sendMessage", chat_id=chat_id,
                    text="🔄 Перезапускаю бота…")
        # PID до перезапуска - чтобы в отчёте было видно, что процесс реально сменился
        RESTART_FLAG.write_text(f"{chat_id}\n{os.getpid()}", encoding="utf-8")
        # Docker: restart policy поднимет процесс. На хосте: заново exec botd.py.
        if Path("/.dockerenv").is_file():
            os._exit(0)
        subprocess.Popen([PYTHON, "-u", "botd.py"], cwd=config.PROJECT_ROOT,
                         start_new_session=True)
        os._exit(0)

    elif data.startswith("rule:"):  # подтверждение нового правила от директора
        pid = data[5:]
        rule = state["briefs"].pop(pid, None)
        save_state(state)
        drop_buttons()
        if not rule:
            answer("Уже неактуально", alert=True)
            return
        backup_id = fixic.apply_rules({"rules": [rule]}, f"правило от {chat_id} через бота")
        answer("Записал ✔")
        tg.call("sendMessage", chat_id=chat_id,
                text=f"✔ Правило записано, действует со следующей карусели.\n"
                     f"Посмотреть все - /rules",
                reply_markup=json.dumps({"inline_keyboard": [[
                    {"text": "↩ Откатить", "callback_data": f"rb:{backup_id}"}]]}) if backup_id else "")

    elif data.startswith("delrule:"):  # удаление правила из /rules
        idx = int(data[8:])
        rules = fixic.list_rules()
        if idx >= len(rules):
            answer("Список изменился, открой /rules заново", alert=True)
            return
        ok = fixic.remove_rule(rules[idx]["line"])
        answer("Убрал ✔" if ok else "Не нашёл правило", alert=not ok)
        if ok:
            tg.call("sendMessage", chat_id=chat_id,
                    text=f"Правило убрано: «{rules[idx]['rule']}»")
            send_rules(chat_id)

    elif data.startswith("np:"):  # уточнение «это новая идея»
        drop_buttons()
        answer()
        set_chat_state(state, chat_id, "director", dialog=[])
        tg.call("sendMessage", chat_id=chat_id, text="Тогда расскажи идею чуть подробнее - соберу бриф.")

    else:
        answer()


def running_runs() -> list[tuple[str, str]]:
    """Идущие прогоны конвейера: [(pid, сколько уже идёт)]."""
    out = subprocess.run(["pgrep", "-f", "run_daily.py"], capture_output=True, text=True).stdout
    runs = []
    for pid in out.split():
        etime = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                               capture_output=True, text=True).stdout.strip()
        if etime:
            runs.append((pid, etime))
    return runs


def ask_restart(chat_id) -> None:
    runs = running_runs()
    status = ("Сейчас идёт прогон (" + "; ".join(f"{p}, уже {t}" for p, t in runs) + ")."
              if runs else "Прогонов сейчас нет.")
    tg.call("sendMessage", chat_id=chat_id, text=(
        f"Что сделать? {status}\n\n"
        "🔄 Перезапустить бота - обновятся команды, кнопки и правила из файлов "
        "(1-2 секунды, идущие прогоны продолжатся).\n"
        "🧨 Бот + оборвать прогоны - то же самое, но зависшие прогоны конвейера "
        "будут остановлены.\n"
        "🏗 Полный перезапуск проекта - гашу процессы конвейера, поднимаю бота "
        "заново и прогоняю проверку: модули, расписание, Telegram, кредиты, "
        "зависшие процессы. Отчёт со сменой номера процесса пришлю сюда "
        "(пара секунд, если прогонов нет; дольше - если надо гасить зависшие).\n\n"
        "Данные (прогоны, правила) не трогаются ни в одном варианте."),
        reply_markup=json.dumps({"inline_keyboard": [
            [{"text": "🔄 Перезапустить бота", "callback_data": "rst:go"}],
            [{"text": "🧨 Бот + оборвать прогоны", "callback_data": "rst:hard"}],
            [{"text": "🏗 Полный перезапуск проекта", "callback_data": "rst:full"}],
            [{"text": "✖ Отмена", "callback_data": "rst:no"}]]}))


def health_text() -> str:
    """Короткая техсводка: что запущено и всё ли на месте."""
    import shutil

    lines = ["🩺 Техпроверка:"]
    lines.append(f"• бот: процесс {os.getpid()} жив")
    running = subprocess.run(["pgrep", "-af", "run_daily.py"], capture_output=True, text=True).stdout.strip()
    lines.append("• прогон конвейера: " + ("; ".join(f"идёт {t}" for _, t in running_runs()) or "не запущен"))
    lines.append(f"• LLM: {config.LLM_MODEL}, kie-ключ: {'есть' if config.KIE_API_KEY else 'НЕТ'}")

    credits = kie_client.get_credits(config.KIE_API_KEY) if config.KIE_API_KEY else None
    if credits is None:
        lines.append("• кредиты kie: не отвечает")
    else:
        # Ориентир по расходу: обычный прогон ~2-5 кредитов, с живой обложкой ~15-20.
        per_run = 18 if config.LIVE_COVER else 5
        warn = " ⚠️ пора пополнить" if credits < per_run * 10 else ""
        lines.append(f"• кредиты kie: {credits:.0f} (хватит примерно на "
                     f"{int(credits // per_run)} каруселей){warn}")
    lines.append(f"• zernio: {'подключён' if config.ZERNIO_API_KEY else 'не подключён'}")
    lines.append(f"• живая обложка: {'вкл' if config.LIVE_COVER else 'выкл'}")
    free_gb = shutil.disk_usage(config.PROJECT_ROOT).free / 2**30
    lines.append(f"• свободно на диске: {free_gb:.0f} ГБ")
    lines.append(f"• выученных правил: {len(fixic.list_rules())}")
    return "\n".join(lines)


def send_rules(chat_id) -> None:
    """Список выученных правил, у каждого - кнопка «убрать»."""
    rules = fixic.list_rules()
    if not rules:
        tg.call("sendMessage", chat_id=chat_id, text=(
            "📖 Выученных правил пока нет.\n"
            "Скажи или напиши боту постоянное пожелание («не показывай кофе в кадре») - "
            "предложу записать навсегда."))
        return
    tg.call("sendMessage", chat_id=chat_id, text="📖 Правила, которые конвейер соблюдает всегда:")
    for i, r in enumerate(rules):
        tg.call("sendMessage", chat_id=chat_id, text=f"[{r['section']}] {r['rule']}",
                reply_markup=json.dumps({"inline_keyboard": [[
                    {"text": "🗑 Убрать это правило", "callback_data": f"delrule:{i}"}]]}))


def run_director(state: dict, chat_id, text: str) -> None:
    entry = state["chats"].get(str(chat_id)) or {}
    dialog = entry.get("dialog", []) if entry.get("mode") == "director" else []
    dialog.append({"role": "user", "text": text})

    result = director.intake(dialog)
    kind = result["kind"]

    if kind == "status":
        state["chats"].pop(str(chat_id), None)
        save_state(state)
        tg.call("sendMessage", chat_id=chat_id, text=director.status_text())
        return

    if kind == "new_rule" and result.get("rule", {}).get("rule"):
        state["chats"].pop(str(chat_id), None)
        pid = new_id(state)
        state["briefs"][pid] = result["rule"]
        save_state(state)
        tg.call("sendMessage", chat_id=chat_id,
                text=f"Понял так: [{result['rule']['section']}] {result['rule']['rule']}\n\n"
                     "Записать в правила навсегда? Будет применяться ко всем будущим каруселям.",
                reply_markup=json.dumps({"inline_keyboard": [[
                    {"text": "✅ Записать", "callback_data": f"rule:{pid}"},
                    {"text": "✖ Отмена", "callback_data": f"cancel:{pid}"}]]}))
        return

    if kind == "maybe_feedback":
        last_runs = sorted((d.name for d in config.RUNS_DIR.glob("20??-??-??*") if d.is_dir()), reverse=True)
        run_id = last_runs[0] if last_runs else ""
        tg.call("sendMessage", chat_id=chat_id,
                text="Уточню, чтобы не напутать: это замечание к готовой карусели или новая идея поста?",
                reply_markup=json.dumps({"inline_keyboard": [[
                    {"text": f"Замечание к {run_id}", "callback_data": f"fb:{run_id}"},
                    {"text": "Новая идея", "callback_data": "np:_"}]]}))
        return

    if kind == "new_post" and result["ready"]:
        state["chats"].pop(str(chat_id), None)
        pid = new_id(state)
        state["briefs"][pid] = result["brief"]
        save_state(state)
        tg.call("sendMessage", chat_id=chat_id,
                text=f"📋 Бриф:\n{result['reply']}",
                reply_markup=json.dumps({"inline_keyboard": [[
                    {"text": "🚀 Погнали", "callback_data": f"go:{pid}"},
                    {"text": "✖ Отмена", "callback_data": f"cancel:{pid}"}]]}))
        return

    # new_post без ready (уточняющий вопрос) или chat
    dialog.append({"role": "director", "text": result["reply"]})
    if kind == "new_post":
        set_chat_state(state, chat_id, "director", dialog=dialog[-8:])
    else:
        state["chats"].pop(str(chat_id), None)
        save_state(state)
    tg.call("sendMessage", chat_id=chat_id, text=result["reply"])


def handle_message(state: dict, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    if not known_chat(chat_id):
        return
    text = msg.get("text", "")
    log(f"сообщение от {chat_id}: {(text or '[голос/медиа]')[:60]!r}")

    if text.startswith("/start") or text.startswith("/help"):
        state["chats"].pop(str(chat_id), None)
        save_state(state)
        tg.call("sendMessage", chat_id=chat_id, text=(
            "Привет! Я конвейер каруселей. Что умею:\n"
            "• превью каруселей приходят сюда с кнопками «Опубликовать / Доработать»;\n"
            "• напиши или надиктуй идею - соберу бриф и запущу внеплановый пост;\n"
            "• скажи постоянное пожелание («не показывай кофе в кадре») - запишу в правила навсегда;\n"
            "• /rules - правила, /status - что готово, /health - техпроверка, /restart - перезапуск."))
        return
    if text.startswith("/status"):
        tg.call("sendMessage", chat_id=chat_id, text=director.status_text())
        return
    if text.startswith("/rules"):
        send_rules(chat_id)
        return
    if text.startswith("/restart"):
        ask_restart(chat_id)
        return
    if text.startswith("/health"):
        tg.call("sendMessage", chat_id=chat_id, text=health_text())
        return
    # «перезапустись» ловим строкой, а не смыслом - чтобы не спутать с «перегенерируй»
    if text and any(w in text.lower() for w in RESTART_WORDS) and len(text) < 60:
        ask_restart(chat_id)
        return
    if text.startswith("/new"):
        set_chat_state(state, chat_id, "director", dialog=[])
        tg.call("sendMessage", chat_id=chat_id, text="Рассказывай идею - текстом или голосом.")
        return

    content = message_text(msg)
    if not content:
        tg.call("sendMessage", chat_id=chat_id, text="Не разобрал сообщение - напиши текстом, пожалуйста.")
        return

    entry = chat_state(state, chat_id)
    if entry and entry.get("_expired"):
        old = entry["_expired"]
        if old.get("mode") == "feedback":
            tg.call("sendMessage", chat_id=chat_id,
                    text="Замечание к прошлой карусели уже не жду (прошло больше 2 часов) - "
                         "считаю это новым разговором.")
        entry = None
        save_state(state)

    if entry and entry["mode"] == "feedback":
        run_id = entry["run_id"]
        state["chats"].pop(str(chat_id), None)
        save_state(state)
        tg.call("sendMessage", chat_id=chat_id, text=f"Принял: «{content[:200]}»\nФиксик думает…")
        try:
            plan = fixic.analyze(content, config.RUNS_DIR / run_id)
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            tg.call("sendMessage", chat_id=chat_id, text=f"⚠ Фиксик споткнулся: {str(exc)[:300]}")
            return
        pid = new_id(state)
        state["proposals"][pid] = {"run_id": run_id, "feedback": content, "plan": plan}
        save_state(state)
        tg.call("sendMessage", chat_id=chat_id, text=fixic.proposal_text(plan),
                reply_markup=json.dumps({"inline_keyboard": [[
                    {"text": "✅ Применить и перегнать", "callback_data": f"apply:{pid}"},
                    {"text": "✖ Отмена", "callback_data": f"cancel:{pid}"}]]}))
        return

    try:
        run_director(state, chat_id, content)
    except Exception as exc:  # noqa: BLE001
        log(traceback.format_exc())
        tg.call("sendMessage", chat_id=chat_id, text=f"⚠ Директор споткнулся: {str(exc)[:300]}")


def safe_handle(handler, state: dict, payload: dict) -> None:
    """Обёртка для потока: падение обработчика не должно ронять демон."""
    try:
        handler(state, payload)
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())


def main() -> None:
    log("бот-демон запущен (директор + фиксик + ретраи)")
    tg.call("setMyCommands", commands=json.dumps([
        {"command": "new", "description": "🆕 Новый пост (внеплановый)"},
        {"command": "rules", "description": "📖 Правила конвейера"},
        {"command": "status", "description": "📊 Статус конвейера"},
        {"command": "health", "description": "🩺 Техпроверка"},
        {"command": "restart", "description": "🔄 Перезапустить бота"},
        {"command": "help", "description": "❓ Что я умею"},
    ]))
    # Если перезапуск был по кнопке - доложить тому, кто просил.
    if RESTART_FLAG.is_file():
        parts = RESTART_FLAG.read_text(encoding="utf-8").strip().split("\n")
        chat_id, old_pid = parts[0], (parts[1] if len(parts) > 1 else "?")
        RESTART_FLAG.unlink(missing_ok=True)
        tg.call("sendMessage", chat_id=chat_id, text=(
            f"✅ Бот перезапущен: процесс {old_pid} → {os.getpid()}, правила перечитаны.\n"
            "Расписание и данные не трогал.\n\n" + health_text()))
    state = load_state()
    last_retry_check = 0.0
    while True:
        try:
            if time.time() - last_retry_check > 60:
                retry_failed_runs()
                last_retry_check = time.time()
            # Короткий long-poll (25с) + свой запас: залипшее соединение рвём быстро,
            # чтобы ответы не ждали по минуте.
            resp = tg.call("getUpdates", offset=state["offset"] + 1, timeout=25,
                           allowed_updates="[]", request_timeout=35)
            if not resp.get("ok"):
                log(f"getUpdates: {resp.get('description')}")
                time.sleep(2)
                continue
            for upd in resp["result"]:
                state["offset"] = max(state["offset"], upd["update_id"])
                save_state(state)
                # Каждый апдейт - в своём потоке: долгая работа (LLM, перегенерация,
                # публикация) больше не морозит приём следующих команд.
                if "callback_query" in upd:
                    threading.Thread(target=safe_handle, args=(handle_callback, state,
                                                               upd["callback_query"]), daemon=True).start()
                elif "message" in upd:
                    threading.Thread(target=safe_handle, args=(handle_message, state,
                                                               upd["message"]), daemon=True).start()
        except KeyboardInterrupt:
            break
        except Exception:  # noqa: BLE001 - демон не должен умирать
            log(traceback.format_exc())
            time.sleep(10)


if __name__ == "__main__":
    main()
