#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Самопроверка конструктора. Запуск: python selftest.py

Поднимает сервис у себя в памяти, подсовывает ему вместо Telegram заглушку
и проигрывает всё, что делает живой человек: открывает редактор, сохраняет
сценарий, подключает бота, пишет боту, жмёт кнопки, отвечает на вопросы.
Ни одного реального запроса наружу не уходит.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

TOKEN = "1111111:TESTTESTTESTTESTTESTTESTTESTTESTTES"
os.environ["BOT_TOKEN"] = TOKEN
os.environ["PUBLIC_URL"] = "http://localhost:8080"

# По умолчанию проверяем на временном файле, ничего никуда не отправляя.
# Чтобы прогнать то же самое на настоящей базе Postgres:
#     TEST_DATABASE_URL="postgresql://…" python selftest.py
# Тестовые записи после прогона удаляются.
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


def init_data(user_id=777, first_name="Тест"):
    """Подпись мини-аппа — ровно так же, как её делает Telegram."""
    user = json.dumps({"id": user_id, "first_name": first_name, "username": "tester"},
                      ensure_ascii=False, separators=(",", ":"))
    data = {"auth_date": str(int(time.time())), "query_id": "AAA", "user": user}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def sent_texts(method="sendMessage"):
    return [c.get("text") or c.get("caption") or "" for c in CALLS if c["method"] == method]


