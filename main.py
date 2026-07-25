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
TAGS_KEY = "#tags"                 # теги лежат среди переменных под этим ключом

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
CONDITION_OPS = ("eq", "ne", "has", "empty", "tag")
ACTION_KINDS = ("set_var", "del_var", "add_tag", "del_tag", "notify")
TIMER_UNITS = ("minute", "hour", "day")
UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}

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
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, chat_id)
);
CREATE TABLE IF NOT EXISTS timers (
    id         BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL,
    chat_id    BIGINT NOT NULL,
    step_id    TEXT NOT NULL,
    run_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS timers_due ON timers (run_at);
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
CREATE TABLE IF NOT EXISTS timers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    step_id    TEXT NOT NULL,
    run_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS timers_due ON timers (run_at);
"""


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
        return {"awaiting": "", "vars": {}, "first": True}
    try:
        variables = json.loads(row["vars"])
    except (TypeError, ValueError):
        variables = {}
    if not isinstance(variables, dict):
        variables = {}
    return {"awaiting": row["awaiting"] or "", "vars": variables, "first": False}


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
    """Имя переменной или тега: буквы, цифры и подчёркивание."""
    return re.sub(r"[^\w]", "", str(value or ""), flags=re.UNICODE)[:40]


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
            step["photo"] = _text(item.get("photo"), 500)
            step["file"] = _text(item.get("file"), 500)
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
                step["actions"].append({
                    "kind": a["kind"],
                    "name": _name(a.get("name")),
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
            step["options"] = []
            for o in (item.get("options") or [])[:10]:
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


def migrate_scenario(raw: Any) -> dict:
    """Переводит схему из первой версии (шаги с триггерами) в блоки.

    Старый шаг знал сам, чем он запускается. Теперь запуск — это отдельные
    блоки «Ключевые слова» и «События», а команда /start отмечает стартовый
    блок. Нужно, чтобы у тех, кто успел собрать бота, ничего не пропало.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        return dict(DEFAULT_SCENARIO)
    if any(isinstance(s, dict) and s.get("type") in TYPES for s in raw["steps"]):
        return raw                              # уже новый формат

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

VAR_RE = re.compile(r"\{(\w+)\}", re.UNICODE)


def fill(text: str, variables: Dict[str, Any]) -> str:
    """Подставляет в текст значения переменных: {имя} -> Иван."""
    return VAR_RE.sub(lambda m: str(variables.get(m.group(1), "")), text or "")


def find_step(steps: List[dict], step_id: str) -> Optional[dict]:
    for step in steps:
        if step.get("id") == step_id:
            return step
    return None


def tags_of(session: dict) -> List[str]:
    tags = session.get("vars", {}).get(TAGS_KEY)
    return list(tags) if isinstance(tags, list) else []


def remember_user(session: dict, user: dict) -> None:
    """Кладёт имя и ник в переменные, чтобы их можно было вставлять в текст."""
    if user.get("first_name"):
        session["vars"]["name"] = user["first_name"]
    if user.get("username"):
        session["vars"]["username"] = user["username"]


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
    variables = session.get("vars", {})
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
    return True


def pick_random(step: dict) -> str:
    """Выбирает вариант с учётом весов. Вариант без стрелки не участвует."""
    options = [o for o in (step.get("options") or []) if o.get("next")]
    if not options:
        return ""
    total = sum(max(0, o.get("weight") or 0) for o in options)
    if total <= 0:
        return random.choice(options)["next"]
    point = random.uniform(0, total)
    running = 0.0
    for option in options:
        running += max(0, option.get("weight") or 0)
        if point <= running:
            return option["next"]
    return options[-1]["next"]


async def notify_owner(project: dict, chat_id: int, session: dict, note: str = "") -> None:
    """Присылает владельцу заявку — в его чат с ботом-конструктором."""
    variables = session.get("vars", {})
    lines = [f"{key}: {value}" for key, value in variables.items()
             if key not in ("name", "username") and key != TAGS_KEY]
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
        value = fill(action.get("value") or "", variables)
        if kind == "set_var" and name:
            variables[name] = value[:500]
        elif kind == "del_var" and name:
            variables.pop(name, None)
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
    variables[TAGS_KEY] = tags


async def send_message_step(token: str, chat_id: int, step: dict,
                            variables: Dict[str, Any]) -> None:
    text = fill(step.get("text", ""), variables)
    markup = keyboard_for(step)
    photo = (step.get("photo") or "").strip()
    document = (step.get("file") or "").strip()
    sent = False

    if photo.startswith(("http://", "https://")):
        result = await tg(token, "sendPhoto", chat_id=chat_id, photo=photo,
                          caption=text[:1024],
                          reply_markup=None if document else markup)
        sent = bool(result.get("ok"))

    if document.startswith(("http://", "https://")):
        result = await tg(token, "sendDocument", chat_id=chat_id, document=document,
                          caption="" if sent else text[:1024], reply_markup=markup)
        sent = sent or bool(result.get("ok"))

    if not sent:
        # Ни картинки, ни файла — или Telegram не принял ссылку.
        await tg(token, "sendMessage", chat_id=chat_id,
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
            await send_message_step(token, chat_id, step, session["vars"])
            step_id = step.get("next") or ""

        elif kind == "input":
            await tg(token, "sendMessage", chat_id=chat_id,
                     text=(fill(step.get("text", ""), session["vars"]) or "…")[:4096])
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
            step_id = pick_random(step)

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

    # 1. Ждём ответ на заданный вопрос.
    awaiting_id = session.get("awaiting") or ""
    if awaiting_id and text and not text.startswith("/"):
        asked = find_step(steps, awaiting_id)
        session["awaiting"] = ""
        if asked:
            name = (asked.get("save_to") or "").strip()
            if name:
                session["vars"][name] = text[:500]
            await save_session(project["id"], chat_id, session)
            await run_step(project, chat_id, asked.get("next") or "",
                           scenario, session)
            return

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


async def tick_timers() -> int:
    """Забирает созревшие таймеры и продолжает схему с их блока."""
    due = await db.fetch(
        "SELECT * FROM timers WHERE run_at <= $1 ORDER BY run_at LIMIT 20",
        time.time(),
    )
    for job in due:
        await db.execute("DELETE FROM timers WHERE id = $1", job["id"])
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
    await db.execute("DELETE FROM timers WHERE project_id = $1", project["id"])
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
.wire-end{fill:#9db4c9}
.wire-end.yes{fill:#2fbf87}
.wire-end.no{fill:#e2483d}

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

@media (max-width:700px){
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
  ["set_var", "записать переменную"],
  ["del_var", "удалить переменную"],
  ["add_tag", "поставить тег"],
  ["del_tag", "снять тег"],
  ["notify", "прислать мне заявку"],
];
var OP_NAMES = [
  ["eq", "равна"], ["ne", "не равна"], ["has", "содержит"],
  ["empty", "пустая"], ["tag", "есть тег"],
];
var UNIT_NAMES = [["minute", "минут"], ["hour", "часов"], ["day", "дней"]];

function labelOf(list, value) {
  for (var i = 0; i < list.length; i++) if (list[i][0] === value) return list[i][1];
  return value || "";
}

/* ---- состояние ---- */
var S = {start: "", steps: []};
var BOT = {connected: false, bot_username: "", people: 0};
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
  if (type === "message") { step.text = ""; step.photo = ""; step.file = ""; step.buttons = []; }
  if (type === "input") { step.text = ""; step.save_to = ""; }
  if (type === "keywords") { step.match = "contains"; step.words = ""; }
  if (type === "event") { step.event = "first"; }
  if (type === "action") { step.actions = []; }
  if (type === "condition") { step.checks = []; step.otherwise = ""; }
  if (type === "random") {
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
      svg.append(wirePath(x1, y1, x2, y2, out.tone));
      var end = document.createElementNS(SVGNS, "circle");
      end.setAttribute("class", "wire-end " + (out.tone || ""));
      end.setAttribute("cx", x2);
      end.setAttribute("cy", y2);
      end.setAttribute("r", 4);
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
    if (t === "message" && (step.photo || step.file)) {
      box.append(el("div", {class: "node-line dim", style: "margin-top:6px"},
        (step.photo ? "🖼 картинка " : "") + (step.file ? "📎 файл" : "")));
    }
    if (t === "input" && step.save_to) {
      box.append(el("div", {class: "node-line dim", style: "margin-top:6px"},
        "ответ → {" + step.save_to + "}"));
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
        box.append(el("div", {class: "node-line"}, "• " + labelOf(ACTION_NAMES, action.kind) +
          (action.name ? " " + action.name : "")));
      });
    }
  } else if (t === "condition") {
    var checks = step.checks || [];
    if (!checks.length) {
      box.append(el("div", {class: "node-fill"}, "Задайте условие…"));
    } else {
      checks.forEach(function (check) {
        box.append(el("div", {class: "node-line"},
          "{" + (check.var || "?") + "} " + labelOf(OP_NAMES, check.op) +
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
    var dot = el("span", {class: "dot " + (out.tone || "") +
                                 (out.kind === "url" ? " url" : "") + (lit ? " on" : "")});
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
    if (pan && !pan.moved) {          /* тап мимо блоков — снимаем выделение */
      if (LINKING || SEL) deselect();
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

function fieldsMessage(body, step) {
  body.append(cap("Текст сообщения"));
  body.append(area(step.text, "Что напишет бот", function (e) {
    step.text = e.target.value;
    refresh(step);
  }));
  body.append(el("div", {class: "note"},
    "Значения ответов вставляются так: {имя}. Всегда есть {name} и {username}."));

  body.append(cap("Картинка — ссылка"));
  body.append(line(step.photo, "https://…", function (e) {
    step.photo = e.target.value;
    refresh(step);
  }));
  body.append(cap("Файл — ссылка"));
  body.append(line(step.file, "https://…", function (e) {
    step.file = e.target.value;
    refresh(step);
  }));

  body.append(cap("Кнопки"));
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
      pick([["goto", "ведёт на блок"], ["url", "открывает сайт"]],
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
  body.append(el("div", {class: "note"},
    "Цвет кнопок Telegram не поддерживает — все кнопки выглядят одинаково."));
}

function fieldsInput(body, step) {
  body.append(cap("Вопрос"));
  body.append(area(step.text, "Например: как вас зовут?", function (e) {
    step.text = e.target.value;
    refresh(step);
  }));
  body.append(cap("Запомнить ответ под именем"));
  body.append(line(step.save_to, "имя", function (e) {
    step.save_to = e.target.value.replace(/[^0-9A-Za-zА-Яа-яЁё_]/g, "");
    e.target.value = step.save_to;
    refresh(step);
  }));
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
        touch(); renderCanvas(); renderPanel();
      }),
      el("button", {class: "mini kill", onclick: function () {
        actions.splice(index, 1);
        touch(); renderCanvas(); renderPanel();
      }}, "✕")));

    if (action.kind === "set_var" || action.kind === "del_var") {
      group.append(el("div", {class: "row", style: "margin-top:6px"},
        line(action.name, "имя переменной", function (e) {
          action.name = e.target.value.replace(/[^0-9A-Za-zА-Яа-яЁё_]/g, "");
          e.target.value = action.name;
          refresh(step);
        })));
    }
    if (action.kind === "set_var") {
      group.append(el("div", {style: "margin-top:6px"},
        line(action.value, "значение, можно с {переменными}", function (e) {
          action.value = e.target.value;
          refresh(step);
        })));
    }
    if (action.kind === "add_tag" || action.kind === "del_tag") {
      group.append(el("div", {style: "margin-top:6px"},
        line(action.name, "название тега", function (e) {
          action.name = e.target.value.replace(/[^0-9A-Za-zА-Яа-яЁё_]/g, "");
          e.target.value = action.name;
          refresh(step);
        })));
    }
    if (action.kind === "notify") {
      group.append(el("div", {style: "margin-top:6px"},
        line(action.value, "текст письма, пусто — все ответы", function (e) {
          action.value = e.target.value;
          refresh(step);
        })));
    }
    body.append(group);
  });
  body.append(el("button", {class: "add", onclick: function () {
    actions.push({kind: "set_var", name: "", value: ""});
    touch(); renderCanvas(); renderPanel();
  }}, "+ Добавить действие"));
}

function fieldsCondition(body, step) {
  body.append(cap("Проверки — должны сойтись все"));
  var checks = step.checks || (step.checks = []);
  checks.forEach(function (check, index) {
    var group = el("div", {class: "group"});
    group.append(el("div", {class: "row"},
      line(check.var, "переменная", function (e) {
        check.var = e.target.value.replace(/[^0-9A-Za-zА-Яа-яЁё_]/g, "");
        e.target.value = check.var;
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
        : line(check.value, check.op === "tag" ? "тег" : "значение", function (e) {
            check.value = e.target.value;
            refresh(step);
          })));
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
  body.append(el("button", {class: "add", onclick: function () {
    options.push({label: String.fromCharCode(65 + options.length), weight: 50, next: ""});
    touch(); renderCanvas(); renderPanel();
  }}, "+ Добавить вариант"));
  body.append(el("div", {class: "note"},
    "Проценты — это веса. Их сумма может быть любой."));
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

function openPalette() {
  var sheet = el("div", {class: "sheet", id: "sheet"});
  sheet.append(el("h3", {}, "Добавить блок"));
  var tiles = el("div", {class: "tiles"});
  ORDER.forEach(function (type) {
    tiles.append(el("button", {class: "tile", onclick: function () {
      closePopups();
      addStep(type);
    }}, el("span", {class: "glyph"}, META[type].glyph), META[type].title));
  });
  sheet.append(tiles);
  popup(sheet);
}

function openMenu() {
  var menu = el("div", {class: "menu", id: "menu"});
  menu.append(el("button", {onclick: function () { closePopups(); save(); }}, "Сохранить"));
  menu.append(el("button", {onclick: function () { closePopups(); autoLayout(); touch(); renderCanvas(); fitView(); }},
    "Разложить блоки"));
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

function addStep(type) {
  var canvas = document.getElementById("canvas");
  var step = blankStep(type);
  step.x = Math.round((canvas.clientWidth / 2 - PAN.x) / PAN.z - NODE_W / 2);
  step.y = Math.round((canvas.clientHeight / 2 - PAN.y) / PAN.z - 70);
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
    await api("/api/scenario", {method: "POST", body: JSON.stringify({scenario: S})});
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

    boot.hidden = true;
    document.getElementById("stage").hidden = false;

    document.getElementById("plus").addEventListener("click", openPalette);
    document.getElementById("menuBtn").addEventListener("click", openMenu);
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
