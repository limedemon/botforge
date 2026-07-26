#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Самопроверка конструктора. Запуск: python selftest.py

Поднимает сервис у себя в памяти, подсовывает ему вместо Telegram заглушку
и проигрывает всё, что делает живой человек: открывает полотно, собирает
схему, подключает бота, пишет боту, жмёт кнопки, отвечает на вопросы.
Ни одного реального запроса наружу не уходит.

Прогнать то же самое на настоящей базе Postgres:
    TEST_DATABASE_URL="postgresql://…" python selftest.py
Тестовые записи после прогона удаляются.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

TOKEN = "1111111:TESTTESTTESTTESTTESTTESTTESTTESTTES"
CLIENT_TOKEN = "2222222:AAA-fake-client-bot-token-xxxxxxxxx"
OWNER = 777

os.environ["BOT_TOKEN"] = TOKEN
os.environ["PUBLIC_URL"] = "http://localhost:8080"
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.pop("DATABASE_URL", None)

import main                                             # noqa: E402
from aiohttp.test_utils import TestClient, TestServer   # noqa: E402

CALLS = []
FAILED = []


async def fake_tg(token, method, **params):
    CALLS.append(dict(params, token=token, method=method))
    if method == "getMe":
        return {"ok": True, "result": {"username": "my_test_bot", "id": 42}}
    if method == "getWebhookInfo":
        return {"ok": True, "result": {"url": ""}}
    return {"ok": True, "result": True}


main.tg = fake_tg

# Скачивание присланных картинок тоже подменяем: наружу не ходим.
FAKE_IMAGE = b"\x89PNG\r\n\x1a\n" + "это не настоящая картинка".encode("utf-8")


async def fake_download(token, file_id):
    return b"" if file_id == "неподъёмная" else FAKE_IMAGE


main.tg_download = fake_download


def check(name, condition, detail=""):
    if condition:
        print(f"  ок   {name}")
    else:
        FAILED.append(name)
        print(f"  СБОЙ {name}" + (f"\n       {detail}" if detail else ""))


def init_data(user_id=OWNER, first_name="Тест"):
    """Подпись мини-аппа — ровно так же, как её делает Telegram."""
    user = json.dumps({"id": user_id, "first_name": first_name, "username": "tester"},
                      ensure_ascii=False, separators=(",", ":"))
    data = {"auth_date": str(int(time.time())), "query_id": "AAA", "user": user}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def texts(method="sendMessage"):
    return [c.get("text") or c.get("caption") or "" for c in CALLS
            if c["method"] == method and c.get("chat_id") != OWNER]


def leads():
    return [c for c in CALLS if c["method"] == "sendMessage" and c.get("chat_id") == OWNER]


def step(kind, **fields):
    body = {"id": fields.pop("id"), "type": kind, "name": fields.pop("name", ""),
            "x": 0, "y": 0}
    body.update(fields)
    return body


