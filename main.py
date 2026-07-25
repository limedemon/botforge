#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Конструктор Telegram-ботов — весь проект в одном файле.

Что это такое
-------------
Вы запускаете этот файл один раз. Он поднимает:

  * бота-конструктора (его токен берётся из переменной окружения BOT_TOKEN);
  * мини-приложение внутри Telegram, где человек мышкой собирает сценарий;
  * общий движок, который исполняет сценарии всех подключённых ботов.

Боты клиентов — это не отдельные программы, а записи в базе. Вебхуки всех
клиентских ботов приходят на адреса вида /hook/<id>/<секрет> одного сервиса.

Переменные окружения
--------------------
  BOT_TOKEN     — обязательно. Токен бота-конструктора от @BotFather.
  DATABASE_URL  — строка подключения к Postgres (Neon). Если не задана,
                  данные лягут в файл botforge.db рядом с этим файлом.
  PUBLIC_URL    — публичный адрес сервиса. На Render подставится сам.
  PORT          — порт. На Render подставится сам, локально 8080.
  TELEGRAM_API  — адрес Bot API, если нужен зеркальный (по умолчанию
                  https://api.telegram.org).

Запуск: python main.py
Недостающие библиотеки файл доустановит сам.
"""

import importlib
import os
import subprocess
import sys

# --------------------------------------------------------------------------
# 0. Доустановка библиотек. Чтобы файл можно было просто запустить.
# --------------------------------------------------------------------------


def _ensure(package: str, module: str = ""):
    """Возвращает модуль, при необходимости поставив пакет через pip."""
    module = module or package
    try:
        return importlib.import_module(module)
    except ImportError:
        pass
    if os.environ.get("NO_AUTO_INSTALL"):
        raise SystemExit(f"Нет библиотеки {package}. Установите: pip install {package}")
    print(f"[setup] ставлю {package}...", flush=True)
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--disable-pip-version-check", package]
    )
    importlib.invalidate_caches()
    return importlib.import_module(module)


_ensure("aiohttp")

import asyncio          # noqa: E402
import hashlib          # noqa: E402
import hmac             # noqa: E402
import json             # noqa: E402
import logging          # noqa: E402
import re               # noqa: E402
import secrets          # noqa: E402
import sqlite3          # noqa: E402
import ssl as ssl_mod   # noqa: E402
import time             # noqa: E402
import urllib.parse     # noqa: E402
from pathlib import Path                                    # noqa: E402
from typing import Any, Dict, List, Optional                # noqa: E402

import aiohttp                                              # noqa: E402
from aiohttp import web                                     # noqa: E402

log = logging.getLogger("botforge")

# --------------------------------------------------------------------------
# 1. Настройки
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = (
    os.environ.get("PUBLIC_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or f"http://localhost:{PORT}"
).rstrip("/")

SQLITE_PATH = Path(__file__).with_name("botforge.db")
INIT_DATA_TTL = 24 * 3600          # сколько живёт подпись мини-аппа
MAX_STEPS = 100
MAX_BUTTONS = 8
MAX_HOPS = 12                      # защита от сценария, зацикленного на себя

# Секрет для вебхука самого конструктора — считается из токена,
# чтобы не заводить ещё одну переменную окружения.
MAIN_SECRET = hashlib.sha256(("main:" + BOT_TOKEN).encode()).hexdigest()[:32]

DEFAULT_SCENARIO: Dict[str, Any] = {
    "steps": [
        {
            "id": "s1", "name": "Приветствие", "kind": "message",
            "trigger": {"type": "command", "value": "/start"},
            "text": "Привет, {name}! Я бот. Что вас интересует?",
            "photo": "", "save_to": "", "notify": False, "next": "",
            "x": 20, "y": 20,
            "buttons": [
                {"text": "О нас", "action": "goto", "value": "s2"},
                {"text": "Оставить заявку", "action": "goto", "value": "s3"},
            ],
        },
        {
            "id": "s2", "name": "О нас", "kind": "message",
            "trigger": {"type": "none", "value": ""},
            "text": "Мы работаем с 2010 года и делаем хорошие вещи.",
            "photo": "", "save_to": "", "notify": False, "next": "",
            "x": 280, "y": 20,
            "buttons": [{"text": "Назад", "action": "goto", "value": "s1"}],
        },
        {
            "id": "s3", "name": "Заявка: имя", "kind": "ask",
            "trigger": {"type": "none", "value": ""},
            "text": "Как вас зовут?",
            "photo": "", "save_to": "имя", "notify": False, "next": "s4",
            "x": 280, "y": 170, "buttons": [],
        },
        {
            "id": "s4", "name": "Заявка: телефон", "kind": "ask",
            "trigger": {"type": "none", "value": ""},
            "text": "Оставьте номер телефона, и мы перезвоним.",
            "photo": "", "save_to": "телефон", "notify": False, "next": "s5",
            "x": 540, "y": 170, "buttons": [],
        },
        {
            "id": "s5", "name": "Спасибо", "kind": "message",
            "trigger": {"type": "none", "value": ""},
            "text": "Спасибо, {имя}! Позвоним на {телефон}.",
            "photo": "", "save_to": "", "notify": True, "next": "",
            "x": 800, "y": 170, "buttons": [],
        },
    ]
}

# --------------------------------------------------------------------------
# 2. База данных. Postgres, если задан DATABASE_URL, иначе файл SQLite.
# --------------------------------------------------------------------------

DDL_PG = """
CREATE TABLE IF NOT EXISTS projects (
    id           BIGSERIAL PRIMARY KEY,
    owner_id     BIGINT UNIQUE NOT NULL,
    bot_token    TEXT NOT NULL DEFAULT '',
    bot_username TEXT NOT NULL DEFAULT '',
    scenario     TEXT NOT NULL,
    hook_secret  TEXT NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    project_id BIGINT NOT NULL,
    chat_id    BIGINT NOT NULL,
    awaiting   TEXT NOT NULL DEFAULT '',
    vars       TEXT NOT NULL DEFAULT '{}',
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, chat_id)
);
"""

DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER UNIQUE NOT NULL,
    bot_token    TEXT NOT NULL DEFAULT '',
    bot_username TEXT NOT NULL DEFAULT '',
    scenario     TEXT NOT NULL,
    hook_secret  TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    project_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    awaiting   TEXT NOT NULL DEFAULT '',
    vars       TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_id, chat_id)
);
"""


