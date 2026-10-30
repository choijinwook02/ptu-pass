import os
import sqlite3
import math
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)

DB_PATH = 'commute.db'
DEFAULT_USER_ID = 'DEMO_TEAM'

# 한국 표준시(KST, UTC+9) 반환 함수
def get_kst_now():
    return datetime.now(timezone.utc) + timedelta(hours=9)

# DB 초기화 함수
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS USERS (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        wallet_balance INTEGER DEFAULT 7500
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS COMMUTE_LOGS (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        log_type TEXT,
        description TEXT,
        points INTEGER,
        created_at TEXT
    )
    ''')
    # 기본 시연 계정 생성 ('시연 자료 팀', 초기 잔액 7,500P)
    cursor.execute(
        "INSERT OR IGNORE INTO USERS (user_id, name, wallet_balance) VALUES (?, ?, ?)",
        (DEFAULT_USER_ID, '시연 자료 팀', 7500)
    )
    # 기존 계정 명칭도 '시연 자료 팀'으로 동기화
    cursor.execute(
        "UPDATE USERS SET name = '시연 자료 팀' WHERE user_id = ?",
        (DEFAULT_USER_ID,)
    )
    conn.commit()
    conn.close()

init_db()

# Haversine 공식 (두 좌표 간 거리 km 계산)
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

@app.route('/')
def home():
    return send_from_directory('static', 'index.html')

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ONLINE", "team": "시연 자료 팀", "kst_time": get_kst_now().strftime("%Y-%m-%d %H:%M:%S")})

# 잔액 조회 API
@app.route('/api/wallet', defaults={'user_id': DEFAULT_USER_ID}, methods=['GET'])
@app.route('/api/wallet/<user_id>', methods=['GET'])
def get_wallet(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name, wallet_balance FROM USERS WHERE user_id = ?", (DEFAULT_USER_ID,))
    row = cursor.fetchone()
    conn.close()
    return jsonify({"name": row[0], "balance": row[1]})

# 교차 검증 및 마일리지 지급 API
@app.route('/api/verify', methods=['POST'])
def verify_commute():
    data = request.json or {}
    start_lat = float(data.get('start_lat', 0))
    start_lon = float(data.get('start_lon', 0))
    end_lat = float(data.get('end_lat', 36.9961))
    end_lon = float(data.get('end_lon', 127.1328))
    duration_minutes = float(data.get('duration_minutes', 38))
    has_payment = data.get('has_payment', False)
    
    kst_now = get_kst_now()
    is_early_mode = data.get('is_early', kst_now.hour < 8)

    distance_km = calculate_distance(start_lat, start_lon, end_lat, end_lon)
    speed_kmh = float(data.get('speed_kmh', (distance_km / (duration_minutes / 60)) if duration_minutes > 0 else 0))

    # 1. 오용 차단: 비정상적 순간이동 (GPS 위치 조작 스푸핑)
    if speed_kmh > 150.0:
        return jsonify({
            "status": "REJECTED",
            "badge": "스푸핑 차단",
            "reason": f"비정상 이동 속도({round(speed_kmh, 1)}km/h) 감지 — GPS 위치 조작(Mock Location) 차단"
        })

    # 2. 오용 차단: 결제 내역 부재
    if not has_payment:
        return jsonify({
            "status": "REJECTED",
            "badge": "지급 거부",
            "reason": "대중교통 결제 트랜잭션 부재 (도보 이동 또는 카드 미태그 오용 차단)"
        })

    # 3. 오용 차단: 도보 통학 (1km 미만 또는 시속 5km 미만)
    if speed_kmh < 5.0 or distance_km < 1.0:
        return jsonify({
            "status": "REJECTED",
            "badge": "지급 거부",
            "reason": f"평택대 정문까지 거리 {round(distance_km, 2)}km (1.0km 미만 기숙사·인근 도보 통학 차단)"
        })

    # 4. 동적 리워드 산출 (기본 300P + 조기등교 200P + km당 50P)
    base_reward = 300
    early_bonus = 200 if is_early_mode else 0
    distance_reward = int(round(distance_km * 50))
    total_reward = base_reward + early_bonus + distance_reward

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE USERS SET wallet_balance = wallet_balance + ? WHERE user_id = ?", (total_reward, DEFAULT_USER_ID))
    cursor.execute("SELECT wallet_balance FROM USERS WHERE user_id = ?", (DEFAULT_USER_ID,))
    new_balance = cursor.fetchone()[0]

    cursor.execute(
        "INSERT INTO COMMUTE_LOGS (user_id, log_type, description, points, created_at) VALUES (?, ?, ?, ?, ?)",
        (DEFAULT_USER_ID, 'EARN', f'통학 검증 승인 ({round(distance_km, 2)}km)', total_reward, kst_now.strftime("%Y-%m-%d %H:%M"))
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
        "reason": f"실제 거리 {round(distance_km, 2)}km 및 대중교통 결제 교차 검증 일치"
    })

# 교내 사용처 쿠폰 교환 API (학식 6,500P / 카페 3,000P / 출력권 1,500P)
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
        "INSERT INTO COMMUTE_LOGS (user_id, log_type, description, points, created_at) VALUES (?, ?, ?, ?, ?)",
        (DEFAULT_USER_ID, 'SPEND', f'{item_name} 발급', -cost, get_kst_now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

    return jsonify({
        "status": "SUCCESS",
        "item_name": item_name,
        "deducted_points": cost,
        "new_balance": new_balance
    })

# 발표 리허설용 잔액 초기화 API (7,500P 복구)
@app.route('/api/reset', methods=['POST'])
def reset_wallet():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE USERS SET wallet_balance = 7500 WHERE user_id = ?", (DEFAULT_USER_ID,))
    cursor.execute("DELETE FROM COMMUTE_LOGS WHERE user_id = ?", (DEFAULT_USER_ID,))
    conn.commit()
    conn.close()
    return jsonify({"status": "RESET_COMPLETE", "new_balance": 7500})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
