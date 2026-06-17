"""Camada de persistencia do Sistema 1 (SQLite).

Guarda usuarios, chaves de API, projetos, cenas e assets de curadoria.
MVP single-file: sem ORM, apenas sqlite3 da stdlib.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from services import scoring

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "plataforma.db"
SECRET_FIELDS = {
    "pexels_key",
    "pixabay_key",
    "groq_key",
    "kaggle_token",
    "coverr_key",
    "nvidia_key",
    "exa_key",
    "firecrawl_key",
}
SECRET_PREFIX = "enc:v1:"
DEV_SECRET_KEY = "dev-insecure-key-change-in-production-please"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # background jobs escrevem enquanto requests leem; espera em vez de "database is locked"
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _secret_key_material() -> bytes:
    raw = os.getenv("API_SECRET_KEY") or os.getenv("APP_SECRET_KEY") or DEV_SECRET_KEY
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    while sum(len(block) for block in blocks) < size:
        counter += 1
        blocks.append(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
    return b"".join(blocks)[:size]


def protect_secret(value: str) -> str:
    """Encrypts API secrets before writing them to SQLite.

    Existing plaintext values are still readable through reveal_secret, so old
    databases migrate lazily the next time settings are saved.
    """
    value = value or ""
    if not value or value.startswith(SECRET_PREFIX):
        return value
    key = _secret_key_material()
    nonce = secrets.token_bytes(16)
    raw = value.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(raw, _keystream(key, nonce, len(raw))))
    mac = hmac.new(key, b"nwrch-secret-v1" + nonce + cipher, hashlib.sha256).digest()
    return SECRET_PREFIX + base64.urlsafe_b64encode(nonce + cipher + mac).decode("ascii")


def reveal_secret(value: str) -> str:
    value = value or ""
    if not value.startswith(SECRET_PREFIX):
        return value
    try:
        blob = base64.urlsafe_b64decode(value[len(SECRET_PREFIX):].encode("ascii"))
        nonce, rest = blob[:16], blob[16:]
        cipher, mac = rest[:-32], rest[-32:]
        key = _secret_key_material()
        expected = hmac.new(key, b"nwrch-secret-v1" + nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return ""
        raw = bytes(a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher))))
        return raw.decode("utf-8")
    except Exception:
        return ""


def _user_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for field in SECRET_FIELDS:
        if field in data:
            data[field] = reveal_secret(data[field])
    return data


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    pexels_key      TEXT DEFAULT '',
    pixabay_key     TEXT DEFAULT '',
    groq_key        TEXT DEFAULT '',
    groq_model      TEXT DEFAULT 'llama-3.3-70b-versatile',
    openrouter_key  TEXT DEFAULT '',
    coverr_key      TEXT DEFAULT '',
    nvidia_key      TEXT DEFAULT '',
    exa_key         TEXT DEFAULT '',
    firecrawl_key   TEXT DEFAULT '',
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
    review_round         INTEGER DEFAULT 0,
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
    screen_mode     TEXT DEFAULT '',
    visual_need     REAL DEFAULT 0,
    visual_strategy TEXT DEFAULT '',
    visual_target   TEXT DEFAULT '',
    keywords_json   TEXT DEFAULT '[]',
    keyword_roles_json TEXT DEFAULT '[]',
    query_ladder_json TEXT DEFAULT '[]',
    must_show_json  TEXT DEFAULT '[]',
    must_not_show_json TEXT DEFAULT '[]',
    asset_type      TEXT DEFAULT 'video',
    overlay_text    TEXT DEFAULT '',
    avatar_safe_area TEXT DEFAULT 'right',
    part            INTEGER DEFAULT 1,
    broll_override  INTEGER DEFAULT 0,
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
    license       TEXT DEFAULT '',
    license_url   TEXT DEFAULT '',
    attribution   TEXT DEFAULT '',
    discovery_provider TEXT DEFAULT '',
    scrape_url    TEXT DEFAULT '',
    scrape_status TEXT DEFAULT '',
    query_role    TEXT DEFAULT '',
    query_text    TEXT DEFAULT '',
    provider_payload_json TEXT DEFAULT '{}',
    confidence    REAL DEFAULT 0,
    state         TEXT DEFAULT 'pending',
    auto_score    REAL DEFAULT 0,
    auto_reason   TEXT DEFAULT '',
    review_round  INTEGER DEFAULT 0,
    rejection_reason TEXT DEFAULT '',
    rejected_at    REAL DEFAULT 0,
    vision_score    REAL DEFAULT 0,
    vision_verdict  TEXT DEFAULT '',
    vision_reason   TEXT DEFAULT '',
    vision_flags_json TEXT DEFAULT '[]',
    vision_provider TEXT DEFAULT '',
    vision_analyzed INTEGER DEFAULT 0,
    FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS render_parts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL,
    part_idx     INTEGER NOT NULL,
    scene_count  INTEGER DEFAULT 0,
    duration     REAL DEFAULT 0,
    zip_name     TEXT DEFAULT '',
    dataset_slug TEXT DEFAULT '',
    kernel_slug  TEXT DEFAULT '',
    status       TEXT DEFAULT 'pending',
    curation_status TEXT DEFAULT 'pending',
    video_path   TEXT DEFAULT '',
    error        TEXT DEFAULT '',
    updated_at   REAL DEFAULT 0,
    UNIQUE (project_id, part_idx),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    project_id  INTEGER,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    message     TEXT DEFAULT '',
    detail      TEXT DEFAULT '',
    result_json TEXT DEFAULT '{}',
    error       TEXT DEFAULT '',
    log_path    TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    finished_at REAL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    project_id  INTEGER,
    job_id      INTEGER,
    provider    TEXT NOT NULL,
    operation   TEXT DEFAULT '',
    status_code INTEGER DEFAULT 0,
    ok          INTEGER DEFAULT 1,
    units       INTEGER DEFAULT 1,
    latency_ms  REAL DEFAULT 0,
    detail      TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
);
"""