class Db:
    """Один и тот же набор методов поверх Postgres и SQLite.

    Запросы пишутся в стиле Postgres ($1, $2...), для SQLite подстановки
    переводятся в «?». Поэтому параметры всегда идут по порядку.
    """

    def __init__(self) -> None:
        self.pg = None                       # пул asyncpg, если Postgres
        self._pg_module = None
        self._broken: tuple = ()             # какие ошибки считаем обрывом связи
        self._sqlite: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    @property
    def kind(self) -> str:
        return "Postgres" if self.pg else "файл SQLite"

    async def start(self) -> None:
        if DATABASE_URL:
            await self._start_pg()
        else:
            self._start_sqlite()

    async def _start_pg(self) -> None:
        asyncpg = self._pg_module = _ensure("asyncpg")
        self._broken = (
            OSError,                                   # обрыв сети
            ConnectionError,
            asyncio.TimeoutError,
            asyncpg.exceptions.PostgresConnectionError,
            asyncpg.exceptions.InterfaceError,
            asyncpg.exceptions.TooManyConnectionsError,
        )
        # asyncpg не понимает часть параметров из строки Neon
        # (sslmode, channel_binding) — убираем их и включаем SSL руками.
        parts = urllib.parse.urlsplit(DATABASE_URL)
        query = dict(urllib.parse.parse_qsl(parts.query))
        sslmode = query.get("sslmode", "require")

        # Neon даёт два адреса: обычный и «-pooler» (это pgbouncer).
        # Через pgbouncer asyncpg работать не может: возвращая соединение
        # в пул, он выполняет SET SESSION AUTHORIZATION, а pgbouncer в режиме
        # транзакций за это рвёт связь. Свой пул у нас и так есть — берём
        # прямой адрес.
        netloc = parts.netloc.replace("-pooler.", ".", 1)
        if netloc != parts.netloc:
            log.info("Убрал «-pooler» из адреса базы: asyncpg держит свой пул")

        dsn = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        ssl_ctx = None if sslmode == "disable" else ssl_mod.create_default_context()

        # Спящая база просыпается не мгновенно — на старте даём ей несколько
        # попыток, иначе сервис упадёт и уйдёт в бесконечный перезапуск.
        for attempt in range(4):
            try:
                self.pg = await asyncpg.create_pool(
                    dsn=dsn,
                    ssl=ssl_ctx,
                    min_size=1,
                    max_size=5,
                    command_timeout=30,
                    # Бесплатная база засыпает; заснувшее соединение надо
                    # выбрасывать, а не пытаться использовать.
                    max_inactive_connection_lifetime=180,
                    statement_cache_size=0,
                )
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                log.warning("База не отвечает (%s), жду и пробую снова", exc)
                await asyncio.sleep(2 * (attempt + 1))

        await self._pg_run("execute", DDL_PG, ())

    def _start_sqlite(self) -> None:
        self._sqlite = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
        self._sqlite.row_factory = sqlite3.Row
        self._sqlite.executescript(DDL_SQLITE)
        self._sqlite.commit()

    @staticmethod
    def _to_sqlite(sql: str) -> str:
        return re.sub(r"\$\d+", "?", sql)

    async def _pg_run(self, action: str, sql: str, args: tuple) -> Any:
        """Запрос к Postgres с переподключением.

        Бесплатная база засыпает, да и связь бывает рваная — соединение из
        пула может оказаться уже мёртвым. Такой запрос повторяем: все наши
        запросы можно выполнить дважды без вреда.
        """
        attempts = 3
        for attempt in range(attempts):
            try:
                async with self.pg.acquire(timeout=20) as conn:
                    return await getattr(conn, action)(sql, *args)
            except self._broken as exc:
                if attempt == attempts - 1:
                    raise
                log.warning("Связь с базой оборвалась (%s), пробую снова",
                            type(exc).__name__)
                await asyncio.sleep(0.5 * (attempt + 1))

    async def execute(self, sql: str, *args: Any) -> None:
        if self.pg:
            await self._pg_run("execute", sql, args)
            return
        async with self._lock:
            self._sqlite.execute(self._to_sqlite(sql), args)
            self._sqlite.commit()

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict]:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetch(self, sql: str, *args: Any) -> List[dict]:
        if self.pg:
            rows = await self._pg_run("fetch", sql, args)
            return [dict(r) for r in rows]
        async with self._lock:
            cur = self._sqlite.execute(self._to_sqlite(sql), args)
            return [dict(r) for r in cur.fetchall()]

    async def close(self) -> None:
        if self.pg:
            await self.pg.close()
        elif self._sqlite:
            self._sqlite.close()


db = Db()


async def get_or_create_project(owner_id: int) -> dict:
    row = await db.fetchrow("SELECT * FROM projects WHERE owner_id = $1", owner_id)
    if row:
        return row
    now = time.time()
    await db.execute(
        "INSERT INTO projects (owner_id, scenario, hook_secret, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, $5) ON CONFLICT (owner_id) DO NOTHING",
        owner_id, json.dumps(DEFAULT_SCENARIO, ensure_ascii=False),
        secrets.token_urlsafe(24), now, now,
    )
    return await db.fetchrow("SELECT * FROM projects WHERE owner_id = $1", owner_id)


async def get_project(project_id: int) -> Optional[dict]:
    return await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)


async def load_session(project_id: int, chat_id: int) -> dict:
    row = await db.fetchrow(
        "SELECT * FROM sessions WHERE project_id = $1 AND chat_id = $2",
        project_id, chat_id,
    )
    if row:
        try:
            variables = json.loads(row["vars"])
        except (TypeError, ValueError):
            variables = {}
        return {"awaiting": row["awaiting"] or "", "vars": variables}
    return {"awaiting": "", "vars": {}}


async def save_session(project_id: int, chat_id: int, session: dict) -> None:
    await db.execute(
        "INSERT INTO sessions (project_id, chat_id, awaiting, vars, updated_at)"
        " VALUES ($1, $2, $3, $4, $5)"
        " ON CONFLICT (project_id, chat_id) DO UPDATE SET"
        " awaiting = excluded.awaiting, vars = excluded.vars,"
        " updated_at = excluded.updated_at",
        project_id, chat_id, session.get("awaiting", ""),
        json.dumps(session.get("vars", {}), ensure_ascii=False), time.time(),
    )


# --------------------------------------------------------------------------
# 3. Telegram Bot API. Своя тонкая обёртка: ботов много, aiogram тут лишний.
# --------------------------------------------------------------------------

_http: Optional[aiohttp.ClientSession] = None


def http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    return _http


async def tg(token: str, method: str, **params: Any) -> dict:
    """Вызов метода Bot API. Всегда возвращает словарь, никогда не падает."""
    if not token:
        return {"ok": False, "description": "нет токена"}
    try:
        async with http().post(f"{API}/bot{token}/{method}", json=params) as resp:
            try:
                return await resp.json()
            except Exception:
                body = (await resp.text())[:200]
                return {"ok": False, "description": f"HTTP {resp.status}: {body}"}
    except Exception as exc:                                  # сеть недоступна
        return {"ok": False, "description": f"нет связи с Telegram: {exc}"}


async def set_webhook(token: str, url: str, secret: str) -> dict:
    return await tg(
        token, "setWebhook", url=url, secret_token=secret,
        allowed_updates=["message", "callback_query"], drop_pending_updates=True,
    )


# --------------------------------------------------------------------------
# 4. Движок сценариев
# --------------------------------------------------------------------------

VAR_RE = re.compile(r"\{(\w+)\}")


def fill(text: str, variables: Dict[str, str]) -> str:
    """Подставляет в текст значения переменных: {имя} -> Иван."""
    return VAR_RE.sub(lambda m: str(variables.get(m.group(1), "")), text or "")


def keyboard_for(step: dict) -> Optional[dict]:
    rows = []
    for button in (step.get("buttons") or [])[:MAX_BUTTONS]:
        title = (button.get("text") or "").strip()
        if not title:
            continue
        if button.get("action") == "url":
            link = (button.get("value") or "").strip()
            if link.startswith(("http://", "https://", "tg://")):
                rows.append([{"text": title, "url": link}])
        else:
            rows.append([{"text": title, "callback_data": "g:" + (button.get("value") or "")}])
    return {"inline_keyboard": rows} if rows else None


def find_step(steps: List[dict], step_id: str) -> Optional[dict]:
    for step in steps:
        if step.get("id") == step_id:
            return step
    return None


def match_trigger(steps: List[dict], text: str) -> Optional[dict]:
    """Ищет шаг, который должен сработать на это сообщение."""
    lowered = text.strip().lower()
    command = lowered.split()[0].split("@")[0] if lowered.startswith("/") else ""

    if command:
        for step in steps:
            trigger = step.get("trigger") or {}
            if trigger.get("type") == "command":
                if (trigger.get("value") or "").strip().lower() == command:
                    return step

    for step in steps:                       # точное совпадение фразы
        trigger = step.get("trigger") or {}
        if trigger.get("type") == "text":
            if (trigger.get("value") or "").strip().lower() == lowered:
                return step

    for step in steps:                       # фраза встречается в сообщении
        trigger = step.get("trigger") or {}
        if trigger.get("type") == "text":
            value = (trigger.get("value") or "").strip().lower()
            if value and value in lowered:
                return step

    for step in steps:                       # «на любое сообщение»
        if (step.get("trigger") or {}).get("type") == "any":
            return step
    return None


async def notify_owner(project: dict, chat_id: int, session: dict) -> None:
    """Присылает владельцу заявку — в его чат с ботом-конструктором."""
    variables = session.get("vars", {})
    lines = [f"{key}: {value}" for key, value in variables.items()
             if key not in ("name", "username")]
    who = variables.get("name", "") or "клиент"
    if variables.get("username"):
        who += f" (@{variables['username']})"
    await tg(
        BOT_TOKEN, "sendMessage",
        chat_id=project["owner_id"],
        text="Новая заявка в вашем боте\nОт: {}\nid чата: {}\n\n{}".format(
            who, chat_id, "\n".join(lines) or "без ответов"
        ),
    )


