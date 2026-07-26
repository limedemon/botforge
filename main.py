#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Конструктор Telegram-ботов — весь проект в одном файле.

Что это такое
-------------
Вы запускаете этот файл один раз. Он поднимает:

  * бота-конструктора (его токен берётся из переменной окружения BOT_TOKEN);
  * мини-приложение внутри Telegram: полотно, на котором человек собирает
    схему из блоков и соединяет их стрелками;
  * общий движок, который исполняет схемы всех подключённых ботов.

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
import random           # noqa: E402
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
MAX_STEPS = 150
MAX_BUTTONS = 10
MAX_HOPS = 30                      # защита от схемы, замкнутой на себя
MAX_ASSETS = 80                    # сколько картинок помещается в галерею
MAX_ASSET_BYTES = 5 * 1024 * 1024  # и какого размера каждая
MAX_VARS = 200
MAX_TAGS = 100
TAGS_KEY = "#tags"                 # теги лежат среди переменных под этим ключом
MENU_KEY = "#menu"                 # чьи кнопки сейчас показаны под полем ввода
RANDOM_KEY = "#rnd:"               # какой вариант рандома уже выпал человеку

# Секрет для вебхука самого конструктора — считается из токена,
# чтобы не заводить ещё одну переменную окружения.
MAIN_SECRET = hashlib.sha256(("main:" + BOT_TOKEN).encode()).hexdigest()[:32]

# --------------------------------------------------------------------------
# 2. Что такое сценарий
#
# Сценарий — это набор блоков и стрелки между ними. У каждого блока свой тип,
# от него зависят и поля, и сколько у блока выходов.
#
#   message   — отправить сообщение (текст, картинка, файл, кнопки)
#   input     — задать вопрос и запомнить ответ
#   keywords  — точка входа: сработать на слова в сообщении
#   event     — точка входа: сработать на событие (первое сообщение, фото…)
#   action    — переменные, теги, уведомление владельцу
#   condition — развилка «да / нет»
#   random    — случайный выбор из вариантов с весами
#   timer     — подождать и продолжить
#   note      — заметка на полотне, ботом не исполняется
# --------------------------------------------------------------------------

TYPES = ("message", "input", "keywords", "event", "action",
         "condition", "random", "timer", "note")

EVENTS = ("first", "unknown", "blocked", "unblocked", "photo", "video",
          "file", "location", "voice", "any")
CONDITION_OPS = ("eq", "ne", "has", "empty", "tag", "gt", "lt", "gte", "lte")
ACTION_KINDS = ("set_var", "add_tag", "del_tag", "notify",
                "subscribe", "unsubscribe")
TIMER_UNITS = ("minute", "hour", "day")
UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}

# --------------------------------------------------------------------------
# 2а. Переменные и теги — общий список на весь проект
#
# Переменная бывает двух видов и двух типов значения:
#
#   вид   user    — своё значение у каждого человека (ответы, счётчики)
#         project — одно значение на всех (цена, название компании)
#   тип   text    — что угодно: буквы, цифры, знаки
#         number  — только число; с ним работают + − × ÷
#
# Действие «Изменить переменную» у текстовой переменной умеет только «=»:
# складывать буквы бессмысленно, поэтому остальные знаки для неё закрыты.
# --------------------------------------------------------------------------

VAR_SCOPES = ("user", "project")
VAR_TYPES = ("text", "number")
SET_OPS = ("set", "add", "sub", "mul", "div")

DEFAULT_SCENARIO: Dict[str, Any] = {
    "start": "s1",
    "steps": [
        {
            "id": "s1", "type": "message", "name": "", "x": 40, "y": 40,
            "text": "Привет, {name}! Я бот. Что вас интересует?",
            "photo": "", "file": "", "next": "",
            "buttons": [
                {"text": "О нас", "action": "goto", "value": "s2"},
                {"text": "Оставить заявку", "action": "goto", "value": "s3"},
            ],
        },
        {
            "id": "s2", "type": "message", "name": "О нас", "x": 340, "y": 40,
            "text": "Мы работаем с 2010 года и делаем хорошие вещи.",
            "photo": "", "file": "", "next": "",
            "buttons": [{"text": "Назад", "action": "goto", "value": "s1"}],
        },
        {
            "id": "s3", "type": "input", "name": "Имя", "x": 340, "y": 250,
            "text": "Как вас зовут?", "save_to": "имя", "next": "s4",
        },
        {
            "id": "s4", "type": "input", "name": "Телефон", "x": 640, "y": 250,
            "text": "Оставьте номер телефона, и мы перезвоним.",
            "save_to": "телефон", "next": "s5",
        },
        {
            "id": "s5", "type": "action", "name": "Прислать заявку",
            "x": 940, "y": 250, "next": "s6",
            "actions": [{"kind": "notify", "name": "", "value": ""}],
        },
        {
            "id": "s6", "type": "message", "name": "Спасибо",
            "x": 1240, "y": 250,
            "text": "Спасибо, {имя}! Позвоним на {телефон}.",
            "photo": "", "file": "", "next": "", "buttons": [],
        },
    ],
}

