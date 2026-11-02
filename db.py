"""PTU 패스 DB 계층.

- DB 경로: 환경변수 PTU_DB_PATH, 없으면 이 파일과 같은 폴더의 commute.db (실행 위치와 무관)
- 연결: 요청마다 하나의 연결을 flask.g에 두고 요청 종료 시 닫는다.
- 스키마: init_db()가 테이블 생성 + 구버전 DB 컬럼 보강(마이그레이션) + 데모 및 고정 학번 계정 시드를 수행한다.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import current_app, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.environ.get("PTU_DB_PATH", os.path.join(BASE_DIR, "commute.db"))

DEMO_USER_ID = "DEMO_TEAM"
DEMO_INITIAL = {"wallet_balance": 7500, "total_distance": 112.5, "early_count": 4}

# Render 서버가 재배포되어 DB가 초기화되더라도 항상 자동 등록되는 고정 학번 계정 목록
# (필요한 팀원 학번과 이름을 자유롭게 추가·수정하세요. 기본 비밀번호: 12345678)
FIXED_USERS = [
    {
        "user_id": "2026000001",
        "name": "시연 자료 팀",
        "dept": "정보통신학과",
        "password_plain": "12345678",
        "wallet_balance": 7500,
        "total_distance": 112.5,
        "early_count": 4,
    },
    {
        "user_id": "2021143028",
        "name": "시연 자료 팀",
        "dept": "정보통신학과",
        "password_plain": "12345678",
        "wallet_balance": 7500,
        "total_distance": 112.5,
        "early_count": 4,
    },
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS USERS (
    user_id              TEXT PRIMARY KEY,          -- 로그인 ID (학번)
    name                 TEXT NOT NULL,
    dept                 TEXT,
    password_hash        TEXT,
    wallet_balance       INTEGER NOT NULL DEFAULT 0,
    total_distance       REAL    NOT NULL DEFAULT 0,
    early_count          INTEGER NOT NULL DEFAULT 0,
    failed_logins        INTEGER NOT NULL DEFAULT 0,  -- 연속 로그인 실패 횟수
    locked_until         TEXT,                        -- 잠금 해제 시각 (UTC ISO)
    registered_device_id TEXT    NOT NULL DEFAULT '', -- 대리출석 방지용 단말기 UUID
    last_earn_date       TEXT    NOT NULL DEFAULT '', -- 1일 1회 중복 적립 제한용 날짜
    created_at           TEXT
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

# 구버전 commute.db를 그대로 쓰기 위한 컬럼 자동 보강 목록
_USER_COLUMN_PATCHES = {
    "password_hash": "ALTER TABLE USERS ADD COLUMN password_hash TEXT",
    "created_at": "ALTER TABLE USERS ADD COLUMN created_at TEXT",
    "failed_logins": "ALTER TABLE USERS ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0",
    "locked_until": "ALTER TABLE USERS ADD COLUMN locked_until TEXT",
    "registered_device_id": "ALTER TABLE USERS ADD COLUMN registered_device_id TEXT NOT NULL DEFAULT ''",
    "last_earn_date": "ALTER TABLE USERS ADD COLUMN last_earn_date TEXT NOT NULL DEFAULT ''",
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


def _make_password_hash(plain_pw, fallback_hash=None):
    """auth.py 또는 werkzeug.security의 해시 함수를 사용하여 비밀번호 해시를 생성한다."""
    try:
        import auth
        for fn_name in ("hash_password", "generate_password_hash", "_hash_password", "make_password_hash"):
            fn = getattr(auth, fn_name, None)
            if callable(fn):
                return fn(plain_pw)
    except Exception:
        pass
    try:
        from werkzeug.security import generate_password_hash
        return generate_password_hash(plain_pw)
    except Exception:
        return fallback_hash


def _seed_initial_logs(conn, user_id):
    """시연 시 원장 테이블이 비어 보이지 않도록 초기 샘플 로그 2건을 자동 삽입한다."""
    count = conn.execute("SELECT COUNT(*) FROM COMMUTE_LOGS WHERE user_id = ?", (user_id,)).fetchone()[0]
    if count == 0:
        conn.execute(
            """INSERT INTO COMMUTE_LOGS
               (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, "BLOCK_WALK", "평택대 제3기숙사 · 도보", 0.35, 3.8, "지급 거부",
             "1.0km 미만 단거리 도보 통학 레드존 및 결제 부재로 오용 차단", 0, "2026-10-29 08:15")
        )
        conn.execute(
            """INSERT INTO COMMUTE_LOGS
               (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, "EARN", "수원 영통역 · 광역버스", 28.4, 44.8, "정상 승인",
             "본인 단말기(UUID) 일치 · 결제 트랜잭션 확인 · 08시 이전 조기 도착 반영", 1920, "2026-10-30 07:38")
        )


def _seed_demo(conn, demo_password_hash=None):
    now_str = kst_now_str()

    # 1. [시연용 데모 계정으로 시작] 버튼용 기본 계정 시드
    conn.execute(
        """INSERT OR IGNORE INTO USERS
           (user_id, name, dept, wallet_balance, total_distance, early_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (DEMO_USER_ID, "시연 자료 팀", "융합복합시스템 연구반",
         DEMO_INITIAL["wallet_balance"], DEMO_INITIAL["total_distance"],
         DEMO_INITIAL["early_count"], now_str),
    )
    if demo_password_hash:
        conn.execute(
            "UPDATE USERS SET password_hash = ? WHERE user_id = ? AND password_hash IS NULL",
            (demo_password_hash, DEMO_USER_ID),
        )
    _seed_initial_logs(conn, DEMO_USER_ID)

    # 2. 10자리 학번 로그인용 고정 계정 시드 (서버 재배포 후에도 회원가입 없이 즉시 로그인 가능)
    for u in FIXED_USERS:
        pw_hash = _make_password_hash(u["password_plain"], fallback_hash=demo_password_hash)
        conn.execute(
            """INSERT OR IGNORE INTO USERS
               (user_id, name, dept, password_hash, wallet_balance, total_distance, early_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (u["user_id"], u["name"], u["dept"], pw_hash,
             u["wallet_balance"], u["total_distance"], u["early_count"], now_str),
        )
        _seed_initial_logs(conn, u["user_id"])


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