async def run_step(project: dict, chat_id: int, step_id: str,
                   steps: List[dict], session: dict) -> None:
    """Проигрывает шаг и, если надо, идёт дальше по цепочке."""
    token = project["bot_token"]
    hops = 0

    while step_id and hops < MAX_HOPS:
        hops += 1
        step = find_step(steps, step_id)
        if not step:
            break

        text = fill(step.get("text", ""), session["vars"]) or "…"
        markup = keyboard_for(step)
        photo = (step.get("photo") or "").strip()

        if photo.startswith(("http://", "https://")):
            result = await tg(token, "sendPhoto", chat_id=chat_id, photo=photo,
                              caption=text[:1024], reply_markup=markup)
            if not result.get("ok"):        # битая ссылка — шлём хотя бы текст
                await tg(token, "sendMessage", chat_id=chat_id,
                         text=text[:4096], reply_markup=markup)
        else:
            await tg(token, "sendMessage", chat_id=chat_id,
                     text=text[:4096], reply_markup=markup)

        if step.get("notify"):
            await notify_owner(project, chat_id, session)

        if step.get("kind") == "ask":
            # Ждём ответ пользователя, дальше не идём.
            session["awaiting"] = step["id"]
            await save_session(project["id"], chat_id, session)
            return

        # Идём дальше только если у шага нет кнопок: с кнопками решает человек.
        step_id = "" if markup else (step.get("next") or "")

    session["awaiting"] = ""
    await save_session(project["id"], chat_id, session)


async def handle_update(project: dict, update: dict) -> None:
    try:
        scenario = json.loads(project["scenario"])
    except (TypeError, ValueError):
        return
    steps = scenario.get("steps") or []
    token = project["bot_token"]

    # --- нажатие на кнопку ---
    callback = update.get("callback_query")
    if callback:
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        await tg(token, "answerCallbackQuery", callback_query_id=callback.get("id"))
        data = callback.get("data") or ""
        if chat_id and data.startswith("g:"):
            session = await load_session(project["id"], chat_id)
            remember_user(session, callback.get("from") or {})
            session["awaiting"] = ""
            await run_step(project, chat_id, data[2:], steps, session)
        return

    # --- обычное сообщение ---
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    text = (message.get("text") or message.get("caption") or "").strip()
    if not chat_id or not text:
        return

    session = await load_session(project["id"], chat_id)
    remember_user(session, message.get("from") or {})

    # Ждём ответ на вопрос?
    awaiting_id = session.get("awaiting") or ""
    if awaiting_id and not text.startswith("/"):
        asked = find_step(steps, awaiting_id)
        session["awaiting"] = ""
        if asked:
            name = (asked.get("save_to") or "").strip()
            if name:
                session["vars"][name] = text[:500]
            await save_session(project["id"], chat_id, session)
            await run_step(project, chat_id, asked.get("next") or "", steps, session)
            return

    step = match_trigger(steps, text)
    if step:
        await run_step(project, chat_id, step["id"], steps, session)
    else:
        await save_session(project["id"], chat_id, session)


def remember_user(session: dict, user: dict) -> None:
    """Кладёт имя и ник в переменные, чтобы их можно было вставлять в текст."""
    if user.get("first_name"):
        session["vars"]["name"] = user["first_name"]
    if user.get("username"):
        session["vars"]["username"] = user["username"]


# --------------------------------------------------------------------------
# 5. Проверка подписи мини-аппа
# --------------------------------------------------------------------------


def same_secret(left: str, right: str) -> bool:
    """Сравнение секретов без подсказок по времени.

    Сравниваем байтами: в строке от чужого может оказаться кириллица или
    эмодзи, а hmac.compare_digest на таких строках падает с ошибкой.
    """
    return hmac.compare_digest(
        str(left).encode("utf-8", "replace"), str(right).encode("utf-8", "replace")
    )


def check_init_data(init_data: str) -> dict:
    """Убеждается, что данные пришли от Telegram, а не подделаны.

    Без этого кто угодно мог бы прислать чужой user_id и открыть чужой проект.
    """
    if not init_data:
        raise web.HTTPUnauthorized(text="Нет данных входа")

    data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received = data.pop("hash", "")
    if not received:
        raise web.HTTPUnauthorized(text="Нет подписи")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not same_secret(expected, received):
        raise web.HTTPUnauthorized(text="Подпись не совпадает")

    try:
        if time.time() - int(data.get("auth_date", "0")) > INIT_DATA_TTL:
            raise web.HTTPUnauthorized(text="Данные устарели, откройте заново")
    except ValueError:
        raise web.HTTPUnauthorized(text="Некорректная дата входа")

    try:
        user = json.loads(data.get("user", "{}"))
    except ValueError:
        raise web.HTTPUnauthorized(text="Некорректные данные пользователя")
    if not user.get("id"):
        raise web.HTTPUnauthorized(text="Нет пользователя")
    return user


async def current_project(request: web.Request) -> tuple:
    user = check_init_data(request.headers.get("X-Init-Data", ""))
    project = await get_or_create_project(int(user["id"]))
    return project, user


# --------------------------------------------------------------------------
# 6. Проверка сценария перед сохранением
# --------------------------------------------------------------------------

TRIGGERS = ("none", "command", "text", "any")


def clean_scenario(raw: Any) -> dict:
    """Оставляет только известные поля — чтобы в базу не попал мусор."""
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise web.HTTPBadRequest(text="Некорректный сценарий")
    if len(raw["steps"]) > MAX_STEPS:
        raise web.HTTPBadRequest(text=f"Слишком много шагов, максимум {MAX_STEPS}")

    steps = []
    for item in raw["steps"]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        trigger = item.get("trigger") or {}
        trigger_type = trigger.get("type")
        step = {
            "id": str(item["id"])[:40],
            "name": str(item.get("name", ""))[:60],
            "kind": "ask" if item.get("kind") == "ask" else "message",
            "trigger": {
                "type": trigger_type if trigger_type in TRIGGERS else "none",
                "value": str(trigger.get("value", ""))[:100],
            },
            "text": str(item.get("text", ""))[:3000],
            "photo": str(item.get("photo", ""))[:500],
            "save_to": re.sub(r"\W", "", str(item.get("save_to", "")))[:40],
            "notify": bool(item.get("notify")),
            "next": str(item.get("next", ""))[:40],
            "buttons": [
                {
                    "text": str(b.get("text", ""))[:64],
                    "action": "url" if b.get("action") == "url" else "goto",
                    "value": str(b.get("value", ""))[:500],
                }
                for b in (item.get("buttons") or [])[:MAX_BUTTONS]
                if isinstance(b, dict)
            ],
        }
        # Куда блок положили на схеме. Если координат нет — редактор
        # расставит блоки сам.
        for axis in ("x", "y"):
            value = item.get(axis)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if -100000 < value < 100000:
                    step[axis] = round(float(value), 1)
        steps.append(step)
    if not steps:
        raise web.HTTPBadRequest(text="В сценарии нет ни одного шага")
    return {"steps": steps}


# --------------------------------------------------------------------------
# 7. Маршруты: страница, API мини-аппа, приём апдейтов
# --------------------------------------------------------------------------

async def page_index(_: web.Request) -> web.Response:
    return web.Response(
        text=PAGE_HTML,               # страница целиком лежит в конце файла
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def page_health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "storage": db.kind})


async def api_state(request: web.Request) -> web.Response:
    project, user = await current_project(request)
    row = await db.fetchrow(
        "SELECT COUNT(*) AS n FROM sessions WHERE project_id = $1", project["id"]
    )
    return web.json_response({
        "scenario": json.loads(project["scenario"]),
        "connected": bool(project["bot_token"]),
        "bot_username": project["bot_username"],
        "first_name": user.get("first_name", ""),
        "people": (row or {}).get("n", 0),
    })


