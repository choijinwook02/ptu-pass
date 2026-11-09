import os
import math
import random
import sqlite3
from flask import Flask, request, jsonify, send_from_directory, g, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash

import auth
from auth import login_required
import db
from db import get_db, kst_now as get_kst_now, DEMO_USER_ID, DEMO_INITIAL

app = Flask(__name__, static_folder='static')
CORS(app)
db.init_app(app)
auth.init_app(app)

# 서버가 절전(Sleep) 후 재기동되어도 로그인 세션 쿠키가 풀리지 않도록 고정 키 설정
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'ptu-commute-pass-fixed-secret-key-2026')

PTU_GATE_LAT = 36.9961
PTU_GATE_LON = 127.1328

# 운영자 대시보드에서 실시간으로 변경 가능한 교차 검증 정책 파라미터
POLICY_CONFIG = {
    "min_distance_km": 1.0,
    "min_speed_kmh": 5.0,
    "max_speed_kmh": 150.0,
    "base_reward_p": 300,
    "early_bird_cutoff_hour": 8,
    "early_bird_bonus_p": 200,
    "distance_rate_p_per_km": 50
}

COUPON_CATALOG = [
    {"id": "CAFE_3000", "name": "교내 카페 3,000원 음료 교환권", "cost": 3000, "category": "CAFE"},
    {"id": "MEAL_6500", "name": "학생식당 1식 식권 (6,500원)", "cost": 6500, "category": "FOOD"},
    {"id": "PRINT_1500", "name": "학술정보관 인쇄·출력 50장권", "cost": 1500, "category": "STUDY"},
]
COUPONS_BY_ID = {c["id"]: c for c in COUPON_CATALOG}

db.init_db(app.config['DB_PATH'], demo_password_hash=auth.demo_password_hash())