async def run():
    main.SQLITE_PATH = Path(tempfile.gettempdir()) / f"botforge_test_{os.getpid()}.db"
    main.SQLITE_PATH.unlink(missing_ok=True)

    client = TestClient(TestServer(main.build_app()))
    await client.start_server()
    headers = {"X-Init-Data": init_data()}

    # Прошлый прогон мог оборваться и оставить свои записи — убираем их,
    # иначе проверки увидят чужой сценарий и всё «сломается» на ровном месте.
    stale = await main.db.fetchrow("SELECT * FROM projects WHERE owner_id = 777")
    if stale:
        await main.db.execute("DELETE FROM sessions WHERE project_id = $1", stale["id"])
        await main.db.execute("DELETE FROM projects WHERE owner_id = $1", 777)
    CALLS.clear()

    print("\n1. Страница и здоровье сервиса")
    resp = await client.get("/health")
    body = await resp.json()
    check("/health отвечает", resp.status == 200 and body["status"] == "ok")
    resp = await client.get("/")
    page = await resp.text()
    check("страница редактора отдаётся", resp.status == 200 and "Конструктор ботов" in page)
    check("официальный скрипт telegram.org не подключён", "telegram-web-app.js" not in page)

    print("\n2. Вход в мини-апп")
    resp = await client.get("/api/state", headers={"X-Init-Data": "hash=подделка"})
    check("подделанная подпись отклоняется", resp.status == 401)
    resp = await client.get("/api/state")
    check("без подписи внутрь не пускает", resp.status == 401)
    resp = await client.get("/api/state", headers=headers)
    state = await resp.json()
    check("настоящая подпись принимается", resp.status == 200)
    check("новому человеку выдан сценарий-заготовка",
          len(state["scenario"]["steps"]) == 5, str(state)[:200])
    check("бот пока не подключён", state["connected"] is False)

    print("\n3. Подключение бота")
    resp = await client.post("/api/bot/connect", headers=headers,
                             json={"token": "не токен"})
    check("мусор вместо токена не проходит", resp.status == 400)
    resp = await client.post("/api/bot/connect", headers=headers,
                             json={"token": "2222222:AAA-fake-client-bot-token-xxxxxx"})
    body = await resp.json()
    check("токен принят", resp.status == 200 and body["bot_username"] == "my_test_bot",
          str(body))
    hook = [c for c in CALLS if c["method"] == "setWebhook"
            and c["token"].startswith("2222222")]
    check("вебхук клиентского бота выставлен", len(hook) == 1)
    check("вебхук закрыт секретом", bool(hook and hook[0].get("secret_token")))

    project = await main.db.fetchrow("SELECT * FROM projects WHERE owner_id = 777")
    path = f"/hook/{project['id']}/{project['hook_secret']}"

    print("\n4. Защита вебхука")
    resp = await client.post(f"/hook/{project['id']}/чужой-секрет", json={})
    check("чужой секрет не пускает", resp.status == 403)
    resp = await client.post("/hook/999999/что-угодно", json={})
    check("несуществующий проект не пускает", resp.status == 403)

    print("\n5. Сценарий-заготовка в работе")
    CALLS.clear()
    await client.post(path, json={
        "message": {"chat": {"id": 555, "type": "private"},
                    "from": {"id": 555, "first_name": "Ваня", "username": "vanya"},
                    "text": "/start"}})
    texts = sent_texts()
    check("на /start бот ответил", len(texts) == 1, str(texts))
    check("имя подставилось в текст", texts and "Привет, Ваня!" in texts[0],
          str(texts))
    keyboard = [c for c in CALLS if c["method"] == "sendMessage"][0].get("reply_markup")
    check("кнопки прикреплены",
          bool(keyboard) and len(keyboard["inline_keyboard"]) == 2, str(keyboard))

    CALLS.clear()
    await client.post(path, json={
        "message": {"chat": {"id": 555, "type": "private"},
                    "from": {"id": 555, "first_name": "Ваня"},
                    "text": "просто болтовня"}})
    check("на постороннее сообщение бот молчит", not sent_texts(), str(sent_texts()))

    print("\n6. Кнопки и анкета")
    CALLS.clear()
    await client.post(path, json={
        "callback_query": {"id": "1", "data": "g:s3",
                           "from": {"id": 555, "first_name": "Ваня"},
                           "message": {"chat": {"id": 555, "type": "private"}}}})
    check("нажатие кнопки отработано",
          any(c["method"] == "answerCallbackQuery" for c in CALLS))
    check("задан вопрос про имя", sent_texts() == ["Как вас зовут?"], str(sent_texts()))

    CALLS.clear()
    await client.post(path, json={
        "message": {"chat": {"id": 555, "type": "private"},
                    "from": {"id": 555, "first_name": "Ваня"}, "text": "Иван"}})
    check("после ответа задан следующий вопрос",
          sent_texts() == ["Оставьте номер телефона, и мы перезвоним."],
          str(sent_texts()))

    CALLS.clear()
    await client.post(path, json={
        "message": {"chat": {"id": 555, "type": "private"},
                    "from": {"id": 555, "first_name": "Ваня"}, "text": "+79990001122"}})
    texts = sent_texts()
    check("ответы подставились в благодарность",
          any("Спасибо, Иван!" in t and "+79990001122" in t for t in texts), str(texts))
    lead = [c for c in CALLS if c["method"] == "sendMessage" and c.get("chat_id") == 777]
    check("заявка пришла владельцу", len(lead) == 1, str(lead)[:200])
    check("в заявке есть оба ответа",
          bool(lead) and "Иван" in lead[0]["text"] and "+79990001122" in lead[0]["text"],
          str(lead)[:300])

    print("\n7. Сохранение своего сценария")
    my = {"steps": [
        {"id": "a", "name": "Старт", "kind": "message",
         "trigger": {"type": "command", "value": "/start"},
         "text": "Меню", "buttons": [
             {"text": "Сайт", "action": "url", "value": "https://example.com"},
             {"text": "Дальше", "action": "goto", "value": "b"}]},
        {"id": "b", "name": "Цепочка", "kind": "message",
         "trigger": {"type": "text", "value": "цена"},
         "text": "Первое", "next": "c"},
        {"id": "c", "name": "Хвост", "kind": "message",
         "trigger": {"type": "none", "value": ""}, "text": "Второе", "next": ""},
    ]}
    resp = await client.post("/api/scenario", headers=headers, json={"scenario": my})
    body = await resp.json()
    check("сценарий сохранён", resp.status == 200 and body["steps"] == 3, str(body))
    resp = await client.post("/api/scenario", headers=headers,
                             json={"scenario": {"steps": "не список"}})
    check("кривой сценарий отклонён", resp.status == 400)
    resp = await client.post("/api/scenario", headers=headers,
                             json={"scenario": {"steps": [{"id": "x"} for _ in range(200)]}})
    check("слишком длинный сценарий отклонён", resp.status == 400)

    CALLS.clear()
    await client.post(path, json={
        "message": {"chat": {"id": 556, "type": "private"},
                    "from": {"id": 556, "first_name": "Оля"}, "text": "/start"}})
    markup = [c for c in CALLS if c["method"] == "sendMessage"][0]["reply_markup"]
    rows = markup["inline_keyboard"]
    check("кнопка-ссылка стала ссылкой", rows[0][0].get("url") == "https://example.com",
          str(rows))
    check("кнопка-переход стала переходом", rows[1][0].get("callback_data") == "g:b",
          str(rows))

    CALLS.clear()
    await client.post(path, json={
        "message": {"chat": {"id": 556, "type": "private"},
                    "from": {"id": 556, "first_name": "Оля"},
                    "text": "а какая у вас ЦЕНА?"}})
    check("сработал запуск по фразе внутри сообщения",
          sent_texts() == ["Первое", "Второе"], str(sent_texts()))

    print("\n8. Мелочи")
    CALLS.clear()
    await client.post(path, json={
        "message": {"chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": 1, "first_name": "Г"}, "text": "/start"}})
    check("в группах бот не отвечает", not CALLS, str(CALLS))

    loop_scenario = {"steps": [
        {"id": "a", "kind": "message", "trigger": {"type": "command", "value": "/start"},
         "text": "круг", "next": "a", "buttons": []}]}
    await client.post("/api/scenario", headers=headers, json={"scenario": loop_scenario})
    CALLS.clear()
    await client.post(path, json={
        "message": {"chat": {"id": 557, "type": "private"},
                    "from": {"id": 557, "first_name": "П"}, "text": "/start"}})
    check("сценарий, зациклённый на себя, не вешает бота",
          0 < len(sent_texts()) <= main.MAX_HOPS, str(len(sent_texts())))

    resp = await client.post("/api/bot/disconnect", headers=headers)
    check("бота можно отключить", resp.status == 200)
    resp = await client.get("/api/state", headers=headers)
    check("после отключения бот отвязан", (await resp.json())["connected"] is False)

    print("\n9. Уборка за собой")
    await main.db.execute("DELETE FROM sessions WHERE project_id = $1", project["id"])
    await main.db.execute("DELETE FROM projects WHERE owner_id = $1", 777)
    left = await main.db.fetchrow("SELECT * FROM projects WHERE owner_id = 777")
    check("тестовые записи удалены", left is None)
    print(f"  (хранилище: {main.db.kind})")

    await client.close()
    main.SQLITE_PATH.unlink(missing_ok=True)


def entry():
    print("Проверка конструктора ботов")
    asyncio.run(run())
    print()
    if FAILED:
        print(f"Не прошло проверок: {len(FAILED)}")
        for name in FAILED:
            print("  -", name)
        sys.exit(1)
    print("Всё работает.")


if __name__ == "__main__":
    entry()