async def api_save(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    body = await request.json()
    scenario = clean_scenario(body.get("scenario"))
    await db.execute(
        "UPDATE projects SET scenario = $1, updated_at = $2 WHERE id = $3",
        json.dumps(scenario, ensure_ascii=False), time.time(), project["id"],
    )
    return web.json_response({"ok": True, "steps": len(scenario["steps"])})


async def api_connect(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    body = await request.json()
    token = str(body.get("token", "")).strip()
    if ":" not in token or len(token) < 20:
        raise web.HTTPBadRequest(text="Это не похоже на токен бота")

    me = await tg(token, "getMe")
    if not me.get("ok"):
        raise web.HTTPBadRequest(
            text="Telegram не принял токен: " + str(me.get("description", "ошибка"))
        )
    username = (me.get("result") or {}).get("username", "")

    info = await tg(token, "getWebhookInfo")
    previous = ((info.get("result") or {}).get("url") or "")
    warning = ""
    if previous and PUBLIC_URL not in previous:
        warning = (f"Этот бот уже был подключён к другому сервису ({previous}). "
                   "Там он работать перестанет.")

    hook = f"{PUBLIC_URL}/hook/{project['id']}/{project['hook_secret']}"
    result = await set_webhook(token, hook, project["hook_secret"])
    if not result.get("ok"):
        raise web.HTTPBadRequest(
            text="Не удалось подключить: " + str(result.get("description"))
        )

    await db.execute(
        "UPDATE projects SET bot_token = $1, bot_username = $2, updated_at = $3"
        " WHERE id = $4",
        token, username, time.time(), project["id"],
    )
    return web.json_response({"ok": True, "bot_username": username, "warning": warning})


async def api_disconnect(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    if project["bot_token"]:
        await tg(project["bot_token"], "deleteWebhook", drop_pending_updates=True)
    await db.execute(
        "UPDATE projects SET bot_token = '', bot_username = '', updated_at = $1"
        " WHERE id = $2",
        time.time(), project["id"],
    )
    return web.json_response({"ok": True})


async def hook_main(request: web.Request) -> web.Response:
    """Апдейты самого бота-конструктора: только приветствие с кнопкой."""
    if not same_secret(request.match_info["secret"], MAIN_SECRET):
        raise web.HTTPForbidden(text="forbidden")
    update = await request.json()
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip().lower()

    if chat_id and text.startswith(("/start", "/help")):
        await tg(
            BOT_TOKEN, "sendMessage", chat_id=chat_id,
            text=("Это конструктор ботов.\n\n"
                  "Нажмите кнопку ниже — откроется редактор. Соберите сценарий, "
                  "вставьте токен своего бота от @BotFather, и он заработает."),
            reply_markup={
                "keyboard": [[{"text": "Открыть конструктор",
                               "web_app": {"url": PUBLIC_URL + "/"}}]],
                "resize_keyboard": True,
            },
        )
    return web.json_response({"ok": True})


async def hook_client(request: web.Request) -> web.Response:
    """Апдейты бота клиента: находим проект и отдаём движку."""
    try:
        project_id = int(request.match_info["project_id"])
    except ValueError:
        raise web.HTTPForbidden(text="forbidden")

    project = await get_project(project_id)
    if not project or not same_secret(request.match_info["secret"], project["hook_secret"]):
        raise web.HTTPForbidden(text="forbidden")

    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret and not same_secret(header_secret, project["hook_secret"]):
        raise web.HTTPForbidden(text="forbidden")

    update = await request.json()
    try:
        await handle_update(project, update)
    except Exception:
        # Отвечаем 200 в любом случае: иначе Telegram будет слать этот
        # апдейт по кругу и бот встанет.
        log.exception("Ошибка в сценарии проекта %s", project_id)
    return web.json_response({"ok": True})


@web.middleware
async def errors_as_json(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status >= 400:
            return web.json_response({"error": exc.text or "ошибка"}, status=exc.status)
        raise
    except Exception:
        log.exception("Ошибка обработки %s", request.path)
        return web.json_response({"error": "Внутренняя ошибка"}, status=500)


# --------------------------------------------------------------------------
# 8. Запуск
# --------------------------------------------------------------------------


async def keep_awake() -> None:
    """Раз в 10 минут дёргает сам себя.

    На бесплатном Render сервис засыпает без запросов, а после сна первый
    апдейт от Telegram теряется, пока он просыпается.
    """
    if PUBLIC_URL.startswith("http://localhost"):
        return
    while True:
        await asyncio.sleep(600)
        try:
            async with http().get(PUBLIC_URL + "/health") as resp:
                await resp.read()
        except Exception:
            pass


async def on_startup(app: web.Application) -> None:
    await db.start()
    log.info("Хранилище: %s", db.kind)
    log.info("Публичный адрес: %s", PUBLIC_URL)

    result = await set_webhook(BOT_TOKEN, f"{PUBLIC_URL}/hook/main/{MAIN_SECRET}",
                               MAIN_SECRET)
    log.info("Вебхук конструктора: %s", result.get("description") or result.get("ok"))

    result = await tg(BOT_TOKEN, "setChatMenuButton", menu_button={
        "type": "web_app", "text": "Конструктор",
        "web_app": {"url": PUBLIC_URL + "/"},
    })
    log.info("Кнопка меню: %s", result.get("description") or result.get("ok"))

    # Адрес сервиса мог смениться — переподписываем ботов клиентов.
    for project in await db.fetch("SELECT * FROM projects WHERE bot_token <> ''"):
        await set_webhook(
            project["bot_token"],
            f"{PUBLIC_URL}/hook/{project['id']}/{project['hook_secret']}",
            project["hook_secret"],
        )

    app["awake"] = asyncio.create_task(keep_awake())


async def on_cleanup(app: web.Application) -> None:
    task = app.get("awake")
    if task:
        task.cancel()
    if _http and not _http.closed:
        await _http.close()
    await db.close()


def build_app() -> web.Application:
    app = web.Application(middlewares=[errors_as_json], client_max_size=1024 * 1024)
    app.router.add_get("/", page_index)
    app.router.add_get("/health", page_health)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/scenario", api_save)
    app.router.add_post("/api/bot/connect", api_connect)
    app.router.add_post("/api/bot/disconnect", api_disconnect)
    app.router.add_post("/hook/main/{secret}", hook_main)
    app.router.add_post("/hook/{project_id}/{secret}", hook_client)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN.\n"
            "Возьмите токен у @BotFather и передайте его через переменную "
            "окружения BOT_TOKEN."
        )
    web.run_app(build_app(), host="0.0.0.0", port=PORT, print=None)


# --------------------------------------------------------------------------
# 9. Страница редактора.
#
# Официальный telegram-web-app.js намеренно НЕ подключается: у части
# провайдеров telegram.org заблокирован, скрипт не загружается и мини-апп
# не открывается вовсе. Всё нужное Telegram и так передаёт в адресе
# страницы после «#», а команды клиенту уходят тем же способом, что и в SDK.
# --------------------------------------------------------------------------

PAGE_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Конструктор ботов</title>
<style>
:root{
  --bg:#ffffff; --text:#000000; --hint:#707579; --link:#3390ec;
  --button:#3390ec; --button-text:#ffffff; --secondary-bg:#f0f2f5; --danger:#e53935;
}
*{box-sizing:border-box}
html,body{margin:0}
body{padding:10px 10px 92px;background:var(--secondary-bg);color:var(--text);
     font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
     -webkit-text-size-adjust:100%}
.boot{padding:48px 16px;text-align:center;color:var(--hint);white-space:pre-line}
.card{background:var(--bg);border-radius:12px;padding:12px;margin:0 0 10px}
.head{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.num{font-size:12px;color:var(--hint);white-space:nowrap}
.name{flex:1;min-width:0;font-weight:600}
label{display:block;font-size:12px;color:var(--hint);margin:10px 0 4px}
input,select,textarea{width:100%;padding:9px 10px;font:inherit;color:var(--text);
  background:var(--secondary-bg);border:1px solid transparent;border-radius:9px}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--link)}
textarea{resize:vertical;min-height:72px}
.row{display:flex;gap:6px;align-items:center}
.row>*{min-width:0}
.icon{flex:none;width:36px;padding:8px 0;text-align:center;border:none;cursor:pointer;
      border-radius:9px;background:var(--secondary-bg);color:var(--text);font-size:15px}
.icon.danger{color:var(--danger)}
.primary{border:none;cursor:pointer;border-radius:10px;padding:12px 18px;font-weight:600;
         background:var(--button);color:var(--button-text)}
.ghost{width:100%;border:none;cursor:pointer;border-radius:12px;padding:13px;
       font-weight:600;background:var(--bg);color:var(--link);margin-bottom:10px}
.mini{width:100%;border:none;cursor:pointer;border-radius:9px;padding:9px;
      font-size:14px;background:var(--secondary-bg);color:var(--link)}
.bgroup{border-left:2px solid var(--secondary-bg);padding-left:8px;margin-bottom:8px}
.bgroup .row{margin-bottom:5px}
.bar{position:fixed;left:0;right:0;bottom:0;display:flex;gap:10px;align-items:center;
     padding:10px 12px calc(10px + env(safe-area-inset-bottom));background:var(--bg);
     border-top:1px solid rgba(128,128,128,.2)}
.hint{flex:1;font-size:12px;color:var(--hint)}
.err{color:var(--danger)}
.ok{color:#2e7d32}
.small{font-size:12px;color:var(--hint);margin-top:6px}
.check{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:14px;color:var(--text)}
.check input{width:auto}
h2{font-size:15px;margin:0 0 6px}
button{font:inherit}

/* ---- переключатель «схема / список» ---- */
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tabs button{flex:1;padding:9px;border:none;border-radius:9px;cursor:pointer;
             background:var(--bg);color:var(--hint)}
.tabs button.on{background:var(--link);color:var(--button-text);font-weight:600}

/* ---- схема ---- */
.canvas{position:relative;overflow:hidden;height:62vh;min-height:300px;margin-bottom:10px;
        background:var(--bg);border-radius:12px;touch-action:none;
        -webkit-user-select:none;user-select:none}
.world{position:absolute;left:0;top:0;transform-origin:0 0}
.wires{position:absolute;left:0;top:0;width:1px;height:1px;overflow:visible;pointer-events:none}
.wire{fill:none;stroke:var(--link);stroke-width:2;opacity:.9}
.wire-end{fill:var(--link)}
.node{position:absolute;width:180px;padding:8px;border-radius:10px;cursor:grab;
      background:var(--secondary-bg);border:2px solid transparent;
      box-shadow:0 1px 5px rgba(0,0,0,.13)}
.node.open{border-color:var(--link)}
.node.dragging{cursor:grabbing;opacity:.85}
.node-head{display:flex;gap:5px;align-items:center;font-size:13px;font-weight:600}
.node-num{color:var(--hint);font-size:11px;font-weight:400}
.node-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node-kind{flex:none;font-size:10px;font-weight:400;padding:1px 5px;border-radius:5px;
           background:var(--link);color:var(--button-text)}
.node-badge{display:inline-block;margin-top:5px;padding:1px 6px;border-radius:5px;
            font-size:11px;background:var(--bg);color:var(--link)}
.node-text{margin:5px 0 2px;font-size:11px;line-height:1.3;color:var(--hint);
           max-height:29px;overflow:hidden}
.port{position:relative;margin-top:4px;padding:3px 16px 3px 7px;border-radius:6px;
      font-size:11px;background:var(--bg);white-space:nowrap;overflow:hidden;
      text-overflow:ellipsis}
.dot{position:absolute;right:-8px;top:50%;width:14px;height:14px;border-radius:50%;
     transform:translateY(-50%);cursor:pointer;background:var(--link);
     border:2px solid var(--bg)}
.dot.armed{background:var(--danger);transform:translateY(-50%) scale(1.4)}
.dot.dot-url{background:var(--hint);cursor:default}
.tools{position:absolute;right:8px;bottom:8px;display:flex;gap:6px}
.tools button{width:34px;height:34px;border:none;border-radius:8px;cursor:pointer;
              background:var(--secondary-bg);color:var(--text);font-size:15px;opacity:.92}
.linking{position:absolute;left:8px;top:8px;right:56px;padding:6px 9px;border-radius:8px;
         font-size:12px;background:var(--link);color:var(--button-text);pointer-events:none}

/* ---- окно правки шага ---- */
.sheet{position:fixed;inset:0;z-index:20;display:flex;align-items:flex-end}
.sheet-bg{position:absolute;inset:0;background:rgba(0,0,0,.45)}
.sheet-body{position:relative;width:100%;max-height:88vh;overflow-y:auto;
            background:var(--secondary-bg);border-radius:14px 14px 0 0;
            padding:10px 10px calc(12px + env(safe-area-inset-bottom))}
</style>
</head>
<body>
<div class="boot" id="boot">Загружаю…</div>
<div id="app" hidden>
  <div class="card" id="bot"></div>
  <div class="tabs">
    <button data-view="canvas" id="tabCanvas">Схема</button>
    <button data-view="list" id="tabList">Список</button>
  </div>
  <div id="canvasView" hidden>
    <div class="canvas" id="canvas">
      <div class="world" id="world"></div>
      <div class="linking" id="linking" hidden></div>
      <div class="tools">
        <button id="zoomIn" title="крупнее">+</button>
        <button id="zoomOut" title="мельче">−</button>
        <button id="zoomFit" title="уместить всё">⤢</button>
      </div>
    </div>
  </div>
  <div id="listView" hidden>
    <div id="steps"></div>
  </div>
  <button class="ghost" id="add">+ Добавить шаг</button>
  <div class="bar">
    <span class="hint" id="hint"></span>
    <button class="primary" id="save">Сохранить</button>
  </div>
</div>
<div class="sheet" id="sheet" hidden>
  <div class="sheet-bg" id="sheetBg"></div>
  <div class="sheet-body">
    <div id="sheetCard"></div>
    <button class="primary" id="sheetDone" style="width:100%;margin-top:4px">Готово</button>
  </div>
</div>
<script>
"use strict";

/* ---- вход. Подпись Telegram кладёт в адрес после «#» ---- */
var LP = new URLSearchParams((location.hash || "").slice(1) || (location.search || "").slice(1));
var INIT = LP.get("tgWebAppData") || "";
try {
  if (INIT) sessionStorage.setItem("tgInitData", INIT);
  else INIT = sessionStorage.getItem("tgInitData") || "";
} catch (e) {}

function postEvent(type, data) {
  data = data || {};
  var json = JSON.stringify(data);
  try {
    if (window.TelegramWebviewProxy && window.TelegramWebviewProxy.postEvent) {
      window.TelegramWebviewProxy.postEvent(type, json);
    } else if (window.external && window.external.notify) {
      window.external.notify(JSON.stringify({eventType: type, eventData: data}));
    } else if (window.parent && window.parent !== window) {
      window.parent.postMessage(JSON.stringify({eventType: type, eventData: data}), "*");
    }
  } catch (e) {}
}
postEvent("web_app_ready");
postEvent("web_app_expand");

/* ---- цвета берём из темы клиента ---- */
(function () {
  var map = {bg_color: "--bg", text_color: "--text", hint_color: "--hint",
             link_color: "--link", button_color: "--button",
             button_text_color: "--button-text", secondary_bg_color: "--secondary-bg"};
  var params = {};
  try { params = JSON.parse(LP.get("tgWebAppThemeParams") || "{}"); } catch (e) { return; }
  Object.keys(params).forEach(function (key) {
    if (map[key] && /^#[0-9a-fA-F]{3,8}$/.test(params[key])) {
      document.documentElement.style.setProperty(map[key], params[key]);
    }
  });
})();

/* ---- мелкие помощники ---- */
function el(tag, props) {
  var node = document.createElement(tag);
  props = props || {};
  Object.keys(props).forEach(function (key) {
    var value = props[key];
    if (value === null || value === undefined || value === false) return;
    if (key === "class") node.className = value;
    else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  });
  for (var i = 2; i < arguments.length; i++) {
    var kid = arguments[i];
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function select(options, value, onchange) {
  var node = el("select", {onchange: onchange});
  options.forEach(function (pair) { node.append(el("option", {value: pair[0]}, pair[1])); });
  node.value = value || "";
  return node;
}

/* Кнопка, которой нужно нажать дважды. Своё подтверждение вместо confirm():
   системные диалоги в мини-аппах открываются не везде. */
function armed(button, warning, action) {
  var original = button.textContent, timer = null, ready = false;
  button.addEventListener("click", function () {
    if (ready) { clearTimeout(timer); action(); return; }
    ready = true;
    button.textContent = warning;
    button.style.width = "auto";
    button.style.padding = "8px 10px";
    timer = setTimeout(function () {
      ready = false;
      button.textContent = original;
      button.style.width = "";
      button.style.padding = "";
    }, 3000);
  });
  return button;
}

async function api(path, options) {
  options = options || {};
  options.headers = {"X-Init-Data": INIT, "Content-Type": "application/json"};
  var response = await fetch(path, options);
  var data = {};
  try { data = await response.json(); } catch (e) {}
  if (!response.ok) throw new Error(data.error || ("ошибка " + response.status));
  return data;
}

/* ---- состояние ---- */
var S = {steps: []};
var BOT = {connected: false, bot_username: "", people: 0};
var VIEW = "canvas";      /* какой режим показан: схема или список */
var OPEN = "";            /* id шага, открытого в окне правки */
var LINKING = null;       /* {id, out} — от какого кружка тянем стрелку */
var PAN = {x: 20, y: 20, z: 1, ready: false};
var NODES = {};           /* id шага -> блок на схеме */
var NODE_W = 180;
var SVGNS = "http://www.w3.org/2000/svg";

function setHint(text, kind) {
  var node = document.getElementById("hint");
  node.textContent = text || "";
  node.className = "hint " + (kind || "");
}
function touch() { setHint("Есть несохранённые изменения"); }

function nextId() {
  var used = {};
  S.steps.forEach(function (s) { used[s.id] = true; });
  var n = 1;
  while (used["s" + n]) n++;
  return "s" + n;
}
function titleOf(step, index) {
  return (step.name || "").trim() || ("Шаг " + (index + 1));
}
function move(index, delta) {
  var target = index + delta;
  if (target < 0 || target >= S.steps.length) return;
  var kept = S.steps[index];
  S.steps[index] = S.steps[target];
  S.steps[target] = kept;
  touch(); render();
}
function removeStep(index) {
  if (S.steps.length < 2) { setHint("Должен остаться хотя бы один шаг", "err"); return; }
  var gone = S.steps[index].id;
  S.steps.splice(index, 1);
  /* Убираем ссылки на удалённый шаг, иначе кнопка вела бы в пустоту. */
  S.steps.forEach(function (step) {
    if (step.next === gone) step.next = "";
    (step.buttons || []).forEach(function (button) {
      if (button.action !== "url" && button.value === gone) button.value = "";
    });
  });
  touch(); render();
}
function stepSelect(value, onchange) {
  var options = [["", "— никуда —"]];
  S.steps.forEach(function (s, i) { options.push([s.id, (i + 1) + ". " + titleOf(s, i)]); });
  return select(options, value, onchange);
}
function byId(id) {
  for (var i = 0; i < S.steps.length; i++) if (S.steps[i].id === id) return S.steps[i];
  return null;
}

/* Куда может увести шаг. У сообщения с кнопками — по выходу на кнопку,
   у остальных — один выход «далее». Это и есть кружки на схеме. */
function outputsOf(step) {
  if (step.kind === "ask") {
    return [{label: "после ответа", kind: "next", to: step.next || ""}];
  }
  var buttons = step.buttons || [];
  if (!buttons.length) {
    return [{label: "далее", kind: "next", to: step.next || ""}];
  }
  return buttons.map(function (button, index) {
    var link = button.action === "url";
    return {
      label: button.text || "кнопка",
      kind: link ? "url" : "button",
      index: index,
      to: link ? "" : (button.value || ""),
    };
  });
}

function setLink(step, outIndex, targetId) {
  var output = outputsOf(step)[outIndex];
  if (!output || output.kind === "url") return;
  if (output.kind === "next") step.next = targetId;
  else step.buttons[output.index].value = targetId;
}

/* ---- карточка одного шага ---- */
function card(step, index) {
  var box = el("div", {class: "card"});

  box.append(el("div", {class: "head"},
    el("span", {class: "num"}, "Шаг " + (index + 1)),
    el("input", {class: "name", placeholder: "название шага", value: step.name || "",
      onchange: function (e) { step.name = e.target.value; touch(); render(); }}),
    el("button", {class: "icon", onclick: function () { move(index, -1); }}, "↑"),
    el("button", {class: "icon", onclick: function () { move(index, 1); }}, "↓"),
    armed(el("button", {class: "icon danger"}, "✕"), "удалить?",
          function () { removeStep(index); })
  ));

  var trigger = step.trigger || (step.trigger = {type: "none", value: ""});
  box.append(el("label", {}, "Когда запускать"));
  var triggerRow = el("div", {class: "row"},
    select([["none", "не запускается сам"], ["command", "по команде"],
            ["text", "по фразе в сообщении"], ["any", "на любое сообщение"]],
      trigger.type || "none",
      function (e) { trigger.type = e.target.value; touch(); render(); })
  );
  if (trigger.type === "command" || trigger.type === "text") {
    triggerRow.append(el("input", {value: trigger.value || "",
      placeholder: trigger.type === "command" ? "/start" : "например: цена",
      oninput: function (e) { trigger.value = e.target.value; touch(); }}));
  }
  box.append(triggerRow);

  box.append(el("label", {}, "Что делает шаг"));
  box.append(select([["message", "отправляет сообщение"],
                     ["ask", "задаёт вопрос и ждёт ответ"]],
    step.kind === "ask" ? "ask" : "message",
    function (e) {
      step.kind = e.target.value;
      if (step.kind === "ask") step.buttons = [];   /* у вопроса кнопок нет */
      touch(); render();
    }));

  box.append(el("label", {}, "Текст"));
  box.append(el("textarea", {placeholder: "что напишет бот",
    oninput: function (e) { step.text = e.target.value; touch(); }}, step.text || ""));

  box.append(el("label", {}, "Картинка — ссылка, можно оставить пустым"));
  box.append(el("input", {value: step.photo || "", placeholder: "https://…",
    oninput: function (e) { step.photo = e.target.value; touch(); }}));

  if (step.kind === "ask") {
    box.append(el("label", {}, "Запомнить ответ под именем"));
    box.append(el("input", {value: step.save_to || "", placeholder: "телефон",
      oninput: function (e) {
        step.save_to = e.target.value.replace(/[^0-9A-Za-zА-Яа-яЁё_]/g, "");
        e.target.value = step.save_to;
        touch();
      }}));
    box.append(el("div", {class: "small"},
      "Дальше вставляйте в текст как {" + (step.save_to || "телефон") + "}"));
    box.append(el("label", {}, "После ответа перейти к шагу"));
    box.append(stepSelect(step.next, function (e) { step.next = e.target.value; touch(); }));
  } else {
    var buttons = step.buttons || (step.buttons = []);
    box.append(el("label", {}, "Кнопки под сообщением"));
    buttons.forEach(function (button, bi) {
      var target = button.action === "url"
        ? el("input", {value: button.value || "", placeholder: "https://…",
            oninput: function (e) { button.value = e.target.value; touch(); }})
        : stepSelect(button.value, function (e) { button.value = e.target.value; touch(); });
      box.append(el("div", {class: "bgroup"},
        el("div", {class: "row"},
          el("input", {value: button.text || "", placeholder: "надпись на кнопке",
            oninput: function (e) { button.text = e.target.value; touch(); }}),
          el("button", {class: "icon danger",
            onclick: function () { buttons.splice(bi, 1); touch(); render(); }}, "✕")),
        el("div", {class: "row"},
          select([["goto", "ведёт на шаг"], ["url", "ведёт на сайт"]],
            button.action || "goto",
            function (e) { button.action = e.target.value; button.value = ""; touch(); render(); }),
          target)
      ));
    });
    if (buttons.length < 8) {
      box.append(el("button", {class: "mini", onclick: function () {
        buttons.push({text: "", action: "goto", value: ""}); touch(); render();
      }}, "+ кнопка"));
    }
    if (!buttons.length) {
      box.append(el("label", {}, "Сразу после — перейти к шагу"));
      box.append(stepSelect(step.next, function (e) { step.next = e.target.value; touch(); }));
    }
    var flag = el("input", {type: "checkbox",
      onchange: function (e) { step.notify = e.target.checked; touch(); }});
    flag.checked = !!step.notify;
    box.append(el("label", {class: "check"}, flag,
      el("span", {}, "Прислать мне заявку с ответами, когда дойдут сюда")));
  }

  return box;
}

function renderList() {
  var box = document.getElementById("steps");
  box.textContent = "";
  S.steps.forEach(function (step, index) { box.append(card(step, index)); });
}

/* ======================= схема ======================= */

/* Блокам без координат раскладываем места сами: по столбцу на шаг цепочки. */
function ensurePositions() {
  var missing = S.steps.filter(function (s) { return typeof s.x !== "number"; });
  if (!missing.length) return;
  if (missing.length === S.steps.length) { autoLayout(); return; }
  var bottom = 0;
  S.steps.forEach(function (s) {
    if (typeof s.y === "number") bottom = Math.max(bottom, s.y);
  });
  missing.forEach(function (step, i) { step.x = 20; step.y = bottom + 150 * (i + 1); });
}

function autoLayout() {
  var COL = 260, ROW = 150, depth = {}, seen = {};
  var roots = S.steps.filter(function (s) { return (s.trigger || {}).type !== "none"; });
  if (!roots.length) roots = S.steps.slice(0, 1);
  var queue = roots.map(function (s) { return [s, 0]; });
  while (queue.length) {
    var pair = queue.shift(), step = pair[0], level = pair[1];
    if (seen[step.id]) continue;
    seen[step.id] = true;
    depth[step.id] = level;
    outputsOf(step).forEach(function (out) {
      var next = out.to ? byId(out.to) : null;
      if (next && !seen[next.id]) queue.push([next, level + 1]);
    });
  }
  var taken = {};
  S.steps.forEach(function (step) {
    var level = depth[step.id] || 0;
    taken[level] = taken[level] || 0;
    step.x = 20 + level * COL;
    step.y = 20 + taken[level] * ROW;
    taken[level]++;
  });
}

function applyPan() {
  document.getElementById("world").style.transform =
    "translate(" + PAN.x + "px," + PAN.y + "px) scale(" + PAN.z + ")";
}

function fitView() {
  var canvas = document.getElementById("canvas");
  if (!S.steps.length || !canvas.clientWidth) return;
  var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  S.steps.forEach(function (s) {
    minX = Math.min(minX, s.x); minY = Math.min(minY, s.y);
    maxX = Math.max(maxX, s.x + NODE_W); maxY = Math.max(maxY, s.y + 130);
  });
  var zoom = Math.min(1, (canvas.clientWidth - 30) / (maxX - minX),
                         (canvas.clientHeight - 30) / (maxY - minY));
  PAN.z = Math.max(0.3, zoom);
  PAN.x = (canvas.clientWidth - (maxX - minX) * PAN.z) / 2 - minX * PAN.z;
  PAN.y = 15 - minY * PAN.z;
  applyPan();
}

function zoomBy(factor) {
  var canvas = document.getElementById("canvas");
  var cx = canvas.clientWidth / 2, cy = canvas.clientHeight / 2;
  var wx = (cx - PAN.x) / PAN.z, wy = (cy - PAN.y) / PAN.z;
  PAN.z = Math.min(2, Math.max(0.3, PAN.z * factor));
  PAN.x = cx - wx * PAN.z;
  PAN.y = cy - wy * PAN.z;
  applyPan();
}

function wirePath(x1, y1, x2, y2) {
  var bend = Math.max(35, Math.abs(x2 - x1) / 2);
  var path = document.createElementNS(SVGNS, "path");
  path.setAttribute("class", "wire");
  path.setAttribute("d", "M " + x1 + " " + y1 +
    " C " + (x1 + bend) + " " + y1 + ", " + (x2 - bend) + " " + y2 +
    ", " + x2 + " " + y2);
  return path;
}

/* Линии рисуем отдельно от блоков: при перетаскивании блок не пересоздаётся,
   перерисовываются только линии. */
function drawWires() {
  var svg = document.getElementById("wires");
  if (!svg) return;
  svg.textContent = "";
  S.steps.forEach(function (step) {
    var from = NODES[step.id];
    if (!from) return;
    outputsOf(step).forEach(function (out, index) {
      var target = out.to ? byId(out.to) : null;
      var to = target ? NODES[target.id] : null;
      if (!to) return;
      var port = from.querySelector('[data-out="' + index + '"]');
      if (!port) return;
      var x1 = step.x + from.offsetWidth;
      var y1 = step.y + port.offsetTop + port.offsetHeight / 2;
      var x2 = target.x;
      var y2 = target.y + to.offsetHeight / 2;
      svg.append(wirePath(x1, y1, x2, y2));
      var end = document.createElementNS(SVGNS, "circle");
      end.setAttribute("class", "wire-end");
      end.setAttribute("cx", x2);
      end.setAttribute("cy", y2);
      end.setAttribute("r", 4);
      svg.append(end);
    });
  });
}

function nodeEl(step, index) {
  var node = el("div", {class: "node" + (step.id === OPEN ? " open" : ""),
                        "data-id": step.id});
  node.style.left = step.x + "px";
  node.style.top = step.y + "px";

  var trigger = step.trigger || {};
  var badge = trigger.type === "command" ? (trigger.value || "команда")
            : trigger.type === "text" ? "фраза: " + (trigger.value || "")
            : trigger.type === "any" ? "любое сообщение" : "";

  node.append(el("div", {class: "node-head"},
    el("span", {class: "node-num"}, index + 1),
    el("span", {class: "node-name"}, titleOf(step, index)),
    step.kind === "ask" ? el("span", {class: "node-kind"}, "вопрос") : null));
  if (badge) node.append(el("div", {class: "node-badge"}, badge));
  node.append(el("div", {class: "node-text"},
    (step.text || "без текста").slice(0, 80)));

  outputsOf(step).forEach(function (out, oi) {
    var port = el("div", {class: "port", "data-out": oi},
      el("span", {}, out.label));
    var lit = LINKING && LINKING.id === step.id && LINKING.out === oi;
    var dot = el("span", {class: "dot" + (out.kind === "url" ? " dot-url" : "") +
                                 (lit ? " armed" : "")});
    if (out.kind !== "url") {
      /* Кружок не должен таскать блок — гасим начало перетаскивания. */
      dot.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
      dot.addEventListener("click", function (e) {
        e.stopPropagation();
        armPort(step, oi);
      });
    }
    port.append(dot);
    node.append(port);
  });

  attachDrag(node, step);
  return node;
}

function attachDrag(node, step) {
  var grab = null;
  node.addEventListener("pointerdown", function (e) {
    if (e.button === 1 || e.button === 2) return;
    e.stopPropagation();                     /* иначе поедет всё полотно */
    grab = {x: e.clientX, y: e.clientY, sx: step.x, sy: step.y, moved: false};
    try { node.setPointerCapture(e.pointerId); } catch (err) {}
    node.classList.add("dragging");
  });
  node.addEventListener("pointermove", function (e) {
    if (!grab) return;
    var dx = (e.clientX - grab.x) / PAN.z, dy = (e.clientY - grab.y) / PAN.z;
    if (!grab.moved && Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
    grab.moved = true;
    step.x = Math.round(grab.sx + dx);
    step.y = Math.round(grab.sy + dy);
    node.style.left = step.x + "px";
    node.style.top = step.y + "px";
    drawWires();
  });
  node.addEventListener("pointerup", function (e) {
    if (!grab) return;
    var moved = grab.moved;
    grab = null;
    node.classList.remove("dragging");
    if (moved) { touch(); return; }          /* блок просто передвинули */
    if (LINKING) { linkTo(step); return; }   /* ждали, куда вести стрелку */
    OPEN = step.id;                          /* обычное касание — правка */
    renderCanvas();
    renderSheet();
  });
  node.addEventListener("pointercancel", function () {
    grab = null;
    node.classList.remove("dragging");
  });
}

function armPort(step, outIndex) {
  var same = LINKING && LINKING.id === step.id && LINKING.out === outIndex;
  LINKING = same ? null : {id: step.id, out: outIndex};
  renderCanvas();
}

function linkTo(target) {
  var source = byId(LINKING.id);
  var output = source ? outputsOf(source)[LINKING.out] : null;
  if (!output) { LINKING = null; renderCanvas(); return; }
  var already = output.to === target.id;
  setLink(source, LINKING.out, already ? "" : target.id);
  LINKING = null;
  touch();
  renderCanvas();
  setHint(already ? "Стрелка убрана" : "Соединено");
}

function renderCanvas() {
  ensurePositions();
  var world = document.getElementById("world");
  world.textContent = "";
  NODES = {};

  var svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", "wires");
  svg.setAttribute("id", "wires");
  world.append(svg);

  S.steps.forEach(function (step, index) {
    var node = nodeEl(step, index);
    world.append(node);
    NODES[step.id] = node;
  });

  if (!PAN.ready) { PAN.ready = true; fitView(); } else { applyPan(); }
  drawWires();                    /* высоты блоков известны только теперь */

  var tip = document.getElementById("linking");
  tip.hidden = !LINKING;
  if (LINKING) {
    var source = byId(LINKING.id);
    var output = source ? outputsOf(source)[LINKING.out] : null;
    tip.textContent = output && output.to
      ? "Коснитесь блока, куда вести. Тот же блок — убрать стрелку."
      : "Коснитесь блока, куда вести стрелку.";
  }
}

/* Полотно: одним пальцем двигаем, двумя — приближаем. */
function attachCanvas() {
  var canvas = document.getElementById("canvas");
  var points = {}, pan = null, pinch = null;

  function ids() { return Object.keys(points); }
  function gap(a, b) {
    return Math.hypot(points[a].x - points[b].x, points[a].y - points[b].y);
  }

  canvas.addEventListener("pointerdown", function (e) {
    points[e.pointerId] = {x: e.clientX, y: e.clientY};
    var list = ids();
    if (list.length === 1) {
      pan = {x: e.clientX, y: e.clientY, px: PAN.x, py: PAN.y, moved: false};
      try { canvas.setPointerCapture(e.pointerId); } catch (err) {}
    } else if (list.length === 2) {
      var box = canvas.getBoundingClientRect();
      var mx = (points[list[0]].x + points[list[1]].x) / 2 - box.left;
      var my = (points[list[0]].y + points[list[1]].y) / 2 - box.top;
      pan = null;
      pinch = {gap: gap(list[0], list[1]), z: PAN.z, box: box,
               wx: (mx - PAN.x) / PAN.z, wy: (my - PAN.y) / PAN.z};
    }
  });

  canvas.addEventListener("pointermove", function (e) {
    if (!(e.pointerId in points)) return;
    points[e.pointerId] = {x: e.clientX, y: e.clientY};
    var list = ids();
    if (pinch && list.length >= 2) {
      var now = gap(list[0], list[1]);
      if (!now) return;
      PAN.z = Math.min(2, Math.max(0.3, pinch.z * now / pinch.gap));
      var mx = (points[list[0]].x + points[list[1]].x) / 2 - pinch.box.left;
      var my = (points[list[0]].y + points[list[1]].y) / 2 - pinch.box.top;
      PAN.x = mx - pinch.wx * PAN.z;
      PAN.y = my - pinch.wy * PAN.z;
      applyPan();
    } else if (pan) {
      var dx = e.clientX - pan.x, dy = e.clientY - pan.y;
      if (!pan.moved && Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
      pan.moved = true;
      PAN.x = pan.px + dx;
      PAN.y = pan.py + dy;
      applyPan();
    }
  });

  function release(e) {
    delete points[e.pointerId];
    if (ids().length < 2) pinch = null;
    if (pan && !pan.moved && LINKING) {  /* мимо блока — стрелку отменяем */
      LINKING = null;
      renderCanvas();
    }
    if (!ids().length) pan = null;
  }
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);

  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    var box = canvas.getBoundingClientRect();
    var mx = e.clientX - box.left, my = e.clientY - box.top;
    var wx = (mx - PAN.x) / PAN.z, wy = (my - PAN.y) / PAN.z;
    PAN.z = Math.min(2, Math.max(0.3, PAN.z * (e.deltaY < 0 ? 1.12 : 0.89)));
    PAN.x = mx - wx * PAN.z;
    PAN.y = my - wy * PAN.z;
    applyPan();
  }, {passive: false});

  /* Кнопки лежат поверх полотна — не даём им сдвигать его. */
  canvas.querySelector(".tools").addEventListener("pointerdown", function (e) {
    e.stopPropagation();
  });
  document.getElementById("zoomIn").addEventListener("click", function () { zoomBy(1.2); });
  document.getElementById("zoomOut").addEventListener("click", function () { zoomBy(0.83); });
  document.getElementById("zoomFit").addEventListener("click", fitView);
}

/* ---- окно правки шага ---- */
function renderSheet() {
  var sheet = document.getElementById("sheet");
  var index = -1;
  S.steps.forEach(function (s, i) { if (s.id === OPEN) index = i; });
  if (index < 0) { OPEN = ""; sheet.hidden = true; return; }
  sheet.hidden = false;
  var body = document.getElementById("sheetCard");
  body.textContent = "";
  body.append(card(S.steps[index], index));
}

function closeSheet() {
  OPEN = "";
  document.getElementById("sheet").hidden = true;
  if (VIEW === "canvas") renderCanvas();
}

function render() {
  document.getElementById("canvasView").hidden = VIEW !== "canvas";
  document.getElementById("listView").hidden = VIEW !== "list";
  document.getElementById("tabCanvas").className = VIEW === "canvas" ? "on" : "";
  document.getElementById("tabList").className = VIEW === "list" ? "on" : "";
  if (VIEW === "canvas") renderCanvas(); else renderList();
  renderSheet();
}

/* ---- панель бота ---- */
function openBot() {
  if (!BOT.bot_username) return;
  var bridge = window.TelegramWebviewProxy ||
               (window.external && window.external.notify) ||
               window.parent !== window;
  /* Внутри Telegram просим открыть чат сам клиент. Уходить по ссылке нельзя:
     это увело бы страницу редактора. */
  if (bridge) postEvent("web_app_open_tg_link", {path_full: "/" + BOT.bot_username});
  else location.href = "https://t.me/" + BOT.bot_username;
}

async function connect(token, button) {
  token = (token || "").trim();
  if (!token) { setHint("Вставьте токен", "err"); return; }
  button.disabled = true;
  setHint("Подключаю…");
  try {
    var result = await api("/api/bot/connect",
      {method: "POST", body: JSON.stringify({token: token})});
    BOT.connected = true;
    BOT.bot_username = result.bot_username;
    renderBot();
    setHint(result.warning || ("Бот @" + result.bot_username + " подключён"),
            result.warning ? "err" : "ok");
  } catch (e) {
    setHint(e.message, "err");
    button.disabled = false;
  }
}

async function disconnect() {
  setHint("Отключаю…");
  try {
    await api("/api/bot/disconnect", {method: "POST"});
    BOT.connected = false;
    BOT.bot_username = "";
    renderBot();
    setHint("Бот отключён");
  } catch (e) { setHint(e.message, "err"); }
}

function renderBot() {
  var box = document.getElementById("bot");
  box.textContent = "";
  box.append(el("h2", {}, "Ваш бот"));
  if (BOT.connected) {
    box.append(el("div", {}, "Работает: @" + BOT.bot_username));
    box.append(el("div", {class: "small"}, "Человек в боте: " + BOT.people));
    box.append(el("div", {class: "row", style: "margin-top:10px"},
      el("button", {class: "primary", onclick: openBot}, "Открыть бота"),
      armed(el("button", {class: "icon danger", style: "width:auto;padding:12px 14px"},
        "Отключить"), "точно?", disconnect)));
  } else {
    box.append(el("div", {class: "small"},
      "Создайте бота у @BotFather, скопируйте его токен и вставьте сюда."));
    var field = el("input", {placeholder: "1234567:AA…", style: "margin-top:8px"});
    var go = el("button", {class: "primary", style: "margin-top:8px;width:100%"},
      "Подключить");
    go.addEventListener("click", function () { connect(field.value, go); });
    box.append(field, go);
  }
}

/* ---- сохранение и добавление ---- */
document.getElementById("save").addEventListener("click", async function () {
  var button = this;
  button.disabled = true;
  setHint("Сохраняю…");
  try {
    var result = await api("/api/scenario",
      {method: "POST", body: JSON.stringify({scenario: S})});
    var hasStart = S.steps.some(function (s) {
      return s.trigger && s.trigger.type === "command" &&
             (s.trigger.value || "").trim() === "/start";
    });
    setHint(hasStart
      ? "Сохранено, шагов: " + result.steps
      : "Сохранено, но ни один шаг не запускается по /start", hasStart ? "ok" : "err");
  } catch (e) {
    setHint(e.message, "err");
  }
  button.disabled = false;
});

document.getElementById("add").addEventListener("click", function () {
  var step = {id: nextId(), name: "", kind: "message",
              trigger: {type: "none", value: ""}, text: "", photo: "",
              save_to: "", notify: false, next: "", buttons: []};
  if (VIEW === "canvas") {
    /* Кладём новый блок на середину того, что человек сейчас видит. */
    var canvas = document.getElementById("canvas");
    step.x = Math.round((canvas.clientWidth / 2 - PAN.x) / PAN.z - NODE_W / 2);
    step.y = Math.round((canvas.clientHeight / 2 - PAN.y) / PAN.z - 45);
    S.steps.push(step);
    OPEN = step.id;
  } else {
    S.steps.push(step);
  }
  touch();
  render();
  if (VIEW === "list") window.scrollTo(0, document.body.scrollHeight);
});

document.getElementById("tabCanvas").addEventListener("click", function () {
  VIEW = "canvas"; render();
});
document.getElementById("tabList").addEventListener("click", function () {
  VIEW = "list"; render();
});
document.getElementById("sheetBg").addEventListener("click", closeSheet);
document.getElementById("sheetDone").addEventListener("click", closeSheet);

/* ---- старт ---- */
(async function () {
  var boot = document.getElementById("boot");
  if (!INIT) {
    boot.className = "boot err";
    boot.textContent = "Telegram не передал данные для входа.\n\n" +
      "Откройте конструктор кнопкой в чате с ботом или кнопкой слева от поля " +
      "ввода — по обычной ссылке в браузере вход не работает.";
    return;
  }
  try {
    var state = await api("/api/state");
    S = (state.scenario && state.scenario.steps) ? state.scenario : {steps: []};
    BOT.connected = state.connected;
    BOT.bot_username = state.bot_username;
    BOT.people = state.people;
    boot.hidden = true;
    document.getElementById("app").hidden = false;
    renderBot();
    attachCanvas();
    render();
    setHint(BOT.connected ? "Бот подключён" : "Подключите бота, чтобы сценарий заработал");
  } catch (e) {
    boot.className = "boot err";
    boot.textContent = "Не удалось загрузить: " + e.message;
  }
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
