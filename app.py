import os
import sqlite3
import math
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)

DB_PATH = 'commute.db'

# DB 초기화 함수 (서버 시작 시 자동 실행)
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS USERS (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        wallet_balance INTEGER DEFAULT 0
    )
    ''')
    # 기본 테스트 계정 생성
    cursor.execute("INSERT OR IGNORE INTO USERS (user_id, name, wallet_balance) VALUES ('2021143028', '이동구', 1000)")
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

# 메인 웹페이지 보여주기
@app.route('/')
def home():
    return send_from_directory('static', 'index.html')

# 잔액 조회 API
@app.route('/api/wallet/<user_id>', methods=['GET'])
def get_wallet(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name, wallet_balance FROM USERS WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return jsonify({"name": row[0], "balance": row[1]})
    return jsonify({"error": "사용자를 찾을 수 없습니다."}), 404

# 교차 검증 및 마일리지 지급 API
@app.route('/api/verify', methods=['POST'])
def verify_commute():
    data = request.json
    user_id = data.get('user_id')
    start_lat = float(data.get('start_lat', 0))
    start_lon = float(data.get('start_lon', 0))
    end_lat = float(data.get('end_lat', 0))
    end_lon = float(data.get('end_lon', 0))
    duration_minutes = float(data.get('duration_minutes', 30))
    has_payment = data.get('has_payment', False)
    arrival_hour = datetime.now().hour

    distance_km = calculate_distance(start_lat, start_lon, end_lat, end_lon)
    speed_kmh = (distance_km / (duration_minutes / 60)) if duration_minutes > 0 else 0

    # 1. 오용 차단: 결제 내역 부재
    if not has_payment:
        return jsonify({"status": "REJECTED", "reason": "대중교통 결제 트랜잭션이 확인되지 않아 보상이 거부되었습니다."})

    # 2. 오용 차단: 도보 통학 (기숙사생 / 인근 거주자)
    if speed_kmh < 5.0 or distance_km < 1.0:
        return jsonify({"status": "REJECTED", "reason": "도보 이동(시속 5km 미만 또는 1km 미만)으로 감지되어 지급이 거부되었습니다."})

    # 3. 오용 차단: 비정상적 순간이동 (GPS 조작)
    if speed_kmh > 150.0:
        return jsonify({"status": "REJECTED", "reason": "비정상적인 속도의 위치 조작이 의심되어 차단되었습니다."})

    # 4. 동적 리워드 계산
    base_reward = 300
    if arrival_hour < 8:
        base_reward += 200  # 오프피크 조기 등교 보너스
    total_reward = int(base_reward + (distance_km * 50))

    # DB 적립
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE USERS SET wallet_balance = wallet_balance + ? WHERE user_id = ?", (total_reward, user_id))
    cursor.execute("SELECT wallet_balance FROM USERS WHERE user_id = ?", (user_id,))
    new_balance = cursor.fetchone()[0]
    conn.commit()
    conn.close()

    return jsonify({
        "status": "APPROVED",
        "distance": round(distance_km, 2),
        "speed": round(speed_kmh, 1),
        "earned_points": total_reward,
        "new_balance": new_balance,
        "message": f"통학 검증 완료! {total_reward} 마일리지가 적립되었습니다."
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)