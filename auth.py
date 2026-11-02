"""PTU 패스 인증: 학번 + 비밀번호, Flask 세션 쿠키.

API
- POST /api/auth/signup  {student_id, name, dept?, password}
- POST /api/auth/login   {student_id, password}   (데모 계정은 student_id="DEMO_TEAM")
- POST /api/auth/logout
- GET  /api/auth/me
"""
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, jsonify, request, session, g
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db, kst_now_str

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

STUDENT_ID_RE = re.compile(r"^\d{10}$")   # 평택대 학번: 10자리 숫자
MIN_PASSWORD_LEN = 8
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 10

# 존재하지 않는 학번으로 로그인할 때도 해시 비교 시간을 들여 응답 시간으로 계정 존재 여부가 드러나지 않게 함
_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))


def _utcnow():
    return datetime.now(timezone.utc)


def _public_user(row):
    return {
        "student_id": row["user_id"],
        "name": row["name"],
        "dept": row["dept"],
        "balance": row["wallet_balance"],
    }


def _err(status, code, message):
    return jsonify({"status": "FAILED", "code": code, "reason": message}), status


def current_user_id():
    return session.get("user_id")


def login_required(view):
    """로그인하지 않은 요청은 401로 막고, g.user_id에 현재 사용자 ID를 넣는다."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        uid = current_user_id()
        if not uid:
            return _err(401, "LOGIN_REQUIRED", "로그인이 필요합니다.")
        row = get_db().execute("SELECT 1 FROM USERS WHERE user_id = ?", (uid,)).fetchone()
        if row is None:          # 탈퇴·DB 초기화 등으로 사라진 계정의 세션
            session.clear()
            return _err(401, "LOGIN_REQUIRED", "로그인이 필요합니다.")
        g.user_id = uid
        return view(*args, **kwargs)
    return wrapper


@bp.post("/signup")
def signup():
    data = request.get_json(silent=True) or {}
    student_id = str(data.get("student_id", "")).strip()
    name = str(data.get("name", "")).strip()
    dept = str(data.get("dept", "")).strip() or None
    password = str(data.get("password", ""))

    if not STUDENT_ID_RE.match(student_id):
        return _err(400, "INVALID_STUDENT_ID", "학번은 10자리 숫자여야 합니다.")
    if not name:
        return _err(400, "INVALID_NAME", "이름을 입력해 주세요.")
    if len(password) < MIN_PASSWORD_LEN:
        return _err(400, "WEAK_PASSWORD", f"비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다.")

    conn = get_db()
    if conn.execute("SELECT 1 FROM USERS WHERE user_id = ?", (student_id,)).fetchone():
        return _err(409, "DUPLICATE", "이미 가입된 학번입니다.")

    conn.execute(
        "INSERT INTO USERS (user_id, name, dept, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (student_id, name, dept, generate_password_hash(password), kst_now_str()),
    )
    conn.commit()

    session.clear()
    session["user_id"] = student_id
    row = conn.execute("SELECT * FROM USERS WHERE user_id = ?", (student_id,)).fetchone()
    return jsonify({"status": "SUCCESS", "user": _public_user(row)}), 201


@bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    student_id = str(data.get("student_id", "")).strip()
    password = str(data.get("password", ""))

    conn = get_db()
    row = conn.execute("SELECT * FROM USERS WHERE user_id = ?", (student_id,)).fetchone()

    if row is None or row["password_hash"] is None:
        check_password_hash(_DUMMY_HASH, password)
        return _err(401, "INVALID_CREDENTIALS", "학번 또는 비밀번호가 올바르지 않습니다.")

    now = _utcnow()
    if row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
        return _err(423, "LOCKED", f"로그인 실패가 반복되어 {LOCK_MINUTES}분간 잠겼습니다. 잠시 후 다시 시도해 주세요.")

    if not check_password_hash(row["password_hash"], password):
        failed = (row["failed_logins"] or 0) + 1
        if failed >= MAX_FAILED_LOGINS:
            locked_until = (now + timedelta(minutes=LOCK_MINUTES)).isoformat()
            conn.execute("UPDATE USERS SET failed_logins = 0, locked_until = ? WHERE user_id = ?",
                         (locked_until, student_id))
            conn.commit()
            return _err(423, "LOCKED", f"로그인 실패가 반복되어 {LOCK_MINUTES}분간 잠겼습니다. 잠시 후 다시 시도해 주세요.")
        conn.execute("UPDATE USERS SET failed_logins = ? WHERE user_id = ?", (failed, student_id))
        conn.commit()
        return _err(401, "INVALID_CREDENTIALS", "학번 또는 비밀번호가 올바르지 않습니다.")

    conn.execute("UPDATE USERS SET failed_logins = 0, locked_until = NULL WHERE user_id = ?", (student_id,))
    conn.commit()

    session.clear()               # 세션 고정 공격 방지: 로그인 시 세션 새로 발급
    session["user_id"] = student_id
    return jsonify({"status": "SUCCESS", "user": _public_user(row)})


@bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"status": "SUCCESS"})


@bp.get("/me")
@login_required
def me():
    row = get_db().execute("SELECT * FROM USERS WHERE user_id = ?", (g.user_id,)).fetchone()
    return jsonify({"status": "SUCCESS", "user": _public_user(row)})


def _load_secret_key(app):
    """SECRET_KEY 환경변수 우선. 없으면 instance/secret_key 파일을 한 번 생성해 재사용
    (재시작·gunicorn 멀티 워커에서도 세션이 유지되도록)."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    os.makedirs(app.instance_path, exist_ok=True)
    path = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def init_app(app):
    if not app.config.get("SECRET_KEY"):   # Flask 기본값이 None이라 setdefault로는 안 채워짐
        app.config["SECRET_KEY"] = _load_secret_key(app)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=7))
    app.register_blueprint(bp)


def demo_password_hash():
    return generate_password_hash(os.environ.get("DEMO_PASSWORD", "demo1234"))
