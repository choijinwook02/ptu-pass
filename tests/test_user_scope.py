"""3단계: 기존 API를 로그인 사용자 기준으로 전환 — 접근 제어, 사용자 분리, 쿠폰 가격 검증."""
import pytest

PYEONGTAEK_STN = {"start_lat": 36.9907, "start_lon": 127.0851}
APPROVE = {**PYEONGTAEK_STN, "speed_kmh": 30, "has_payment": True, "is_early": False}

PROTECTED = [
    ("get", "/api/wallet"),
    ("post", "/api/verify"),
    ("post", "/api/exchange"),
    ("post", "/api/reset"),
    ("get", "/api/logs"),
    ("get", "/api/stats/benefit"),
]
PUBLIC = [
    ("get", "/"),
    ("get", "/api/health"),
    ("get", "/api/stats/hourly"),
    ("get", "/api/stats/fraud"),
    ("get", "/api/leaderboard/departments"),
    ("get", "/api/transit/stops"),
    ("post", "/api/simulate/batch"),
    ("get", "/api/coupons/catalog"),
    ("get", "/api/system/spec"),
]


# ---------- 접근 제어 ----------

@pytest.mark.parametrize("method,url", PROTECTED)
def test_protected_requires_login(client, method, url):
    res = getattr(client, method)(url, json={})
    assert res.status_code == 401 and res.get_json()["code"] == "LOGIN_REQUIRED"


@pytest.mark.parametrize("method,url", PUBLIC)
def test_public_stays_open(client, method, url):
    assert getattr(client, method)(url).status_code == 200


def test_wallet_by_other_user_id_removed(demo_client):
    assert demo_client.get("/api/wallet/DEMO_TEAM").status_code == 404


def test_nothing_written_without_login(client, raw_db):
    client.post("/api/verify", json=APPROVE)
    client.post("/api/exchange", json={"coupon_id": "CAFE_3000"})
    assert raw_db.execute("SELECT COUNT(*) FROM COMMUTE_LOGS").fetchone()[0] == 0


# ---------- 사용자 분리 ----------

def test_new_user_starts_at_zero_and_earns(make_user):
    a = make_user("2026000001", name="김가")
    w = a.get("/api/wallet").get_json()
    assert (w["name"], w["balance"], w["total_distance"], w["early_count"]) == ("김가", 0, 0, 0)

    res = a.post("/api/verify", json=APPROVE).get_json()
    assert res["status"] == "APPROVED" and res["new_balance"] == res["earned_points"]
    assert a.get("/api/wallet").get_json()["balance"] == res["earned_points"]


def test_users_do_not_see_each_other(make_user, demo_client):
    a = make_user("2026000001")
    b = make_user("2026000002")
    a.post("/api/verify", json=APPROVE)
    a.post("/api/verify", json={**APPROVE, "has_payment": False})

    assert len(a.get("/api/logs").get_json()["logs"]) == 2
    assert b.get("/api/logs").get_json()["logs"] == []
    assert b.get("/api/wallet").get_json()["balance"] == 0
    assert demo_client.get("/api/wallet").get_json()["balance"] == 7500
    assert demo_client.get("/api/logs").get_json()["logs"] == []


def test_benefit_is_per_user(make_user, demo_client):
    a = make_user("2026000001")
    assert a.get("/api/stats/benefit").get_json()["current_refund_value_krw"] == 0
    assert demo_client.get("/api/stats/benefit").get_json()["current_refund_value_krw"] == 7500


# ---------- 쿠폰 교환 ----------

def test_client_cost_is_ignored(demo_client):
    """예전 코드는 cost=-100000을 보내면 잔액이 늘었음 → 이제 가격은 서버 카탈로그 기준."""
    res = demo_client.post("/api/exchange", json={"coupon_id": "PRINT_1500", "cost": -100000})
    assert res.status_code == 200
    assert res.get_json()["deducted_points"] == 1500
    assert demo_client.get("/api/wallet").get_json()["balance"] == 6000


@pytest.mark.parametrize("body", [{}, {"coupon_id": "FREE_MONEY"}, {"item_name": "교내 카페 3,000원 음료 교환권", "cost": 3000}])
def test_unknown_coupon_rejected(demo_client, body):
    res = demo_client.post("/api/exchange", json=body)
    assert res.status_code == 400
    assert demo_client.get("/api/wallet").get_json()["balance"] == 7500


def test_insufficient_balance_leaves_no_trace(make_user, raw_db):
    a = make_user("2026000001")
    res = a.post("/api/exchange", json={"coupon_id": "CAFE_3000"})
    assert res.status_code == 400 and res.get_json()["balance"] == 0
    assert raw_db.execute("SELECT COUNT(*) FROM COMMUTE_LOGS").fetchone()[0] == 0


def test_exchange_logs_catalog_name(demo_client):
    demo_client.post("/api/exchange", json={"coupon_id": "MEAL_6500"})
    log = demo_client.get("/api/logs").get_json()["logs"][0]
    assert log["log_type"] == "SPEND" and log["points"] == -6500
    assert log["description"] == "학생식당 1식 식권 (6,500원) 발급"


def test_catalog_endpoint_matches_server_prices(client):
    coupons = {c["id"]: c["cost"] for c in client.get("/api/coupons/catalog").get_json()["coupons"]}
    assert coupons == {"CAFE_3000": 3000, "MEAL_6500": 6500, "PRINT_1500": 1500}


# ---------- 리셋 ----------

def test_reset_only_for_demo(make_user):
    a = make_user("2026000001")
    a.post("/api/verify", json=APPROVE)
    res = a.post("/api/reset")
    assert res.status_code == 403
    assert len(a.get("/api/logs").get_json()["logs"]) == 1   # 지워지지 않음


def test_demo_reset_does_not_touch_other_users(make_user, demo_client):
    a = make_user("2026000001")
    a.post("/api/verify", json=APPROVE)
    earned = a.get("/api/wallet").get_json()["balance"]

    demo_client.post("/api/verify", json=APPROVE)
    assert demo_client.post("/api/reset").status_code == 200

    assert demo_client.get("/api/wallet").get_json()["balance"] == 7500
    assert a.get("/api/wallet").get_json()["balance"] == earned
    assert len(a.get("/api/logs").get_json()["logs"]) == 1
