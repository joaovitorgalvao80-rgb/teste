"""Camada de persistencia do Sistema 1 (SQLite).

Guarda usuarios, chaves de API, projetos, cenas e assets de curadoria.
MVP single-file: sem ORM, apenas sqlite3 da stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "plataforma.db"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    pexels_key      TEXT DEFAULT '',
    pixabay_key     TEXT DEFAULT '',
    groq_key        TEXT DEFAULT '',
    groq_model      TEXT DEFAULT 'llama-3.3-70b-versatile',
    kaggle_username TEXT DEFAULT '',
    kaggle_token    TEXT DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    script      TEXT DEFAULT '',
    config_json          TEXT DEFAULT '{}',
    status               TEXT DEFAULT 'created',
    kaggle_dataset_slug  TEXT DEFAULT '',
    kaggle_kernel_slug   TEXT DEFAULT '',
    kaggle_status        TEXT DEFAULT '',
    created_at           REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scenes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL,
    scene_id        TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    zone            TEXT DEFAULT '',
    start_time      REAL DEFAULT 0,
    end_time        REAL DEFAULT 0,
    duration        REAL DEFAULT 0,
    narration       TEXT DEFAULT '',
    visual_goal     TEXT DEFAULT '',
    keywords_json   TEXT DEFAULT '[]',
    must_show_json  TEXT DEFAULT '[]',
    must_not_show_json TEXT DEFAULT '[]',
    asset_type      TEXT DEFAULT 'video',
    overlay_text    TEXT DEFAULT '',
    avatar_safe_area TEXT DEFAULT 'right',
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id      INTEGER NOT NULL,
    source        TEXT NOT NULL,
    source_id     TEXT DEFAULT '',
    asset_type    TEXT DEFAULT 'video',
    preview_url   TEXT DEFAULT '',
    download_url  TEXT NOT NULL,
    page_url      TEXT DEFAULT '',
    width         INTEGER DEFAULT 0,
    height        INTEGER DEFAULT 0,
    duration      REAL DEFAULT 0,
    keyword       TEXT DEFAULT '',
    author        TEXT DEFAULT '',
    author_url    TEXT DEFAULT '',
    state         TEXT DEFAULT 'pending',
    FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
);
"""


_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN kaggle_username TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN kaggle_token TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN kaggle_dataset_slug TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN kaggle_kernel_slug TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN kaggle_status TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN groq_model TEXT DEFAULT 'llama-3.3-70b-versatile'",
]


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        # migrações para bancos já existentes (ignora se coluna já existe)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Password hashing (pbkdf2, stdlib only)
# ----------------------------------------------------------------------------
def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