# --------------------------------------------------------------------------
# 3. База данных. Postgres, если задан DATABASE_URL, иначе файл SQLite.
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
    subscribed INTEGER NOT NULL DEFAULT 1,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, chat_id)
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS subscribed INTEGER NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS timers (
    id         BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL,
    chat_id    BIGINT NOT NULL,
    step_id    TEXT NOT NULL,
    run_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS timers_due ON timers (run_at);
CREATE TABLE IF NOT EXISTS assets (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   BIGINT NOT NULL,
    token      TEXT NOT NULL UNIQUE,
    mime       TEXT NOT NULL DEFAULT 'image/jpeg',
    bytes      BYTEA NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_owner ON assets (owner_id, created_at);
CREATE TABLE IF NOT EXISTS variables (
    id         BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL,
    name       TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'user',
    vtype      TEXT NOT NULL DEFAULT 'text',
    descr      TEXT NOT NULL DEFAULT '',
    value      TEXT NOT NULL DEFAULT '',
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS variables_name ON variables (project_id, name);
CREATE TABLE IF NOT EXISTS tags (
    id         BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL,
    name       TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tags_name ON tags (project_id, name);
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
    subscribed INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_id, chat_id)
);
CREATE TABLE IF NOT EXISTS timers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    step_id    TEXT NOT NULL,
    run_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS timers_due ON timers (run_at);
CREATE TABLE IF NOT EXISTS assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    token      TEXT NOT NULL UNIQUE,
    mime       TEXT NOT NULL DEFAULT 'image/jpeg',
    bytes      BLOB NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_owner ON assets (owner_id, created_at);
CREATE TABLE IF NOT EXISTS variables (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'user',
    vtype      TEXT NOT NULL DEFAULT 'text',
    descr      TEXT NOT NULL DEFAULT '',
    value      TEXT NOT NULL DEFAULT '',
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS variables_name ON variables (project_id, name);
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tags_name ON tags (project_id, name);
"""

# Столбцы, дописанные к уже существующим таблицам. У SQLite нет
# «ADD COLUMN IF NOT EXISTS», поэтому пробуем и молча пропускаем, если он есть.
SQLITE_PATCHES = (
    "ALTER TABLE sessions ADD COLUMN subscribed INTEGER NOT NULL DEFAULT 1",
)


class Db:
    """Один и тот же набор методов поверх Postgres и SQLite.

    Запросы пишутся в стиле Postgres ($1, $2...), для SQLite подстановки
    переводятся в «?». Поэтому параметры всегда идут по порядку.
    """

    def __init__(self) -> None:
        self.pg = None                       # пул asyncpg, если Postgres
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
        asyncpg = _ensure("asyncpg")
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
        for patch in SQLITE_PATCHES:
            try:
                self._sqlite.execute(patch)
            except sqlite3.OperationalError:
                pass                                   # столбец уже на месте
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
    if not row:
        blank = {"awaiting": "", "vars": {}, "first": True, "subscribed": True}
        await attach_registry(project_id, blank)
        return blank
    try:
        variables = json.loads(row["vars"])
    except (TypeError, ValueError):
        variables = {}
    if not isinstance(variables, dict):
        variables = {}
    session = {"awaiting": row["awaiting"] or "", "vars": variables, "first": False,
               "subscribed": bool(row.get("subscribed", 1))}
    await attach_registry(project_id, session)
    return session


async def save_session(project_id: int, chat_id: int, session: dict) -> None:
    await db.execute(
        "INSERT INTO sessions (project_id, chat_id, awaiting, vars, subscribed, updated_at)"
        " VALUES ($1, $2, $3, $4, $5, $6)"
        " ON CONFLICT (project_id, chat_id) DO UPDATE SET"
        " awaiting = excluded.awaiting, vars = excluded.vars,"
        " subscribed = excluded.subscribed, updated_at = excluded.updated_at",
        project_id, chat_id, session.get("awaiting", ""),
        json.dumps(session.get("vars", {}), ensure_ascii=False),
        1 if session.get("subscribed", True) else 0, time.time(),
    )


# --------------------------------------------------------------------------
# 3а. Списки проекта: переменные, теги, картинки
# --------------------------------------------------------------------------


async def list_vars(project_id: int) -> List[dict]:
    return await db.fetch(
        "SELECT * FROM variables WHERE project_id = $1 ORDER BY archived, name",
        project_id,
    )


async def var_registry(project_id: int) -> Dict[str, dict]:
    """Все переменные проекта: имя -> что это за переменная."""
    return {row["name"]: row for row in await list_vars(project_id)}


async def ensure_var(project_id: int, name: str, scope: str = "user",
                     vtype: str = "text") -> None:
    """Заводит переменную, если её ещё нет.

    Имя переменной может появиться прямо на схеме — в блоке «Ввод» или в
    действии. Чтобы оно не потерялось, такую переменную сразу заносим в
    общий список: человек увидит её на странице «Переменные».
    """
    if not name:
        return
    row = await db.fetchrow(
        "SELECT id FROM variables WHERE project_id = $1 AND name = $2",
        project_id, name,
    )
    if row:
        return
    count = await db.fetchrow(
        "SELECT COUNT(*) AS n FROM variables WHERE project_id = $1", project_id)
    if (count or {}).get("n", 0) >= MAX_VARS:
        return
    await db.execute(
        "INSERT INTO variables (project_id, name, scope, vtype, descr, value,"
        " archived, created_at) VALUES ($1, $2, $3, $4, '', '', 0, $5)",
        project_id, name,
        scope if scope in VAR_SCOPES else "user",
        vtype if vtype in VAR_TYPES else "text", time.time(),
    )


async def list_tags(project_id: int) -> List[dict]:
    return await db.fetch(
        "SELECT * FROM tags WHERE project_id = $1 ORDER BY name", project_id)


async def ensure_tag(project_id: int, name: str) -> None:
    if not name:
        return
    row = await db.fetchrow(
        "SELECT id FROM tags WHERE project_id = $1 AND name = $2", project_id, name)
    if row:
        return
    count = await db.fetchrow(
        "SELECT COUNT(*) AS n FROM tags WHERE project_id = $1", project_id)
    if (count or {}).get("n", 0) >= MAX_TAGS:
        return
    await db.execute(
        "INSERT INTO tags (project_id, name, created_at) VALUES ($1, $2, $3)",
        project_id, name, time.time(),
    )


async def save_asset(owner_id: int, blob: bytes, mime: str) -> str:
    """Кладёт картинку в галерею владельца и возвращает её метку."""
    token = secrets.token_urlsafe(16)
    await db.execute(
        "INSERT INTO assets (owner_id, token, mime, bytes, created_at)"
        " VALUES ($1, $2, $3, $4, $5)",
        owner_id, token, mime, blob, time.time(),
    )
    # Галерея не резиновая: самые старые вытесняются новыми.
    extra = await db.fetch(
        "SELECT token FROM assets WHERE owner_id = $1 ORDER BY created_at DESC",
        owner_id,
    )
    for old in extra[MAX_ASSETS:]:
        await db.execute("DELETE FROM assets WHERE token = $1", old["token"])
    return token


# --------------------------------------------------------------------------
# 4. Telegram Bot API. Своя тонкая обёртка: ботов много, aiogram тут лишний.
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
    params = {k: v for k, v in params.items() if v is not None}
    try:
        async with http().post(f"{API}/bot{token}/{method}", json=params) as resp:
            try:
                return await resp.json()
            except Exception:
                body = (await resp.text())[:200]
                return {"ok": False, "description": f"HTTP {resp.status}: {body}"}
    except Exception as exc:                                  # сеть недоступна
        return {"ok": False, "description": f"нет связи с Telegram: {exc}"}


async def tg_download(token: str, file_id: str) -> bytes:
    """Скачивает присланный файл. Пустые байты — значит не получилось."""
    info = await tg(token, "getFile", file_id=file_id)
    path = ((info.get("result") or {}).get("file_path") or "")
    if not path:
        return b""
    try:
        async with http().get(f"{API}/file/bot{token}/{path}") as resp:
            if resp.status != 200:
                return b""
            blob = await resp.content.read(MAX_ASSET_BYTES + 1)
            return b"" if len(blob) > MAX_ASSET_BYTES else blob
    except Exception:
        return b""


async def set_webhook(token: str, url: str, secret: str) -> dict:
    return await tg(
        token, "setWebhook", url=url, secret_token=secret,
        allowed_updates=["message", "callback_query", "my_chat_member"],
        drop_pending_updates=True,
    )


# --------------------------------------------------------------------------
# 5. Проверка схемы перед сохранением
#
# В базу кладём только известные поля и только те, что имеют смысл для типа
# блока. Всё остальное отбрасываем, чтобы туда не попал мусор.
# --------------------------------------------------------------------------


def _text(value: Any, limit: int) -> str:
    return str(value if value is not None else "")[:limit]


def _name(value: Any) -> str:
    """Имя переменной или тега: буквы, цифры, пробел и подчёркивание."""
    text = re.sub(r"[^\w ]", "", str(value or ""), flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()[:40]


def _media(value: Any) -> str:
    """Картинка или файл: либо метка из галереи, либо обычная ссылка."""
    text = _text(value, 500).strip()
    if re.fullmatch(r"asset:[A-Za-z0-9_-]{1,64}", text):
        return text
    return text if text.startswith(("http://", "https://")) else ""


def clean_scenario(raw: Any) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise web.HTTPBadRequest(text="Некорректная схема")
    if len(raw["steps"]) > MAX_STEPS:
        raise web.HTTPBadRequest(text=f"Слишком много блоков, максимум {MAX_STEPS}")

    steps = []
    for item in raw["steps"]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        kind = item.get("type")
        if kind not in TYPES:
            continue

        step = {
            "id": _text(item["id"], 40),
            "type": kind,
            "name": _text(item.get("name"), 60),
        }
        for axis in ("x", "y"):
            value = item.get(axis)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if -100000 < value < 100000:
                    step[axis] = round(float(value), 1)

        if kind != "note":
            step["next"] = _text(item.get("next"), 40)

        if kind == "message":
            step["text"] = _text(item.get("text"), 3000)
            step["photo"] = _media(item.get("photo"))
            step["file"] = _media(item.get("file"))
            step["inline"] = bool(item.get("inline"))
            step["buttons"] = [
                {
                    "text": _text(b.get("text"), 64),
                    "action": "url" if b.get("action") == "url" else "goto",
                    "value": _text(b.get("value"), 500),
                }
                for b in (item.get("buttons") or [])[:MAX_BUTTONS]
                if isinstance(b, dict)
            ]

        elif kind == "input":
            step["text"] = _text(item.get("text"), 3000)
            step["save_to"] = _name(item.get("save_to"))
            step["expect"] = "number" if item.get("expect") == "number" else "any"
            step["retry"] = _text(item.get("retry"), 500)

        elif kind == "keywords":
            step["match"] = "exact" if item.get("match") == "exact" else "contains"
            step["words"] = _text(item.get("words"), 500)

        elif kind == "event":
            event = item.get("event")
            step["event"] = event if event in EVENTS else "first"

        elif kind == "action":
            step["actions"] = []
            for a in (item.get("actions") or [])[:10]:
                if not isinstance(a, dict) or a.get("kind") not in ACTION_KINDS:
                    continue
                op = a.get("op")
                step["actions"].append({
                    "kind": a["kind"],
                    "name": _name(a.get("name")),
                    "op": op if op in SET_OPS else "set",
                    "value": _text(a.get("value"), 500),
                })

        elif kind == "condition":
            step["checks"] = []
            for c in (item.get("checks") or [])[:10]:
                if not isinstance(c, dict):
                    continue
                op = c.get("op")
                step["checks"].append({
                    "var": _name(c.get("var")),
                    "op": op if op in CONDITION_OPS else "eq",
                    "value": _text(c.get("value"), 200),
                })
            step["otherwise"] = _text(item.get("otherwise"), 40)

        elif kind == "random":
            step["always"] = bool(item.get("always"))
            step["options"] = []
            for o in (item.get("options") or [])[:6]:
                if not isinstance(o, dict):
                    continue
                weight = o.get("weight")
                if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                    weight = 50
                step["options"].append({
                    "label": _text(o.get("label"), 20),
                    "weight": max(0, min(100, round(float(weight)))),
                    "next": _text(o.get("next"), 40),
                })
            step.pop("next", None)

        elif kind == "timer":
            amount = item.get("amount")
            if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                amount = 1
            step["amount"] = max(1, min(365, int(amount)))
            unit = item.get("unit")
            step["unit"] = unit if unit in TIMER_UNITS else "day"

        elif kind == "note":
            step["text"] = _text(item.get("text"), 1000)

        steps.append(step)

    if not steps:
        raise web.HTTPBadRequest(text="На схеме нет ни одного блока")

    known = {s["id"] for s in steps}
    start = _text(raw.get("start"), 40)
    if start not in known:
        first = next((s["id"] for s in steps if s["type"] == "message"), steps[0]["id"])
        start = first
    return {"start": start, "steps": steps}


def refresh_actions(raw: dict) -> dict:
    """Подтягивает старые действия под нынешний набор.

    Раньше «удалить переменную» было отдельным действием — теперь это то же
    «Изменить переменную», только с пустым значением. И у каждого действия
    появился знак: «=», «+», «−», «×», «÷».
    """
    for step in raw.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for action in step.get("actions") or []:
            if not isinstance(action, dict):
                continue
            if action.get("kind") == "del_var":
                action["kind"] = "set_var"
                action["value"] = ""
            action.setdefault("op", "set")
    return raw


def migrate_scenario(raw: Any) -> dict:
    """Переводит схему из первой версии (шаги с триггерами) в блоки.

    Старый шаг знал сам, чем он запускается. Теперь запуск — это отдельные
    блоки «Ключевые слова» и «События», а команда /start отмечает стартовый
    блок. Нужно, чтобы у тех, кто успел собрать бота, ничего не пропало.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        return dict(DEFAULT_SCENARIO)
    if any(isinstance(s, dict) and s.get("type") in TYPES for s in raw["steps"]):
        return refresh_actions(raw)             # уже новый формат

    steps, extra, start = [], [], ""
    for old in raw["steps"]:
        if not isinstance(old, dict) or not old.get("id"):
            continue
        trigger = old.get("trigger") or {}
        ttype, tvalue = trigger.get("type"), str(trigger.get("value") or "")

        step = {
            "id": str(old["id"]), "name": str(old.get("name") or ""),
            "next": str(old.get("next") or ""),
            "text": str(old.get("text") or ""),
        }
        for axis in ("x", "y"):
            if isinstance(old.get(axis), (int, float)):
                step[axis] = float(old[axis])

        if old.get("kind") == "ask":
            step["type"] = "input"
            step["save_to"] = str(old.get("save_to") or "")
        else:
            step["type"] = "message"
            step["photo"] = str(old.get("photo") or "")
            step["file"] = ""
            # В самой первой версии цель кнопки лежала в поле goto.
            step["buttons"] = [
                {
                    "text": str(b.get("text") or ""),
                    "action": "url" if b.get("action") == "url" else "goto",
                    "value": str(b.get("value") or b.get("goto") or ""),
                }
                for b in (old.get("buttons") or []) if isinstance(b, dict)
            ]

        # Старая галочка «прислать мне заявку» становится блоком «Действие».
        if old.get("notify"):
            helper = {
                "id": step["id"] + "_n", "type": "action", "name": "Заявка",
                "next": step["next"],
                "actions": [{"kind": "notify", "name": "", "value": ""}],
                "x": step.get("x", 0) + 300, "y": step.get("y", 0),
            }
            step["next"] = helper["id"]
            extra.append(helper)

        if ttype == "command" and tvalue.strip().lower() == "/start":
            start = step["id"]
        elif ttype == "text" and tvalue:
            extra.append({
                "id": step["id"] + "_k", "type": "keywords", "name": "",
                "match": "contains", "words": tvalue, "next": step["id"],
                "x": step.get("x", 0) - 300, "y": step.get("y", 0),
            })
        elif ttype == "any":
            extra.append({
                "id": step["id"] + "_e", "type": "event", "name": "",
                "event": "any", "next": step["id"],
                "x": step.get("x", 0) - 300, "y": step.get("y", 0),
            })

        steps.append(step)

    steps.extend(extra)
    if not steps:
        return dict(DEFAULT_SCENARIO)
    return {"start": start or steps[0]["id"], "steps": steps}


def load_scenario(project: dict) -> dict:
    try:
        raw = json.loads(project["scenario"])
    except (TypeError, ValueError):
        return dict(DEFAULT_SCENARIO)
    return migrate_scenario(raw)


# --------------------------------------------------------------------------
# 6. Движок: как схема превращается в поведение бота
# --------------------------------------------------------------------------

# Имя переменной может быть из нескольких слов — «{КЛИК за всё время}».
# Скобки с чем-то другим внутри (смайлик, знак) под это не подходят и
# остаются в тексте как есть.
VAR_RE = re.compile(r"\{([\w ]{1,40})\}", re.UNICODE)


def fill(text: str, variables: Dict[str, Any]) -> str:
    """Подставляет в текст значения переменных: {имя} -> Иван."""
    return VAR_RE.sub(lambda m: str(variables.get(m.group(1), "")), text or "")


def as_number(value: Any) -> float:
    """Число из чего угодно. «12 руб» -> 12, пусто -> 0."""
    text = str(value if value is not None else "").replace(",", ".").strip()
    found = re.search(r"-?\d+(?:\.\d+)?", text)
    try:
        return float(found.group(0)) if found else 0.0
    except ValueError:
        return 0.0


def calculate(text: str) -> Optional[float]:
    """Считает выражение из чисел и знаков + − × ÷ со скобками.

    Нужно, чтобы в значении можно было написать «{счёт} + 1»: переменные
    к этому месту уже заменены на числа. Если это не похоже на выражение,
    возвращаем None — значит, значение возьмут как есть.
    """
    source = (text or "")
    for was, now in (("×", "*"), ("÷", "/"), ("−", "-"), ("–", "-"), (",", ".")):
        source = source.replace(was, now)
    if not source.strip() or not re.fullmatch(r"[\d\s.+\-*/()]+", source):
        return None

    tokens = re.findall(r"\d+(?:\.\d+)?|[+\-*/()]", source)
    place = 0

    def peek() -> str:
        return tokens[place] if place < len(tokens) else ""

    def take() -> str:
        nonlocal place
        place += 1
        return tokens[place - 1]

    def atom() -> float:
        token = peek()
        if token == "(":
            take()
            value = summa()
            if peek() == ")":
                take()
            return value
        if token == "-":
            take()
            return -atom()
        if token == "+":
            take()
            return atom()
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            return float(take())
        raise ValueError("не выражение")

    def product() -> float:
        value = atom()
        while peek() in ("*", "/"):
            sign, right = take(), atom()
            value = value * right if sign == "*" else (value / right if right else value)
        return value

    def summa() -> float:
        value = product()
        while peek() in ("+", "-"):
            sign, right = take(), product()
            value = value + right if sign == "+" else value - right
        return value

    try:
        result = summa()
    except (ValueError, IndexError, ZeroDivisionError, OverflowError):
        return None
    return result if place == len(tokens) else None


def looks_like_number(text: str) -> bool:
    """Правда ли, что человек написал именно число, а не «примерно пять»."""
    return bool(re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", (text or "").strip()))


def show_number(value: float) -> str:
    """Число обратно в текст: 12.0 -> «12», 12.5 -> «12.5»."""
    if value != value or value in (float("inf"), float("-inf")):
        return "0"
    rounded = round(value, 6)
    if rounded == int(rounded):
        return str(int(rounded))
    return ("%.6f" % rounded).rstrip("0").rstrip(".")


def find_step(steps: List[dict], step_id: str) -> Optional[dict]:
    for step in steps:
        if step.get("id") == step_id:
            return step
    return None


def tags_of(session: dict) -> List[str]:
    tags = session.get("vars", {}).get(TAGS_KEY)
    return list(tags) if isinstance(tags, list) else []


async def attach_registry(project_id: int, session: dict) -> None:
    """Кладёт рядом с сеансом список переменных проекта.

    Проектные переменные — общие для всех, они лежат в базе, а не в сеансе.
    Читаем их один раз на весь разбор сообщения.
    """
    meta, shared = {}, {}
    for row in await list_vars(project_id):
        meta[row["name"]] = {"scope": row["scope"], "vtype": row["vtype"]}
        if row["scope"] == "project":
            shared[row["name"]] = row["value"]
    session["reg"] = {"meta": meta, "project": shared}


def all_vars(session: dict) -> Dict[str, Any]:
    """Что можно подставить в текст: общие переменные плюс личные."""
    shared = (session.get("reg") or {}).get("project") or {}
    return dict(shared, **session.get("vars", {}))


def var_kind(session: dict, name: str) -> tuple:
    meta = ((session.get("reg") or {}).get("meta") or {}).get(name) or {}
    return meta.get("scope") or "user", meta.get("vtype") or "text"


async def write_var(project: dict, session: dict, name: str, value: str) -> None:
    """Пишет значение туда, где эта переменная живёт."""
    scope, _ = var_kind(session, name)
    if scope == "project":
        (session.setdefault("reg", {}).setdefault("project", {}))[name] = value
        await db.execute(
            "UPDATE variables SET value = $1 WHERE project_id = $2 AND name = $3",
            value, project["id"], name,
        )
    else:
        session.setdefault("vars", {})[name] = value


def remember_user(session: dict, user: dict) -> None:
    """Кладёт имя и ник в переменные, чтобы их можно было вставлять в текст."""
    if user.get("first_name"):
        session["vars"]["name"] = user["first_name"]
    if user.get("username"):
        session["vars"]["username"] = user["username"]


def keyboard_for(step: dict, variables: Optional[Dict[str, Any]] = None) -> dict:
    """Кнопки сообщения — под полем ввода или внутри самого сообщения.

    Под полем ввода (обычная клавиатура): нажатие приходит боту простым
    текстом, поэтому потом мы ищем кнопку по надписи (см. match_menu).
    Ссылку такая кнопка нести не умеет.

    Внутри сообщения (галочка «кнопки внутри сообщения»): нажатие приходит
    отдельным событием, и такая кнопка умеет быть настоящей ссылкой.

    Если кнопок нет, просим Telegram убрать прежние: иначе под полем ввода
    так и висели бы кнопки от предыдущего блока.
    """
    variables = variables or {}
    buttons = (step.get("buttons") or [])[:MAX_BUTTONS]

    if step.get("inline"):
        rows = []
        for button in buttons:
            title = fill((button.get("text") or "").strip(), variables)
            if not title:
                continue
            value = (button.get("value") or "").strip()
            if button.get("action") == "url":
                if value.startswith(("http://", "https://", "tg://")):
                    rows.append([{"text": title, "url": value}])
            else:
                rows.append([{"text": title, "callback_data": "g:" + value[:60]}])
        return {"inline_keyboard": rows} if rows else {}

    rows = []
    for button in buttons:
        title = fill((button.get("text") or "").strip(), variables)
        if title:
            rows.append([{"text": title}])
    if not rows:
        return {"remove_keyboard": True}
    return {"keyboard": rows, "resize_keyboard": True}


def match_menu(steps: List[dict], session: dict, text: str) -> Optional[dict]:
    """Ищет кнопку показанной сейчас клавиатуры по надписи."""
    menu_id = session.get("vars", {}).get(MENU_KEY) or ""
    wanted = (text or "").strip().lower()
    if not menu_id or not wanted:
        return None
    step = find_step(steps, menu_id)
    if not step:
        return None
    variables = all_vars(session)
    for button in step.get("buttons") or []:
        title = fill((button.get("text") or "").strip(), variables)
        if title.strip().lower() == wanted:
            return button
    return None


def media_kind(message: dict) -> str:
    if message.get("photo"):
        return "photo"
    if message.get("video") or message.get("video_note"):
        return "video"
    if message.get("document"):
        return "file"
    if message.get("location") or message.get("venue"):
        return "location"
    if message.get("voice") or message.get("audio"):
        return "voice"
    return ""


def match_event(steps: List[dict], name: str) -> Optional[dict]:
    if not name:
        return None
    for step in steps:
        if step.get("type") == "event" and step.get("event") == name:
            return step
    return None


def match_keywords(steps: List[dict], text: str) -> Optional[dict]:
    """Ищет блок «Ключевые слова», подходящий к сообщению.

    Точное совпадение важнее: если один блок ждёт ровно «цена», а другой —
    любое сообщение со словом «цена», выиграет первый.
    """
    lowered = text.strip().lower()
    if not lowered:
        return None
    loose = None
    for step in steps:
        if step.get("type") != "keywords":
            continue
        words = [w.strip().lower() for w in (step.get("words") or "").split(",")]
        words = [w for w in words if w]
        if not words:
            continue
        if step.get("match") == "exact":
            if lowered in words:
                return step
        elif loose is None and any(word in lowered for word in words):
            loose = step
    return loose


def condition_holds(step: dict, session: dict) -> bool:
    """Все проверки блока «Условие» должны сойтись, иначе идём по «Нет»."""
    variables = all_vars(session)
    tags = tags_of(session)
    for check in step.get("checks") or []:
        current = str(variables.get(check.get("var") or "", "")).strip()
        wanted = fill(check.get("value") or "", variables).strip()
        op = check.get("op") or "eq"
        if op == "eq" and current.lower() != wanted.lower():
            return False
        if op == "ne" and current.lower() == wanted.lower():
            return False
        if op == "has" and wanted.lower() not in current.lower():
            return False
        if op == "empty" and current:
            return False
        if op == "tag" and wanted not in tags:
            return False
        # Сравнение чисел: «12» меньше «9» только по алфавиту, поэтому
        # для «больше» и «меньше» обе стороны переводим в числа.
        if op in ("gt", "lt", "gte", "lte"):
            left, right = as_number(current), as_number(wanted)
            if op == "gt" and not left > right:
                return False
            if op == "lt" and not left < right:
                return False
            if op == "gte" and not left >= right:
                return False
            if op == "lte" and not left <= right:
                return False
    return True


def pick_random(step: dict, session: dict) -> str:
    """Выбирает вариант с учётом весов. Вариант без стрелки не участвует.

    Обычно выпавший вариант закрепляется за человеком: если ему один раз
    выпало «A», то же выпадет и в следующий раз — так делают A/B-проверки.
    Галочка «выбирать заново каждый раз» это отключает.
    """
    options = [o for o in (step.get("options") or []) if o.get("next")]
    if not options:
        return ""

    memory = session.setdefault("vars", {})
    key = RANDOM_KEY + (step.get("id") or "")
    if not step.get("always"):
        was = memory.get(key)
        if was and any(o["next"] == was for o in options):
            return was

    total = sum(max(0, o.get("weight") or 0) for o in options)
    if total <= 0:
        chosen = random.choice(options)["next"]
    else:
        point, running, chosen = random.uniform(0, total), 0.0, options[-1]["next"]
        for option in options:
            running += max(0, option.get("weight") or 0)
            if point <= running:
                chosen = option["next"]
                break
    memory[key] = chosen
    return chosen


async def notify_owner(project: dict, chat_id: int, session: dict, note: str = "") -> None:
    """Присылает владельцу заявку — в его чат с ботом-конструктором."""
    variables = session.get("vars", {})
    lines = [f"{key}: {value}" for key, value in variables.items()
             if key not in ("name", "username") and not str(key).startswith("#")]
    who = variables.get("name", "") or "клиент"
    if variables.get("username"):
        who += f" (@{variables['username']})"
    body = note.strip() or "\n".join(lines) or "без ответов"
    await tg(
        BOT_TOKEN, "sendMessage", chat_id=project["owner_id"],
        text=f"Новая заявка в вашем боте\nОт: {who}\nid чата: {chat_id}\n\n{body}",
    )


async def apply_actions(project: dict, chat_id: int, step: dict, session: dict) -> None:
    variables = session.setdefault("vars", {})
    tags = tags_of(session)
    for action in step.get("actions") or []:
        kind = action.get("kind")
        name = action.get("name") or ""
        value = fill(action.get("value") or "", all_vars(session))
        if kind == "set_var" and name:
            await write_var(project, session, name,
                            new_value(session, name, action.get("op"), value))
        elif kind == "add_tag":
            tag = name or value.strip()
            if tag and tag not in tags:
                tags.append(tag)
        elif kind == "del_tag":
            tag = name or value.strip()
            if tag in tags:
                tags.remove(tag)
        elif kind == "notify":
            await notify_owner(project, chat_id, session, value)
        elif kind == "subscribe":
            session["subscribed"] = True
        elif kind == "unsubscribe":
            session["subscribed"] = False
    variables[TAGS_KEY] = tags


def new_value(session: dict, name: str, op: str, value: str) -> str:
    """Считает новое значение переменной с учётом её типа и знака.

    У текстовой переменной знак всегда «=»: складывать и умножать буквы
    не получится. У числовой пустое значение считается нулём.
    """
    _, vtype = var_kind(session, name)
    op = op if op in SET_OPS else "set"
    if vtype != "number":
        return str(value)[:500]

    was = as_number(all_vars(session).get(name, 0))
    counted = calculate(value)
    add = counted if counted is not None else as_number(value)
    if op == "set":
        return show_number(add)
    if op == "add":
        return show_number(was + add)
    if op == "sub":
        return show_number(was - add)
    if op == "mul":
        return show_number(was * add)
    if op == "div":
        return show_number(was / add) if add else show_number(was)
    return show_number(add)


def media_url(value: str) -> str:
    """Адрес картинки: из галереи или обычная ссылка.

    Картинки из галереи лежат у нас в базе и отдаются по своему адресу —
    Telegram забирает их оттуда сам.
    """
    text = (value or "").strip()
    if text.startswith("asset:"):
        return f"{PUBLIC_URL}/img/{text[6:]}"
    return text if text.startswith(("http://", "https://")) else ""


async def tg_text(token: str, method: str, **params: Any) -> dict:
    """Отправка с разметкой <b>, <i>, <code>.

    Если в тексте окажется одинокая угловая скобка, Telegram откажется его
    разбирать. Тогда шлём заново как есть: лучше сообщение без оформления,
    чем молчание бота.
    """
    result = await tg(token, method, parse_mode="HTML", **params)
    if not result.get("ok") and "parse" in str(result.get("description", "")).lower():
        result = await tg(token, method, **params)
    return result


async def send_message_step(token: str, chat_id: int, step: dict,
                            variables: Dict[str, Any], markup: dict) -> None:
    text = fill(step.get("text", ""), variables)
    photo = media_url(step.get("photo"))
    document = media_url(step.get("file"))
    markup = markup or None
    sent = False

    if photo:
        result = await tg_text(token, "sendPhoto", chat_id=chat_id, photo=photo,
                               caption=text[:1024],
                               reply_markup=None if document else markup)
        sent = bool(result.get("ok"))

    if document:
        result = await tg_text(token, "sendDocument", chat_id=chat_id,
                               document=document,
                               caption="" if sent else text[:1024],
                               reply_markup=markup)
        sent = sent or bool(result.get("ok"))

    if not sent:
        # Ни картинки, ни файла — или Telegram не принял ссылку.
        await tg_text(token, "sendMessage", chat_id=chat_id,
                      text=(text or "…")[:4096], reply_markup=markup)


async def schedule_timer(project: dict, chat_id: int, step: dict) -> None:
    target = step.get("next") or ""
    if not target:
        return
    seconds = UNIT_SECONDS.get(step.get("unit") or "day", 86400)
    seconds *= max(1, int(step.get("amount") or 1))
    await db.execute(
        "INSERT INTO timers (project_id, chat_id, step_id, run_at)"
        " VALUES ($1, $2, $3, $4)",
        project["id"], chat_id, target, time.time() + seconds,
    )


async def run_step(project: dict, chat_id: int, step_id: str,
                   scenario: dict, session: dict) -> None:
    """Идёт по схеме от указанного блока, пока не упрётся в ожидание или конец."""
    steps = scenario.get("steps") or []
    token = project["bot_token"]
    hops = 0

    while step_id and hops < MAX_HOPS:
        hops += 1
        step = find_step(steps, step_id)
        if not step:
            break
        kind = step.get("type")

        if kind == "message":
            variables = all_vars(session)
            markup = keyboard_for(step, variables)
            await send_message_step(token, chat_id, step, variables, markup)
            # Запоминаем, чьи кнопки сейчас под полем ввода: нажатие придёт
            # обычным текстом, и по нему надо будет узнать кнопку.
            session["vars"][MENU_KEY] = step["id"] if markup.get("keyboard") else ""
            step_id = step.get("next") or ""

        elif kind == "input":
            # Ждём ответ словами — прежние кнопки только мешают.
            await tg(token, "sendMessage", chat_id=chat_id,
                     text=(fill(step.get("text", ""), all_vars(session)) or "…")[:4096],
                     reply_markup={"remove_keyboard": True})
            session["vars"][MENU_KEY] = ""
            session["awaiting"] = step["id"]
            await save_session(project["id"], chat_id, session)
            return

        elif kind == "action":
            await apply_actions(project, chat_id, step, session)
            step_id = step.get("next") or ""

        elif kind == "condition":
            step_id = (step.get("next") if condition_holds(step, session)
                       else step.get("otherwise")) or ""

        elif kind == "random":
            step_id = pick_random(step, session)

        elif kind == "timer":
            await schedule_timer(project, chat_id, step)
            session["awaiting"] = ""
            await save_session(project["id"], chat_id, session)
            return

        elif kind == "note":
            break

        else:                                  # keywords, event — просто проход
            step_id = step.get("next") or ""

    session["awaiting"] = ""
    await save_session(project["id"], chat_id, session)


async def handle_update(project: dict, update: dict) -> None:
    scenario = load_scenario(project)
    steps = scenario.get("steps") or []
    token = project["bot_token"]

    # --- бота заблокировали или разблокировали ---
    member = update.get("my_chat_member")
    if member:
        chat = member.get("chat") or {}
        if chat.get("type") != "private" or not chat.get("id"):
            return
        status = (member.get("new_chat_member") or {}).get("status") or ""
        name = ("blocked" if status == "kicked"
                else "unblocked" if status == "member" else "")
        step = match_event(steps, name)
        if step:
            session = await load_session(project["id"], chat["id"])
            session.pop("first", None)
            remember_user(session, member.get("from") or {})
            await run_step(project, chat["id"], step.get("next") or "",
                           scenario, session)
        return

    # --- нажатие на кнопку ---
    callback = update.get("callback_query")
    if callback:
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        await tg(token, "answerCallbackQuery", callback_query_id=callback.get("id"))
        data = callback.get("data") or ""
        if chat_id and data.startswith("g:"):
            session = await load_session(project["id"], chat_id)
            session.pop("first", None)
            remember_user(session, callback.get("from") or {})
            session["awaiting"] = ""
            await run_step(project, chat_id, data[2:], scenario, session)
        return

    # --- обычное сообщение ---
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or not chat.get("id"):
        return
    chat_id = chat["id"]

    session = await load_session(project["id"], chat_id)
    was_first = session.pop("first", False)
    remember_user(session, message.get("from") or {})
    text = (message.get("text") or message.get("caption") or "").strip()

    # 1. Нажали кнопку под полем ввода — она приходит обычным текстом.
    button = match_menu(steps, session, text)
    if button:
        if button.get("action") == "url":
            # Обычная кнопка не умеет быть ссылкой — присылаем её сообщением.
            link = (button.get("value") or "").strip()
            await tg(token, "sendMessage", chat_id=chat_id,
                     text=link or "Ссылка не указана")
            await save_session(project["id"], chat_id, session)
        else:
            session["awaiting"] = ""
            await run_step(project, chat_id, button.get("value") or "",
                           scenario, session)
        return

    # 2. Ждём ответ на заданный вопрос.
    awaiting_id = session.get("awaiting") or ""
    if awaiting_id and text and not text.startswith("/"):
        asked = find_step(steps, awaiting_id)
        if asked:
            # Ждали число, а пришли буквы — переспрашиваем, не сходя с места.
            if asked.get("expect") == "number" and not looks_like_number(text):
                retry = fill(asked.get("retry") or "", all_vars(session))
                await tg_text(token, "sendMessage", chat_id=chat_id,
                              text=(retry or "Нужно число. Напишите цифрами.")[:4096])
                await save_session(project["id"], chat_id, session)
                return

            session["awaiting"] = ""
            name = (asked.get("save_to") or "").strip()
            if name:
                _, vtype = var_kind(session, name)
                await write_var(project, session, name,
                                show_number(as_number(text)) if vtype == "number"
                                else text[:500])
            await save_session(project["id"], chat_id, session)
            await run_step(project, chat_id, asked.get("next") or "",
                           scenario, session)
            return
        session["awaiting"] = ""

    # 2. Команда запуска.
    if text.lower().startswith("/start"):
        await run_step(project, chat_id, scenario.get("start") or "",
                       scenario, session)
        return

    # 3. Человек написал боту впервые.
    if was_first:
        step = match_event(steps, "first")
        if step:
            await run_step(project, chat_id, step.get("next") or "",
                           scenario, session)
            return

    # 4. Прислал фото, файл, голосовое и так далее.
    step = match_event(steps, media_kind(message))
    if step:
        await run_step(project, chat_id, step.get("next") or "", scenario, session)
        return

    # 5. Ключевые слова.
    step = match_keywords(steps, text) if text else None
    if step:
        await run_step(project, chat_id, step.get("next") or "", scenario, session)
        return

    # 6. Команда, которой в схеме нет.
    if text.startswith("/"):
        step = match_event(steps, "unknown")
        if step:
            await run_step(project, chat_id, step.get("next") or "",
                           scenario, session)
            return

    # 7. Совсем на любое сообщение.
    step = match_event(steps, "any")
    if step:
        await run_step(project, chat_id, step.get("next") or "", scenario, session)
        return

    await save_session(project["id"], chat_id, session)


async def claim_timers(limit: int = 20) -> List[dict]:
    """Забирает созревшие задания себе.

    В Postgres — одним запросом, с удалением: если сервис вдруг окажется
    запущен в двух экземплярах, каждое задание достанется ровно одному и
    человек не получит одно сообщение дважды. С файловой базой экземпляр
    всегда один, там достаточно выбрать и удалить.
    """
    now = time.time()
    if db.pg:
        return await db.fetch(
            "DELETE FROM timers WHERE id IN ("
            " SELECT id FROM timers WHERE run_at <= $1 ORDER BY run_at LIMIT $2"
            ") RETURNING *", now, limit,
        )
    due = await db.fetch(
        "SELECT * FROM timers WHERE run_at <= $1 ORDER BY run_at LIMIT $2", now, limit)
    for job in due:
        await db.execute("DELETE FROM timers WHERE id = $1", job["id"])
    return due


async def tick_timers() -> int:
    """Забирает созревшие таймеры и продолжает схему с их блока."""
    due = await claim_timers()
    for job in due:
        project = await get_project(job["project_id"])
        if not project or not project["bot_token"]:
            continue
        session = await load_session(project["id"], job["chat_id"])
        session.pop("first", None)
        await run_step(project, job["chat_id"], job["step_id"],
                       load_scenario(project), session)
    return len(due)


async def run_timers() -> None:
    """Раз в двадцать секунд проверяет, не пора ли кому-то продолжить."""
    while True:
        await asyncio.sleep(20)
        try:
            await tick_timers()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в таймерах")


# --------------------------------------------------------------------------
# 7. Проверка подписи мини-аппа
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
# 8. Маршруты: страница, API мини-аппа, приём апдейтов
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
        "scenario": load_scenario(project),
        "connected": bool(project["bot_token"]),
        "bot_username": project["bot_username"],
        "first_name": user.get("first_name", ""),
        "people": (row or {}).get("n", 0),
        "vars": [short_var(v) for v in await list_vars(project["id"])],
        "tags": [t["name"] for t in await list_tags(project["id"])],
    })


def short_var(row: dict) -> dict:
    return {"name": row["name"], "scope": row["scope"], "vtype": row["vtype"],
            "descr": row["descr"], "value": row["value"],
            "archived": bool(row["archived"])}


async def register_names(project_id: int, scenario: dict) -> None:
    """Заводит переменные и теги, которые человек написал прямо на схеме."""
    for step in scenario.get("steps") or []:
        if step.get("type") == "input" and step.get("save_to"):
            await ensure_var(project_id, step["save_to"], "user",
                             "number" if step.get("expect") == "number" else "text")
        for action in step.get("actions") or []:
            if action.get("kind") == "set_var" and action.get("name"):
                await ensure_var(project_id, action["name"], "user",
                                 "number" if action.get("op") != "set" else "text")
            if action.get("kind") in ("add_tag", "del_tag") and action.get("name"):
                await ensure_tag(project_id, action["name"])
        for check in step.get("checks") or []:
            if check.get("op") == "tag":
                await ensure_tag(project_id, _name(check.get("value")))
            elif check.get("var"):
                await ensure_var(project_id, check["var"])


async def api_save(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    body = await request.json()
    scenario = clean_scenario(body.get("scenario"))
    await db.execute(
        "UPDATE projects SET scenario = $1, updated_at = $2 WHERE id = $3",
        json.dumps(scenario, ensure_ascii=False), time.time(), project["id"],
    )
    await register_names(project["id"], scenario)
    return web.json_response({
        "ok": True, "steps": len(scenario["steps"]),
        "vars": [short_var(v) for v in await list_vars(project["id"])],
        "tags": [t["name"] for t in await list_tags(project["id"])],
    })


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
    await db.execute("DELETE FROM timers WHERE project_id = $1", project["id"])
    await db.execute(
        "UPDATE projects SET bot_token = '', bot_username = '', updated_at = $1"
        " WHERE id = $2",
        time.time(), project["id"],
    )
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------
# 8а. Галерея картинок
# --------------------------------------------------------------------------


async def api_assets(request: web.Request) -> web.Response:
    _, user = await current_project(request)
    rows = await db.fetch(
        "SELECT token, mime, created_at FROM assets WHERE owner_id = $1"
        " ORDER BY created_at DESC", int(user["id"]),
    )
    return web.json_response({"assets": [
        {"token": r["token"], "created_at": r["created_at"]} for r in rows]})


async def api_asset_delete(request: web.Request) -> web.Response:
    _, user = await current_project(request)
    body = await request.json()
    await db.execute("DELETE FROM assets WHERE owner_id = $1 AND token = $2",
                     int(user["id"]), str(body.get("token", ""))[:64])
    return web.json_response({"ok": True})


async def page_image(request: web.Request) -> web.Response:
    """Отдаёт картинку из галереи.

    Подписи тут нет и быть не может: за картинкой приходит сам Telegram,
    а не человек. Вместо подписи — длинная случайная метка в адресе.
    """
    token = request.match_info["token"].split(".")[0][:64]
    row = await db.fetchrow("SELECT mime, bytes FROM assets WHERE token = $1", token)
    if not row:
        raise web.HTTPNotFound(text="нет такой картинки")
    return web.Response(
        body=bytes(row["bytes"]), content_type=row["mime"] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=604800"},
    )


# --------------------------------------------------------------------------
# 8б. Переменные, теги, люди, рассылка
# --------------------------------------------------------------------------


async def api_vars(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    return web.json_response(
        {"vars": [short_var(v) for v in await list_vars(project["id"])]})


async def api_var_save(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    body = await request.json()
    name = _name(body.get("name"))
    if not name:
        raise web.HTTPBadRequest(text="Не указано название переменной")

    was = _name(body.get("was")) or name
    scope = body.get("scope") if body.get("scope") in VAR_SCOPES else "user"
    vtype = body.get("vtype") if body.get("vtype") in VAR_TYPES else "text"
    descr = _text(body.get("descr"), 300)
    value = _text(body.get("value"), 500)
    if vtype == "number":
        value = show_number(as_number(value)) if value.strip() else ""
    archived = 1 if body.get("archived") else 0

    old = await db.fetchrow(
        "SELECT * FROM variables WHERE project_id = $1 AND name = $2",
        project["id"], was)
    twin = await db.fetchrow(
        "SELECT * FROM variables WHERE project_id = $1 AND name = $2",
        project["id"], name)
    if twin and (not old or twin["id"] != old["id"]):
        raise web.HTTPBadRequest(text="Переменная с таким названием уже есть")

    if old:
        await db.execute(
            "UPDATE variables SET name = $1, scope = $2, vtype = $3, descr = $4,"
            " value = $5, archived = $6 WHERE id = $7",
            name, scope, vtype, descr, value, archived, old["id"],
        )
    else:
        count = await db.fetchrow(
            "SELECT COUNT(*) AS n FROM variables WHERE project_id = $1", project["id"])
        if (count or {}).get("n", 0) >= MAX_VARS:
            raise web.HTTPBadRequest(text=f"Больше {MAX_VARS} переменных не бывает")
        await db.execute(
            "INSERT INTO variables (project_id, name, scope, vtype, descr, value,"
            " archived, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            project["id"], name, scope, vtype, descr, value, archived, time.time(),
        )
    return web.json_response(
        {"ok": True, "vars": [short_var(v) for v in await list_vars(project["id"])]})


async def api_var_delete(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    body = await request.json()
    await db.execute("DELETE FROM variables WHERE project_id = $1 AND name = $2",
                     project["id"], _name(body.get("name")))
    return web.json_response(
        {"ok": True, "vars": [short_var(v) for v in await list_vars(project["id"])]})


async def api_tags(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    return web.json_response({"tags": [t["name"] for t in await list_tags(project["id"])]})


async def api_tag_save(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    body = await request.json()
    name = _name(body.get("name"))
    if not name:
        raise web.HTTPBadRequest(text="Не указано название тега")
    was = _name(body.get("was"))

    if was and was != name:
        await db.execute("UPDATE tags SET name = $1 WHERE project_id = $2 AND name = $3",
                         name, project["id"], was)
        await rename_tag_everywhere(project["id"], was, name)
    else:
        await ensure_tag(project["id"], name)
    return web.json_response(
        {"ok": True, "tags": [t["name"] for t in await list_tags(project["id"])]})


async def api_tag_delete(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    body = await request.json()
    name = _name(body.get("name"))
    await db.execute("DELETE FROM tags WHERE project_id = $1 AND name = $2",
                     project["id"], name)
    await rename_tag_everywhere(project["id"], name, "")
    return web.json_response(
        {"ok": True, "tags": [t["name"] for t in await list_tags(project["id"])]})


async def rename_tag_everywhere(project_id: int, was: str, now: str) -> None:
    """Переименовали или убрали тег — правим его и у людей."""
    if not was:
        return
    rows = await db.fetch(
        "SELECT chat_id, vars FROM sessions WHERE project_id = $1", project_id)
    for row in rows:
        try:
            variables = json.loads(row["vars"])
        except (TypeError, ValueError):
            continue
        tags = variables.get(TAGS_KEY)
        if not isinstance(tags, list) or was not in tags:
            continue
        tags = [now if t == was else t for t in tags if now or t != was]
        variables[TAGS_KEY] = list(dict.fromkeys(t for t in tags if t))
        await db.execute(
            "UPDATE sessions SET vars = $1 WHERE project_id = $2 AND chat_id = $3",
            json.dumps(variables, ensure_ascii=False), project_id, row["chat_id"],
        )


async def api_users(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    rows = await db.fetch(
        "SELECT chat_id, vars, subscribed, updated_at FROM sessions"
        " WHERE project_id = $1 ORDER BY updated_at DESC LIMIT 300", project["id"])
    people = []
    for row in rows:
        try:
            variables = json.loads(row["vars"])
        except (TypeError, ValueError):
            variables = {}
        if not isinstance(variables, dict):
            variables = {}
        tags = variables.get(TAGS_KEY)
        people.append({
            "chat_id": row["chat_id"],
            "name": str(variables.get("name", "")) or "без имени",
            "username": str(variables.get("username", "")),
            "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
            "subscribed": bool(row["subscribed"]),
            "last": row["updated_at"],
        })
    return web.json_response({"people": people})


async def api_user_tags(request: web.Request) -> web.Response:
    project, _ = await current_project(request)
    body = await request.json()
    chat_id = int(body.get("chat_id") or 0)
    wanted = [_name(t) for t in (body.get("tags") or [])][:20]
    wanted = list(dict.fromkeys(t for t in wanted if t))

    row = await db.fetchrow(
        "SELECT vars FROM sessions WHERE project_id = $1 AND chat_id = $2",
        project["id"], chat_id)
    if not row:
        raise web.HTTPBadRequest(text="Такого человека нет")
    try:
        variables = json.loads(row["vars"])
    except (TypeError, ValueError):
        variables = {}
    variables[TAGS_KEY] = wanted
    await db.execute(
        "UPDATE sessions SET vars = $1 WHERE project_id = $2 AND chat_id = $3",
        json.dumps(variables, ensure_ascii=False), project["id"], chat_id)
    for tag in wanted:
        await ensure_tag(project["id"], tag)
    return web.json_response({"ok": True})


async def api_broadcast(request: web.Request) -> web.Response:
    """Рассылка: одно сообщение всем, кто от неё не отписался."""
    project, _ = await current_project(request)
    if not project["bot_token"]:
        raise web.HTTPBadRequest(text="Сначала подключите бота")
    body = await request.json()
    text = _text(body.get("text"), 3000).strip()
    if not text:
        raise web.HTTPBadRequest(text="Напишите текст рассылки")
    only_tag = _name(body.get("tag"))

    rows = await db.fetch(
        "SELECT chat_id, vars FROM sessions WHERE project_id = $1 AND subscribed = 1",
        project["id"])
    targets = []
    for row in rows:
        try:
            variables = json.loads(row["vars"])
        except (TypeError, ValueError):
            variables = {}
        if not isinstance(variables, dict):
            variables = {}
        tags = variables.get(TAGS_KEY) or []
        if only_tag and (not isinstance(tags, list) or only_tag not in tags):
            continue
        targets.append((row["chat_id"], variables))

    asyncio.create_task(deliver(project, text, targets))
    return web.json_response({"ok": True, "people": len(targets)})


async def deliver(project: dict, text: str, targets: List[tuple]) -> None:
    """Шлём не спеша: Telegram не любит больше тридцати сообщений в секунду."""
    for chat_id, variables in targets:
        try:
            await tg_text(project["bot_token"], "sendMessage", chat_id=chat_id,
                          text=fill(text, variables)[:4096])
        except Exception:
            log.exception("Рассылка: не ушло сообщение в чат %s", chat_id)
        await asyncio.sleep(0.05)
    log.info("Рассылка проекта %s: %s человек", project["id"], len(targets))


async def take_picture(chat_id: int, message: dict) -> bool:
    """Присланную боту картинку кладём в галерею этого человека.

    Так картинки для сообщений не приходится нигде выкладывать: скинул боту
    в переписке — и она сразу видна в конструкторе.
    """
    file_id, mime = "", "image/jpeg"
    sizes = message.get("photo") or []
    document = message.get("document") or {}
    if sizes:
        file_id = (sizes[-1] or {}).get("file_id") or ""
    elif str(document.get("mime_type") or "").startswith("image/"):
        file_id = document.get("file_id") or ""
        mime = document["mime_type"]
    if not file_id:
        return False

    blob = await tg_download(BOT_TOKEN, file_id)
    if not blob:
        await tg(BOT_TOKEN, "sendMessage", chat_id=chat_id,
                 text="Не смог забрать картинку. Она слишком большая — до 5 МБ.")
        return True

    await save_asset(chat_id, blob, mime)
    row = await db.fetchrow(
        "SELECT COUNT(*) AS n FROM assets WHERE owner_id = $1", chat_id)
    await tg(
        BOT_TOKEN, "sendMessage", chat_id=chat_id,
        text=(f"Картинка сохранена. Всего в галерее: {(row or {}).get('n', 1)}.\n"
              "Она уже доступна в блоке «Сообщение» — кнопка «Выбрать картинку»."),
    )
    return True


async def hook_main(request: web.Request) -> web.Response:
    """Апдейты самого бота-конструктора: приветствие и приём картинок."""
    if not same_secret(request.match_info["secret"], MAIN_SECRET):
        raise web.HTTPForbidden(text="forbidden")
    update = await request.json()
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip().lower()

    if chat_id and await take_picture(chat_id, message):
        return web.json_response({"ok": True})

    if chat_id and text.startswith(("/start", "/help")):
        await tg(
            BOT_TOKEN, "sendMessage", chat_id=chat_id,
            text=("Это конструктор ботов.\n\n"
                  "Нажмите кнопку ниже — откроется полотно. Соберите схему из "
                  "блоков, вставьте токен своего бота от @BotFather, и он "
                  "заработает."),
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
        log.exception("Ошибка в схеме проекта %s", project_id)
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
# 9. Запуск
# --------------------------------------------------------------------------


async def keep_awake() -> None:
    """Раз в 10 минут дёргает сам себя.

    На бесплатном Render сервис засыпает без запросов, а спящий сервис не
    примет апдейт от Telegram и не отработает таймеры.
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
    app["timers"] = asyncio.create_task(run_timers())


async def on_cleanup(app: web.Application) -> None:
    for key in ("awake", "timers"):
        task = app.get(key)
        if task:
            task.cancel()
    if _http and not _http.closed:
        await _http.close()
    await db.close()


def build_app() -> web.Application:
    app = web.Application(middlewares=[errors_as_json], client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", page_index)
    app.router.add_get("/health", page_health)
    app.router.add_get("/img/{token}", page_image)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/scenario", api_save)
    app.router.add_post("/api/bot/connect", api_connect)
    app.router.add_post("/api/bot/disconnect", api_disconnect)
    app.router.add_get("/api/assets", api_assets)
    app.router.add_post("/api/assets/delete", api_asset_delete)
    app.router.add_get("/api/vars", api_vars)
    app.router.add_post("/api/vars/save", api_var_save)
    app.router.add_post("/api/vars/delete", api_var_delete)
    app.router.add_get("/api/tags", api_tags)
    app.router.add_post("/api/tags/save", api_tag_save)
    app.router.add_post("/api/tags/delete", api_tag_delete)
    app.router.add_get("/api/users", api_users)
    app.router.add_post("/api/users/tags", api_user_tags)
    app.router.add_post("/api/broadcast", api_broadcast)
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
# 10. Страница редактора.
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
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>Конструктор ботов</title>
<style>
:root{
  --ink:#1b2733; --soft:#7c8b9a; --line:#e3e8ef; --sheet:#ffffff;
  --accent:#3390ec; --accent-ink:#ffffff; --danger:#e2483d; --ok:#1f9254;
  --canvas:#f1f4f8; --grid:#e2e8f0;
}
*{box-sizing:border-box}
[hidden]{display:none !important}
html,body{margin:0;height:100%;overflow:hidden}
body{background:var(--canvas);color:var(--ink);
     font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
     -webkit-text-size-adjust:100%;-webkit-tap-highlight-color:transparent}
button,input,select,textarea{font:inherit;color:var(--ink)}
button{cursor:pointer}

.boot{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
      padding:32px;text-align:center;color:var(--soft);white-space:pre-line;
      background:var(--canvas)}
.boot.err{color:var(--danger)}

/* ---------- полотно ---------- */
.stage{position:fixed;inset:0}
.canvas{position:absolute;inset:0;overflow:hidden;touch-action:none;
        -webkit-user-select:none;user-select:none;
        background-color:var(--canvas);
        background-image:linear-gradient(var(--grid) 1px,transparent 1px),
                         linear-gradient(90deg,var(--grid) 1px,transparent 1px)}
.world{position:absolute;left:0;top:0;transform-origin:0 0}
.wires{position:absolute;left:0;top:0;width:1px;height:1px;overflow:visible;
       pointer-events:none}
.wire{fill:none;stroke:#9db4c9;stroke-width:2}
.wire.yes{stroke:#2fbf87}
.wire.no{stroke:#e2483d}
/* Конец линии — такой же закрашенный кружок, как и её начало. */
.wire-end{fill:#9db4c9}

/* ---------- блок ---------- */
.node{position:absolute;width:210px;border-radius:12px;background:#fff;color:var(--ink);
      box-shadow:0 2px 10px rgba(21,40,60,.13);cursor:grab;overflow:visible}
.node.sel{box-shadow:0 0 0 2px #2fbf87,0 4px 14px rgba(21,40,60,.18)}
.node.dragging{cursor:grabbing}
.node-head{display:flex;align-items:center;gap:6px;padding:7px 10px;
           border-radius:12px 12px 0 0;font-weight:700;font-size:13px}
.node-head .glyph{font-size:13px;opacity:.8}
.node-head .caption{flex:1;min-width:0;text-align:center;overflow:hidden;
                    text-overflow:ellipsis;white-space:nowrap}
.h-gray{background:#eceff3;color:#243447}
.h-teal{background:#d6f3ec;color:#0c6a59}
.h-blue{background:#dbedfb;color:#12608e}
.h-amber{background:#fdeacd;color:#9a5600}
.h-indigo{background:#e0e4fd;color:#333f9b}
.h-purple{background:#f0e4fd;color:#6a2ea0}
.h-pink{background:#fddce8;color:#ae2a5a}
.h-yellow{background:#fbf3c4;color:#77600a}
.node.note{background:#fdf8d4}
.node.note .node-head{background:#fbf3c4;color:#77600a}
.node-body{padding:9px 10px 4px}
.node-line{font-size:12px;color:var(--ink);word-break:break-word}
.node-line.dim{color:var(--soft)}
.node-cap{font-size:10px;letter-spacing:.03em;text-transform:uppercase;color:var(--soft);
          margin-bottom:2px}
.node-fill{margin-top:6px;padding:7px 9px;border-radius:8px;background:#f2f5f8;
           font-size:12px;color:var(--soft);text-align:center}
.node-fill.filled{color:var(--ink);text-align:left}
/* Обрезка длинной надписи висит на самом тексте, а не на полоске: полоска
   не должна ничего прятать, из неё наружу торчит кружок. */
.port{position:relative;margin:6px 10px 0;padding:6px 18px 6px 9px;border-radius:8px;
      background:#eef2f6;font-size:12px}
.port>span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.port.tail{background:transparent;text-align:right;color:var(--soft);padding-right:18px;
           margin-bottom:8px}
.dot{position:absolute;right:-7px;top:50%;width:13px;height:13px;border-radius:50%;
     transform:translateY(-50%);background:#fff;border:2px solid #9db4c9;cursor:pointer}
.dot.yes{border-color:#2fbf87}
.dot.no{border-color:#e2483d}
/* Из кружка уже тянется линия — закрашиваем, чтобы сразу видеть занятые
   выходы. Цвет ободка остаётся, иначе «да» и «нет» станут неразличимы. */
.dot.wired{background:#9db4c9}
.dot.on{background:#e2483d;border-color:#e2483d;transform:translateY(-50%) scale(1.45)}
.dot.url{border-color:#c7d2dc;background:#eef2f6;cursor:default}
.badge{position:absolute;left:50%;top:-11px;transform:translateX(-50%);padding:2px 10px;
       border-radius:11px;background:#1fa463;color:#fff;font-size:11px;font-weight:600;
       white-space:nowrap}
.side{position:absolute;left:100%;top:0;margin-left:8px;display:flex;flex-direction:column;
      gap:6px}
.side button{width:30px;height:30px;border:none;border-radius:9px;background:#fff;
             color:var(--soft);font-size:14px;box-shadow:0 2px 8px rgba(21,40,60,.16)}
.side button.kill{color:var(--danger)}

/* ---------- верх и низ ---------- */
.topbar{position:absolute;left:10px;top:10px;right:10px;display:flex;gap:8px;z-index:4;
        pointer-events:none}
.topbar>*{pointer-events:auto}
.chip{display:flex;align-items:center;gap:8px;max-width:60%;padding:7px 12px;border:none;
      border-radius:12px;background:#fff;box-shadow:0 2px 10px rgba(21,40,60,.14)}
.chip .who{min-width:0;text-align:left}
.chip .who b{display:block;font-size:13px;overflow:hidden;text-overflow:ellipsis;
             white-space:nowrap}
.chip .who span{display:block;font-size:11px;color:var(--soft)}
.round{flex:none;width:38px;height:38px;border:none;border-radius:12px;background:#fff;
       box-shadow:0 2px 10px rgba(21,40,60,.14);font-size:16px}
.grow{flex:1}
.save{border:none;border-radius:12px;padding:0 14px;height:38px;font-weight:600;
      background:var(--accent);color:var(--accent-ink);box-shadow:0 2px 10px rgba(21,40,60,.14)}
.save.clean{background:#fff;color:var(--ok)}
.save.bad{background:var(--danger);color:#fff}

.corner{position:absolute;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));
        display:flex;flex-direction:column;gap:8px;z-index:4}
.corner button{width:40px;height:40px;border:none;border-radius:12px;background:#fff;
               font-size:17px;box-shadow:0 2px 10px rgba(21,40,60,.14)}
.plus{position:absolute;left:12px;bottom:calc(12px + env(safe-area-inset-bottom));
      width:52px;height:52px;border:none;border-radius:17px;background:var(--accent);
      color:#fff;font-size:26px;line-height:1;z-index:4;
      box-shadow:0 4px 14px rgba(51,144,236,.45)}
.tip{position:absolute;left:50%;top:64px;transform:translateX(-50%);z-index:4;
     padding:7px 13px;border-radius:11px;background:#243447;color:#fff;font-size:12px;
     pointer-events:none;max-width:82%;text-align:center}

/* ---------- панель ---------- */
.panel{position:absolute;left:0;top:0;bottom:0;width:330px;z-index:6;overflow-y:auto;
       background:var(--sheet);box-shadow:2px 0 16px rgba(21,40,60,.16);
       padding:0 0 20px}
.panel-head{position:sticky;top:0;display:flex;align-items:center;gap:8px;padding:12px 14px;
            background:var(--sheet);border-bottom:1px solid var(--line);z-index:1}
.panel-head b{flex:1;font-size:15px}
.panel-head button{border:none;background:transparent;color:var(--soft);font-size:18px}
.panel-body{padding:12px 14px}
.cap{display:block;margin:14px 0 5px;font-size:11px;letter-spacing:.04em;
     text-transform:uppercase;color:var(--soft)}
.cap:first-child{margin-top:0}
.inp{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:10px;
     background:#f7f9fb}
.inp:focus{outline:none;border-color:var(--accent);background:#fff}
textarea.inp{min-height:78px;resize:vertical}
.row{display:flex;gap:6px;align-items:center}
.row>.inp{min-width:0}
.row>.combo{flex:1;min-width:0}
.mini{flex:none;width:34px;height:34px;border:none;border-radius:9px;background:#f0f3f7;
      color:var(--soft);font-size:14px}
.mini.kill{color:var(--danger)}
.add{width:100%;margin-top:8px;padding:10px;border:1px dashed var(--line);border-radius:10px;
     background:transparent;color:var(--accent);font-weight:600}
.group{margin-top:8px;padding:9px;border:1px solid var(--line);border-radius:11px}
.note{margin-top:6px;font-size:11px;color:var(--soft)}
.wide{width:100%;margin-top:10px;padding:11px;border:none;border-radius:11px;
      background:var(--accent);color:#fff;font-weight:600}
.wide.ghost{background:#f0f3f7;color:var(--ink)}
.wide.kill{background:#fdecea;color:var(--danger)}
.check{display:flex;align-items:center;gap:9px;margin-top:12px;font-size:13px}
.check input{width:18px;height:18px}
.slider{width:100%}

/* ---------- всплывашки ---------- */
.shade{position:absolute;inset:0;z-index:5;background:rgba(20,32,45,.35)}
.sheet{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:7;
       width:min(340px,92vw);max-height:80vh;overflow-y:auto;padding:14px;
       border-radius:16px;background:var(--sheet);box-shadow:0 10px 40px rgba(21,40,60,.3)}
.sheet h3{margin:0 0 10px;font-size:15px}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.tile{padding:12px 6px;border:1px solid var(--line);border-radius:12px;background:#fff;
      text-align:center;font-size:11px;line-height:1.25}
.tile .glyph{display:block;font-size:19px;margin-bottom:5px}
.menu{position:absolute;right:10px;top:56px;z-index:7;width:236px;padding:6px;
      border-radius:14px;background:var(--sheet);box-shadow:0 10px 30px rgba(21,40,60,.25)}
.menu button{display:block;width:100%;padding:11px 12px;border:none;border-radius:10px;
             background:transparent;text-align:left;font-size:14px}
.menu button.kill{color:var(--danger)}
.menu hr{margin:5px 8px;border:none;border-top:1px solid var(--line)}
.sheet.big{width:min(620px,94vw);max-height:86vh}

/* ---------- галерея картинок ---------- */
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;
         margin-top:10px}
.shot{position:relative;padding:0;border:1px solid var(--line);border-radius:11px;
      background:#f2f5f8;overflow:hidden;aspect-ratio:1/1}
.shot img{width:100%;height:100%;object-fit:cover;display:block}
.shot.on{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.shot .drop{position:absolute;right:3px;top:3px;width:22px;height:22px;border:none;
            border-radius:50%;background:rgba(20,32,45,.6);color:#fff;font-size:11px;
            line-height:1}
.preview{margin-top:8px;border:1px solid var(--line);border-radius:11px;overflow:hidden;
         background:#f2f5f8}
.preview img{display:block;width:100%;max-height:190px;object-fit:contain}

/* ---------- выпадающий выбор с поиском ---------- */
.combo{position:relative}
.combo-list{position:absolute;left:0;right:0;top:100%;z-index:9;max-height:200px;
            overflow-y:auto;margin-top:3px;border-radius:11px;background:var(--sheet);
            box-shadow:0 8px 26px rgba(21,40,60,.22)}
.combo-list button{display:flex;align-items:center;gap:7px;width:100%;padding:9px 11px;
                   border:none;background:transparent;text-align:left;font-size:13px}
.combo-list button:hover{background:#f2f5f8}
.combo-list .kind{margin-left:auto;font-size:11px;color:var(--soft)}
.combo-list .empty{padding:10px 11px;font-size:12px;color:var(--soft)}
.pin{flex:none;width:9px;height:9px;border-radius:50%;background:#3390ec}
.pin.project{background:#8b5cf6}
.pin.tag{background:#9aa8b6}

/* ---------- списки на страницах ---------- */
.tabs{display:flex;gap:6px;margin:10px 0 4px}
.tabs button{padding:7px 12px;border:none;border-radius:10px;background:#f0f3f7;
             font-size:13px}
.tabs button.on{background:var(--accent);color:#fff;font-weight:600}
.rows{margin-top:6px}
.rw{display:flex;align-items:center;gap:8px;padding:9px 4px;border-bottom:1px solid var(--line)}
.rw .grow{min-width:0}
.rw b{display:block;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rw small{display:block;font-size:11px;color:var(--soft)}
.rw .mini{width:30px;height:30px}
.pill{display:inline-block;padding:2px 9px;border-radius:10px;background:#e8f0fb;
      color:#12608e;font-size:11px;margin:2px 4px 2px 0}
.pill.project{background:#efe6fd;color:#6a2ea0}
.pill.tag{background:#eceff3;color:#41556b}
.empty{padding:18px 4px;text-align:center;font-size:13px;color:var(--soft)}

@media (max-width:700px){
  /* Кнопок наверху много, а места мало — ужимаем их и подпись бота. */
  .chip{max-width:34%;padding:7px 9px}
  .round{width:34px;height:34px;font-size:14px}
  .save{padding:0 10px;height:34px}
  .panel{left:0;right:0;top:auto;bottom:0;width:auto;max-height:66vh;
         border-radius:16px 16px 0 0;box-shadow:0 -3px 18px rgba(21,40,60,.22);
         padding-bottom:calc(16px + env(safe-area-inset-bottom))}
  /* Панель занимает низ экрана — кнопки из-под неё убираем, иначе они
     оказываются под ней и по ним не попасть. */
  .stage.opened .plus,.stage.opened .corner{display:none}
}
@media (min-width:701px){
  .stage.opened .topbar{left:344px}
}
</style>
</head>
<body>
<div class="boot" id="boot">Загружаю…</div>

<div class="stage" id="stage" hidden>
  <div class="canvas" id="canvas"><div class="world" id="world"></div></div>

  <div class="topbar">
    <button class="chip" id="chip"><span>🤖</span><span class="who" id="chipWho"></span></button>
    <span class="grow"></span>
    <button class="round" id="peopleBtn" title="Люди">👥</button>
    <button class="round" id="tagsBtn" title="Теги">🏷</button>
    <button class="round" id="varsBtn" title="Переменные">{ }</button>
    <button class="save" id="save">Сохранить</button>
    <button class="round" id="menuBtn">☰</button>
  </div>

  <button class="plus" id="plus">+</button>
  <div class="corner">
    <button id="zoomIn">+</button>
    <button id="zoomOut">−</button>
    <button id="zoomFit">⤢</button>
  </div>

  <div class="tip" id="tip" hidden></div>
  <div class="panel" id="panel" hidden></div>
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

function pick(options, value, onchange) {
  var node = el("select", {class: "inp", onchange: onchange});
  options.forEach(function (pair) { node.append(el("option", {value: pair[0]}, pair[1])); });
  node.value = value || "";
  return node;
}

/* Поле с подсказкой: пока печатаешь, снизу висит список подходящего.
   Можно выбрать готовое, а можно написать своё — тогда оно заведётся само. */
function combo(value, placeholder, items, onpick) {
  var box = el("div", {class: "combo"});
  var list = null;
  var field = el("input", {class: "inp", value: value || "",
                           placeholder: placeholder || ""});

  function close() {
    if (list) { list.remove(); list = null; }
  }

  function open() {
    close();
    var wanted = field.value.trim().toLowerCase();
    var shown = items().filter(function (item) {
      return !wanted || item.name.toLowerCase().indexOf(wanted) >= 0;
    }).slice(0, 40);
    list = el("div", {class: "combo-list"});
    if (!shown.length) {
      list.append(el("div", {class: "empty"}, "Пока такого нет — напишите своё."));
    }
    shown.forEach(function (item) {
      list.append(el("button", {onpointerdown: function (e) {
        e.preventDefault();               /* иначе поле потеряет фокус раньше */
        field.value = item.name;
        close();
        onpick(item.name);
      }},
        el("span", {class: "pin " + (item.tone || "")}),
        el("span", {}, item.name),
        item.kind ? el("span", {class: "kind"}, item.kind) : null));
    });
    box.append(list);
  }

  field.addEventListener("focus", open);
  field.addEventListener("input", function () {
    field.value = cleanChars(field.value);
    open();
    onpick(field.value);
  });
  field.addEventListener("blur", function () {
    field.value = cleanName(field.value);       /* набрали — привели в порядок */
    onpick(field.value);
    setTimeout(close, 150);
  });
  box.append(field);
  return box;
}

/* Что показывать в подсказках. */
function varItems() {
  return VARS.filter(function (v) { return !v.archived; }).map(function (v) {
    return {name: v.name, kind: v.vtype === "number" ? "число" : "текст",
            tone: v.scope === "project" ? "project" : ""};
  });
}
function tagItems() {
  return TAGS.map(function (name) { return {name: name, kind: "", tone: "tag"}; });
}

/* Кнопка «{ }» рядом с текстом: вставляет переменную прямо туда, где курсор. */
function insertVarButton(field, onchange) {
  return el("button", {class: "mini", title: "Вставить переменную",
                       onclick: function () {
    chooseFrom("Вставить переменную", varItems, function (name) {
      var text = field.value || "";
      var from = field.selectionStart, to = field.selectionEnd;
      if (typeof from !== "number") { from = to = text.length; }
      field.value = text.slice(0, from) + "{" + name + "}" + text.slice(to);
      onchange(field.value);
      try { field.focus(); } catch (e) {}
    });
  }}, "{ }");
}

/* Кнопка, которую надо нажать дважды: системные диалоги в мини-аппах
   открываются не везде, поэтому подтверждаем так. */
function armed(button, warning, action) {
  var original = button.textContent, timer = null, ready = false;
  button.addEventListener("click", function (e) {
    e.stopPropagation();
    if (ready) { clearTimeout(timer); action(); return; }
    ready = true;
    button.textContent = warning;
    timer = setTimeout(function () {
      ready = false;
      button.textContent = original;
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

/* ---- какие бывают блоки ---- */
var META = {
  message:   {title: "Сообщение",      glyph: "✉", tone: "gray"},
  input:     {title: "Ввод",           glyph: "✍", tone: "teal"},
  keywords:  {title: "Ключевые слова", glyph: "✎", tone: "blue"},
  event:     {title: "События",        glyph: "◎", tone: "blue"},
  action:    {title: "Действие",       glyph: "⚡", tone: "amber"},
  condition: {title: "Условие",        glyph: "⑂", tone: "indigo"},
  random:    {title: "Рандом",         glyph: "⤨", tone: "purple"},
  timer:     {title: "Таймер",         glyph: "◷", tone: "pink"},
  note:      {title: "Заметка",        glyph: "▤", tone: "yellow"},
};
var ORDER = ["message", "input", "keywords", "event", "action", "condition",
             "random", "timer", "note"];

var EVENT_NAMES = [
  ["first", "впервые написал боту"],
  ["unknown", "ввёл несуществующую команду"],
  ["blocked", "заблокировал бота"],
  ["unblocked", "разблокировал бота"],
  ["photo", "отправил фото"],
  ["video", "отправил видео"],
  ["file", "отправил файл"],
  ["location", "отправил местоположение"],
  ["voice", "отправил голосовое"],
  ["any", "написал что угодно"],
];
var ACTION_NAMES = [
  ["set_var", "Изменить переменную"],
  ["add_tag", "Добавить тег"],
  ["del_tag", "Удалить тег"],
  ["notify", "Отправить уведомление"],
  ["subscribe", "Подписать на рассылку"],
  ["unsubscribe", "Отписать от рассылки"],
];
var ACTION_GLYPHS = {set_var: "✎", add_tag: "🏷", del_tag: "🚫",
                     notify: "🔔", subscribe: "👤", unsubscribe: "👤"};
var OP_NAMES = [
  ["eq", "равна"], ["ne", "не равна"], ["has", "содержит"],
  ["empty", "пустая"], ["tag", "есть тег"],
  ["gt", "больше"], ["lt", "меньше"],
  ["gte", "больше или равна"], ["lte", "меньше или равна"],
];
/* Знак у действия «Изменить переменную». У текстовой переменной есть только
   «=»: складывать и умножать буквы не получится. */
var SET_OPS = [["set", "="], ["add", "+"], ["sub", "−"], ["mul", "×"], ["div", "÷"]];
var UNIT_NAMES = [["minute", "минут"], ["hour", "часов"], ["day", "дней"]];
var TYPE_NAMES = [["text", "Текст"], ["number", "Число"]];
var SCOPE_NAMES = [["user", "Пользовательская"], ["project", "Проектная"]];

function labelOf(list, value) {
  for (var i = 0; i < list.length; i++) if (list[i][0] === value) return list[i][1];
  return value || "";
}

/* ---- состояние ---- */
var S = {start: "", steps: []};
var VARS = [];            /* общий список переменных проекта */
var TAGS = [];            /* общий список тегов */
var SHOTS = null;         /* галерея картинок, подгружается по требованию */
var BOT = {connected: false, bot_username: "", people: 0};

function varByName(name) {
  for (var i = 0; i < VARS.length; i++) if (VARS[i].name === name) return VARS[i];
  return null;
}
function isNumberVar(name) {
  var found = varByName(name);
  return !!found && found.vtype === "number";
}
/* Пока человек печатает, убираем только запрещённые знаки: если резать
   пробелы сразу, название из двух слов набрать не получится. */
function cleanChars(text) {
  return String(text || "").replace(/[^0-9A-Za-zА-Яа-яЁё_ ]/g, "").slice(0, 40);
}
/* А это — окончательный вид названия, к нему приводим, когда набор закончен. */
function cleanName(text) {
  return cleanChars(text).replace(/\s+/g, " ").trim();
}
var SEL = "";             /* какой блок выбран */
var LINKING = null;       /* {id, out} — от какого кружка тянем стрелку */
var PAN = {x: 40, y: 90, z: 1, ready: false};
var NODES = {};
var DIRTY = false;
var NODE_W = 210;
var SVGNS = "http://www.w3.org/2000/svg";

function byId(id) {
  for (var i = 0; i < S.steps.length; i++) if (S.steps[i].id === id) return S.steps[i];
  return null;
}
function titleOf(step) {
  return (step.name || "").trim() || META[step.type].title;
}
function nextId() {
  var used = {};
  S.steps.forEach(function (s) { used[s.id] = true; });
  var n = 1;
  while (used["s" + n]) n++;
  return "s" + n;
}
function touch() {
  DIRTY = true;
  paintSave();
}

/* Выходы блока — они же кружки на схеме. */
function outputsOf(step) {
  var out = [];
  if (step.type === "note") return out;
  if (step.type === "random") {
    (step.options || []).forEach(function (option, index) {
      out.push({label: (option.label || "?") + "  " + (option.weight || 0) + "%",
                kind: "option", index: index, to: option.next || "", tone: ""});
    });
    return out;
  }
  if (step.type === "condition") {
    out.push({label: "Да", kind: "next", to: step.next || "", tone: "yes"});
    out.push({label: "Нет", kind: "otherwise", to: step.otherwise || "", tone: "no"});
    return out;
  }
  if (step.type === "message") {
    (step.buttons || []).forEach(function (button, index) {
      var link = button.action === "url";
      out.push({label: button.text || "кнопка", kind: link ? "url" : "button",
                index: index, to: link ? "" : (button.value || ""), tone: ""});
    });
  }
  out.push({label: "Следующий шаг", kind: "next", to: step.next || "",
            tone: "", tail: true});
  return out;
}

function setLink(step, outIndex, targetId) {
  var output = outputsOf(step)[outIndex];
  if (!output || output.kind === "url") return;
  if (output.kind === "next") step.next = targetId;
  else if (output.kind === "otherwise") step.otherwise = targetId;
  else if (output.kind === "option") step.options[output.index].next = targetId;
  else step.buttons[output.index].value = targetId;
}

function blankStep(type) {
  var step = {id: nextId(), type: type, name: "", next: "", x: 0, y: 0};
  if (type === "message") {
    step.text = ""; step.photo = ""; step.file = ""; step.inline = false; step.buttons = [];
  }
  if (type === "input") { step.text = ""; step.save_to = ""; step.expect = "any"; step.retry = ""; }
  if (type === "keywords") { step.match = "contains"; step.words = ""; }
  if (type === "event") { step.event = "first"; }
  if (type === "action") { step.actions = []; }
  if (type === "condition") { step.checks = []; step.otherwise = ""; }
  if (type === "random") {
    step.always = false;
    step.options = [{label: "A", weight: 50, next: ""},
                    {label: "B", weight: 50, next: ""}];
    delete step.next;
  }
  if (type === "timer") { step.amount = 1; step.unit = "day"; }
  if (type === "note") { step.text = ""; delete step.next; }
  return step;
}

/* ======================= полотно ======================= */

function ensurePositions() {
  var missing = S.steps.filter(function (s) { return typeof s.x !== "number"; });
  if (!missing.length) return;
  if (missing.length === S.steps.length) { autoLayout(); return; }
  var bottom = 0;
  S.steps.forEach(function (s) {
    if (typeof s.y === "number") bottom = Math.max(bottom, s.y);
  });
  missing.forEach(function (step, i) { step.x = 40; step.y = bottom + 180 * (i + 1); });
}

function autoLayout() {
  var COL = 300, ROW = 190, depth = {}, seen = {};
  var roots = S.steps.filter(function (s) {
    return s.id === S.start || s.type === "keywords" || s.type === "event";
  });
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
    step.x = 40 + level * COL;
    step.y = 40 + taken[level] * ROW;
    taken[level]++;
  });
}

function applyPan() {
  document.getElementById("world").style.transform =
    "translate(" + PAN.x + "px," + PAN.y + "px) scale(" + PAN.z + ")";
  var canvas = document.getElementById("canvas");
  var cell = 40 * PAN.z;
  canvas.style.backgroundSize = cell + "px " + cell + "px";
  canvas.style.backgroundPosition = PAN.x + "px " + PAN.y + "px";
}

function fitView() {
  var canvas = document.getElementById("canvas");
  if (!S.steps.length || !canvas.clientWidth) return;
  var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  S.steps.forEach(function (s) {
    minX = Math.min(minX, s.x); minY = Math.min(minY, s.y);
    maxX = Math.max(maxX, s.x + NODE_W); maxY = Math.max(maxY, s.y + 190);
  });
  var zoom = Math.min(1, (canvas.clientWidth - 60) / (maxX - minX),
                         (canvas.clientHeight - 140) / (maxY - minY));
  PAN.z = Math.max(0.25, zoom);
  PAN.x = (canvas.clientWidth - (maxX - minX) * PAN.z) / 2 - minX * PAN.z;
  PAN.y = 70 - minY * PAN.z;
  applyPan();
}

function zoomBy(factor) {
  var canvas = document.getElementById("canvas");
  var cx = canvas.clientWidth / 2, cy = canvas.clientHeight / 2;
  var wx = (cx - PAN.x) / PAN.z, wy = (cy - PAN.y) / PAN.z;
  PAN.z = Math.min(2, Math.max(0.25, PAN.z * factor));
  PAN.x = cx - wx * PAN.z;
  PAN.y = cy - wy * PAN.z;
  applyPan();
}

function wirePath(x1, y1, x2, y2, tone) {
  var bend = Math.max(40, Math.abs(x2 - x1) / 2);
  var path = document.createElementNS(SVGNS, "path");
  path.setAttribute("class", "wire " + (tone || ""));
  path.setAttribute("d", "M " + x1 + " " + y1 +
    " C " + (x1 + bend) + " " + y1 + ", " + (x2 - bend) + " " + y2 +
    ", " + x2 + " " + y2);
  return path;
}

/* Куда линия входит в блок: не в середину левого края, а на той же высоте,
   на какой линии из блока выходят — вровень с нижней полоской. Так стрелки
   идут ровно и не перечёркивают блок посередине. */
function inletY(node) {
  var ports = node.querySelectorAll ? node.querySelectorAll(".port") : [];
  var last = ports.length ? ports[ports.length - 1] : null;
  if (last) return last.offsetTop + last.offsetHeight / 2;
  return Math.max(0, node.offsetHeight - 18);      /* у заметки полосок нет */
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
      var y2 = target.y + inletY(to);
      svg.append(wirePath(x1, y1, x2, y2, out.tone));
      var end = document.createElementNS(SVGNS, "circle");
      end.setAttribute("class", "wire-end");
      end.setAttribute("cx", x2);
      end.setAttribute("cy", y2);
      end.setAttribute("r", 5);
      svg.append(end);
    });
  });
}

function shorten(text, limit) {
  text = (text || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, limit) + "…" : text;
}

/* Что видно внутри блока, не открывая настройки. */
function nodeBody(step) {
  var box = el("div", {class: "node-body"});
  var t = step.type;

  if (t === "message" || t === "input" || t === "note") {
    var text = shorten(step.text, 90);
    box.append(el("div", {class: "node-fill" + (text ? " filled" : "")},
      text || (t === "input" ? "Задайте вопрос…" : "Добавьте текст…")));
    if (t === "message" && (step.photo || step.file || step.inline)) {
      box.append(el("div", {class: "node-line dim", style: "margin-top:6px"},
        (step.photo ? "🖼 картинка " : "") + (step.file ? "📎 файл " : "") +
        (step.inline ? "кнопки в сообщении" : "")));
    }
    if (t === "input" && step.save_to) {
      box.append(el("div", {class: "node-line dim", style: "margin-top:6px"},
        (step.expect === "number" ? "только число → {" : "ответ → {") +
        step.save_to + "}"));
    }
  } else if (t === "keywords") {
    box.append(el("div", {class: "node-cap"},
      step.match === "exact" ? "при точном совпадении с" : "если встречается"));
    box.append(el("div", {class: "node-line" + (step.words ? "" : " dim")},
      shorten(step.words, 60) || "Укажите ключевые слова…"));
  } else if (t === "event") {
    box.append(el("div", {class: "node-cap"}, "если пользователь"));
    box.append(el("div", {class: "node-line"}, labelOf(EVENT_NAMES, step.event)));
  } else if (t === "action") {
    var list = step.actions || [];
    if (!list.length) {
      box.append(el("div", {class: "node-fill"}, "Нажмите, чтобы добавить действие"));
    } else {
      list.forEach(function (action) {
        box.append(el("div", {class: "node-cap"}, labelOf(ACTION_NAMES, action.kind)));
        var tail = action.name || "—";
        if (action.kind === "set_var") {
          tail = (action.name || "переменная не указана") + " " +
                 labelOf(SET_OPS, action.op || "set") + " " +
                 (shorten(action.value, 18) || "пусто");
        } else if (action.kind === "notify") {
          tail = shorten(action.value, 28) || "все ответы";
        } else if (action.kind === "subscribe" || action.kind === "unsubscribe") {
          tail = action.kind === "subscribe" ? "подписать" : "отписать";
        }
        box.append(el("div", {class: "node-line"}, tail));
      });
    }
  } else if (t === "condition") {
    var checks = step.checks || [];
    if (!checks.length) {
      box.append(el("div", {class: "node-fill"}, "Задайте условие…"));
    } else {
      checks.forEach(function (check) {
        var left = check.op === "tag" ? "тег" : "{" + (check.var || "?") + "}";
        box.append(el("div", {class: "node-line"},
          left + " " + labelOf(OP_NAMES, check.op) +
          (check.op === "empty" ? "" : " " + shorten(check.value, 20))));
      });
    }
  } else if (t === "timer") {
    box.append(el("div", {class: "node-line", style: "text-align:center"},
      "Ждать " + (step.amount || 1) + " " + labelOf(UNIT_NAMES, step.unit)));
  }
  return box;
}

function nodeEl(step) {
  var meta = META[step.type];
  var node = el("div", {class: "node" + (step.id === SEL ? " sel" : "") +
                               (step.type === "note" ? " note" : ""),
                        "data-id": step.id});
  node.style.left = step.x + "px";
  node.style.top = step.y + "px";

  if (step.id === S.start) node.append(el("div", {class: "badge"}, "Стартовый шаг"));

  node.append(el("div", {class: "node-head h-" + meta.tone},
    el("span", {class: "glyph"}, meta.glyph),
    el("span", {class: "caption"}, titleOf(step))));
  node.append(nodeBody(step));

  outputsOf(step).forEach(function (out, oi) {
    var port = el("div", {class: "port" + (out.tail ? " tail" : ""), "data-out": oi},
      el("span", {}, out.label));
    var lit = LINKING && LINKING.id === step.id && LINKING.out === oi;
    var wired = !!(out.to && byId(out.to));
    var dot = el("span", {class: "dot " + (out.tone || "") +
                                 (out.kind === "url" ? " url" : "") +
                                 (wired ? " wired" : "") + (lit ? " on" : "")});
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

  if (step.id === SEL) {
    var side = el("div", {class: "side"});
    side.append(el("button", {onclick: function (e) { e.stopPropagation(); copyStep(step); }}, "⧉"));
    side.append(armed(el("button", {class: "kill"}, "🗑"), "✓",
                      function () { removeStep(step.id); }));
    side.querySelectorAll("button").forEach(function (b) {
      b.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
    });
    node.append(side);
  }

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
  node.addEventListener("pointerup", function () {
    if (!grab) return;
    var moved = grab.moved;
    grab = null;
    node.classList.remove("dragging");
    if (moved) { touch(); return; }          /* блок просто передвинули */
    if (LINKING) { linkTo(step); return; }   /* ждали, куда вести стрелку */
    select(step.id);
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
  if (SEL === source.id) renderPanel();
}

function select(id) {
  SEL = id;
  LINKING = null;
  renderCanvas();
  renderPanel();
}

function deselect() {
  SEL = "";
  LINKING = null;
  renderCanvas();
  renderPanel();
}

function copyStep(step) {
  var copy = JSON.parse(JSON.stringify(step));
  copy.id = nextId();
  copy.x = (step.x || 0) + 40;
  copy.y = (step.y || 0) + 40;
  S.steps.push(copy);
  touch();
  select(copy.id);
}

function removeStep(id) {
  if (S.steps.length < 2) { flash("Должен остаться хотя бы один блок"); return; }
  S.steps = S.steps.filter(function (s) { return s.id !== id; });
  /* Стрелки, которые вели в удалённый блок, никуда не ведут. */
  S.steps.forEach(function (step) {
    if (step.next === id) step.next = "";
    if (step.otherwise === id) step.otherwise = "";
    (step.buttons || []).forEach(function (b) {
      if (b.action !== "url" && b.value === id) b.value = "";
    });
    (step.options || []).forEach(function (o) { if (o.next === id) o.next = ""; });
  });
  if (S.start === id) {
    var first = S.steps.filter(function (s) { return s.type === "message"; })[0];
    S.start = (first || S.steps[0]).id;
  }
  touch();
  deselect();
}

function renderCanvas() {
  ensurePositions();
  if (SEL && !byId(SEL)) SEL = "";
  var world = document.getElementById("world");
  world.textContent = "";
  NODES = {};

  var svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", "wires");
  svg.setAttribute("id", "wires");
  world.append(svg);

  S.steps.forEach(function (step) {
    var node = nodeEl(step);
    world.append(node);
    NODES[step.id] = node;
  });

  if (!PAN.ready) { PAN.ready = true; fitView(); } else { applyPan(); }
  drawWires();                    /* высоты блоков известны только теперь */

  var tip = document.getElementById("tip");
  tip.hidden = !LINKING;
  if (LINKING) {
    var source = byId(LINKING.id);
    var output = source ? outputsOf(source)[LINKING.out] : null;
    tip.textContent = output && output.to
      ? "Коснитесь блока, куда вести. Тот же блок — убрать стрелку."
      : "Коснитесь блока, куда вести стрелку.";
  }
}

/* Полотно: одним пальцем двигаем, двумя — приближаем.
   Два быстрых касания (или правая кнопка мыши) по пустому месту — палитра. */
function attachCanvas() {
  var canvas = document.getElementById("canvas");
  var points = {}, pan = null, pinch = null, tap = null;

  function worldAt(e) {
    var box = canvas.getBoundingClientRect();
    return {x: (e.clientX - box.left - PAN.x) / PAN.z,
            y: (e.clientY - box.top - PAN.y) / PAN.z};
  }

  function onNode(e) {
    return !!(e.target && e.target.closest && e.target.closest(".node"));
  }

  canvas.addEventListener("contextmenu", function (e) {
    e.preventDefault();
    if (!onNode(e)) openPalette(worldAt(e));
  });

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
      PAN.z = Math.min(2, Math.max(0.25, pinch.z * now / pinch.gap));
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
    if (pan && !pan.moved && !onNode(e)) {
      var now = new Date().getTime();
      var again = tap && now - tap.at < 340 &&
                  Math.abs(e.clientX - tap.x) < 26 && Math.abs(e.clientY - tap.y) < 26;
      if (again) {                    /* два касания подряд — добавляем блок */
        tap = null;
        openPalette(worldAt(e));
      } else {                        /* одиночный тап мимо блоков — снять выбор */
        tap = {at: now, x: e.clientX, y: e.clientY};
        if (LINKING || SEL) deselect();
      }
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
    PAN.z = Math.min(2, Math.max(0.25, PAN.z * (e.deltaY < 0 ? 1.12 : 0.89)));
    PAN.x = mx - wx * PAN.z;
    PAN.y = my - wy * PAN.z;
    applyPan();
  }, {passive: false});
}

/* ======================= панель настроек ======================= */

function cap(text) { return el("span", {class: "cap"}, text); }

function line(value, placeholder, oninput) {
  return el("input", {class: "inp", value: value || "", placeholder: placeholder || "",
                      oninput: oninput});
}
function area(value, placeholder, oninput) {
  return el("textarea", {class: "inp", placeholder: placeholder || "",
                         oninput: oninput}, value || "");
}
function stepPick(value, onchange) {
  var options = [["", "— не выбран —"]];
  S.steps.forEach(function (s) {
    if (s.type !== "note") options.push([s.id, titleOf(s)]);
  });
  return pick(options, value, onchange);
}

/* Мелкая правка текста не должна перерисовывать панель — иначе поле теряет
   курсор прямо во время набора. Обновляем только сам блок на схеме. */
function refresh(step) {
  touch();
  var fresh = nodeEl(step);
  var old = NODES[step.id];
  if (old) { old.replaceWith(fresh); NODES[step.id] = fresh; drawWires(); }
}

function renderPanel() {
  var panel = document.getElementById("panel");
  var stage = document.getElementById("stage");
  var step = SEL ? byId(SEL) : null;
  if (!step) {
    panel.hidden = true;
    stage.classList.remove("opened");
    return;
  }
  panel.hidden = false;
  stage.classList.add("opened");
  panel.textContent = "";

  panel.append(el("div", {class: "panel-head"},
    el("span", {}, META[step.type].glyph),
    el("b", {}, META[step.type].title),
    el("button", {onclick: deselect}, "✕")));

  var body = el("div", {class: "panel-body"});
  panel.append(body);

  body.append(cap("Название блока"));
  body.append(line(step.name, META[step.type].title, function (e) {
    step.name = e.target.value;
    refresh(step);
  }));

  if (step.type === "message") fieldsMessage(body, step);
  if (step.type === "input") fieldsInput(body, step);
  if (step.type === "keywords") fieldsKeywords(body, step);
  if (step.type === "event") fieldsEvent(body, step);
  if (step.type === "action") fieldsAction(body, step);
  if (step.type === "condition") fieldsCondition(body, step);
  if (step.type === "random") fieldsRandom(body, step);
  if (step.type === "timer") fieldsTimer(body, step);
  if (step.type === "note") fieldsNote(body, step);

  if (step.type !== "note" && step.type !== "random" && step.type !== "condition") {
    body.append(cap("Следующий шаг"));
    body.append(stepPick(step.next, function (e) {
      step.next = e.target.value;
      touch();
      renderCanvas();
    }));
  }

  if (step.type !== "note") {
    var start = el("input", {type: "checkbox", onchange: function (e) {
      if (e.target.checked) S.start = step.id;
      else if (S.start === step.id) S.start = "";
      touch();
      renderCanvas();
    }});
    start.checked = S.start === step.id;
    body.append(el("label", {class: "check"}, start,
      el("span", {}, "Стартовый шаг — с него бот начинает по /start")));
  }

  body.append(armed(el("button", {class: "wide kill"}, "Удалить блок"),
                    "Точно удалить?", function () { removeStep(step.id); }));
}

/* Строка «текстовое поле + кнопка вставки переменной». */
function textWithVars(value, placeholder, onchange) {
  var field = area(value, placeholder, function (e) { onchange(e.target.value); });
  var box = el("div", {});
  box.append(field);
  box.append(el("div", {class: "row", style: "margin-top:6px"},
    insertVarButton(field, onchange),
    el("div", {class: "note", style: "margin:0;flex:1"},
      "Кнопка { } вставит переменную. Оформление: <b>жирный</b>, <i>курсив</i>, <code>код</code>.")));
  return box;
}

function shotSrc(value) {
  var text = String(value || "");
  return text.indexOf("asset:") === 0 ? "/img/" + text.slice(6) : text;
}

/* Картинка выбирается из галереи — того, что человек прислал боту в личку.
   Ссылку руками вводить больше не нужно. */
function photoField(step) {
  var box = el("div", {});
  if (step.photo) {
    box.append(el("div", {class: "preview"}, el("img", {src: shotSrc(step.photo)})));
  }
  var row = el("div", {class: "row", style: "margin-top:8px"});
  row.append(el("button", {class: "wide ghost", style: "margin:0", onclick: function () {
    openGallery(step);
  }}, step.photo ? "Выбрать другую" : "Выбрать картинку"));
  if (step.photo) {
    row.append(el("button", {class: "mini kill", onclick: function () {
      step.photo = "";
      touch(); renderCanvas(); renderPanel();
    }}, "✕"));
  }
  box.append(row);
  box.append(el("div", {class: "note"},
    "Пришлите картинку боту-конструктору в переписке — и она появится здесь."));
  return box;
}

async function openGallery(step) {
  var sheet = el("div", {class: "sheet big", id: "sheet"});
  var grid = el("div", {class: "gallery"});
  sheet.append(el("h3", {}, "Ваши картинки"), grid,
    el("div", {class: "note"},
      "Новая появится здесь сразу, как пришлёте её боту-конструктору в личные сообщения."));
  popup(sheet);

  if (!SHOTS) {
    grid.append(el("div", {class: "empty"}, "Загружаю…"));
    try {
      SHOTS = (await api("/api/assets")).assets || [];
    } catch (e) {
      SHOTS = [];
      flash(e.message, "long");
    }
  }
  grid.textContent = "";
  if (!SHOTS.length) {
    grid.append(el("div", {class: "empty", style: "grid-column:1/-1"},
      "Пока пусто. Пришлите боту картинку в переписке."));
  }
  SHOTS.forEach(function (shot) {
    var mark = "asset:" + shot.token;
    var tile = el("button", {class: "shot" + (step.photo === mark ? " on" : ""),
                             onclick: function () {
      step.photo = mark;
      closePopups();
      touch(); renderCanvas(); renderPanel();
    }}, el("img", {src: "/img/" + shot.token, loading: "lazy", alt: ""}));
    tile.append(armed(el("button", {class: "drop"}, "✕"), "✓", async function () {
      try {
        await api("/api/assets/delete",
                  {method: "POST", body: JSON.stringify({token: shot.token})});
      } catch (e) { flash(e.message, "long"); return; }
      SHOTS = SHOTS.filter(function (s) { return s.token !== shot.token; });
      if (step.photo === mark) { step.photo = ""; touch(); renderCanvas(); }
      closePopups();
      openGallery(step);
    }));
    grid.append(tile);
  });
}

function fieldsMessage(body, step) {
  body.append(cap("Текст сообщения"));
  body.append(textWithVars(step.text, "Что напишет бот", function (value) {
    step.text = value;
    refresh(step);
  }));

  body.append(cap("Картинка"));
  body.append(photoField(step));

  body.append(cap("Файл — ссылка"));
  body.append(line(step.file, "https://…", function (e) {
    step.file = e.target.value;
    refresh(step);
  }));

  body.append(cap("Кнопки"));
  var inline = el("input", {type: "checkbox", onchange: function (e) {
    step.inline = e.target.checked;
    touch(); renderCanvas(); renderPanel();
  }});
  inline.checked = !!step.inline;
  body.append(el("label", {class: "check", style: "margin-top:0"}, inline,
    el("span", {}, "Кнопки внутри сообщения, а не под полем ввода")));

  var buttons = step.buttons || (step.buttons = []);
  buttons.forEach(function (button, index) {
    var group = el("div", {class: "group"});
    group.append(el("div", {class: "row"},
      line(button.text, "надпись на кнопке", function (e) {
        button.text = e.target.value;
        refresh(step);
      }),
      el("button", {class: "mini kill", onclick: function () {
        buttons.splice(index, 1);
        touch(); renderCanvas(); renderPanel();
      }}, "✕")));
    group.append(el("div", {class: "row", style: "margin-top:6px"},
      pick([["goto", "ведёт на блок"], ["url", "присылает ссылку"]],
        button.action || "goto", function (e) {
          button.action = e.target.value;
          button.value = "";
          touch(); renderCanvas(); renderPanel();
        }),
      button.action === "url"
        ? line(button.value, "https://…", function (e) {
            button.value = e.target.value;
            refresh(step);
          })
        : stepPick(button.value, function (e) {
            button.value = e.target.value;
            touch(); renderCanvas();
          })));
    body.append(group);
  });
  if (buttons.length < 10) {
    body.append(el("button", {class: "add", onclick: function () {
      buttons.push({text: "Кнопка", action: "goto", value: ""});
      touch(); renderCanvas(); renderPanel();
    }}, "+ Добавить кнопку"));
  }
  body.append(el("div", {class: "note"}, step.inline
    ? "Кнопки внутри сообщения: такая кнопка умеет быть настоящей ссылкой. " +
      "Цвет кнопок Telegram не поддерживает."
    : "Кнопки под полем ввода. Такая кнопка не умеет быть ссылкой — поэтому " +
      "«присылает ссылку» отправляет её отдельным сообщением. Поставьте галочку " +
      "выше, чтобы ссылка открывалась прямо из кнопки."));
}

function fieldsInput(body, step) {
  body.append(cap("Вопрос"));
  body.append(textWithVars(step.text, "Например: как вас зовут?", function (value) {
    step.text = value;
    refresh(step);
  }));

  body.append(cap("Запомнить ответ в переменную"));
  body.append(combo(step.save_to, "имя", varItems, function (name) {
    step.save_to = name;
    refresh(step);
  }));

  body.append(cap("Что человек должен ответить"));
  body.append(pick([["any", "что угодно"], ["number", "только число"]],
    step.expect || "any", function (e) {
      step.expect = e.target.value;
      touch(); renderCanvas(); renderPanel();
    }));

  if (step.expect === "number") {
    body.append(cap("Если ответ не число"));
    body.append(line(step.retry, "Нужно число. Напишите цифрами.", function (e) {
      step.retry = e.target.value;
      refresh(step);
    }));
    body.append(el("div", {class: "note"},
      "Бот не пойдёт дальше, пока не получит число."));
  }

  body.append(el("div", {class: "note"},
    "Дальше подставляйте в текст как {" + (step.save_to || "имя") + "}."));
}

function fieldsKeywords(body, step) {
  body.append(cap("Как сравнивать"));
  body.append(pick([["contains", "слово встречается в сообщении"],
                    ["exact", "сообщение точно совпадает"]],
    step.match || "contains", function (e) {
      step.match = e.target.value;
      refresh(step);
    }));
  body.append(cap("Слова через запятую"));
  body.append(area(step.words, "цена, стоимость, сколько стоит", function (e) {
    step.words = e.target.value;
    refresh(step);
  }));
}

function fieldsEvent(body, step) {
  body.append(cap("Если пользователь"));
  body.append(pick(EVENT_NAMES, step.event || "first", function (e) {
    step.event = e.target.value;
    refresh(step);
  }));
}

function fieldsAction(body, step) {
  body.append(cap("Что сделать"));
  var actions = step.actions || (step.actions = []);
  actions.forEach(function (action, index) {
    var group = el("div", {class: "group"});
    group.append(el("div", {class: "row"},
      pick(ACTION_NAMES, action.kind, function (e) {
        action.kind = e.target.value;
        action.name = "";
        action.value = "";
        action.op = "set";
        touch(); renderCanvas(); renderPanel();
      }),
      el("button", {class: "mini kill", onclick: function () {
        actions.splice(index, 1);
        touch(); renderCanvas(); renderPanel();
      }}, "✕")));

    if (action.kind === "set_var") {
      /* Знаки «+ − × ÷» имеют смысл только у числовой переменной. Панель при
         этом не перерисовываем — иначе поле теряло бы курсор во время набора,
         поэтому просто включаем и выключаем нужное. */
      var value = line(action.value, "значение", function (e) {
        action.value = e.target.value;
        refresh(step);
      });
      var signs = pick(SET_OPS, action.op || "set", function (e) {
        action.op = e.target.value;
        refresh(step);
      });
      signs.style.flex = "none";
      signs.style.width = "62px";
      var hint = el("div", {class: "note"},
        "Переменная текстовая: её можно только записать. Чтобы складывать и " +
        "умножать, сделайте её числовой на странице «Переменные».");

      function syncSigns() {
        var numeric = isNumberVar(action.name);
        signs.disabled = !numeric;
        if (!numeric) {
          action.op = "set";
          signs.value = "set";
        }
        value.placeholder = numeric ? "число или {переменная} + 1"
                                    : "значение, можно с {переменными}";
        hint.hidden = numeric || !action.name;
      }

      group.append(el("div", {style: "margin-top:6px"},
        combo(action.name, "переменная", varItems, function (name) {
          action.name = name;
          syncSigns();
          refresh(step);
        })));
      group.append(el("div", {class: "row", style: "margin-top:6px"}, signs, value,
        insertVarButton(value, function (text) {
          action.value = text;
          value.value = text;
          refresh(step);
        })));
      group.append(hint);
      syncSigns();
    }

    if (action.kind === "add_tag" || action.kind === "del_tag") {
      group.append(el("div", {style: "margin-top:6px"},
        combo(action.name, "название тега", tagItems, function (name) {
          action.name = name;
          refresh(step);
        })));
    }

    if (action.kind === "notify") {
      var note = line(action.value, "текст уведомления, пусто — все ответы",
        function (e) {
          action.value = e.target.value;
          refresh(step);
        });
      group.append(el("div", {class: "row", style: "margin-top:6px"}, note,
        insertVarButton(note, function (text) {
          action.value = text;
          note.value = text;
          refresh(step);
        })));
      group.append(el("div", {class: "note"},
        "Придёт вам в переписку с ботом-конструктором."));
    }

    if (action.kind === "subscribe" || action.kind === "unsubscribe") {
      group.append(el("div", {class: "note"}, action.kind === "subscribe"
        ? "Человек снова начнёт получать рассылки."
        : "Человек перестанет получать рассылки, но бот будет отвечать как обычно."));
    }

    body.append(group);
  });
  body.append(el("button", {class: "add", onclick: function () {
    actions.push({kind: "set_var", name: "", op: "set", value: ""});
    touch(); renderCanvas(); renderPanel();
  }}, "+ Добавить действие"));
}

function fieldsCondition(body, step) {
  body.append(cap("Проверки — должны сойтись все"));
  var checks = step.checks || (step.checks = []);
  checks.forEach(function (check, index) {
    var group = el("div", {class: "group"});
    var tagCheck = check.op === "tag";
    group.append(el("div", {class: "row"},
      tagCheck
        ? el("div", {class: "inp", style: "background:#eef2f6;color:#7c8b9a"}, "у человека")
        : combo(check.var, "переменная", varItems, function (name) {
            check.var = name;
            refresh(step);
          }),
      el("button", {class: "mini kill", onclick: function () {
        checks.splice(index, 1);
        touch(); renderCanvas(); renderPanel();
      }}, "✕")));
    group.append(el("div", {class: "row", style: "margin-top:6px"},
      pick(OP_NAMES, check.op || "eq", function (e) {
        check.op = e.target.value;
        touch(); renderCanvas(); renderPanel();
      }),
      check.op === "empty" ? null
        : tagCheck
          ? combo(check.value, "тег", tagItems, function (name) {
              check.value = name;
              refresh(step);
            })
          : line(check.value, "значение", function (e) {
              check.value = e.target.value;
              refresh(step);
            })));
    if (check.op === "gt" || check.op === "lt" || check.op === "gte" || check.op === "lte") {
      group.append(el("div", {class: "note"}, "Сравниваются числа."));
    }
    body.append(group);
  });
  body.append(el("button", {class: "add", onclick: function () {
    checks.push({var: "", op: "eq", value: ""});
    touch(); renderCanvas(); renderPanel();
  }}, "+ Добавить проверку"));

  body.append(cap("Если сошлось — идти на"));
  body.append(stepPick(step.next, function (e) {
    step.next = e.target.value;
    touch(); renderCanvas();
  }));
  body.append(cap("Если нет — идти на"));
  body.append(stepPick(step.otherwise, function (e) {
    step.otherwise = e.target.value;
    touch(); renderCanvas();
  }));
}

function fieldsRandom(body, step) {
  body.append(cap("Варианты"));
  var options = step.options || (step.options = []);
  options.forEach(function (option, index) {
    var group = el("div", {class: "group"});
    var percent = el("input", {class: "inp", type: "number", min: "0", max: "100",
                               value: option.weight, style: "width:74px",
                               oninput: function (e) {
                                 option.weight = Math.max(0, Math.min(100, +e.target.value || 0));
                                 slider.value = option.weight;
                                 refresh(step);
                               }});
    var slider = el("input", {class: "slider", type: "range", min: "0", max: "100",
                              value: option.weight, oninput: function (e) {
                                option.weight = +e.target.value;
                                percent.value = option.weight;
                                refresh(step);
                              }});
    group.append(el("div", {class: "row"},
      line(option.label, "A", function (e) {
        option.label = e.target.value.slice(0, 20);
        refresh(step);
      }),
      percent,
      el("button", {class: "mini kill", onclick: function () {
        options.splice(index, 1);
        touch(); renderCanvas(); renderPanel();
      }}, "✕")));
    group.append(el("div", {style: "margin-top:6px"}, slider));
    group.append(el("div", {style: "margin-top:6px"},
      stepPick(option.next, function (e) {
        option.next = e.target.value;
        touch(); renderCanvas();
      })));
    body.append(group);
  });
  if (options.length < 6) {
    body.append(el("button", {class: "add", onclick: function () {
      options.push({label: String.fromCharCode(65 + options.length), weight: 50, next: ""});
      touch(); renderCanvas(); renderPanel();
    }}, "+ Добавить вариант"));
  }

  var always = el("input", {type: "checkbox", onchange: function (e) {
    step.always = e.target.checked;
    touch(); renderCanvas();
  }});
  always.checked = !!step.always;
  body.append(el("label", {class: "check"}, always,
    el("span", {}, "Выбирать заново каждый раз")));
  body.append(el("div", {class: "note"},
    "Проценты — это веса, их сумма может быть любой. Обычно выпавший вариант " +
    "закрепляется за человеком: раз выпало «A» — так и будет дальше. Галочка " +
    "выше это отключает."));
}

function fieldsTimer(body, step) {
  body.append(cap("Подождать"));
  body.append(el("div", {class: "row"},
    el("input", {class: "inp", type: "number", min: "1", max: "365",
                 value: step.amount || 1, style: "width:88px",
                 oninput: function (e) {
                   step.amount = Math.max(1, Math.min(365, +e.target.value || 1));
                   refresh(step);
                 }}),
    pick(UNIT_NAMES, step.unit || "day", function (e) {
      step.unit = e.target.value;
      refresh(step);
    })));
  body.append(el("div", {class: "note"},
    "Бот подождёт и сам продолжит со следующего блока."));
}

function fieldsNote(body, step) {
  body.append(cap("Комментарий"));
  body.append(area(step.text, "Текст комментария", function (e) {
    step.text = e.target.value;
    refresh(step);
  }));
  body.append(el("div", {class: "note"},
    "Заметка нужна только вам, боту она ничего не говорит."));
}

/* ======================= верх, меню, палитра ======================= */

function flash(text, kind) {
  var tip = document.getElementById("tip");
  tip.hidden = false;
  tip.textContent = text;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(function () {
    if (!LINKING) tip.hidden = true;
  }, kind === "long" ? 5000 : 2200);
}

function paintSave() {
  var button = document.getElementById("save");
  button.className = "save" + (DIRTY ? "" : " clean");
  button.textContent = DIRTY ? "Сохранить" : "Сохранено";
}

function paintChip() {
  var who = document.getElementById("chipWho");
  who.textContent = "";
  who.append(el("b", {}, BOT.connected ? "@" + BOT.bot_username : "Бот не подключён"),
             el("span", {}, BOT.connected ? "людей: " + BOT.people : "нажмите, чтобы привязать"));
}

function closePopups() {
  ["shade", "sheet", "menu"].forEach(function (id) {
    var node = document.getElementById(id);
    if (node) node.remove();
  });
}

function popup(node) {
  closePopups();
  var stage = document.getElementById("stage");
  var shade = el("div", {class: "shade", id: "shade", onclick: closePopups});
  stage.append(shade, node);
}

function openPalette(at) {
  var sheet = el("div", {class: "sheet", id: "sheet"});
  sheet.append(el("h3", {}, "Добавить блок"));
  var tiles = el("div", {class: "tiles"});
  ORDER.forEach(function (type) {
    tiles.append(el("button", {class: "tile", onclick: function () {
      closePopups();
      addStep(type, at);
    }}, el("span", {class: "glyph"}, META[type].glyph), META[type].title));
  });
  sheet.append(tiles);
  popup(sheet);
}

/* Небольшое окно со списком: выбрать переменную, тег, картинку. */
function chooseFrom(title, itemsFn, onpick) {
  var sheet = el("div", {class: "sheet", id: "sheet"});
  sheet.append(el("h3", {}, title));
  var items = itemsFn();
  if (!items.length) {
    sheet.append(el("div", {class: "empty"}, "Пока пусто."));
  }
  var rows = el("div", {class: "combo-list", style: "position:static;box-shadow:none;max-height:56vh"});
  items.forEach(function (item) {
    rows.append(el("button", {onclick: function () {
      closePopups();
      onpick(item.name);
    }},
      el("span", {class: "pin " + (item.tone || "")}),
      el("span", {}, item.name),
      item.kind ? el("span", {class: "kind"}, item.kind) : null));
  });
  sheet.append(rows);
  popup(sheet);
}

function openMenu() {
  var menu = el("div", {class: "menu", id: "menu"});
  menu.append(el("button", {onclick: function () { closePopups(); save(); }}, "Сохранить"));
  menu.append(el("button", {onclick: function () { closePopups(); autoLayout(); touch(); renderCanvas(); fitView(); }},
    "Разложить блоки"));
  menu.append(el("hr"));
  menu.append(el("button", {onclick: function () { closePopups(); openVars(); }},
    "Переменные"));
  menu.append(el("button", {onclick: function () { closePopups(); openTags(); }}, "Теги"));
  menu.append(el("button", {onclick: function () { closePopups(); openPeople(); }}, "Люди"));
  menu.append(el("hr"));
  menu.append(el("button", {onclick: function () { closePopups(); openBotSheet(); }},
    BOT.connected ? "Настройки бота" : "Подключить бота"));
  if (BOT.connected) {
    menu.append(el("button", {onclick: function () { closePopups(); openBot(); }}, "Открыть бота"));
  }
  popup(menu);
}

function openBotSheet() {
  var sheet = el("div", {class: "sheet", id: "sheet"});
  sheet.append(el("h3", {}, "Ваш бот"));
  if (BOT.connected) {
    sheet.append(el("div", {}, "Работает: @" + BOT.bot_username));
    sheet.append(el("div", {class: "note"}, "Людей в боте: " + BOT.people));
    sheet.append(el("button", {class: "wide ghost", onclick: function () {
      closePopups(); openBot();
    }}, "Открыть бота"));
    sheet.append(armed(el("button", {class: "wide kill"}, "Отключить бота"),
                       "Точно отключить?", function () { closePopups(); disconnect(); }));
  } else {
    sheet.append(el("div", {class: "note"},
      "Создайте бота у @BotFather, скопируйте токен и вставьте сюда."));
    var field = el("input", {class: "inp", placeholder: "1234567:AA…",
                             style: "margin-top:10px"});
    var go = el("button", {class: "wide"}, "Подключить");
    go.addEventListener("click", function () { connect(field.value, go); });
    sheet.append(field, go);
  }
  popup(sheet);
}

/* ======================= переменные, теги, люди ======================= */

function when(seconds) {
  var d = new Date(seconds * 1000);
  var two = function (n) { return (n < 10 ? "0" : "") + n; };
  return two(d.getDate()) + "." + two(d.getMonth() + 1) + "." + d.getFullYear() +
         " в " + two(d.getHours()) + ":" + two(d.getMinutes());
}

var VIEW = {scope: "user"};

function openVars() {
  var sheet = el("div", {class: "sheet big", id: "sheet"});
  sheet.append(el("h3", {}, "Переменные"));
  sheet.append(el("div", {class: "note"},
    "Пользовательская — своё значение у каждого человека. " +
    "Проектная — одно на всех. Числовая переменная умеет складываться и " +
    "умножаться, текстовая хранит что угодно."));

  var mine = VARS.filter(function (v) { return v.scope === "user"; });
  var shared = VARS.filter(function (v) { return v.scope === "project"; });
  var tabs = el("div", {class: "tabs"});
  [["user", "Пользовательские", mine.length], ["project", "Проектные", shared.length]]
    .forEach(function (pair) {
      tabs.append(el("button", {class: VIEW.scope === pair[0] ? "on" : "",
                                onclick: function () {
        VIEW.scope = pair[0];
        openVars();
      }}, pair[1] + " " + pair[2]));
    });
  sheet.append(tabs);

  var shown = (VIEW.scope === "user" ? mine : shared);
  var live = shown.filter(function (v) { return !v.archived; });
  var old = shown.filter(function (v) { return v.archived; });

  var rows = el("div", {class: "rows"});
  if (!live.length) rows.append(el("div", {class: "empty"}, "Пока ни одной."));
  live.forEach(function (item) { rows.append(varRow(item)); });
  sheet.append(rows);

  if (old.length) {
    sheet.append(cap("Архивированные"));
    var older = el("div", {class: "rows"});
    old.forEach(function (item) { older.append(varRow(item)); });
    sheet.append(older);
  }

  sheet.append(el("button", {class: "wide", onclick: function () {
    openVarForm(null);
  }}, "+ Создать переменную"));
  popup(sheet);
}

function varRow(item) {
  var about = (item.vtype === "number" ? "число" : "текст") +
              (item.descr ? " · " + item.descr : "");
  var row = el("div", {class: "rw"},
    el("div", {class: "grow"},
      el("b", {}, item.name),
      el("small", {}, about)),
    item.scope === "project" && item.value
      ? el("span", {class: "pill project"}, shorten(item.value, 18)) : null,
    el("button", {class: "mini", onclick: function () { openVarForm(item); }}, "✎"));
  return row;
}

function openVarForm(item) {
  var draft = item
    ? {was: item.name, name: item.name, scope: item.scope, vtype: item.vtype,
       descr: item.descr, value: item.value, archived: item.archived}
    : {was: "", name: "", scope: VIEW.scope, vtype: "text", descr: "", value: "",
       archived: false};

  function draw() {
    var sheet = el("div", {class: "sheet", id: "sheet"});
    sheet.append(el("h3", {}, item ? "Изменение переменной" : "Создание переменной"));

    sheet.append(cap("Вид"));
    var kinds = el("div", {class: "tabs"});
    SCOPE_NAMES.forEach(function (pair) {
      kinds.append(el("button", {class: draft.scope === pair[0] ? "on" : "",
                                 onclick: function () {
        draft.scope = pair[0];
        draw();
      }}, pair[1]));
    });
    sheet.append(kinds);

    sheet.append(cap("Название переменной"));
    sheet.append(line(draft.name, "Например: сумма заказа", function (e) {
      e.target.value = cleanChars(e.target.value);
      draft.name = cleanName(e.target.value);
    }));

    sheet.append(cap("Тип значения"));
    sheet.append(pick(TYPE_NAMES, draft.vtype, function (e) {
      draft.vtype = e.target.value;
      draw();
    }));

    if (draft.scope === "project") {
      sheet.append(cap("Значение переменной"));
      sheet.append(line(draft.value, draft.vtype === "number" ? "0" : "Значение",
        function (e) { draft.value = e.target.value; }));
    }

    sheet.append(cap("Описание переменной"));
    sheet.append(line(draft.descr, "Дополнительная информация", function (e) {
      draft.descr = e.target.value;
    }));

    if (item) {
      var box = el("input", {type: "checkbox", onchange: function (e) {
        draft.archived = e.target.checked;
      }});
      box.checked = !!draft.archived;
      sheet.append(el("label", {class: "check"}, box,
        el("span", {}, "В архив — убрать из подсказок")));
    }

    var go = el("button", {class: "wide"}, item ? "Сохранить" : "Создать");
    go.addEventListener("click", async function () {
      if (!draft.name) { flash("Впишите название"); return; }
      go.disabled = true;
      try {
        VARS = (await api("/api/vars/save",
          {method: "POST", body: JSON.stringify(draft)})).vars;
        closePopups();
        openVars();
      } catch (e) {
        flash(e.message, "long");
        go.disabled = false;
      }
    });
    sheet.append(go);
    sheet.append(el("button", {class: "wide ghost", onclick: function () {
      closePopups();
      openVars();
    }}, "Отмена"));

    if (item) {
      sheet.append(armed(el("button", {class: "wide kill"}, "Удалить переменную"),
        "Точно удалить?", async function () {
          try {
            VARS = (await api("/api/vars/delete",
              {method: "POST", body: JSON.stringify({name: item.name})})).vars;
            closePopups();
            openVars();
          } catch (e) { flash(e.message, "long"); }
        }));
    }
    popup(sheet);
  }
  draw();
}

function openTags() {
  var sheet = el("div", {class: "sheet big", id: "sheet"});
  sheet.append(el("h3", {}, "Теги"));
  sheet.append(el("div", {class: "note"},
    "Тегом отмечают людей: «новичок», «купил», «ждёт звонка». " +
    "По тегу можно сделать развилку в схеме или отправить рассылку только своим."));

  var rows = el("div", {class: "rows"});
  if (!TAGS.length) rows.append(el("div", {class: "empty"}, "Пока ни одного."));
  TAGS.forEach(function (name) {
    rows.append(el("div", {class: "rw"},
      el("div", {class: "grow"}, el("span", {class: "pill tag"}, name)),
      armed(el("button", {class: "mini kill"}, "🗑"), "✓", async function () {
        try {
          TAGS = (await api("/api/tags/delete",
            {method: "POST", body: JSON.stringify({name: name})})).tags;
          closePopups();
          openTags();
        } catch (e) { flash(e.message, "long"); }
      })));
  });
  sheet.append(rows);

  var field = el("input", {class: "inp", placeholder: "Например: важный клиент",
                           style: "margin-top:12px"});
  var go = el("button", {class: "wide"}, "Создать тег");
  go.addEventListener("click", async function () {
    var name = cleanName(field.value);
    if (!name) { flash("Впишите название"); return; }
    go.disabled = true;
    try {
      TAGS = (await api("/api/tags/save",
        {method: "POST", body: JSON.stringify({name: name})})).tags;
      closePopups();
      openTags();
    } catch (e) {
      flash(e.message, "long");
      go.disabled = false;
    }
  });
  sheet.append(field, go);
  popup(sheet);
}

async function openPeople() {
  var sheet = el("div", {class: "sheet big", id: "sheet"});
  var rows = el("div", {class: "rows"}, el("div", {class: "empty"}, "Загружаю…"));
  sheet.append(el("h3", {}, "Люди"), rows);
  sheet.append(el("button", {class: "wide", onclick: openBroadcast}, "Отправить рассылку"));
  popup(sheet);

  var people = [];
  try {
    people = (await api("/api/users")).people || [];
  } catch (e) {
    flash(e.message, "long");
  }
  rows.textContent = "";
  if (!people.length) {
    rows.append(el("div", {class: "empty"},
      "Пока никто не писал вашему боту."));
    return;
  }
  people.forEach(function (person) {
    var tags = el("div", {});
    (person.tags || []).forEach(function (name) {
      tags.append(el("span", {class: "pill tag"}, name));
    });
    rows.append(el("div", {class: "rw"},
      el("div", {class: "grow"},
        el("b", {}, person.name + (person.username ? " @" + person.username : "")),
        el("small", {}, (person.subscribed ? "подписан" : "отписан") +
                        " · " + when(person.last)),
        tags),
      el("button", {class: "mini", onclick: function () {
        openPersonTags(person);
      }}, "🏷")));
  });
}

function openPersonTags(person) {
  var chosen = (person.tags || []).slice();
  var sheet = el("div", {class: "sheet", id: "sheet"});
  sheet.append(el("h3", {}, "Теги: " + person.name));

  function draw() {
    var rows = el("div", {class: "rows"});
    if (!TAGS.length) {
      rows.append(el("div", {class: "empty"},
        "Тегов пока нет — создайте их на странице «Теги»."));
    }
    TAGS.forEach(function (name) {
      var on = chosen.indexOf(name) >= 0;
      rows.append(el("div", {class: "rw"},
        el("div", {class: "grow"}, el("span", {class: "pill tag"}, name)),
        el("button", {class: "mini", onclick: function () {
          if (on) chosen = chosen.filter(function (t) { return t !== name; });
          else chosen.push(name);
          list.replaceWith(list = draw());
        }}, on ? "✓" : "+")));
    });
    return rows;
  }

  var list = draw();
  sheet.append(list);
  var go = el("button", {class: "wide"}, "Сохранить");
  go.addEventListener("click", async function () {
    go.disabled = true;
    try {
      await api("/api/users/tags", {method: "POST",
        body: JSON.stringify({chat_id: person.chat_id, tags: chosen})});
      closePopups();
      openPeople();
    } catch (e) {
      flash(e.message, "long");
      go.disabled = false;
    }
  });
  sheet.append(go);
  popup(sheet);
}

function openBroadcast() {
  var sheet = el("div", {class: "sheet", id: "sheet"});
  sheet.append(el("h3", {}, "Рассылка"));
  sheet.append(el("div", {class: "note"},
    "Уйдёт всем, кто не отписался. Подставляются переменные: {name}, {username}."));

  var text = area("", "Что написать людям", function () {});
  sheet.append(cap("Текст"), text);
  sheet.append(cap("Только с тегом"));
  var only = pick([["", "— всем —"]].concat(TAGS.map(function (t) { return [t, t]; })),
                  "", function () {});
  sheet.append(only);

  var go = el("button", {class: "wide"}, "Отправить");
  armed(go, "Точно отправить?", async function () {
    if (!text.value.trim()) { flash("Напишите текст"); return; }
    go.disabled = true;
    try {
      var result = await api("/api/broadcast", {method: "POST",
        body: JSON.stringify({text: text.value, tag: only.value})});
      closePopups();
      flash("Рассылка пошла: " + result.people + " чел.", "long");
    } catch (e) {
      flash(e.message, "long");
      go.disabled = false;
      go.textContent = "Отправить";
    }
  });
  sheet.append(go);
  popup(sheet);
}

function addStep(type, at) {
  var canvas = document.getElementById("canvas");
  var step = blankStep(type);
  if (at) {                              /* добавили двойным касанием — сюда же */
    step.x = Math.round(at.x - NODE_W / 2);
    step.y = Math.round(at.y - 40);
  } else {
    step.x = Math.round((canvas.clientWidth / 2 - PAN.x) / PAN.z - NODE_W / 2);
    step.y = Math.round((canvas.clientHeight / 2 - PAN.y) / PAN.z - 70);
  }
  S.steps.push(step);
  if (!S.start && type !== "note") S.start = step.id;
  touch();
  select(step.id);
}

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

async function save() {
  var button = document.getElementById("save");
  button.disabled = true;
  button.textContent = "Сохраняю…";
  try {
    var result = await api("/api/scenario",
      {method: "POST", body: JSON.stringify({scenario: S})});
    /* Имена, написанные прямо на схеме, сервис заносит в общие списки —
       забираем их обратно, чтобы подсказки сразу о них знали. */
    if (result.vars) VARS = result.vars;
    if (result.tags) TAGS = result.tags;
    DIRTY = false;
    paintSave();
    if (!S.start) flash("Сохранено, но стартовый блок не отмечен — бот не ответит на /start", "long");
  } catch (e) {
    button.className = "save bad";
    button.textContent = "Не сохранилось";
    flash(e.message, "long");
  }
  button.disabled = false;
}

async function connect(token, button) {
  token = (token || "").trim();
  if (!token) { flash("Вставьте токен"); return; }
  button.disabled = true;
  button.textContent = "Подключаю…";
  try {
    var result = await api("/api/bot/connect",
      {method: "POST", body: JSON.stringify({token: token})});
    BOT.connected = true;
    BOT.bot_username = result.bot_username;
    closePopups();
    paintChip();
    flash(result.warning || ("Бот @" + result.bot_username + " подключён"),
          result.warning ? "long" : "");
  } catch (e) {
    flash(e.message, "long");
    button.disabled = false;
    button.textContent = "Подключить";
  }
}

async function disconnect() {
  try {
    await api("/api/bot/disconnect", {method: "POST"});
    BOT.connected = false;
    BOT.bot_username = "";
    paintChip();
    flash("Бот отключён");
  } catch (e) { flash(e.message, "long"); }
}

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
    S = (state.scenario && state.scenario.steps) ? state.scenario : {start: "", steps: []};
    if (!S.steps.length) S.steps.push(blankStep("message"));
    BOT.connected = state.connected;
    BOT.bot_username = state.bot_username;
    BOT.people = state.people;
    VARS = state.vars || [];
    TAGS = state.tags || [];

    boot.hidden = true;
    document.getElementById("stage").hidden = false;

    document.getElementById("plus").addEventListener("click", function () { openPalette(); });
    document.getElementById("menuBtn").addEventListener("click", openMenu);
    document.getElementById("varsBtn").addEventListener("click", openVars);
    document.getElementById("tagsBtn").addEventListener("click", openTags);
    document.getElementById("peopleBtn").addEventListener("click", openPeople);
    document.getElementById("chip").addEventListener("click", openBotSheet);
    document.getElementById("save").addEventListener("click", save);
    document.getElementById("zoomIn").addEventListener("click", function () { zoomBy(1.2); });
    document.getElementById("zoomOut").addEventListener("click", function () { zoomBy(0.83); });
    document.getElementById("zoomFit").addEventListener("click", fitView);

    attachCanvas();
    paintChip();
    paintSave();
    renderCanvas();
    renderPanel();
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