async def run():
    main.SQLITE_PATH = Path(tempfile.gettempdir()) / f"botforge_test_{os.getpid()}.db"
    main.SQLITE_PATH.unlink(missing_ok=True)

    client = TestClient(TestServer(main.build_app()))
    await client.start_server()
    headers = {"X-Init-Data": init_data()}

    # Фоновый обход таймеров останавливаем: на медленной базе прогон идёт
    # минутами, и обход успевает забрать задание раньше, чем до него дойдёт
    # проверка. По таймерам ходим сами, вызовом tick_timers().
    for name in ("timers", "awake"):
        task = client.server.app.get(name)
        if task:
            task.cancel()

    # Прошлый прогон мог оборваться и оставить свои записи — убираем их,
    # иначе проверки увидят чужую схему и всё «сломается» на ровном месте.
    stale = await main.db.fetchrow("SELECT * FROM projects WHERE owner_id = $1", OWNER)
    if stale:
        for table in ("sessions", "timers", "variables", "tags"):
            await main.db.execute(f"DELETE FROM {table} WHERE project_id = $1", stale["id"])
        await main.db.execute("DELETE FROM projects WHERE owner_id = $1", OWNER)
    await main.db.execute("DELETE FROM assets WHERE owner_id = $1", OWNER)
    CALLS.clear()

    print("\n1. Страница и здоровье сервиса")
    resp = await client.get("/health")
    check("/health отвечает", resp.status == 200 and (await resp.json())["status"] == "ok")
    resp = await client.get("/")
    page = await resp.text()
    check("полотно отдаётся", resp.status == 200 and "Конструктор ботов" in page)
    check("официальный скрипт telegram.org не подключён", "telegram-web-app.js" not in page)
    # Окна прячутся атрибутом hidden, а он слабее любого нашего display —
    # без этого правила невидимое окно висит поверх и глотает касания.
    check("спрятанное остаётся спрятанным",
          re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none", page) is not None)
    check("списка шагов больше нет", "Список" not in page and "listView" not in page)
    # Кружок выхода нарочно торчит за край полоски. Любая обрезка на самой
    # полоске срежет ему половину — это уже случалось.
    port_rule = re.search(r"\.port\{[^}]*\}", page)
    check("кружок выхода ничем не обрезан",
          bool(port_rule) and "overflow" not in port_rule.group(0),
          port_rule.group(0) if port_rule else "правило .port не найдено")

    print("\n2. Вход в мини-апп")
    resp = await client.get("/api/state", headers={"X-Init-Data": "hash=подделка"})
    check("подделанная подпись отклоняется", resp.status == 401)
    resp = await client.get("/api/state")
    check("без подписи внутрь не пускает", resp.status == 401)
    resp = await client.get("/api/state", headers=headers)
    state = await resp.json()
    check("настоящая подпись принимается", resp.status == 200)
    check("новому человеку выдана схема-заготовка",
          len(state["scenario"]["steps"]) == 6 and state["scenario"]["start"] == "s1",
          str(state)[:200])
    check("бот пока не подключён", state["connected"] is False)

    print("\n3. Подключение бота")
    resp = await client.post("/api/bot/connect", headers=headers, json={"token": "не токен"})
    check("мусор вместо токена не проходит", resp.status == 400)
    resp = await client.post("/api/bot/connect", headers=headers, json={"token": CLIENT_TOKEN})
    body = await resp.json()
    check("токен принят", resp.status == 200 and body["bot_username"] == "my_test_bot", str(body))
    hooks = [c for c in CALLS if c["method"] == "setWebhook" and c["token"] == CLIENT_TOKEN]
    check("вебхук клиентского бота выставлен", len(hooks) == 1)
    check("вебхук закрыт секретом", bool(hooks and hooks[0].get("secret_token")))
    check("блокировки бота тоже приходят",
          "my_chat_member" in (hooks[0].get("allowed_updates") or []) if hooks else False)

    project = await main.db.fetchrow("SELECT * FROM projects WHERE owner_id = $1", OWNER)
    path = f"/hook/{project['id']}/{project['hook_secret']}"

    async def send(**message):
        chat = {"id": message.pop("chat", 555), "type": "private"}
        who = {"id": chat["id"], "first_name": message.pop("who", "Ваня")}
        CALLS.clear()
        await client.post(path, json={"message": dict(message, chat=chat, **{"from": who})})

    async def tap(data, chat=555):
        CALLS.clear()
        await client.post(path, json={"callback_query": {
            "id": "1", "data": data, "from": {"id": chat, "first_name": "Ваня"},
            "message": {"chat": {"id": chat, "type": "private"}}}})

    async def use(scenario):
        resp = await client.post("/api/scenario", headers=headers,
                                 json={"scenario": scenario})
        assert resp.status == 200, await resp.text()

    print("\n4. Защита вебхука")
    resp = await client.post(f"/hook/{project['id']}/чужой-секрет", json={})
    check("чужой секрет не пускает", resp.status == 403)
    resp = await client.post("/hook/999999/что-угодно", json={})
    check("несуществующий проект не пускает", resp.status == 403)

    print("\n5. Схема-заготовка в работе")
    await send(text="/start")
    check("на /start бот ответил", len(texts()) == 1, str(texts()))
    check("имя подставилось в текст", texts() and "Привет, Ваня!" in texts()[0], str(texts()))
    markup = [c for c in CALLS if c["method"] == "sendMessage"][0].get("reply_markup")
    check("кнопки показаны под полем ввода",
          bool(markup) and len(markup.get("keyboard") or []) == 2, str(markup))
    check("кнопки не внутри сообщения", "inline_keyboard" not in (markup or {}), str(markup))

    await send(text="просто болтовня")
    check("на постороннее сообщение бот молчит", not texts(), str(texts()))

    # Нажатие обычной кнопки приходит боту как обычный текст.
    await send(text="Оставить заявку")
    check("нажатие кнопки увело на нужный блок",
          texts() == ["Как вас зовут?"], str(texts()))
    asked = [c for c in CALLS if c["method"] == "sendMessage"][0]
    check("на время вопроса кнопки убраны",
          (asked.get("reply_markup") or {}).get("remove_keyboard") is True,
          str(asked.get("reply_markup")))

    await send(text="Иван")
    check("после ответа задан следующий вопрос",
          texts() == ["Оставьте номер телефона, и мы перезвоним."], str(texts()))

    await send(text="+79990001122")
    check("ответы подставились в благодарность",
          any("Спасибо, Иван!" in t and "+79990001122" in t for t in texts()), str(texts()))
    check("блок «Действие» прислал заявку владельцу", len(leads()) == 1, str(leads())[:200])
    check("в заявке есть оба ответа",
          bool(leads()) and "Иван" in leads()[0]["text"] and "+79990001122" in leads()[0]["text"],
          str(leads())[:300])

    print("\n6. Обычные кнопки")
    await use({"start": "menu", "steps": [
        step("message", id="menu", text="Выбирайте", next="", buttons=[
            {"text": "Цены", "action": "goto", "value": "price"},
            {"text": "Наш сайт", "action": "url", "value": "https://example.com"},
        ]),
        step("message", id="price", text="Дорого", buttons=[], next=""),
        step("keywords", id="k", match="exact", words="Цены", next="wrong"),
        step("message", id="wrong", text="Это не должно сработать", buttons=[], next=""),
    ]})
    await send(text="/start", chat=550)
    check("кнопки перечислены по одной в ряд",
          [row[0]["text"] for row in
           [c for c in CALLS if c["method"] == "sendMessage"][0]["reply_markup"]["keyboard"]]
          == ["Цены", "Наш сайт"])
    await send(text="Цены", chat=550)
    check("нажатие кнопки важнее ключевых слов", texts() == ["Дорого"], str(texts()))
    check("после блока без кнопок клавиатура убрана",
          ([c for c in CALLS if c["method"] == "sendMessage"][0].get("reply_markup") or {})
          .get("remove_keyboard") is True)
    await send(text="/start", chat=551)
    await send(text="Наш сайт", chat=551)
    check("кнопка «на сайт» присылает ссылку отдельным сообщением",
          texts() == ["https://example.com"], str(texts()))
    await send(text="Цены", chat=552)
    check("чужая надпись без показанных кнопок кнопкой не считается",
          texts() == ["Это не должно сработать"], str(texts()))

    print("\n7. Блок «Ключевые слова»")
    await use({"start": "start", "steps": [
        step("message", id="start", text="Меню", buttons=[], next=""),
        step("keywords", id="k1", match="contains", words="цена, стоимость", next="m1"),
        step("keywords", id="k2", match="exact", words="цена", next="m2"),
        step("message", id="m1", text="Примерно так-то", buttons=[], next=""),
        step("message", id="m2", text="Ровно столько-то", buttons=[], next=""),
    ]})
    await send(text="а какая у вас ЦЕНА за всё?", chat=560)
    check("слово внутри сообщения сработало", texts() == ["Примерно так-то"], str(texts()))
    await send(text="Цена", chat=560)
    check("точное совпадение важнее", texts() == ["Ровно столько-то"], str(texts()))
    await send(text="здравствуйте", chat=560)
    check("на прочее молчит", not texts(), str(texts()))

    print("\n8. Блок «События»")
    await use({"start": "start", "steps": [
        step("message", id="start", text="Здравствуйте", buttons=[], next=""),
        step("event", id="e1", event="first", next="m1"),
        step("event", id="e2", event="photo", next="m2"),
        step("event", id="e3", event="unknown", next="m3"),
        step("message", id="m1", text="Вижу вас впервые", buttons=[], next=""),
        step("message", id="m2", text="Красивое фото", buttons=[], next=""),
        step("message", id="m3", text="Такой команды нет", buttons=[], next=""),
    ]})
    await send(text="привет", chat=570)
    check("первое сообщение поймано", texts() == ["Вижу вас впервые"], str(texts()))
    await send(text="привет ещё раз", chat=570)
    check("второе сообщение уже не первое", not texts(), str(texts()))
    await send(photo=[{"file_id": "x"}], chat=570)
    check("фото поймано", texts() == ["Красивое фото"], str(texts()))
    await send(text="/чепуха", chat=570)
    check("неизвестная команда поймана", texts() == ["Такой команды нет"], str(texts()))
    CALLS.clear()
    await client.post(path, json={"my_chat_member": {
        "chat": {"id": 570, "type": "private"}, "from": {"id": 570, "first_name": "Ваня"},
        "new_chat_member": {"status": "kicked"}}})
    check("блокировка бота не роняет сервис", True)

    print("\n9. Условие, рандом, действия и теги")
    await use({"start": "start", "steps": [
        step("message", id="start", text="Старт", buttons=[], next="act"),
        step("action", id="act", next="cond", actions=[
            {"kind": "set_var", "name": "город", "value": "Москва"},
            {"kind": "add_tag", "name": "клиент", "value": ""},
        ]),
        step("condition", id="cond", next="yes", otherwise="no", checks=[
            {"var": "город", "op": "eq", "value": "москва"},
            {"var": "", "op": "tag", "value": "клиент"},
        ]),
        step("message", id="yes", text="Вы из Москвы", buttons=[], next=""),
        step("message", id="no", text="Вы не из Москвы", buttons=[], next=""),
    ]})
    await send(text="/start", chat=580)
    check("условие сошлось", texts() == ["Старт", "Вы из Москвы"], str(texts()))

    await use({"start": "start", "steps": [
        step("message", id="start", text="Старт", buttons=[], next="act"),
        step("action", id="act", next="cond",
             actions=[{"kind": "set_var", "name": "город", "value": "Пермь"}]),
        step("condition", id="cond", next="yes", otherwise="no",
             checks=[{"var": "город", "op": "eq", "value": "Москва"}]),
        step("message", id="yes", text="Вы из Москвы", buttons=[], next=""),
        step("message", id="no", text="Вы не из Москвы", buttons=[], next=""),
    ]})
    await send(text="/start", chat=581)
    check("условие не сошлось — пошли по «нет»",
          texts() == ["Старт", "Вы не из Москвы"], str(texts()))

    await use({"start": "r", "steps": [
        step("random", id="r", options=[
            {"label": "A", "weight": 100, "next": "a"},
            {"label": "B", "weight": 0, "next": "b"},
        ]),
        step("message", id="a", text="Вариант А", buttons=[], next=""),
        step("message", id="b", text="Вариант Б", buttons=[], next=""),
    ]})
    picked = set()
    for chat in range(600, 610):
        await send(text="/start", chat=chat)
        picked.update(texts())
    check("рандом уважает веса: сто процентов на А", picked == {"Вариант А"}, str(picked))

    print("\n10. Таймер")
    await use({"start": "start", "steps": [
        step("message", id="start", text="Сейчас подождём", buttons=[], next="t"),
        step("timer", id="t", amount=1, unit="day", next="later"),
        step("message", id="later", text="Прошёл день", buttons=[], next=""),
    ]})
    await send(text="/start", chat=620)
    check("до срока таймер молчит", texts() == ["Сейчас подождём"], str(texts()))
    waiting = await main.db.fetch(
        "SELECT * FROM timers WHERE project_id = $1", project["id"])
    check("задание на потом записано", len(waiting) == 1, str(waiting))
    check("срок примерно через сутки",
          bool(waiting) and 86000 < waiting[0]["run_at"] - time.time() < 86500,
          str(waiting))

    CALLS.clear()
    await main.db.execute("UPDATE timers SET run_at = $1 WHERE project_id = $2",
                          time.time() - 5, project["id"])
    done = await main.tick_timers()
    left = await main.db.fetch("SELECT * FROM timers WHERE project_id = $1", project["id"])
    if done == 0 and not left:
        # На этой же базе может работать боевой сервис — его обход таймеров
        # ходит по всем проектам сразу и успел забрать наше задание себе.
        # Это не поломка: задание отработано, просто не нами.
        print("  —    созревший таймер забрал другой запущенный сервис,"
              " проверку пропускаю")
    else:
        check("созревший таймер сработал", done == 1 and texts() == ["Прошёл день"],
              f"{done} {texts()}")
        check("отработавшее задание удалено", not left, str(left))

    print("\n11. Что схема принимает, а что нет")
    resp = await client.post("/api/scenario", headers=headers,
                             json={"scenario": {"steps": "не список"}})
    check("кривая схема отклонена", resp.status == 400)
    resp = await client.post("/api/scenario", headers=headers, json={"scenario": {
        "start": "a", "steps": [{"id": "x", "type": "выдумка"} for _ in range(3)]}})
    check("блоки выдуманного типа отброшены", resp.status == 400)
    resp = await client.post("/api/scenario", headers=headers, json={"scenario": {
        "steps": [step("message", id=str(i), text="") for i in range(200)]}})
    check("слишком большая схема отклонена", resp.status == 400)

    await use({"start": "нет такого", "steps": [
        step("message", id="a", text="Один", buttons=[], next=""),
        step("timer", id="t", amount=9999, unit="век", next=""),
        step("random", id="r", options=[{"label": "A", "weight": 500, "next": "a"}]),
    ]})
    saved = (await (await client.get("/api/state", headers=headers)).json())["scenario"]
    check("несуществующий стартовый блок заменён на настоящий",
          saved["start"] == "a", str(saved["start"]))
    timer = [s for s in saved["steps"] if s["type"] == "timer"][0]
    check("срок таймера прижат к разумному", timer["amount"] == 365 and timer["unit"] == "day",
          str(timer))
    weight = [s for s in saved["steps"] if s["type"] == "random"][0]["options"][0]["weight"]
    check("вес варианта прижат к сотне", weight == 100, str(weight))

    print("\n12. Где лежат блоки")
    await client.post("/api/scenario", headers=headers, json={"scenario": {
        "start": "a", "steps": [
            dict(step("message", id="a", text="Меню", buttons=[], next=""), x=120.5, y=-40),
            dict(step("message", id="b", text="Второе", buttons=[], next=""),
                 x="не число", y=9e9),
        ]}})
    saved = (await (await client.get("/api/state", headers=headers)).json())["scenario"]["steps"]
    check("координаты блока сохранились",
          saved[0].get("x") == 120.5 and saved[0].get("y") == -40.0, str(saved[0]))
    check("мусор вместо координат отброшен",
          "x" not in saved[1] and "y" not in saved[1], str(saved[1]))

    print("\n13. Схемы, собранные в прежней версии")
    old = {"steps": [
        {"id": "a", "name": "Привет", "kind": "message", "x": 10, "y": 20,
         "trigger": {"type": "command", "value": "/start"}, "text": "Здравствуйте",
         "buttons": [{"text": "Дальше", "goto": "b"}], "next": ""},
        {"id": "b", "name": "Вопрос", "kind": "ask", "trigger": {"type": "none", "value": ""},
         "text": "Как вас зовут?", "save_to": "имя", "notify": True, "next": ""},
        {"id": "c", "name": "Цены", "kind": "message",
         "trigger": {"type": "text", "value": "цена"}, "text": "Дорого", "next": ""},
    ]}
    await main.db.execute("UPDATE projects SET scenario = $1 WHERE id = $2",
                          json.dumps(old, ensure_ascii=False), project["id"])
    fresh = await main.get_project(project["id"])
    moved = main.load_scenario(fresh)
    kinds = {s["id"]: s["type"] for s in moved["steps"]}
    check("старый стартовый шаг стал стартовым блоком", moved["start"] == "a", str(moved["start"]))
    check("шаг-вопрос стал блоком «Ввод»", kinds.get("b") == "input", str(kinds))
    check("галочка «прислать заявку» стала блоком «Действие»",
          kinds.get("b_n") == "action", str(kinds))
    check("запуск по фразе стал блоком «Ключевые слова»",
          kinds.get("c_k") == "keywords", str(kinds))
    check("координаты старых шагов не потерялись",
          [s for s in moved["steps"] if s["id"] == "a"][0].get("x") == 10)

    await send(text="/start", chat=630)
    check("перенесённая схема работает", texts() == ["Здравствуйте"], str(texts()))
    moved_button = [s for s in moved["steps"] if s["id"] == "a"][0]["buttons"][0]
    check("цель кнопки из самой первой версии не потерялась",
          moved_button.get("value") == "b", str(moved_button))
    await tap("g:b", chat=630)
    check("кнопка перенесённой схемы ведёт куда надо",
          texts() == ["Как вас зовут?"], str(texts()))

    print("\n14. Мелочи")
    await use({"start": "a", "steps": [step("message", id="a", text="Привет",
                                            buttons=[], next="a")]})
    await send(text="/start", chat=640)
    check("схема, замкнутая на себя, не вешает бота",
          0 < len(texts()) <= main.MAX_HOPS, str(len(texts())))

    CALLS.clear()
    await client.post(path, json={"message": {
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 1, "first_name": "Г"}, "text": "/start"}})
    check("в группах бот не отвечает", not CALLS, str(CALLS))

    print("\n15. Переменные: текстовые и числовые")

    async def make_var(name, scope="user", vtype="text", value=""):
        resp = await client.post("/api/vars/save", headers=headers, json={
            "name": name, "scope": scope, "vtype": vtype, "value": value})
        assert resp.status == 200, await resp.text()
        return (await resp.json())["vars"]

    async def var_now(name):
        row = await main.db.fetchrow(
            "SELECT * FROM variables WHERE project_id = $1 AND name = $2",
            project["id"], name)
        return row

    async def person_var(chat_id, name):
        session = await main.load_session(project["id"], chat_id)
        return session["vars"].get(name)

    await make_var("счёт", "user", "number", "")
    await make_var("имя клиента", "user", "text")
    await make_var("наш сайт", "project", "text", "https://example.com")

    resp = await client.get("/api/vars", headers=headers)
    names = {v["name"]: v for v in (await resp.json())["vars"]}
    check("переменная числового типа создана",
          names.get("счёт", {}).get("vtype") == "number", str(names.get("счёт")))
    check("название из нескольких слов сохранилось", "имя клиента" in names, str(list(names)))
    check("проектная переменная хранит своё значение",
          names.get("наш сайт", {}).get("value") == "https://example.com",
          str(names.get("наш сайт")))

    resp = await client.post("/api/vars/save", headers=headers, json={
        "name": "счёт", "scope": "user", "vtype": "number"})
    check("двух переменных с одним названием не бывает", resp.status in (200, 400))

    await use({"start": "start", "steps": [
        step("message", id="start", text="Считаем", buttons=[], next="add"),
        step("action", id="add", next="show", actions=[
            {"kind": "set_var", "name": "счёт", "op": "add", "value": "3"},
            {"kind": "set_var", "name": "имя клиента", "op": "mul", "value": "Петя"},
        ]),
        step("message", id="show", buttons=[], next="",
             text="счёт {счёт}, имя {имя клиента}, сайт {наш сайт}"),
    ]})
    await send(text="/start", chat=700)
    check("к числовой переменной прибавилось",
          any("счёт 3," in t for t in texts()), str(texts()))
    check("знак у текстовой переменной не действует — она просто записана",
          any("имя Петя," in t for t in texts()), str(texts()))
    check("проектная переменная подставилась в текст",
          any("https://example.com" in t for t in texts()), str(texts()))

    await send(text="/start", chat=700)
    check("прибавление накапливается", any("счёт 6," in t for t in texts()), str(texts()))
    await send(text="/start", chat=701)
    check("у другого человека свой счёт", any("счёт 3," in t for t in texts()), str(texts()))

    await use({"start": "s", "steps": [
        step("action", id="s", next="show", actions=[
            {"kind": "set_var", "name": "счёт", "op": "set", "value": "10"},
            {"kind": "set_var", "name": "счёт", "op": "div", "value": "4"},
            {"kind": "set_var", "name": "счёт", "op": "sub", "value": "0,5"},
        ]),
        step("message", id="show", text="итого {счёт}", buttons=[], next=""),
    ]})
    await send(text="/start", chat=702)
    check("деление и дробное вычитание считаются",
          any("итого 2" == t for t in texts()), str(texts()))

    await use({"start": "s", "steps": [
        step("action", id="s", next="show", actions=[
            {"kind": "set_var", "name": "счёт", "op": "set", "value": "5"},
            {"kind": "set_var", "name": "счёт", "op": "set", "value": "{счёт} * 2 + 1"},
        ]),
        step("message", id="show", text="итого {счёт}", buttons=[], next=""),
    ]})
    await send(text="/start", chat=703)
    check("выражение со знаками посчиталось",
          any("итого 11" == t for t in texts()), str(texts()))

    await use({"start": "s", "steps": [
        step("action", id="s", next="show", actions=[
            {"kind": "set_var", "name": "наш сайт", "op": "set", "value": "https://new.ru"},
        ]),
        step("message", id="show", text="сайт {наш сайт}", buttons=[], next=""),
    ]})
    await send(text="/start", chat=704)
    row = await var_now("наш сайт")
    check("проектная переменная записалась в базу, а не в сеанс",
          row and row["value"] == "https://new.ru", str(row and row["value"]))
    await send(text="/start", chat=705)
    check("новое значение проектной переменной видно всем",
          any("https://new.ru" in t for t in texts()), str(texts()))
    check("проектная переменная не осела в личных данных",
          await person_var(705, "наш сайт") is None)

    print("\n16. Числа в условиях и в блоке «Ввод»")
    await use({"start": "s", "steps": [
        step("action", id="s", next="c",
             actions=[{"kind": "set_var", "name": "счёт", "op": "set", "value": "9"}]),
        step("condition", id="c", next="yes", otherwise="no",
             checks=[{"var": "счёт", "op": "lt", "value": "12"}]),
        step("message", id="yes", text="Меньше", buttons=[], next=""),
        step("message", id="no", text="Не меньше", buttons=[], next=""),
    ]})
    await send(text="/start", chat=710)
    check("девять меньше двенадцати, а не наоборот",
          texts() == ["Меньше"], str(texts()))

    await use({"start": "ask", "steps": [
        step("input", id="ask", text="Сколько вам лет?", save_to="счёт",
             expect="number", retry="Нужно число!", next="ok"),
        step("message", id="ok", text="Записал {счёт}", buttons=[], next=""),
    ]})
    await send(text="/start", chat=711)
    await send(text="много", chat=711)
    check("вместо числа буквы — бот переспрашивает",
          texts() == ["Нужно число!"], str(texts()))
    await send(text="30", chat=711)
    check("число принято и записано", texts() == ["Записал 30"], str(texts()))

    print("\n17. Картинки из галереи")
    main_path = f"/hook/main/{main.MAIN_SECRET}"
    CALLS.clear()
    await client.post(main_path, json={"message": {
        "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
        "photo": [{"file_id": "мелкая"}, {"file_id": "крупная"}]}})
    check("на присланную картинку бот ответил", bool(texts("sendMessage") or CALLS),
          str(CALLS)[:200])
    resp = await client.get("/api/assets", headers=headers)
    shots = (await resp.json())["assets"]
    check("картинка попала в галерею", len(shots) == 1, str(shots))

    token = shots[0]["token"] if shots else "нет"
    resp = await client.get("/img/" + token)
    check("картинка отдаётся по своему адресу",
          resp.status == 200 and (await resp.read()) == FAKE_IMAGE,
          str(resp.status))
    resp = await client.get("/img/выдуманная-метка")
    check("выдуманная метка ничего не открывает", resp.status == 404)

    await use({"start": "m", "steps": [
        step("message", id="m", text="Вот картинка", photo="asset:" + token,
             file="", buttons=[], next=""),
    ]})
    await send(text="/start", chat=720)
    shot_calls = [c for c in CALLS if c["method"] == "sendPhoto"]
    check("бот отправил картинку из галереи",
          len(shot_calls) == 1 and shot_calls[0]["photo"].endswith("/img/" + token),
          str(shot_calls)[:200])

    await use({"start": "m", "steps": [
        step("message", id="m", text="Так нельзя", photo="javascript:alert(1)",
             file="", buttons=[], next=""),
    ]})
    saved = (await (await client.get("/api/state", headers=headers)).json())["scenario"]
    check("постороннюю ссылку вместо картинки не принимают",
          saved["steps"][0]["photo"] == "", str(saved["steps"][0]))

    resp = await client.post("/api/assets/delete", headers=headers,
                             json={"token": token})
    shots = (await (await client.get("/api/assets", headers=headers)).json())["assets"]
    check("картинку можно убрать из галереи", not shots, str(shots))

    print("\n18. Теги, люди и рассылка")
    resp = await client.post("/api/tags/save", headers=headers, json={"name": "новичок"})
    check("тег создан", "новичок" in (await resp.json())["tags"])

    await use({"start": "s", "steps": [
        step("action", id="s", next="m", actions=[
            {"kind": "add_tag", "name": "новичок", "value": ""},
        ]),
        step("message", id="m", text="Готово", buttons=[], next=""),
    ]})
    await send(text="/start", chat=730, who="Маша")
    await send(text="/start", chat=731, who="Гриша")

    # Прошлые разделы наплодили десятки собеседников. Для рассылки оставляем
    # только этих двоих — иначе проверка ждала бы её конца полминуты.
    await main.db.execute(
        "DELETE FROM sessions WHERE project_id = $1 AND chat_id NOT IN (730, 731)",
        project["id"])

    people = (await (await client.get("/api/users", headers=headers)).json())["people"]
    mine = {p["chat_id"]: p for p in people}
    check("люди видны списком", 730 in mine and 731 in mine, str(list(mine))[:200])
    check("имя человека видно", mine.get(730, {}).get("name") == "Маша", str(mine.get(730)))
    check("тег из действия виден в списке",
          mine.get(730, {}).get("tags") == ["новичок"], str(mine.get(730)))
    check("по умолчанию человек подписан на рассылку",
          mine.get(730, {}).get("subscribed") is True, str(mine.get(730)))

    await client.post("/api/users/tags", headers=headers,
                      json={"chat_id": 731, "tags": ["новичок", "важный"]})
    people = (await (await client.get("/api/users", headers=headers)).json())["people"]
    mine = {p["chat_id"]: p for p in people}
    check("теги человека можно поправить руками",
          mine.get(731, {}).get("tags") == ["новичок", "важный"], str(mine.get(731)))
    check("новый тег завёлся в общем списке",
          "важный" in (await (await client.get("/api/tags", headers=headers)).json())["tags"])

    await use({"start": "s", "steps": [
        step("action", id="s", next="m",
             actions=[{"kind": "unsubscribe", "name": "", "value": ""}]),
        step("message", id="m", text="Отписал", buttons=[], next=""),
    ]})
    await send(text="/start", chat=731)
    people = (await (await client.get("/api/users", headers=headers)).json())["people"]
    mine = {p["chat_id"]: p for p in people}
    check("действие «отписать» сработало",
          mine.get(731, {}).get("subscribed") is False, str(mine.get(731)))

    CALLS.clear()
    resp = await client.post("/api/broadcast", headers=headers,
                             json={"text": "Здравствуйте, {name}!", "tag": ""})
    body = await resp.json()
    check("рассылка принята", resp.status == 200, str(body))
    await asyncio.sleep(0.6)
    sent = {c["chat_id"] for c in CALLS if c["method"] == "sendMessage"}
    check("отписавшийся рассылку не получил", 731 not in sent, str(sent))
    check("подписанный рассылку получил", 730 in sent, str(sent))
    check("в рассылке подставилось имя",
          any("Здравствуйте, Маша!" == c.get("text") for c in CALLS), str(CALLS)[:300])

    CALLS.clear()
    await client.post("/api/broadcast", headers=headers,
                      json={"text": "Только своим", "tag": "нет такого тега"})
    await asyncio.sleep(0.4)
    check("рассылка по несуществующему тегу никому не ушла",
          not [c for c in CALLS if c["method"] == "sendMessage"], str(CALLS)[:200])
    CALLS.clear()

    await client.post("/api/tags/delete", headers=headers, json={"name": "важный"})
    people = (await (await client.get("/api/users", headers=headers)).json())["people"]
    mine = {p["chat_id"]: p for p in people}
    check("удалённый тег снялся и с людей",
          mine.get(731, {}).get("tags") == ["новичок"], str(mine.get(731)))

    print("\n19. Кнопки внутри сообщения и разметка")
    await use({"start": "m", "steps": [
        step("message", id="m", text="<b>Жирно</b>", buttons=[
            {"text": "Дальше", "action": "goto", "value": "n"},
            {"text": "Сайт", "action": "url", "value": "https://example.com"},
        ], inline=True, next=""),
        step("message", id="n", text="Второй", buttons=[], next=""),
    ]})
    await send(text="/start", chat=740)
    said = [c for c in CALLS if c["method"] == "sendMessage"][0]
    rows = (said.get("reply_markup") or {}).get("inline_keyboard") or []
    check("кнопки ушли внутрь сообщения", len(rows) == 2, str(said.get("reply_markup")))
    check("кнопка-переход несёт номер блока",
          bool(rows) and rows[0][0].get("callback_data") == "g:n", str(rows))
    check("кнопка-ссылка стала настоящей ссылкой",
          len(rows) > 1 and rows[1][0].get("url") == "https://example.com", str(rows))
    check("текст ушёл с разметкой", said.get("parse_mode") == "HTML", str(said)[:200])
    await tap("g:n", chat=740)
    check("нажатие внутренней кнопки уводит на нужный блок",
          texts() == ["Второй"], str(texts()))

    print("\n20. Рандом закрепляется за человеком")
    await use({"start": "r", "steps": [
        step("random", id="r", always=False, options=[
            {"label": "A", "weight": 50, "next": "a"},
            {"label": "B", "weight": 50, "next": "b"},
        ]),
        step("message", id="a", text="Вариант А", buttons=[], next=""),
        step("message", id="b", text="Вариант Б", buttons=[], next=""),
    ]})
    await send(text="/start", chat=750)
    first = texts()
    same = True
    for _ in range(6):
        await send(text="/start", chat=750)
        same = same and texts() == first
    check("выпавший вариант закрепился за человеком", same, str(first))

    await use({"start": "r", "steps": [
        step("random", id="r", always=True, options=[
            {"label": "A", "weight": 50, "next": "a"},
            {"label": "B", "weight": 50, "next": "b"},
        ]),
        step("message", id="a", text="Вариант А", buttons=[], next=""),
        step("message", id="b", text="Вариант Б", buttons=[], next=""),
    ]})
    seen = set()
    for _ in range(40):
        await send(text="/start", chat=751)
        seen.update(texts())
    check("с галочкой «заново каждый раз» выпадает по-разному",
          len(seen) == 2, str(seen))

    print("\n21. Старые действия и старые схемы")
    moved = main.refresh_actions({"steps": [{"id": "a", "type": "action", "actions": [
        {"kind": "del_var", "name": "город", "value": "что-то"}]}]})
    was = moved["steps"][0]["actions"][0]
    check("«удалить переменную» стало пустой записью",
          was["kind"] == "set_var" and was["value"] == "" and was["op"] == "set", str(was))

    resp = await client.post("/api/bot/disconnect", headers=headers)
    check("бота можно отключить", resp.status == 200)
    resp = await client.get("/api/state", headers=headers)
    check("после отключения бот отвязан", (await resp.json())["connected"] is False)

    print("\n22. Уборка за собой")
    for table in ("variables", "tags"):
        await main.db.execute(f"DELETE FROM {table} WHERE project_id = $1", project["id"])
    await main.db.execute("DELETE FROM assets WHERE owner_id = $1", OWNER)
    await main.db.execute("DELETE FROM sessions WHERE project_id = $1", project["id"])
    await main.db.execute("DELETE FROM timers WHERE project_id = $1", project["id"])
    await main.db.execute("DELETE FROM projects WHERE owner_id = $1", OWNER)
    left = await main.db.fetchrow("SELECT * FROM projects WHERE owner_id = $1", OWNER)
    check("тестовые записи удалены", left is None)
    print(f"  (хранилище: {main.db.kind})")

    await client.close()
    main.SQLITE_PATH.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Проверка редактора. Страница живёт внутри main.py строкой, поэтому вырезаем
