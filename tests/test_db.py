"""1단계: DB 계층 분리 — 스키마/마이그레이션/경로 + 기존 API 동작 유지(회귀) 검증."""
import os
import sqlite3

import db

# 평택역 광역환승센터 → 평택대 정문 (약 4.2km)
PYEONGTAEK_STN = {"start_lat": 36.9907, "start_lon": 127.0851}


# ---------- 스키마 / 시드 ----------

def test_fresh_db_has_tables_and_index(raw_db):
    names = {r["name"] for r in raw_db.execute("SELECT name FROM sqlite_master")}
    assert {"USERS", "COMMUTE_LOGS", "idx_logs_user"} <= names


def test_demo_account_seeded(raw_db):
    row = raw_db.execute("SELECT * FROM USERS WHERE user_id = ?", (db.DEMO_USER_ID,)).fetchone()
    assert row["wallet_balance"] == 7500
    assert row["total_distance"] == 112.5
    assert row["early_count"] == 4


def test_new_user_defaults_are_zero(raw_db):
    raw_db.execute("INSERT INTO USERS (user_id, name) VALUES ('20261234', '홍길동')")
    row = raw_db.execute("SELECT * FROM USERS WHERE user_id = '20261234'").fetchone()
    assert (row["wallet_balance"], row["total_distance"], row["early_count"]) == (0, 0, 0)


def test_init_db_is_idempotent_and_keeps_data(db_path, raw_db):
    raw_db.execute("UPDATE USERS SET wallet_balance = 999 WHERE user_id = ?", (db.DEMO_USER_ID,))
    raw_db.commit()
    db.init_db(db_path)
    db.init_db(db_path)
    row = raw_db.execute("SELECT wallet_balance FROM USERS WHERE user_id = ?", (db.DEMO_USER_ID,)).fetchone()
    assert row["wallet_balance"] == 999
    assert raw_db.execute("SELECT COUNT(*) FROM USERS").fetchone()[0] == 1


def test_foreign_key_rejects_log_for_unknown_user(raw_db):
    try:
        raw_db.execute("INSERT INTO COMMUTE_LOGS (user_id, log_type) VALUES ('NOBODY', 'EARN')")
        assert False, "FK 위반이 통과됨"
    except sqlite3.IntegrityError:
        pass


# ---------- 마이그레이션 (기존 레포의 구버전 commute.db) ----------

def test_migrates_legacy_schema(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE USERS (user_id TEXT PRIMARY KEY, name TEXT, dept TEXT,
            wallet_balance INTEGER DEFAULT 7500, total_distance REAL DEFAULT 112.5,
            early_count INTEGER DEFAULT 4);
        CREATE TABLE COMMUTE_LOGS (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
            log_type TEXT, origin_label TEXT, distance_km REAL, speed_kmh REAL,
            status TEXT, description TEXT, points INTEGER, created_at TEXT);
        INSERT INTO USERS VALUES ('DEMO_TEAM', '시연 자료 팀', 'x', 8200, 120.0, 5);
        INSERT INTO COMMUTE_LOGS (user_id, log_type, points) VALUES ('DEMO_TEAM', 'EARN', 700);
    """)
    conn.commit()
    conn.close()

    db.init_db(path, demo_password_hash="HASHED")

    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(USERS)")}
    assert {"password_hash", "created_at"} <= cols
    row = conn.execute("SELECT * FROM USERS WHERE user_id = 'DEMO_TEAM'").fetchone()
    assert row["wallet_balance"] == 8200          # 기존 잔액 보존
    assert row["password_hash"] == "HASHED"       # 비밀번호 없던 데모 계정에만 설정
    assert conn.execute("SELECT COUNT(*) FROM COMMUTE_LOGS").fetchone()[0] == 1
    conn.close()


def test_demo_password_not_overwritten(db_path, raw_db):
    db.init_db(db_path, demo_password_hash="FIRST")
    db.init_db(db_path, demo_password_hash="SECOND")
    row = raw_db.execute("SELECT password_hash FROM USERS WHERE user_id = ?", (db.DEMO_USER_ID,)).fetchone()
    assert row["password_hash"] == "FIRST"


def test_default_db_path_is_absolute():
    assert os.path.isabs(db.DEFAULT_DB_PATH)


# ---------- 기존 API 회귀 ----------

def test_wallet(client):
    data = client.get("/api/wallet").get_json()
    assert data["balance"] == 7500 and data["name"] == "시연 자료 팀"


def test_verify_approved_updates_balance_and_log(client):
    res = client.post("/api/verify", json={**PYEONGTAEK_STN, "speed_kmh": 30,
                                            "has_payment": True, "is_early": True}).get_json()
    assert res["status"] == "APPROVED"
    assert res["new_balance"] == 7500 + res["earned_points"]
    logs = client.get("/api/logs").get_json()["logs"]
    assert logs[0]["log_type"] == "EARN" and logs[0]["points"] == res["earned_points"]


def test_verify_rejections(client):
    spoof = client.post("/api/verify", json={**PYEONGTAEK_STN, "speed_kmh": 300, "has_payment": True}).get_json()
    nopay = client.post("/api/verify", json={**PYEONGTAEK_STN, "speed_kmh": 30, "has_payment": False}).get_json()
    walk = client.post("/api/verify", json={"start_lat": 36.9962, "start_lon": 127.1329,
                                            "speed_kmh": 30, "has_payment": True}).get_json()
    assert spoof["badge"] == "스푸핑 차단"
    assert nopay["status"] == "REJECTED"
    assert walk["status"] == "REJECTED" and "1.0km" in walk["reason"]
    assert client.get("/api/wallet").get_json()["balance"] == 7500


def test_exchange_and_reset(client):
    ok = client.post("/api/exchange", json={"item_name": "카페", "cost": 3000})
    assert ok.get_json()["new_balance"] == 4500
    fail = client.post("/api/exchange", json={"item_name": "식권", "cost": 6500})
    assert fail.status_code == 400
    client.post("/api/reset")
    assert client.get("/api/wallet").get_json()["balance"] == 7500
    assert client.get("/api/logs").get_json()["logs"] == []