_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN kaggle_username TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN kaggle_token TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN kaggle_dataset_slug TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN kaggle_kernel_slug TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN kaggle_status TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN groq_model TEXT DEFAULT 'llama-3.3-70b-versatile'",
    "ALTER TABLE users ADD COLUMN openrouter_key TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN coverr_key TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN nvidia_key TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN exa_key TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN firecrawl_key TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN review_round INTEGER DEFAULT 0",
    "ALTER TABLE scenes ADD COLUMN part INTEGER DEFAULT 1",
    "ALTER TABLE scenes ADD COLUMN keyword_roles_json TEXT DEFAULT '[]'",
    "ALTER TABLE scenes ADD COLUMN screen_mode TEXT DEFAULT ''",
    "ALTER TABLE scenes ADD COLUMN visual_need REAL DEFAULT 0",
    "ALTER TABLE scenes ADD COLUMN visual_strategy TEXT DEFAULT ''",
    "ALTER TABLE scenes ADD COLUMN visual_target TEXT DEFAULT ''",
    "ALTER TABLE scenes ADD COLUMN query_ladder_json TEXT DEFAULT '[]'",
    "ALTER TABLE scenes ADD COLUMN broll_override INTEGER DEFAULT 0",
    "ALTER TABLE assets ADD COLUMN auto_score REAL DEFAULT 0",
    "ALTER TABLE assets ADD COLUMN auto_reason TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN review_round INTEGER DEFAULT 0",
    "ALTER TABLE assets ADD COLUMN rejection_reason TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN rejected_at REAL DEFAULT 0",
    "ALTER TABLE assets ADD COLUMN vision_score REAL DEFAULT 0",
    "ALTER TABLE assets ADD COLUMN vision_verdict TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN vision_reason TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN vision_flags_json TEXT DEFAULT '[]'",
    "ALTER TABLE assets ADD COLUMN vision_provider TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN vision_analyzed INTEGER DEFAULT 0",
    "ALTER TABLE assets ADD COLUMN license TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN license_url TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN attribution TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN discovery_provider TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN scrape_url TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN scrape_status TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN query_role TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN query_text TEXT DEFAULT ''",
    "ALTER TABLE assets ADD COLUMN provider_payload_json TEXT DEFAULT '{}'",
    "ALTER TABLE assets ADD COLUMN confidence REAL DEFAULT 0",
    "ALTER TABLE render_parts ADD COLUMN curation_status TEXT DEFAULT 'pending'",
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
        return secrets.compare_digest(hash_password(password, salt), stored)
    except ValueError:
        # hash sem separador ou salt nao-hex: credencial invalida, nao 500
        return False


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
        return _user_to_dict(row) if row else None
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_to_dict(row) if row else None
    finally:
        conn.close()