# 서버가 잠들었다 깨어나도(Sleep -> Wake) 기본 학번 계정과 보안 컬럼이 항상 유지되도록 보장
def ensure_security_columns(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for col_sql in [
        "ALTER TABLE USERS ADD COLUMN registered_device_id TEXT DEFAULT ''",
        "ALTER TABLE USERS ADD COLUMN last_earn_date TEXT DEFAULT ''"
    ]:
        try:
            cursor.execute(col_sql)
        except sqlite3.OperationalError:
            pass

    now_str = get_kst_now().strftime("%Y-%m-%d %H:%M")
    default_pw_hash = generate_password_hash("12345678")
    permanent_users = [
        ("2021143024", "서우림", "정보통신학과", default_pw_hash, 7500, 112.5, 4, now_str),
        ("2026000001", "시연 자료 팀", "정보통신학과", default_pw_hash, 7500, 112.5, 4, now_str),
        ("9999999999", "캠퍼스 관리자", "운영관리팀", default_pw_hash, 15000, 140.0, 6, now_str),
    ]
    for u in permanent_users:
        cursor.execute(
            """INSERT OR IGNORE INTO USERS
               (user_id, name, dept, password_hash, wallet_balance, total_distance, early_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            u
        )
    conn.commit()
    conn.close()

ensure_security_columns(app.config['DB_PATH'])

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# [API 1] 메인 웹페이지 렌더링
@app.route('/')
def home():
    return send_from_directory('static', 'index.html')

# [API 2] 서버 상태 및 KST 시각 헬스체크 (Keep-Alive)
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ONLINE",
        "team": "시연 자료 팀",
        "kst_time": get_kst_now().strftime("%Y-%m-%d %H:%M:%S"),
        "engine_version": "v2.7-TDM-Admin-Suite"
    })

# [API 3] 전자지갑 잔액, 누적 통학 실적 및 기기·적립일 상태 조회
@app.route('/api/wallet', methods=['GET'])
@login_required
def get_wallet():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, dept, wallet_balance, total_distance, early_count, registered_device_id, last_earn_date FROM USERS WHERE user_id = ?",
        (g.user_id,)
    )
    row = cursor.fetchone()
    return jsonify({
        "name": row[0],
        "dept": row[1],
        "balance": row[2],
        "total_distance": round(row[3], 1),
        "early_count": row[4],
        "registered_device_id": row[5] or "",
        "last_earn_date": row[6] or ""
    })

# [API 4] 실시간 GPS·결제·기기식별·1일1회 교차 검증 및 마일리지 적립
@app.route('/api/verify', methods=['POST'])
@login_required
def verify_commute():
    data = request.json or {}
    origin_label = data.get('origin_label', '실제 단말기 GPS')
    start_lat = float(data.get('start_lat', 0))
    start_lon = float(data.get('start_lon', 0))
    end_lat = float(data.get('end_lat', PTU_GATE_LAT))
    end_lon = float(data.get('end_lon', PTU_GATE_LON))
    duration_minutes = float(data.get('duration_minutes', 38))
    has_payment = data.get('has_payment', False)
    client_device_id = data.get('device_id', 'PTU-DEV-DEFAULT')
    enforce_daily_limit = data.get('enforce_daily_limit', True)

    kst_now = get_kst_now()
    today_str = kst_now.strftime("%Y-%m-%d")
    cutoff_hr = int(POLICY_CONFIG["early_bird_cutoff_hour"])
    is_early_mode = data.get('is_early', kst_now.hour < cutoff_hr)

    distance_km = calculate_distance(start_lat, start_lon, end_lat, end_lon)
    speed_kmh = float(data.get('speed_kmh', (distance_km / (duration_minutes / 60)) if duration_minutes > 0 else 0))

    min_dist = float(POLICY_CONFIG["min_distance_km"])
    min_spd = float(POLICY_CONFIG["min_speed_kmh"])
    max_spd = float(POLICY_CONFIG["max_speed_kmh"])

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT registered_device_id, last_earn_date FROM USERS WHERE user_id = ?", (g.user_id,))
    user_row = cursor.fetchone()
    reg_device_id = (user_row[0] or "") if user_row else ""
    last_earn_date = (user_row[1] or "") if user_row else ""

    # 최초 인증 시 로그인한 사용자의 본인 단말기 UUID 자동 등록
    if not reg_device_id and not client_device_id.startswith("UNREGISTERED"):
        reg_device_id = client_device_id
        cursor.execute("UPDATE USERS SET registered_device_id = ? WHERE user_id = ?", (reg_device_id, g.user_id))
        conn.commit()

    # 0-1. 대리 출석 방지용 기기 식별(Device Binding) 불일치 차단
    if client_device_id.startswith("UNREGISTERED") or (reg_device_id and client_device_id != reg_device_id):
        reason = f"등록된 본인 단말기({reg_device_id or '본인 기기'})와 기기 식별값({client_device_id})이 불일치하여 대리 출석으로 차단되었습니다."
        cursor.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (g.user_id, 'BLOCK_DEVICE', origin_label, round(distance_km, 2), round(speed_kmh, 1), '대리출석 차단', reason, 0, kst_now.strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        return jsonify({"status": "REJECTED", "badge": "대리출석 차단", "reason": reason})

    # 0-2. 1일 1회 중복 적립 제한 차단
    if enforce_daily_limit and last_earn_date == today_str:
        reason = f"금일({today_str}) 이미 통학 마일리지가 적립되었습니다. (1일 1회 중복 수급 제한)"
        cursor.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (g.user_id, 'BLOCK_DUPLICATE', origin_label, round(distance_km, 2), round(speed_kmh, 1), '중복적립 차단', reason, 0, kst_now.strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        return jsonify({"status": "REJECTED", "badge": "중복적립 차단", "reason": reason})

    # 1. GPS 스푸핑(위치 변조) 차단
    if speed_kmh > max_spd:
        reason = f"비정상 이동 속도({round(speed_kmh, 1)}km/h > 상한 {max_spd}km/h) 감지 — GPS 스푸핑 차단"
        cursor.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (g.user_id, 'BLOCK_SPOOF', origin_label, round(distance_km, 2), round(speed_kmh, 1), '스푸핑 차단', reason, 0, kst_now.strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        return jsonify({"status": "REJECTED", "badge": "스푸핑 차단", "reason": reason})

    # 2. 대중교통 결제 내역 미존재 차단
    if not has_payment:
        reason = "대중교통 결제 트랜잭션 부재 (도보 이동 또는 카드 미태그 오용 차단)"
        cursor.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (g.user_id, 'BLOCK_NOPAY', origin_label, round(distance_km, 2), round(speed_kmh, 1), '지급 거부', reason, 0, kst_now.strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        return jsonify({"status": "REJECTED", "badge": "지급 거부", "reason": reason})

    # 3. 단거리 도보 통학 차단
    if speed_kmh < min_spd or distance_km < min_dist:
        reason = f"평택대 정문까지 {round(distance_km, 2)}km ({min_dist}km 미만 기숙사·인근 도보 통학 레드존 차단)"
        cursor.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (g.user_id, 'BLOCK_WALK', origin_label, round(distance_km, 2), round(speed_kmh, 1), '지급 거부', reason, 0, kst_now.strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        return jsonify({"status": "REJECTED", "badge": "지급 거부", "reason": reason})

    # 4. 정상 승인 및 동적 마일리지 산출 (운영자 설정 정책값 연동)
    base_reward = int(POLICY_CONFIG["base_reward_p"])
    early_bonus = int(POLICY_CONFIG["early_bird_bonus_p"]) if is_early_mode else 0
    dist_rate = int(POLICY_CONFIG["distance_rate_p_per_km"])
    distance_reward = int(round(distance_km * dist_rate))
    total_reward = base_reward + early_bonus + distance_reward
    reason = f"본인 기기({reg_device_id}) · 실제 거리 {round(distance_km, 2)}km · 대중교통 결제 교차 검증 일치"

    cursor.execute(
        "UPDATE USERS SET wallet_balance = wallet_balance + ?, total_distance = total_distance + ?, early_count = early_count + ?, last_earn_date = ? WHERE user_id = ?",
        (total_reward, round(distance_km, 2), 1 if is_early_mode else 0, today_str, g.user_id)
    )
    cursor.execute("SELECT wallet_balance FROM USERS WHERE user_id = ?", (g.user_id,))
    new_balance = cursor.fetchone()[0]

    cursor.execute(
        "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (g.user_id, 'EARN', origin_label, round(distance_km, 2), round(speed_kmh, 1), '정상 승인', reason, total_reward, kst_now.strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()

    return jsonify({
        "status": "APPROVED",
        "distance": round(distance_km, 2),
        "speed": round(speed_kmh, 1),
        "base_reward": base_reward,
        "early_bonus": early_bonus,
        "distance_reward": distance_reward,
        "earned_points": total_reward,
        "new_balance": new_balance,
        "last_earn_date": today_str,
        "reason": reason
    })

# [API 5] 교내 사용처 모바일 쿠폰 발급 및 포인트 차감
@app.route('/api/exchange', methods=['POST'])
@login_required
def exchange_coupon():
    data = request.get_json(silent=True) or {}
    coupon = COUPONS_BY_ID.get(data.get('coupon_id'))
    if coupon is None:
        return jsonify({"status": "FAILED", "reason": "존재하지 않는 쿠폰입니다."}), 400
    cost = coupon["cost"]

    conn = get_db()
    updated = conn.execute(
        "UPDATE USERS SET wallet_balance = wallet_balance - ? WHERE user_id = ? AND wallet_balance >= ?",
        (cost, g.user_id, cost),
    ).rowcount
    balance = conn.execute("SELECT wallet_balance FROM USERS WHERE user_id = ?", (g.user_id,)).fetchone()[0]
    if updated == 0:
        conn.rollback()
        return jsonify({"status": "FAILED", "reason": "보유 마일리지가 부족합니다.", "balance": balance}), 400

    conn.execute(
        "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (g.user_id, 'SPEND', '교내 제휴처', 0, 0, '사용 완료', f'{coupon["name"]} 발급', -cost, get_kst_now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()

    return jsonify({
        "status": "SUCCESS",
        "coupon_id": coupon["id"],
        "item_name": coupon["name"],
        "deducted_points": cost,
        "new_balance": balance,
        "voucher_code": f"PTU-{random.randint(1000,9999)}-{random.randint(1000,9999)}"
    })

# [API 6] 발표 리허설용 데이터 초기화 (7,500P 및 1일 1회 적립 제한 리셋)
@app.route('/api/reset', methods=['POST'])
@login_required
def reset_wallet():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE USERS SET wallet_balance = ?, total_distance = ?, early_count = ?, last_earn_date = '' WHERE user_id = ?",
        (DEMO_INITIAL["wallet_balance"], DEMO_INITIAL["total_distance"], DEMO_INITIAL["early_count"], g.user_id)
    )
    cursor.execute("DELETE FROM COMMUTE_LOGS WHERE user_id = ?", (g.user_id,))
    conn.commit()
    return jsonify({"status": "RESET_COMPLETE", "new_balance": 7500})

# [API 7] 실시간 검증 및 입출금 로그 조회
@app.route('/api/logs', methods=['GET'])
@login_required
def get_logs():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at FROM COMMUTE_LOGS WHERE user_id = ? ORDER BY id DESC LIMIT 15", (g.user_id,))
    rows = cursor.fetchall()
    logs = [{
        "log_type": r[0], "origin_label": r[1], "distance_km": r[2],
        "speed_kmh": r[3], "status": r[4], "description": r[5], "points": r[6], "created_at": r[7]
    } for r in rows]
    return jsonify({"logs": logs})

# [API 8] 시간대별 통학 인원 분산 통계 (Chart.js 연동용)
@app.route('/api/stats/hourly', methods=['GET'])
def get_hourly_stats():
    return jsonify({
        "labels": ["07:00~07:30", "07:30~08:00", "08:00~08:30 (피크)", "08:30~09:00 (피크)", "09:00~09:30", "09:30~10:00"],
        "before_tdm": [85, 140, 420, 510, 230, 115],
        "after_tdm": [190, 310, 295, 345, 225, 135],
        "peak_reduction_rate": 28.4
    })

# [API 9] 오용 탐지 엔진 유형별 차단 통계
@app.route('/api/stats/fraud', methods=['GET'])
def get_fraud_stats():
    return jsonify({
        "labels": ["정상 대중교통 승인", "1km 미만 도보 차단", "결제 미태그 차단", "대리출석·중복·스푸핑 차단"],
        "counts": [142, 34, 16, 8],
        "block_accuracy": 98.5
    })

# [API 10] 실용적 통학 편익 지표
@app.route('/api/stats/benefit', methods=['GET'])
@login_required
def get_practical_benefit():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT wallet_balance, total_distance, early_count FROM USERS WHERE user_id = ?", (g.user_id,))
    row = cursor.fetchone()
    balance = row[0] if row else 7500
    total_km = row[1] if row else 112.5
    early_cnt = row[2] if row else 4
    return jsonify({
        "wait_time_saved_min": 14.5,
        "monthly_welfare_krw": 46500,
        "current_refund_value_krw": balance,
        "total_distance_km": round(total_km, 1),
        "early_commute_count": early_cnt
    })

# [API 11] 학과별 조기 통학 참여 및 마일리지 리더보드
@app.route('/api/leaderboard/departments', methods=['GET'])
def get_department_leaderboard():
    return jsonify({
        "departments": [
            {"rank": 1, "dept": "융합소프트웨어학과", "participants": 218, "total_points": 482500, "avg_wait_saved": "15.2분"},
            {"rank": 2, "dept": "정보통신학과", "participants": 195, "total_points": 451200, "avg_wait_saved": "14.5분"},
            {"rank": 3, "dept": "스마트모빌리티학과", "participants": 184, "total_points": 398000, "avg_wait_saved": "13.8분"},
            {"rank": 4, "dept": "국제물류학과", "participants": 156, "total_points": 342100, "avg_wait_saved": "12.9분"},
            {"rank": 5, "dept": "간호학과", "participants": 142, "total_points": 289400, "avg_wait_saved": "11.5분"}
        ]
    })

# [API 12] 주요 대중교통 환승 거점 및 캠퍼스 정류장 좌표
@app.route('/api/transit/stops', methods=['GET'])
def get_transit_stops():
    return jsonify({
        "campus_gate": {"name": "평택대학교 정문 (기준점)", "lat": PTU_GATE_LAT, "lon": PTU_GATE_LON, "walk_limit_km": POLICY_CONFIG["min_distance_km"]},
        "hubs": [
            {"name": "평택역 광역환승센터", "lat": 36.9907, "lon": 127.0851, "line": "1호선 / 시내버스 20·50번"},
            {"name": "평택지제역 SRT·1호선", "lat": 37.0188, "lon": 127.0704, "line": "SRT / 광역·시내 노선"},
            {"name": "안성IC 광역정류장", "lat": 36.9945, "lon": 127.1485, "line": "광역버스 5300·8201번"},
            {"name": "수원 영통역 광역정류장", "lat": 37.2519, "lon": 127.0713, "line": "광역버스 직행 노선"}
        ]
    })

# [API 13] 200명 가상 통학 시나리오 배치 시뮬레이터
@app.route('/api/simulate/batch', methods=['POST'])
def simulate_batch():
    total_samples = 200
    approved = random.randint(138, 146)
    walk_blocked = random.randint(28, 35)
    nopay_blocked = random.randint(12, 18)
    spoof_blocked = total_samples - (approved + walk_blocked + nopay_blocked)
    return jsonify({
        "total_samples": total_samples,
        "approved_count": approved,
        "walk_blocked": walk_blocked,
        "nopay_blocked": nopay_blocked,
        "spoof_blocked": spoof_blocked,
        "fraud_detection_rate": round(((walk_blocked + nopay_blocked + spoof_blocked) / (total_samples - approved)) * 100, 1),
        "peak_dispersion_effect": round(random.uniform(26.8, 29.5), 1),
        "wait_time_saved_min": round(random.uniform(13.8, 15.6), 1)
    })

# [API 14] 교내 제휴 상점 카탈로그 조회
@app.route('/api/coupons/catalog', methods=['GET'])
def get_coupons_catalog():
    return jsonify({"coupons": COUPON_CATALOG})

# [API 15] 교차 검증 엔진 임계값 파라미터 명세
@app.route('/api/system/spec', methods=['GET'])
def get_system_spec():
    return jsonify({
        **POLICY_CONFIG,
        "daily_limit": "1일 1회 적립 제한",
        "device_binding": "UUID 단말기 고유 식별 대리출석 방지"
    })

# ================= [신설] 서버 절전(Sleep) 후 계정 자동 복구 API =================
# [API 16] 클라이언트 로컬 백업 기반 회원 정보 자동 복원 (Render 무료 서버 초기화 대응)
@app.route('/api/auth/restore', methods=['POST'])
def restore_user_account():
    data = request.get_json(silent=True) or {}
    student_id = str(data.get("student_id", "")).strip()
    password = str(data.get("password", ""))
    name = str(data.get("name", "학우")).strip() or "학우"
    dept = str(data.get("dept", "정보통신학과")).strip() or "정보통신학과"
    balance = int(data.get("balance", 0))
    total_distance = float(data.get("total_distance", 0.0))

    if not student_id or len(password) < 8:
        return jsonify({"status": "FAILED", "reason": "복구 정보가 올바르지 않습니다."}), 400

    conn = get_db()
    existing = conn.execute("SELECT * FROM USERS WHERE user_id = ?", (student_id,)).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO USERS (user_id, name, dept, password_hash, wallet_balance, total_distance, early_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (student_id, name, dept, generate_password_hash(password), balance, total_distance, 1, get_kst_now().strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
    session.clear()
    session["user_id"] = student_id
    row = conn.execute("SELECT * FROM USERS WHERE user_id = ?", (student_id,)).fetchone()
    return jsonify({
        "status": "RESTORED",
        "user": {
            "student_id": row["user_id"],
            "name": row["name"],
            "dept": row["dept"],
            "balance": row["wallet_balance"]
        }
    })

# ================= [신설] 운영자 관제 대시보드 전용 REST API =================
# [API 17] 운영자 통합 관제 현황 (전체 회원·전체 로그·KPI·정책값 조회)
@app.route('/api/admin/overview', methods=['GET'])
@login_required
def admin_overview():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, name, dept, wallet_balance, total_distance, early_count,
               registered_device_id, last_earn_date, failed_logins, locked_until, created_at
        FROM USERS ORDER BY created_at DESC, user_id ASC
    """)
    users = [{
        "user_id": r[0],
        "name": r[1],
        "dept": r[2] or "미지정",
        "wallet_balance": r[3],
        "total_distance": round(r[4], 1),
        "early_count": r[5],
        "registered_device_id": r[6] or "미등록",
        "last_earn_date": r[7] or "-",
        "failed_logins": r[8] or 0,
        "is_locked": bool(r[9]),
        "created_at": r[10] or "-"
    } for r in cursor.fetchall()]

    cursor.execute("""
        SELECT L.id, L.user_id, U.name, L.log_type, L.origin_label, L.distance_km,
               L.speed_kmh, L.status, L.description, L.points, L.created_at
        FROM COMMUTE_LOGS L
        LEFT JOIN USERS U ON L.user_id = U.user_id
        ORDER BY L.id DESC LIMIT 30
    """)
    logs = [{
        "id": r[0],
        "user_id": r[1],
        "user_name": r[2] or r[1],
        "log_type": r[3],
        "origin_label": r[4],
        "distance_km": r[5],
        "speed_kmh": r[6],
        "status": r[7],
        "description": r[8],
        "points": r[9],
        "created_at": r[10]
    } for r in cursor.fetchall()]

    total_earned = conn.execute("SELECT COALESCE(SUM(points), 0) FROM COMMUTE_LOGS WHERE log_type = 'EARN'").fetchone()[0]
    total_spent = conn.execute("SELECT COALESCE(SUM(ABS(points)), 0) FROM COMMUTE_LOGS WHERE log_type = 'SPEND'").fetchone()[0]
    earn_count = conn.execute("SELECT COUNT(*) FROM COMMUTE_LOGS WHERE log_type = 'EARN'").fetchone()[0]
    block_count = conn.execute("SELECT COUNT(*) FROM COMMUTE_LOGS WHERE log_type LIKE 'BLOCK_%'").fetchone()[0]

    return jsonify({
        "kpi": {
            "total_users": len(users),
            "earn_count": earn_count,
            "block_count": block_count,
            "total_wallet_points": sum(u["wallet_balance"] for u in users),
            "total_earned_points": total_earned,
            "total_spent_points": total_spent
        },
        "policy": POLICY_CONFIG,
        "users": users,
        "logs": logs
    })

# [API 18] 운영자 검증 정책 파라미터 실시간 변경
@app.route('/api/admin/policy', methods=['POST'])
@login_required
def admin_update_policy():
    data = request.get_json(silent=True) or {}
    for key in ["min_distance_km", "min_speed_kmh", "max_speed_kmh"]:
        if key in data:
            POLICY_CONFIG[key] = round(float(data[key]), 2)
    for key in ["base_reward_p", "early_bird_cutoff_hour", "early_bird_bonus_p", "distance_rate_p_per_km"]:
        if key in data:
            POLICY_CONFIG[key] = int(data[key])
    return jsonify({"status": "SUCCESS", "policy": POLICY_CONFIG})

# [API 19] 운영자 회원 개별 제어 (기기 UUID 초기화, 1일 제한 해제, 포인트 지급, 계정 잠금 해제)
@app.route('/api/admin/user/action', methods=['POST'])
@login_required
def admin_user_action():
    data = request.get_json(silent=True) or {}
    target_id = str(data.get("user_id", "")).strip()
    action = str(data.get("action", "")).strip()

    conn = get_db()
    row = conn.execute("SELECT user_id, name, wallet_balance FROM USERS WHERE user_id = ?", (target_id,)).fetchone()
    if row is None:
        return jsonify({"status": "FAILED", "reason": "대상 회원을 찾을 수 없습니다."}), 404

    now_str = get_kst_now().strftime("%Y-%m-%d %H:%M")
    if action == "RESET_DEVICE":
        conn.execute("UPDATE USERS SET registered_device_id = '' WHERE user_id = ?", (target_id,))
        msg = f"[{row['name']}] 단말기 고유 식별값(UUID) 바인딩이 초기화되었습니다."
    elif action == "CLEAR_DAILY":
        conn.execute("UPDATE USERS SET last_earn_date = '' WHERE user_id = ?", (target_id,))
        msg = f"[{row['name']}] 금일 1일 1회 적립 제한이 해제되었습니다."
    elif action == "UNLOCK_ACCOUNT":
        conn.execute("UPDATE USERS SET failed_logins = 0, locked_until = NULL WHERE user_id = ?", (target_id,))
        msg = f"[{row['name']}] 로그인 실패 잠금이 해제되었습니다."
    elif action == "GRANT_1000":
        conn.execute("UPDATE USERS SET wallet_balance = wallet_balance + 1000 WHERE user_id = ?", (target_id,))
        conn.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_id, 'EARN', '운영자 관제 센터', 0, 0, '관리자 지급', '운영자 수동 마일리지 지급 (+1,000P)', 1000, now_str)
        )
        msg = f"[{row['name']}] 회원에게 +1,000 P가 즉시 지급되었습니다."
    else:
        return jsonify({"status": "FAILED", "reason": "지원하지 않는 제어 명령입니다."}), 400

    conn.commit()
    return jsonify({"status": "SUCCESS", "message": msg})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
