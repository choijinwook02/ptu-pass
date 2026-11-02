"""2단계: 회원가입 / 로그인 / 로그아웃 / 내 정보."""
import pytest

import auth
import db

SID = "2026123456"
PW = "commute2026"


def signup(client, **over):
    body = {"student_id": SID, "name": "홍길동", "dept": "정보통신학과", "password": PW, **over}
    return client.post("/api/auth/signup", json=body)


def login(client, sid=SID, pw=PW):
    return client.post("/api/auth/login", json={"student_id": sid, "password": pw})


# ---------- 회원가입 ----------

def test_signup_creates_user_and_logs_in(client, raw_db):
    res = signup(client)
    assert res.status_code == 201
    assert res.get_json()["user"] == {"student_id": SID, "name": "홍길동",
                                      "dept": "정보통신학과", "balance": 0}
    assert client.get("/api/auth/me").get_json()["user"]["student_id"] == SID

    row = raw_db.execute("SELECT password_hash FROM USERS WHERE user_id = ?", (SID,)).fetchone()
    assert row["password_hash"] != PW and row["password_hash"].startswith(("scrypt:", "pbkdf2:"))


@pytest.mark.parametrize("sid", ["123456789", "12345678901", "20261234ab", "", "DEMO_TEAM"])
def test_signup_rejects_bad_student_id(client, sid):
    res = signup(client, student_id=sid)
    assert res.status_code == 400 and res.get_json()["code"] == "INVALID_STUDENT_ID"


def test_signup_rejects_short_password_and_empty_name(client):
    assert signup(client, password="1234567").get_json()["code"] == "WEAK_PASSWORD"
    assert signup(client, name="  ").get_json()["code"] == "INVALID_NAME"


def test_signup_duplicate(client):
    signup(client)
    res = signup(client, name="다른사람")
    assert res.status_code == 409 and res.get_json()["code"] == "DUPLICATE"


# ---------- 로그인 / 로그아웃 ----------

def test_login_logout_flow(app):
    signup(app.test_client())
    c = app.test_client()                    # 새 브라우저
    assert c.get("/api/auth/me").status_code == 401
    assert login(c).status_code == 200
    assert c.get("/api/auth/me").get_json()["user"]["name"] == "홍길동"
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401


def test_wrong_password_and_unknown_user_same_message(client):
    signup(client)
    wrong = login(client, pw="nope-nope")
    unknown = login(client, sid="2099000000")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json()["reason"] == unknown.get_json()["reason"]


def test_lockout_after_repeated_failures(client, raw_db):
    signup(client)
    for _ in range(auth.MAX_FAILED_LOGINS - 1):
        assert login(client, pw="wrong-pass").status_code == 401
    assert login(client, pw="wrong-pass").status_code == 423
    assert login(client).status_code == 423          # 잠금 중엔 맞는 비번도 거부

    raw_db.execute("UPDATE USERS SET locked_until = '2000-01-01T00:00:00+00:00' WHERE user_id = ?", (SID,))
    raw_db.commit()                                   # 잠금 시간 경과
    assert login(client).status_code == 200


def test_success_resets_failure_count(client, raw_db):
    signup(client)
    for _ in range(auth.MAX_FAILED_LOGINS - 1):
        login(client, pw="wrong-pass")
    assert login(client).status_code == 200
    row = raw_db.execute("SELECT failed_logins FROM USERS WHERE user_id = ?", (SID,)).fetchone()
    assert row["failed_logins"] == 0


def test_session_for_deleted_user_is_rejected(client, raw_db):
    signup(client)
    raw_db.execute("DELETE FROM USERS WHERE user_id = ?", (SID,))
    raw_db.commit()
    assert client.get("/api/auth/me").status_code == 401


def test_session_cookie_is_httponly(client):
    res = signup(client)
    cookie = res.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


# ---------- 데모 계정 ----------

def test_demo_account_login(client):
    res = login(client, sid=db.DEMO_USER_ID, pw="demo1234")
    assert res.status_code == 200
    assert res.get_json()["user"]["balance"] == 7500


def test_demo_password_from_env(monkeypatch, db_path, app):
    monkeypatch.setenv("DEMO_PASSWORD", "presentation!")
    # 비밀번호가 비어 있는 상태에서 init → 환경변수 값이 적용되어야 함
    conn = db.connect(db_path)
    conn.execute("UPDATE USERS SET password_hash = NULL WHERE user_id = ?", (db.DEMO_USER_ID,))
    conn.commit()
    conn.close()
    db.init_db(db_path, demo_password_hash=auth.demo_password_hash())
    c = app.test_client()
    assert login(c, sid=db.DEMO_USER_ID, pw="demo1234").status_code == 401
    assert login(c, sid=db.DEMO_USER_ID, pw="presentation!").status_code == 200
