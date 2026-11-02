"""PTU 패스 DB 계층.

- DB 경로: 환경변수 PTU_DB_PATH, 없으면 이 파일과 같은 폴더의 commute.db (실행 위치와 무관)
- 연결: 요청마다 하나의 연결을 flask.g에 두고 요청 종료 시 닫는다.
- 스키마: init_db()가 테이블 생성 + 구버전 DB 컬럼 보강(마이그레이션) + 데모 계정 시드를 수행한다.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import current_app, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.environ.get("PTU_DB_PATH", os.path.join(BASE_DIR, "commute.db"))

DEMO_USER_ID = "DEMO_TEAM"
DEMO_INITIAL = {"wallet_balance": 7500, "total_distance": 112.5, "early_count": 4}

SCHEMA = """
CREATE TABLE IF NOT EXISTS USERS (
    user_id        TEXT PRIMARY KEY,          -- 로그인 ID (학번)
    name           TEXT NOT NULL,
    dept           TEXT,
    password_hash  TEXT,
    wallet_balance INTEGER NOT NULL DEFAULT 0,
    total_distance REAL    NOT NULL DEFAULT 0,
    early_count    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS COMMUTE_LOGS (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL REFERENCES USERS(user_id) ON DELETE CASCADE,
    log_type     TEXT NOT NULL,
    origin_label TEXT,
    distance_km  REAL,
    speed_kmh    REAL,
    status       TEXT,
    description  TEXT,
    points       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_logs_user ON COMMUTE_LOGS(user_id, id);
"""

# 구버전 commute.db(USERS에 password_hash/created_at 없음)를 그대로 쓰기 위한 컬럼 보강 목록
_USER_COLUMN_PATCHES = {
    "password_hash": "ALTER TABLE USERS ADD COLUMN password_hash TEXT",
    "created_at": "ALTER TABLE USERS ADD COLUMN created_at TEXT",
}


def kst_now():
    return datetime.now(timezone.utc) + timedelta(hours=9)


def kst_now_str():
    return kst_now().strftime("%Y-%m-%d %H:%M")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """현재 요청의 DB 연결 (없으면 생성)."""
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _migrate(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(USERS)")}
    for col, ddl in _USER_COLUMN_PATCHES.items():
        if col not in existing:
            conn.execute(ddl)


def _seed_demo(conn, demo_password_hash=None):
    conn.execute(
        """INSERT OR IGNORE INTO USERS
           (user_id, name, dept, wallet_balance, total_distance, early_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (DEMO_USER_ID, "시연 자료 팀", "융합복합시스템 연구반",
         DEMO_INITIAL["wallet_balance"], DEMO_INITIAL["total_distance"],
         DEMO_INITIAL["early_count"], kst_now_str()),
    )
    if demo_password_hash:
        # 데모 계정 비밀번호가 아직 없을 때만 설정 (팀원이 바꾼 값은 덮어쓰지 않음)
        conn.execute(
            "UPDATE USERS SET password_hash = ? WHERE user_id = ? AND password_hash IS NULL",
            (demo_password_hash, DEMO_USER_ID),
        )


def init_db(path, demo_password_hash=None):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed_demo(conn, demo_password_hash)
        conn.commit()
    finally:
        conn.close()


def init_app(app):
    app.config.setdefault("DB_PATH", DEFAULT_DB_PATH)
    app.teardown_appcontext(close_db)
