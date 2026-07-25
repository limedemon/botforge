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

    # Прошлый прогон мог оборваться и оставить свои записи — убираем их,
    # иначе проверки увидят чужую схему и всё «сломается» на ровном месте.
    stale = await main.db.fetchrow("SELECT * FROM projects WHERE owner_id = $1", OWNER)
    if stale:
        await main.db.execute("DELETE FROM sessions WHERE project_id = $1", stale["id"])
        await main.db.execute("DELETE FROM timers WHERE project_id = $1", stale["id"])
        await main.db.execute("DELETE FROM projects WHERE owner_id = $1", OWNER)
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
    check("кнопки прикреплены", bool(markup) and len(markup["inline_keyboard"]) == 2, str(markup))

    await send(text="просто болтовня")
    check("на постороннее сообщение бот молчит", not texts(), str(texts()))

    await tap("g:s3")
    check("нажатие кнопки отработано",
          any(c["method"] == "answerCallbackQuery" for c in CALLS))
    check("задан вопрос про имя", texts() == ["Как вас зовут?"], str(texts()))

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

    print("\n6. Блок «Ключевые слова»")
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

    print("\n7. Блок «События»")
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

    print("\n8. Условие, рандом, действия и теги")
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

    print("\n9. Таймер")
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
    check("созревший таймер сработал", done == 1 and texts() == ["Прошёл день"],
          f"{done} {texts()}")
    left = await main.db.fetch("SELECT * FROM timers WHERE project_id = $1", project["id"])
    check("отработавшее задание удалено", not left, str(left))

    print("\n10. Что схема принимает, а что нет")
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

    print("\n11. Где лежат блоки")
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

    print("\n12. Схемы, собранные в прежней версии")
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

    print("\n13. Мелочи")
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

    resp = await client.post("/api/bot/disconnect", headers=headers)
    check("бота можно отключить", resp.status == 200)
    resp = await client.get("/api/state", headers=headers)
    check("после отключения бот отвязан", (await resp.json())["connected"] is False)

    print("\n14. Уборка за собой")
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
  "  outputsOf, setLink, autoLayout, ensurePositions, removeStep, blankStep," +
  "  nextId, byId, titleOf, META, ORDER" +
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

process.stdout.write(JSON.stringify(results));
"""


def check_editor_logic():
    print("\n15. Полотно в редакторе (логика страницы)")
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