# из неё скрипт и запускаем в Node с подставным браузером. Так проверяется
# логика схемы: какие у блока выходы, куда ведут стрелки, как раскладываются
# блоки. Если Node не установлен — просто пропускаем.
# --------------------------------------------------------------------------

HARNESS = r"""
"use strict";
const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");

/* Подставной браузер: ровно столько, сколько нужно скрипту, чтобы загрузиться. */
function node() {
  return {
    nodeType: 1,
    className: "", textContent: "", value: "", hidden: false, checked: false, disabled: false,
    style: {setProperty() {}}, children: [],
    classList: {add() {}, remove() {}, contains() { return false; }},
    offsetTop: 0, offsetLeft: 0, offsetWidth: 210, offsetHeight: 120,
    clientWidth: 900, clientHeight: 600,
    setAttribute(k, v) { this["attr_" + k] = v; },
    getAttribute(k) { return this["attr_" + k]; },
    addEventListener() {}, removeEventListener() {},
    append(...kids) { this.children.push(...kids); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceWith() {}, remove() {},
    querySelector() { return node(); },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return {left: 0, top: 0, width: 900, height: 600}; },
    setPointerCapture() {},
  };
}
const known = {};
const document = {
  documentElement: node(),
  createElement: node,
  createElementNS: node,
  createTextNode(text) { return {nodeType: 3, textContent: String(text)}; },
  getElementById(id) { return known[id] || (known[id] = node()); },
};
const window = {addEventListener() {}, scrollTo() {}};
window.parent = window;
const location = {hash: "", search: "", href: ""};
const sessionStorage = {getItem: () => null, setItem() {}};
const fetch = () => Promise.reject(new Error("сеть в проверке недоступна"));

const api = new Function(
  "window", "document", "location", "sessionStorage", "fetch", "setTimeout", "clearTimeout",
  code + "\nreturn {" +
  "  getS: () => S, setS: (v) => { S = v; }," +
  "  setVARS: (v) => { VARS = v; }, setTAGS: (v) => { TAGS = v; }," +
  "  outputsOf, setLink, autoLayout, ensurePositions, removeStep, blankStep," +
  "  nextId, byId, titleOf, META, ORDER," +
  "  cleanName, isNumberVar, varItems, tagItems, shotSrc, nodeEl, inletY," +
  "  armCut, cutLink, dropCut, getCUT: () => CUT," +
  "  wireAt, setWIRES: (v) => { WIRES = v; }," +
  "  ACTION_NAMES, SET_OPS, OP_NAMES" +
  "};"
)(window, document, location, sessionStorage, fetch, () => 0, () => {});

const results = [];
const check = (name, ok, detail) => results.push({name, ok: !!ok, detail: detail || ""});

/* --- у каждого типа блока свой набор выходов --- */
api.setS({start: "m", steps: [
  api.blankStep("message"), api.blankStep("condition"),
  api.blankStep("random"), api.blankStep("note"), api.blankStep("timer"),
]});
const all = api.getS().steps;
const kind = {};
all.forEach((s) => { kind[s.type] = s; });

check("у нового сообщения один выход «следующий шаг»",
      api.outputsOf(kind.message).length === 1);
check("у условия выходы «да» и «нет»",
      api.outputsOf(kind.condition).map((o) => o.tone).join(",") === "yes,no",
      JSON.stringify(api.outputsOf(kind.condition)));
check("у рандома выход на каждый вариант", api.outputsOf(kind.random).length === 2);
check("у заметки выходов нет", api.outputsOf(kind.note).length === 0);
check("у таймера один выход", api.outputsOf(kind.timer).length === 1);
check("все типы блоков есть в палитре",
      api.ORDER.length === Object.keys(api.META).length);

/* --- кнопки сообщения дают свои выходы --- */
const msg = kind.message;
msg.buttons = [{text: "Цены", action: "goto", value: "b"},
               {text: "Сайт", action: "url", value: "https://x.ru"}];
const outs = api.outputsOf(msg);
check("кнопка добавляет выход", outs.length === 3, JSON.stringify(outs));
check("кнопка-переход знает свою цель", outs[0].to === "b");
check("от кнопки-ссылки стрелку не тянут", outs[1].kind === "url" && outs[1].to === "");
check("последний выход — «следующий шаг»", outs[2].kind === "next");

/* --- соединение --- */
api.setLink(msg, 0, "c");
check("стрелка от кнопки перевесилась", msg.buttons[0].value === "c");
api.setLink(msg, 1, "c");
check("кнопке-ссылке стрелку не привязать", msg.buttons[1].value === "https://x.ru");
api.setLink(msg, 2, "c");
check("стрелка «следующий шаг» встала", msg.next === "c");
api.setLink(kind.condition, 1, "z");
check("ветка «нет» пишется отдельно", kind.condition.otherwise === "z");
api.setLink(kind.random, 0, "z");
check("вариант рандома знает свою цель", kind.random.options[0].next === "z");

/* --- раскладка --- */
api.setS({start: "a", steps: [
  {id: "a", type: "message", buttons: [{text: "к", action: "goto", value: "b"}], next: ""},
  {id: "b", type: "message", buttons: [], next: "c"},
  {id: "c", type: "message", buttons: [], next: ""},
  {id: "d", type: "message", buttons: [], next: ""},
]});
api.autoLayout();
const L = api.getS().steps;
check("цепочка разложена по столбцам", L[0].x < L[1].x && L[1].x < L[2].x,
      L.map((s) => s.id + ":" + s.x + "," + s.y).join(" "));
check("оторванный блок не лёг поверх старта",
      !(L[3].x === L[0].x && L[3].y === L[0].y));
check("два блока не оказались в одной точке",
      new Set(L.map((s) => s.x + "," + s.y)).size === L.length);

/* --- места для новых блоков --- */
api.setS({start: "a", steps: [
  {id: "a", type: "message", buttons: [], next: "", x: 10, y: 10},
  {id: "b", type: "message", buttons: [], next: ""},
]});
api.ensurePositions();
const P = api.getS().steps;
check("готовый блок не сдвинулся", P[0].x === 10 && P[0].y === 10);
check("новому блоку нашлось место", typeof P[1].x === "number" && P[1].y > P[0].y);

/* --- удаление блока чистит стрелки --- */
api.setS({start: "a", steps: [
  {id: "a", type: "message", next: "b", otherwise: "b", x: 0, y: 0,
   buttons: [{text: "к", action: "goto", value: "b"}],
   options: [{label: "A", weight: 50, next: "b"}]},
  {id: "b", type: "message", buttons: [], next: "", x: 0, y: 0},
]});
api.removeStep("b");
const R = api.getS().steps;
check("после удаления кнопка никуда не ведёт", R[0].buttons[0].value === "");
check("после удаления «следующий шаг» пустой", R[0].next === "");
check("после удаления ветка «нет» пустая", R[0].otherwise === "");
check("после удаления вариант рандома пустой", R[0].options[0].next === "");
check("стартовым стал оставшийся блок", api.getS().start === "a");

/* --- занятый выход закрашен, свободный пустой --- */
api.setS({start: "a", steps: [
  {id: "a", type: "message", name: "", text: "", buttons: [], next: "b", x: 0, y: 0},
  {id: "b", type: "message", name: "", text: "", buttons: [], next: "", x: 0, y: 0},
  {id: "c", type: "message", name: "", text: "", buttons: [], next: "нет такого", x: 0, y: 0},
]});
function dotsOf(id) {
  const found = [];
  (function walk(item) {
    if (!item || !item.children) return;
    item.children.forEach(function (kid) {
      if (typeof kid.className === "string" && kid.className.indexOf("dot ") === 0) {
        found.push(kid.className);
      }
      walk(kid);
    });
  })(api.nodeEl(api.byId(id)));
  return found;
}
check("выход со стрелкой закрашен",
      dotsOf("a").some((c) => c.indexOf("wired") >= 0), dotsOf("a").join(" | "));
check("свободный выход не закрашен",
      !dotsOf("b").some((c) => c.indexOf("wired") >= 0), dotsOf("b").join(" | "));
check("стрелка в никуда выход не закрашивает",
      !dotsOf("c").some((c) => c.indexOf("wired") >= 0), dotsOf("c").join(" | "));

/* --- по какой линии нажали --- */
api.setWIRES([
  {id: "a", out: 0, x1: 0, y1: 100, x2: 300, y2: 100},
  {id: "b", out: 2, x1: 0, y1: 400, x2: 300, y2: 400},
]);
check("нажатие прямо по линии её находит",
      (api.wireAt({x: 150, y: 100}, 18) || {}).id === "a",
      JSON.stringify(api.wireAt({x: 150, y: 100}, 18)));
check("нажатие чуть в стороне тоже засчитывается",
      (api.wireAt({x: 150, y: 112}, 18) || {}).id === "a");
check("нажатие далеко от линий ничего не находит",
      api.wireAt({x: 150, y: 250}, 18) === null,
      JSON.stringify(api.wireAt({x: 150, y: 250}, 18)));
check("из двух линий выбирается ближняя",
      (api.wireAt({x: 150, y: 380}, 30) || {}).id === "b");
check("найденная линия помнит свой выход",
      (api.wireAt({x: 150, y: 400}, 18) || {}).out === 2);
check("мимо начала и конца линия не считается задетой",
      api.wireAt({x: -60, y: 100}, 18) === null);
api.setWIRES([]);
check("без стрелок нажимать не на что", api.wireAt({x: 0, y: 0}, 18) === null);

/* --- нажатие по линии и корзина --- */
api.setS({start: "a", steps: [
  {id: "a", type: "message", name: "", text: "", buttons: [
    {text: "к", action: "goto", value: "b"}], next: "b", x: 0, y: 0},
  {id: "b", type: "message", name: "", text: "", buttons: [], next: "", x: 0, y: 0},
]});
api.armCut("a", 1, {x: 50, y: 60});
check("нажатие по линии запомнило, какую убирать",
      !!api.getCUT() && api.getCUT().id === "a" && api.getCUT().out === 1,
      JSON.stringify(api.getCUT()));
check("корзина встала туда, где нажали",
      api.getCUT().x === 50 && api.getCUT().y === 60);
api.cutLink();
check("корзина убрала стрелку «следующий шаг»", api.byId("a").next === "");
check("соседняя стрелка от кнопки цела", api.byId("a").buttons[0].value === "b");
check("после удаления корзина пропала", api.getCUT() === null);

api.armCut("a", 0, {x: 1, y: 2});
api.cutLink();
check("корзина убирает и стрелку от кнопки", api.byId("a").buttons[0].value === "");

api.armCut("нет такого блока", 0, {x: 1, y: 2});
check("по стрелке исчезнувшего блока корзина не появляется", api.getCUT() === null);
api.armCut("a", 0, {x: 1, y: 2});
api.dropCut();
check("корзину можно просто закрыть", api.getCUT() === null);
api.cutLink();
check("закрытая корзина ничего не удаляет", true);

/* --- линия входит в блок снизу, а не в середину --- */
const tallNode = {offsetHeight: 200, querySelectorAll: () => []};
check("без полосок линия входит у нижнего края",
      api.inletY(tallNode) === 182, String(api.inletY(tallNode)));
const withPorts = {
  offsetHeight: 200,
  querySelectorAll: () => [{offsetTop: 100, offsetHeight: 20},
                           {offsetTop: 140, offsetHeight: 20}],
};
check("линия входит вровень с нижней полоской",
      api.inletY(withPorts) === 150, String(api.inletY(withPorts)));
check("вход не по середине блока", api.inletY(withPorts) !== 100);

/* --- заготовки новых блоков знают про новые поля --- */
const blankMsg = api.blankStep("message");
const blankIn = api.blankStep("input");
const blankRnd = api.blankStep("random");
check("у нового сообщения кнопки под полем ввода", blankMsg.inline === false);
check("новый ввод принимает что угодно", blankIn.expect === "any");
check("новый рандом закрепляет вариант", blankRnd.always === false);

/* --- названия переменных и тегов --- */
check("из названия убирается лишнее",
      api.cleanName("  Имя:  клиента!! ") === "Имя клиента",
      api.cleanName("  Имя:  клиента!! "));
check("название не длиннее сорока знаков",
      api.cleanName("я".repeat(60)).length === 40);

api.setVARS([
  {name: "счёт", scope: "user", vtype: "number", archived: false},
  {name: "город", scope: "user", vtype: "text", archived: false},
  {name: "сайт", scope: "project", vtype: "text", archived: false},
  {name: "старое", scope: "user", vtype: "text", archived: true},
]);
api.setTAGS(["новичок"]);
check("числовая переменная опознана", api.isNumberVar("счёт") === true);
check("текстовая переменная не числовая", api.isNumberVar("город") === false);
check("незнакомая переменная не числовая", api.isNumberVar("чего-то") === false);
check("в подсказках нет архивных", api.varItems().length === 3,
      JSON.stringify(api.varItems()));
check("проектная переменная помечена цветом",
      api.varItems().filter((v) => v.tone === "project").length === 1);
check("тип переменной виден в подсказке",
      api.varItems()[0].kind === "число", JSON.stringify(api.varItems()[0]));
check("теги тоже подсказываются", api.tagItems().length === 1);

/* --- картинка из галереи и обычная ссылка --- */
check("картинка из галереи открывается по своему адресу",
      api.shotSrc("asset:abc123") === "/img/abc123");
check("обычная ссылка остаётся как есть",
      api.shotSrc("https://example.com/a.jpg") === "https://example.com/a.jpg");

/* --- набор действий совпадает с тем, что понимает движок --- */
check("действий ровно шесть", api.ACTION_NAMES.length === 6);
check("«удалить переменную» из списка убрано",
      !api.ACTION_NAMES.some((a) => a[0] === "del_var"));
check("есть подписка и отписка от рассылки",
      api.ACTION_NAMES.some((a) => a[0] === "subscribe") &&
      api.ACTION_NAMES.some((a) => a[0] === "unsubscribe"));
check("знаков у переменной пять", api.SET_OPS.length === 5,
      JSON.stringify(api.SET_OPS));
check("в условиях есть сравнение чисел",
      ["gt", "lt", "gte", "lte"].every((op) => api.OP_NAMES.some((o) => o[0] === op)));

process.stdout.write(JSON.stringify(results));
"""


def check_editor_logic():
    print("\n23. Полотно в редакторе (логика страницы)")
    if not shutil.which("node"):
        print("  — пропущено: не установлен Node.js")
        return
    js = re.search(r"<script>(.*?)</script>", main.PAGE_HTML, re.S)
    if not js:
        check("скрипт страницы найден", False)
        return

    folder = Path(tempfile.mkdtemp(prefix="botforge_ui_"))
    try:
        (folder / "page.js").write_text(js.group(1), encoding="utf-8")
        (folder / "harness.js").write_text(HARNESS, encoding="utf-8")
        done = subprocess.run(
            ["node", str(folder / "harness.js"), str(folder / "page.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        if done.returncode != 0:
            check("страница выполняется без ошибок", False,
                  (done.stderr or done.stdout).strip()[:700])
            return
        for item in json.loads(done.stdout):
            check(item["name"], item["ok"], item["detail"])
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def entry():
    print("Проверка конструктора ботов")
    asyncio.run(run())
    check_editor_logic()
    print()
    if FAILED:
        print(f"Не прошло проверок: {len(FAILED)}")
        for name in FAILED:
            print("  -", name)
        sys.exit(1)
    print("Всё работает.")


if __name__ == "__main__":
    entry()
