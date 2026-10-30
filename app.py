import os
import math
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify

app = Flask(__name__, static_folder='static', template_folder='static')

DB_PATH = 'commute.db'

# 평택대학교 정문 기준 좌표
PTU_LAT = 36.9961
PTU_LON = 127.1328

# 1. 데이터베이스 및 테이블 초기화 함수
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 지갑 잔액 테이블 (단일 사용자 또는 팀 공용 지갑)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS wallet (
            id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL
        )
    ''')
    
    # 검증 심사 및 적립 이력 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            origin_info TEXT NOT NULL,
            distance REAL NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            points INTEGER NOT NULL
        )
    ''')
    
    # 초기 잔액 세팅 (초기값 7,500P가 없을 때만 등록)
    cursor.execute('SELECT balance FROM wallet WHERE id = 1')
    if not cursor.fetchone():
        cursor.execute('INSERT INTO wallet (id, balance) VALUES (1, 7500)')
        
    conn.commit()
    conn.close()

# 서버 구동 시 DB 자동 생성
init_db()

# Haversine 공식 거리(km) 계산 함수
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0  # 지구 반지름 (km)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 2)

# 메인 페이지 렌더링
@app.route('/')
def index():
    return render_template('index.html')

# API 1: 현재 지갑 잔액 및 최근 이력 조회
@app.route('/api/wallet', methods=['GET'])
def get_wallet():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT balance FROM wallet WHERE id = 1')
    row = cursor.fetchone()
    balance = row[0] if row else 7500
    
    cursor.execute('SELECT created_at, origin_info, distance, status, reason, points FROM history ORDER BY id DESC LIMIT 10')
    history_rows = cursor.fetchall()
    conn.close()
    
    history_list = []
    for r in history_rows:
        history_list.append({
            'created_at': r[0],
            'origin_info': r[1],
            'distance': r[2],
            'status': r[3],
            'reason': r[4],
            'points': r[5]
        })
        
    return jsonify({
        'balance': balance,
        'history': history_list
    })

# API 2: 통학 교차 검증 및 마일리지 적립 (핵심 백엔드 엔진)
@app.route('/api/verify', methods=['POST'])
def verify_commute():
    data = request.get_json() or {}
    
    user_lat = data.get('lat')
    user_lon = data.get('lon')
    is_transit_tagged = data.get('is_transit_tagged', False)
    
    if user_lat is None or user_lon is None:
        return jsonify({'success': False, 'message': 'GPS 좌표가 올바르지 않습니다.'}), 400

    # 서버에서 평택대 정문까지의 직선거리 독립 연산
    distance_km = calculate_distance(user_lat, user_lon, PTU_LAT, PTU_LON)
    now = datetime.now()
    created_at = now.strftime('%Y-%m-%d %H:%M')
    origin_info = f"위도 {user_lat:.3f}, 경도 {user_lon:.3f}"
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 교차 검증 오용 판별 알고리즘
    if not is_transit_tagged:
        status = 'REJECTED'
        reason = '대중교통 결제 트랜잭션 부재 (도보 이동 또는 카드 미태그)'
        earned_points = 0
    elif distance_km < 1.0:
        status = 'REJECTED'
        reason = f'평택대 정문까지 거리가 {distance_km}km로 도보 기준(1km 미만)에 해당하여 오용 차단됨'
        earned_points = 0
    else:
        status = 'APPROVED'
        base_reward = 300
        early_bonus = 200 if now.hour < 8 else 0  # 8시 이전 조기 도착 보너스
        distance_reward = int(round(distance_km * 50))
        earned_points = base_reward + early_bonus + distance_reward
        reason = f'통학 거리 {distance_km}km 및 대중교통 결제 일치 검증 통과 (거리 비례 지급)'

    # 승인 시 잔액 업데이트
    if status == 'APPROVED':
        cursor.execute('UPDATE wallet SET balance = balance + ? WHERE id = 1', (earned_points,))
        
    # 심사 내역 DB에 영구 기록
    cursor.execute('''
        INSERT INTO history (created_at, origin_info, distance, status, reason, points)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (created_at, origin_info, distance_km, status, reason, earned_points))
    
    cursor.execute('SELECT balance FROM wallet WHERE id = 1')
    current_balance = cursor.fetchone()[0]
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': status == 'APPROVED',
        'status': status,
        'distance': distance_km,
        'reason': reason,
        'earned_points': earned_points,
        'current_balance': current_balance,
        'created_at': created_at,
        'origin_info': origin_info
    })

# API 3: 교내 제휴처 쿠폰 교환 (학식 6,500P, 카페 3,000P 실제 잔액 차감)
@app.route('/api/exchange', methods=['POST'])
def exchange_coupon():
    data = request.get_json() or {}
    item_name = data.get('item_name')
    cost = data.get('cost', 0)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT balance FROM wallet WHERE id = 1')
    current_balance = cursor.fetchone()[0]
    
    if current_balance < cost:
        conn.close()
        return jsonify({'success': False, 'message': '마일리지가 부족합니다.'}), 400
        
    # 포인트 차감
    new_balance = current_balance - cost
    cursor.execute('UPDATE wallet SET balance = ? WHERE id = 1', (new_balance,))
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    cursor.execute('''
        INSERT INTO history (created_at, origin_info, distance, status, reason, points)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (now_str, '교내 시설 사용', 0.0, 'EXCHANGED', f'{item_name} 교환 결제 완료', -cost))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'item_name': item_name,
        'deducted': cost,
        'new_balance': new_balance
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