# ----------------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------------
def create_user(username: str, password: str) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), time.time()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_user_by_name(username: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_api_keys(user_id: int, pexels: str, pixabay: str, groq: str, groq_model: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET pexels_key = ?, pixabay_key = ?, groq_key = ?, groq_model = ? WHERE id = ?",
            (pexels, pixabay, groq, groq_model or "llama-3.3-70b-versatile", user_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_kaggle_keys(user_id: int, kaggle_username: str, kaggle_token: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET kaggle_username = ?, kaggle_token = ? WHERE id = ?",
            (kaggle_username, kaggle_token, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_kaggle_job(project_id: int, dataset_slug: str, kernel_slug: str, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE projects SET kaggle_dataset_slug = ?, kaggle_kernel_slug = ?, kaggle_status = ? WHERE id = ?",
            (dataset_slug, kernel_slug, status, project_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_kaggle_status(project_id: int, status: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE projects SET kaggle_status = ? WHERE id = ?", (status, project_id))
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Projects
# ----------------------------------------------------------------------------
def create_project(user_id: int, name: str, script: str, config: dict) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO projects (user_id, name, script, config_json, status, created_at) "
            "VALUES (?, ?, ?, ?, 'created', ?)",
            (user_id, name, script, json.dumps(config, ensure_ascii=False), time.time()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_projects(user_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_project(project_id: int, user_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_project_status(project_id: int, status: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
        conn.commit()
    finally:
        conn.close()


def delete_project(project_id: int, user_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id))
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Scenes
# ----------------------------------------------------------------------------
def replace_scenes(project_id: int, scenes: list[dict]) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM scenes WHERE project_id = ?", (project_id,))
        for s in scenes:
            conn.execute(
                """INSERT INTO scenes
                (project_id, scene_id, idx, zone, start_time, end_time, duration,
                 narration, visual_goal, keywords_json, must_show_json, must_not_show_json,
                 asset_type, overlay_text, avatar_safe_area)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id,
                    s["scene_id"],
                    s["idx"],
                    s.get("zone", ""),
                    s.get("start_time", 0),
                    s.get("end_time", 0),
                    s.get("duration", 0),
                    s.get("narration", ""),
                    s.get("visual_goal", ""),
                    json.dumps(s.get("keywords", []), ensure_ascii=False),
                    json.dumps(s.get("must_show", []), ensure_ascii=False),
                    json.dumps(s.get("must_not_show", []), ensure_ascii=False),
                    s.get("asset_type", "video"),
                    s.get("overlay_text", ""),
                    s.get("avatar_safe_area", "right"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _scene_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["keywords"] = json.loads(d.pop("keywords_json") or "[]")
    d["must_show"] = json.loads(d.pop("must_show_json") or "[]")
    d["must_not_show"] = json.loads(d.pop("must_not_show_json") or "[]")
    return d


def list_scenes(project_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY idx", (project_id,)
        ).fetchall()
        return [_scene_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_scene(scene_db_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_db_id,)).fetchone()
        return _scene_to_dict(row) if row else None
    finally:
        conn.close()


def update_scene_keywords(scene_db_id: int, keywords: list[str]) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE scenes SET keywords_json = ? WHERE id = ?",
            (json.dumps(keywords, ensure_ascii=False), scene_db_id),
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Assets
# ----------------------------------------------------------------------------
def add_assets(scene_db_id: int, assets: list[dict]) -> int:
    """Insere assets evitando duplicar pela download_url na mesma cena."""
    conn = _connect()
    inserted = 0
    try:
        existing = {
            r["download_url"]
            for r in conn.execute(
                "SELECT download_url FROM assets WHERE scene_id = ?", (scene_db_id,)
            ).fetchall()
        }
        for a in assets:
            if a["download_url"] in existing:
                continue
            existing.add(a["download_url"])
            conn.execute(
                """INSERT INTO assets
                (scene_id, source, source_id, asset_type, preview_url, download_url, page_url,
                 width, height, duration, keyword, author, author_url, state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
                (
                    scene_db_id,
                    a.get("source", ""),
                    str(a.get("source_id", "")),
                    a.get("asset_type", "video"),
                    a.get("preview_url", ""),
                    a["download_url"],
                    a.get("page_url", ""),
                    a.get("width", 0),
                    a.get("height", 0),
                    a.get("duration", 0),
                    a.get("keyword", ""),
                    a.get("author", ""),
                    a.get("author_url", ""),
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def list_assets(scene_db_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM assets WHERE scene_id = ? ORDER BY id", (scene_db_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_assets_by_state(project_id: int, states: list[str]) -> list[dict]:
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in states)
        rows = conn.execute(
            f"""SELECT a.*, s.scene_id AS scene_code, s.idx AS scene_idx
                FROM assets a JOIN scenes s ON a.scene_id = s.id
                WHERE s.project_id = ? AND a.state IN ({placeholders})
                ORDER BY s.idx, a.id""",
            (project_id, *states),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def asset_belongs_to_user(asset_id: int, user_id: int) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT 1
               FROM assets a
               JOIN scenes s ON a.scene_id = s.id
               JOIN projects p ON s.project_id = p.id
               WHERE a.id = ? AND p.user_id = ?""",
            (asset_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def set_asset_state(asset_id: int, state: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT scene_id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            return None
        scene_id = row["scene_id"]
        # Uma cena tem apenas 1 asset 'selected'; ao selecionar, rebaixa os outros.
        if state == "selected":
            conn.execute(
                "UPDATE assets SET state = 'pending' WHERE scene_id = ? AND state = 'selected'",
                (scene_id,),
            )
        conn.execute("UPDATE assets SET state = ? WHERE id = ?", (state, asset_id))
        conn.commit()
        updated = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return dict(updated) if updated else None
    finally:
        conn.close()


def get_asset(asset_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