def count_users() -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        return int(row["total"] if row else 0)
    finally:
        conn.close()


def update_api_keys(
    user_id: int,
    pexels: str,
    pixabay: str,
    groq: str,
    groq_model: str = "",
    coverr: str = "",
    nvidia: str = "",
    exa: str = "",
    firecrawl: str = "",
) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET pexels_key = ?, pixabay_key = ?, groq_key = ?, groq_model = ?, "
            "coverr_key = ?, nvidia_key = ?, exa_key = ?, firecrawl_key = ? WHERE id = ?",
            (
                protect_secret(pexels),
                protect_secret(pixabay),
                protect_secret(groq),
                groq_model or "llama-3.3-70b-versatile",
                protect_secret(coverr),
                protect_secret(nvidia),
                protect_secret(exa),
                protect_secret(firecrawl),
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_kaggle_keys(user_id: int, kaggle_username: str, kaggle_token: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET kaggle_username = ?, kaggle_token = ? WHERE id = ?",
            (kaggle_username, protect_secret(kaggle_token), user_id),
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


def clear_kaggle_job(project_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE projects SET kaggle_dataset_slug = '', kaggle_kernel_slug = '', kaggle_status = '' WHERE id = ?",
            (project_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Jobs / operational history
# ----------------------------------------------------------------------------
ACTIVE_JOB_STATUSES = {"queued", "running", "canceling"}


def _job_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        data["result"] = json.loads(data.pop("result_json") or "{}")
    except json.JSONDecodeError:
        data["result"] = {}
    try:
        from services import ops_status

        return ops_status.decorate_job(data)
    except Exception:
        return data


def create_job(
    user_id: int,
    kind: str,
    project_id: Optional[int] = None,
    message: str = "",
    log_path: str = "",
) -> int:
    now = time.time()
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO jobs
               (user_id, project_id, kind, status, message, log_path, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (user_id, project_id, kind, message, log_path, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_job(
    job_id: int,
    status: Optional[str] = None,
    message: Optional[str] = None,
    detail: Optional[str] = None,
    result: Optional[dict] = None,
    error: Optional[str] = None,
    log_path: Optional[str] = None,
    finished: bool = False,
) -> None:
    fields: list[str] = ["updated_at = ?"]
    values: list[Any] = [time.time()]
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if message is not None:
        fields.append("message = ?")
        values.append(message)
    if detail is not None:
        fields.append("detail = ?")
        values.append(detail)
    if result is not None:
        fields.append("result_json = ?")
        values.append(json.dumps(result, ensure_ascii=False))
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if log_path is not None:
        fields.append("log_path = ?")
        values.append(log_path)
    if finished:
        fields.append("finished_at = ?")
        values.append(time.time())
    values.append(job_id)
    conn = _connect()
    try:
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def finish_job(job_id: int, message: str = "", result: Optional[dict] = None) -> None:
    update_job(job_id, status="complete", message=message, result=result or {}, error="", finished=True)


def fail_job(job_id: int, message: str, error: str = "") -> None:
    update_job(job_id, status="error", message=message, error=error or message, finished=True)


def request_job_cancel(job_id: int, user_id: int) -> Optional[dict]:
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            """UPDATE jobs
               SET status = 'canceling',
                   message = 'Parando tarefa...',
                   detail = 'Cancelamento solicitado pelo usuario',
                   updated_at = ?
               WHERE id = ? AND user_id = ? AND status IN ('queued', 'running')""",
            (now, job_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_job(job_id, user_id)


def cancel_job(job_id: int, message: str = "Tarefa cancelada") -> None:
    update_job(job_id, status="canceled", message=message, error="", finished=True)


def is_job_canceling(job_id: int) -> bool:
    conn = _connect()
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return bool(row and row["status"] == "canceling")
    finally:
        conn.close()


def fail_stale_jobs() -> int:
    """Marca como erro jobs 'queued'/'running' herdados de um processo anterior.

    Jobs rodam em BackgroundTasks do proprio processo; depois de um restart
    nenhum deles continua, entao nao podem ficar pendurados na UI.
    """
    now = time.time()
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE jobs
               SET status = 'error',
                   message = 'Interrompido por reinicio do servidor',
                   error = 'Interrompido por reinicio do servidor',
                   updated_at = ?, finished_at = ?
               WHERE status IN ('queued', 'running', 'canceling')""",
            (now, now),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def has_active_job(project_id: int, kind: Optional[str] = None) -> bool:
    """True se o projeto tem job 'queued'/'running' (opcionalmente do mesmo kind).

    Protege contra duplo clique: dois POSTs podem chegar antes de o status
    do projeto virar busy.
    """
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        sql = f"SELECT 1 FROM jobs WHERE project_id = ? AND status IN ({placeholders})"
        params: list[Any] = [project_id, *sorted(ACTIVE_JOB_STATUSES)]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return conn.execute(sql + " LIMIT 1", params).fetchone() is not None
    finally:
        conn.close()


def get_job(job_id: int, user_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
        return _job_to_dict(row) if row else None
    finally:
        conn.close()


def list_project_jobs(project_id: int, user_id: int, limit: int = 8) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE project_id = ? AND user_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (project_id, user_id, limit),
        ).fetchall()
        return [_job_to_dict(r) for r in rows]
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# API usage telemetry
# ----------------------------------------------------------------------------
def record_api_usage(
    user_id: Optional[int],
    project_id: Optional[int],
    job_id: Optional[int],
    provider: str,
    operation: str = "",
    status_code: Optional[int] = None,
    ok: bool = True,
    units: int = 1,
    latency_ms: float = 0,
    detail: str = "",
) -> None:
    provider = str(provider or "").strip().lower()
    if not provider or not user_id:
        return
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO api_usage
               (user_id, project_id, job_id, provider, operation, status_code,
                ok, units, latency_ms, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                project_id,
                job_id,
                provider[:40],
                str(operation or "")[:80],
                int(status_code or 0),
                1 if ok else 0,
                max(1, int(units or 1)),
                float(latency_ms or 0),
                str(detail or "")[:240],
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def api_usage_summary(user_id: int, project_id: Optional[int] = None, limit: int = 8) -> dict:
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        totals = conn.execute(
            f"""SELECT COUNT(*) AS calls,
                       COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures,
                       COALESCE(SUM(units), 0) AS units,
                       COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS avg_latency
                FROM api_usage
                WHERE {clause}""",
            params,
        ).fetchone()
        providers = conn.execute(
            f"""SELECT provider,
                       COUNT(*) AS calls,
                       COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures,
                       COALESCE(SUM(units), 0) AS units,
                       COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS avg_latency
                FROM api_usage
                WHERE {clause}
                GROUP BY provider
                ORDER BY calls DESC, provider
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        recent = conn.execute(
            f"""SELECT provider, operation, status_code, ok, units, latency_ms, detail, created_at
                FROM api_usage
                WHERE {clause}
                ORDER BY created_at DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        now = time.time()
        hourly_rows = conn.execute(
            f"""SELECT provider, COUNT(*) AS calls,
                       COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures
                FROM api_usage
                WHERE {clause} AND created_at >= ?
                GROUP BY provider ORDER BY calls DESC""",
            (*params, now - 3600),
        ).fetchall()
        daily_rows = conn.execute(
            f"""SELECT provider, COUNT(*) AS calls,
                       COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures
                FROM api_usage
                WHERE {clause} AND created_at >= ?
                GROUP BY provider ORDER BY calls DESC""",
            (*params, now - 86400),
        ).fetchall()
        hourly = {row["provider"]: int(row["calls"]) for row in hourly_rows}
        daily  = {row["provider"]: int(row["calls"]) for row in daily_rows}
        providers_enriched = []
        for row in providers:
            p = dict(row)
            p["calls_1h"]  = hourly.get(p["provider"], 0)
            p["calls_24h"] = daily.get(p["provider"], 0)
            providers_enriched.append(p)
        return {
            "total_calls": int(totals["calls"] or 0),
            "failed_calls": int(totals["failures"] or 0),
            "total_units": int(totals["units"] or 0),
            "avg_latency_ms": round(float(totals["avg_latency"] or 0), 1),
            "providers": providers_enriched,
            "recent": [dict(row) for row in recent],
        }
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


def set_project_config(project_id: int, config: dict) -> None:
    """Persiste o config_json completo do projeto (ex.: grava o video_theme)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE projects SET config_json = ? WHERE id = ?",
            (json.dumps(config, ensure_ascii=False), project_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_project_needs_package(project_id: int) -> None:
    conn = _connect()
    try:
        row = conn.execute("SELECT status FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return
        # 'needs_package' so faz sentido quando ja existe um pacote para invalidar;
        # antes disso (curadoria, revisao etc.) mudar assets nao deve mexer no status.
        if row["status"] not in {"packaged", "needs_package", "package_failed"}:
            return
        conn.execute(
            """UPDATE projects
               SET status = 'needs_package',
                   kaggle_dataset_slug = '',
                   kaggle_kernel_slug = '',
                   kaggle_status = ''
               WHERE id = ?""",
            (project_id,),
        )
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
                 narration, visual_goal, screen_mode, visual_need, visual_strategy, visual_target,
                 keywords_json, keyword_roles_json, query_ladder_json,
                 must_show_json, must_not_show_json,
                 asset_type, overlay_text, avatar_safe_area, part)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    s.get("screen_mode", ""),
                    float(s.get("visual_need") or 0),
                    s.get("visual_strategy", ""),
                    s.get("visual_target", ""),
                    json.dumps(s.get("keywords", []), ensure_ascii=False),
                    json.dumps(
                        s.get("keyword_roles") or scoring.assign_roles(s.get("keywords", [])),
                        ensure_ascii=False,
                    ),
                    json.dumps(s.get("query_ladder", s.get("keywords", [])), ensure_ascii=False),
                    json.dumps(s.get("must_show", []), ensure_ascii=False),
                    json.dumps(s.get("must_not_show", []), ensure_ascii=False),
                    s.get("asset_type", "video"),
                    s.get("overlay_text", ""),
                    s.get("avatar_safe_area", "right"),
                    int(s.get("part", 1) or 1),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _scene_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["keywords"] = json.loads(d.pop("keywords_json") or "[]")
    roles = json.loads(d.pop("keyword_roles_json", None) or "[]")
    # bancos antigos (sem a coluna preenchida) caem na derivação por posição
    if len(roles) != len(d["keywords"]):
        roles = scoring.assign_roles(d["keywords"])
    d["keyword_roles"] = roles
    d["query_ladder"] = json.loads(d.pop("query_ladder_json", None) or "[]")
    if not d["query_ladder"]:
        d["query_ladder"] = list(d["keywords"])
    d["must_show"] = json.loads(d.pop("must_show_json") or "[]")
    d["must_not_show"] = json.loads(d.pop("must_not_show_json") or "[]")
    d["broll_override"] = int(d.get("broll_override") or 0)
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


def update_scene_keywords(
    scene_db_id: int, keywords: list[str], roles: Optional[list[str]] = None
) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE scenes SET keywords_json = ?, keyword_roles_json = ?, query_ladder_json = ? WHERE id = ?",
            (
                json.dumps(keywords, ensure_ascii=False),
                json.dumps(roles or scoring.assign_roles(keywords), ensure_ascii=False),
                json.dumps(keywords, ensure_ascii=False),
                scene_db_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_scene_broll_override(scene_db_id: int, value: int) -> None:
    """Override manual de b-roll/avatar por cena: 0=auto, 1=forcar b-roll
    (cena sem avatar), -1=forcar avatar (cena sem b-roll)."""
    value = max(-1, min(1, int(value)))
    conn = _connect()
    try:
        conn.execute(
            "UPDATE scenes SET broll_override = ? WHERE id = ?", (value, scene_db_id)
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Assets
# ----------------------------------------------------------------------------
def _json_payload(value: Any) -> str:
    if value is None or value == "":
        return "{}"
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            return json.dumps({"raw": value}, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


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
                 width, height, duration, keyword, author, author_url, license, license_url,
                 attribution, discovery_provider, scrape_url, scrape_status,
                 query_role, query_text, provider_payload_json, confidence, state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
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
                    a.get("license", ""),
                    a.get("license_url", ""),
                    a.get("attribution", ""),
                    a.get("discovery_provider", ""),
                    a.get("scrape_url", ""),
                    a.get("scrape_status", ""),
                    a.get("query_role", ""),
                    a.get("query_text", a.get("keyword", "")),
                    _json_payload(a.get("provider_payload_json", a.get("provider_payload"))),
                    float(a.get("confidence") or 0),
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


def list_assets_for_project(project_id: int) -> dict[int, list[dict]]:
    """Todos os assets do projeto agrupados por scene_id, em uma unica query.

    Evita o N+1 (uma conexao/query por cena) na pagina do projeto.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT a.* FROM assets a
               JOIN scenes s ON a.scene_id = s.id
               WHERE s.project_id = ?
               ORDER BY a.scene_id, a.id""",
            (project_id,),
        ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for r in rows:
            grouped.setdefault(r["scene_id"], []).append(dict(r))
        return grouped
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


def get_asset_project(asset_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT p.id AS project_id, p.user_id AS user_id, s.id AS scene_id
               FROM assets a
               JOIN scenes s ON a.scene_id = s.id
               JOIN projects p ON s.project_id = p.id
               WHERE a.id = ?""",
            (asset_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_asset_state(
    asset_id: int,
    state: str,
    auto_score: Optional[float] = None,
    auto_reason: Optional[str] = None,
    review_round: Optional[int] = None,
    rejection_reason: Optional[str] = None,
) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT scene_id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            return None
        scene_id = row["scene_id"]
        # Uma cena tem apenas 1 take escolhido ('selected' ou 'accepted');
        # ao promover um, rebaixa os irmaos.
        if state in {"selected", "accepted"}:
            conn.execute(
                "UPDATE assets SET state = 'pending' "
                "WHERE scene_id = ? AND state IN ('selected', 'accepted') AND id != ?",
                (scene_id, asset_id),
            )
        fields = ["state = ?"]
        values: list[Any] = [state]
        if auto_score is not None:
            fields.append("auto_score = ?")
            values.append(auto_score)
        if auto_reason is not None:
            fields.append("auto_reason = ?")
            values.append(auto_reason)
        if review_round is not None:
            fields.append("review_round = ?")
            values.append(review_round)
        if state == "rejected":
            fields.append("rejection_reason = ?")
            values.append((rejection_reason or "").strip()[:80])
            fields.append("rejected_at = ?")
            values.append(time.time())
        elif state in {"pending", "selected", "accepted", "favorite"}:
            fields.append("rejection_reason = ''")
            fields.append("rejected_at = 0")
        values.append(asset_id)
        conn.execute(f"UPDATE assets SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        updated = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return dict(updated) if updated else None
    finally:
        conn.close()


def set_asset_vision(
    asset_id: int,
    score: float,
    verdict: str,
    reason: str,
    flags: list[str],
    provider: str,
) -> None:
    """Persiste o resultado da análise de visão de um asset (item de roadmap)."""
    conn = _connect()
    try:
        conn.execute(
            """UPDATE assets
               SET vision_score = ?, vision_verdict = ?, vision_reason = ?,
                   vision_flags_json = ?, vision_provider = ?, vision_analyzed = 1
               WHERE id = ?""",
            (
                float(score or 0),
                verdict or "",
                reason or "",
                json.dumps(flags or [], ensure_ascii=False),
                provider or "",
                asset_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def set_project_review_round(project_id: int, review_round: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE projects SET review_round = ? WHERE id = ?", (review_round, project_id)
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Render parts (modo video longo: 1 pacote + 1 kernel Kaggle por parte)
# ----------------------------------------------------------------------------
def replace_parts(project_id: int, parts: list[dict]) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM render_parts WHERE project_id = ?", (project_id,))
        now = time.time()
        for p in parts:
            conn.execute(
                """INSERT INTO render_parts
                   (project_id, part_idx, scene_count, duration, status, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (project_id, p["part_idx"], p.get("scene_count", 0), p.get("duration", 0), now),
            )
        conn.commit()
    finally:
        conn.close()


def list_parts(project_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM render_parts WHERE project_id = ? ORDER BY part_idx",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_part(project_id: int, part_idx: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM render_parts WHERE project_id = ? AND part_idx = ?",
            (project_id, part_idx),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_part(project_id: int, part_idx: int, **fields: Any) -> None:
    allowed = {"scene_count", "duration", "zip_name", "dataset_slug", "kernel_slug",
               "status", "curation_status", "video_path", "error"}
    sets = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"campo invalido para render_parts: {key}")
        sets.append(f"{key} = ?")
        values.append(value)
    sets.append("updated_at = ?")
    values.append(time.time())
    values.extend([project_id, part_idx])
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE render_parts SET {', '.join(sets)} WHERE project_id = ? AND part_idx = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def get_asset(asset_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
