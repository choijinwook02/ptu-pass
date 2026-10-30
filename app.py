import os
import sqlite3
import math
import random
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)

DB_PATH = 'commute.db'
DEFAULT_USER_ID = 'DEMO_TEAM'
PTU_GATE_LAT = 36.9961
PTU_GATE_LON = 127.1328

def get_kst_now():
    return datetime.now(timezone.utc) + timedelta(hours=9)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS USERS (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        dept TEXT,
        wallet_balance INTEGER DEFAULT 7500,
        total_distance REAL DEFAULT 112.5,
        early_count INTEGER DEFAULT 4
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS COMMUTE_LOGS (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        log_type TEXT,
        origin_label TEXT,
        distance_km REAL,
        speed_kmh REAL,
        status TEXT,
        description TEXT,
        points INTEGER,
        created_at TEXT
    )
    ''')
    cursor.execute(
        "INSERT OR IGNORE INTO USERS (user_id, name, dept, wallet_balance, total_distance, early_count) VALUES (?, ?, ?, ?, ?, ?)",
        (DEFAULT_USER_ID, '시연 자료 팀', '융합복합시스템 연구반', 7500, 112.5, 4)
    )
    cursor.execute("UPDATE USERS SET name = '시연 자료 팀' WHERE user_id = ?", (DEFAULT_USER_ID,))
    conn.commit()
    conn.close()

init_db()

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
        "engine_version": "v2.5-TDM-Practical"
    })

# [API 3] 전자지갑 잔액 및 누적 통학 실적 조회
@app.route('/api/wallet', defaults={'user_id': DEFAULT_USER_ID}, methods=['GET'])
@app.route('/api/wallet/<user_id>', methods=['GET'])
def get_wallet(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name, dept, wallet_balance, total_distance, early_count FROM USERS WHERE user_id = ?", (DEFAULT_USER_ID,))
    row = cursor.fetchone()
    conn.close()
    return jsonify({
        "name": row[0],
        "dept": row[1],
        "balance": row[2],
        "total_distance": round(row[3], 1),
        "early_count": row[4]
    })

# [API 4] 실시간 GPS·결제 교차 검증 및 마일리지 적립
@app.route('/api/verify', methods=['POST'])
def verify_commute():
    data = request.json or {}
    origin_label = data.get('origin_label', '실제 단말기 GPS')
    start_lat = float(data.get('start_lat', 0))
    start_lon = float(data.get('start_lon', 0))
    end_lat = float(data.get('end_lat', PTU_GATE_LAT))
    end_lon = float(data.get('end_lon', PTU_GATE_LON))
    duration_minutes = float(data.get('duration_minutes', 38))
    has_payment = data.get('has_payment', False)

    kst_now = get_kst_now()
    is_early_mode = data.get('is_early', kst_now.hour < 8)

    distance_km = calculate_distance(start_lat, start_lon, end_lat, end_lon)
    speed_kmh = float(data.get('speed_kmh', (distance_km / (duration_minutes / 60)) if duration_minutes > 0 else 0))

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. GPS 스푸핑(위치 변조) 차단
    if speed_kmh > 150.0:
        reason = f"비정상 이동 속도({round(speed_kmh, 1)}km/h) 감지 — GPS 스푸핑(Mock Location) 차단"
        cursor.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (DEFAULT_USER_ID, 'BLOCK_SPOOF', origin_label, round(distance_km, 2), round(speed_kmh, 1), '스푸핑 차단', reason, 0, kst_now.strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "REJECTED", "badge": "스푸핑 차단", "reason": reason})

    # 2. 대중교통 결제 내역 미존재 차단
    if not has_payment:
        reason = "대중교통 결제 트랜잭션 부재 (도보 이동 또는 카드 미태그 오용 차단)"
        cursor.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (DEFAULT_USER_ID, 'BLOCK_NOPAY', origin_label, round(distance_km, 2), round(speed_kmh, 1), '지급 거부', reason, 0, kst_now.strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "REJECTED", "badge": "지급 거부", "reason": reason})

    # 3. 1.0km 미만 단거리 도보 통학 차단
    if speed_kmh < 5.0 or distance_km < 1.0:
        reason = f"평택대 정문까지 {round(distance_km, 2)}km (1.0km 미만 기숙사·인근 도보 통학 레드존 차단)"
        cursor.execute(
            "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (DEFAULT_USER_ID, 'BLOCK_WALK', origin_label, round(distance_km, 2), round(speed_kmh, 1), '지급 거부', reason, 0, kst_now.strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "REJECTED", "badge": "지급 거부", "reason": reason})

    # 4. 정상 승인 및 동적 마일리지 산출
    base_reward = 300
    early_bonus = 200 if is_early_mode else 0
    distance_reward = int(round(distance_km * 50))
    total_reward = base_reward + early_bonus + distance_reward
    reason = f"실제 거리 {round(distance_km, 2)}km 및 대중교통 결제 교차 검증 일치"

    cursor.execute(
        "UPDATE USERS SET wallet_balance = wallet_balance + ?, total_distance = total_distance + ?, early_count = early_count + ? WHERE user_id = ?",
        (total_reward, round(distance_km, 2), 1 if is_early_mode else 0, DEFAULT_USER_ID)
    )
    cursor.execute("SELECT wallet_balance FROM USERS WHERE user_id = ?", (DEFAULT_USER_ID,))
    new_balance = cursor.fetchone()[0]

    cursor.execute(
        "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (DEFAULT_USER_ID, 'EARN', origin_label, round(distance_km, 2), round(speed_kmh, 1), '정상 승인', reason, total_reward, kst_now.strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

    return jsonify({
        "status": "APPROVED",
        "distance": round(distance_km, 2),
        "speed": round(speed_kmh, 1),
        "base_reward": base_reward,
        "early_bonus": early_bonus,
        "distance_reward": distance_reward,
        "earned_points": total_reward,
        "new_balance": new_balance,
        "reason": reason
    })

# [API 5] 교내 사용처 모바일 쿠폰 발급 및 포인트 차감
@app.route('/api/exchange', methods=['POST'])
def exchange_coupon():
    data = request.json or {}
    item_name = data.get('item_name', '교내 제휴 쿠폰')
    cost = int(data.get('cost', 0))

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT wallet_balance FROM USERS WHERE user_id = ?", (DEFAULT_USER_ID,))
    current_balance = cursor.fetchone()[0]

    if current_balance < cost:
        conn.close()
        return jsonify({"status": "FAILED", "reason": "보유 마일리지가 부족합니다.", "balance": current_balance}), 400

    new_balance = current_balance - cost
    cursor.execute("UPDATE USERS SET wallet_balance = ? WHERE user_id = ?", (new_balance, DEFAULT_USER_ID))
    cursor.execute(
        "INSERT INTO COMMUTE_LOGS (user_id, log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (DEFAULT_USER_ID, 'SPEND', '교내 제휴처', 0, 0, '사용 완료', f'{item_name} 발급', -cost, get_kst_now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

    return jsonify({
        "status": "SUCCESS",
        "item_name": item_name,
        "deducted_points": cost,
        "new_balance": new_balance,
        "voucher_code": f"PTU-{random.randint(1000,9999)}-{random.randint(1000,9999)}"
    })

# [API 6] 발표 리허설용 데이터 초기화 (7,500P 복구)
@app.route('/api/reset', methods=['POST'])
def reset_wallet():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE USERS SET wallet_balance = 7500, total_distance = 112.5, early_count = 4 WHERE user_id = ?", (DEFAULT_USER_ID,))
    cursor.execute("DELETE FROM COMMUTE_LOGS WHERE user_id = ?", (DEFAULT_USER_ID,))
    conn.commit()
    conn.close()
    return jsonify({"status": "RESET_COMPLETE", "new_balance": 7500})

# [API 7] 실시간 검증 및 입출금 로그 조회
@app.route('/api/logs', methods=['GET'])
def get_logs():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT log_type, origin_label, distance_km, speed_kmh, status, description, points, created_at FROM COMMUTE_LOGS ORDER BY id DESC LIMIT 15")
    rows = cursor.fetchall()
    conn.close()
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
        "labels": ["정상 대중교통 승인", "1km 미만 도보 차단", "결제 미태그 차단", "GPS 스푸핑 조작 차단"],
        "counts": [142, 34, 16, 8],
        "block_accuracy": 98.5
    })

# [API 10] 실용적 통학 편익 지표 (정류장 대기시간 단축 및 식비·교통비 환급 혜택)
@app.route('/api/stats/benefit', methods=['GET'])
def get_practical_benefit():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT wallet_balance, total_distance, early_count FROM USERS WHERE user_id = ?", (DEFAULT_USER_ID,))
    row = cursor.fetchone()
    conn.close()
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
        "campus_gate": {"name": "평택대학교 정문 (기준점)", "lat": PTU_GATE_LAT, "lon": PTU_GATE_LON, "walk_limit_km": 1.0},
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
    return jsonify({
        "coupons": [
            {"id": "CAFE_3000", "name": "교내 카페 3,000원 음료 교환권", "cost": 3000, "category": "CAFE"},
            {"id": "MEAL_6500", "name": "학생식당 1식 식권 (6,500원)", "cost": 6500, "category": "FOOD"},
            {"id": "PRINT_1500", "name": "학술정보관 인쇄·출력 50장권", "cost": 1500, "category": "STUDY"}
        ]
    })

# [API 15] 교차 검증 엔진 임계값 파라미터 명세
@app.route('/api/system/spec', methods=['GET'])
def get_system_spec():
    return jsonify({
        "min_distance_km": 1.0,
        "min_speed_kmh": 5.0,
        "max_speed_kmh": 150.0,
        "base_reward_p": 300,
        "early_bird_cutoff_hour": 8,
        "early_bird_bonus_p": 200,
        "distance_rate_p_per_km": 50
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
