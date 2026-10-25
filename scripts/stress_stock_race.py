"""Stress the local order state race without contacting real payment/model APIs."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import json
import secrets
import subprocess
import threading
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8080/api/v1"
MYSQL = ROOT / ".local/mysql-8.4.13-winx64/bin/mysql.exe"
MYSQL_ADMIN = ROOT / ".local/mysql-admin.ini"
RACE_ORDERS = 40
TIMEOUT_ORDERS = 20


def api(route, body=None, token=None, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        BASE + route,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.load(exc)
        except Exception:
            payload = {"error": str(exc)}
        return exc.code, payload


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def mysql(sql):
    result = subprocess.run(
        [
            str(MYSQL),
            "--defaults-extra-file=" + str(MYSQL_ADMIN),
            "--batch",
            "--skip-column-names",
            "xzxg_shop_local",
            "--execute=" + sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def stock(product_id, sku_id):
    raw = mysql(
        "SELECT p.stock_quantity, ps.stock_quantity "
        "FROM products p JOIN product_skus ps ON ps.product_id=p.product_id "
        f"WHERE p.product_id='{product_id}' AND ps.sku_id='{sku_id}';"
    )
    require(bool(raw), "cannot read stress product inventory")
    product_stock, sku_stock = raw.split("\t")
    return int(product_stock), int(sku_stock)


def create_order(username, product_id, sku_id):
    status, login = api(
        "/auth/register",
        {
            "username": username,
            "password": secrets.token_hex(16),
            "display_name": "库存并发测试",
        },
    )
    require(status == 201, f"register failed: {username} status={status}")
    token = login["token"]
    status, _ = api(
        "/cart/items",
        {"product_id": product_id, "sku_id": sku_id, "quantity": 1},
        token,
    )
    require(status == 200, "add cart failed: " + username)
    status, checkout = api("/orders:checkout", {}, token)
    require(status == 200 and len(checkout["items"]) == 1, "checkout failed: " + username)
    return {"token": token, "order_id": checkout["items"][0]["order_id"]}


def create_orders(prefix, count, product_id, sku_id):
    # Prepare deterministically so a lost HTTP response cannot leave an
    # ambiguous POST. Contention is applied to terminal state transitions.
    return [
        create_order(f"{prefix}{index:03d}", product_id, sku_id)
        for index in range(count)
    ]


def race_pay_and_cancel(records):
    start = threading.Event()
    diagnostics = {}

    def invoke(record, action):
        start.wait()
        if action == "pay":
            status, payload = api(
                "/orders/" + record["order_id"] + ":pay",
                {"method": "local_stress_mock"},
                record["token"],
            )
        else:
            status, payload = api(
                "/orders/" + record["order_id"] + ":cancel",
                {"reason": "并发库存测试"},
                record["token"],
            )
        return record["order_id"], action, status, payload

    outcomes = {record["order_id"]: {} for record in records}
    with ThreadPoolExecutor(max_workers=min(20, len(records) * 2)) as pool:
        futures = [
            pool.submit(invoke, record, action)
            for record in records
            for action in ("pay", "cancel")
        ]
        start.set()
        for future in as_completed(futures):
            order_id, action, status, payload = future.result()
            outcomes[order_id][action] = status
            diagnostics[(order_id, action)] = payload
    records_by_id = {record["order_id"]: record for record in records}
    for order_id, result in outcomes.items():
        if sorted(result.values()) != [200, 409]:
            record = records_by_id[order_id]
            final_http, final_order = api("/orders/" + order_id, token=record["token"])
            raise AssertionError(
                "race did not have one winner: "
                f"{order_id} responses={result} payloads="
                f"{{'pay': {diagnostics[(order_id, 'pay')]!r}, "
                f"'cancel': {diagnostics[(order_id, 'cancel')]!r}}} "
                f"final_http={final_http} final_order={final_order!r}"
            )
    return outcomes


def repeat_terminal_actions(records):
    def invoke(record, action):
        if action == "pay":
            return api(
                "/orders/" + record["order_id"] + ":pay",
                {"method": "local_stress_mock"},
                record["token"],
            )[0]
        return api(
            "/orders/" + record["order_id"] + ":cancel",
            {"reason": "重复终态请求"},
            record["token"],
        )[0]

    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(
            pool.map(
                lambda pair: invoke(*pair),
                [(record, action) for record in records for action in ("pay", "cancel")],
            )
        )
    require(all(status == 409 for status in statuses), "terminal order accepted a repeated action")


def trigger_timeout_sweep(records):
    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        statuses = list(pool.map(lambda record: api("/orders", token=record["token"])[0], records))
    require(all(status == 200 for status in statuses), "concurrent timeout sweep request failed")


def cleanup(prefix, product_id, sku_id):
    account_filter = f"SELECT account_id FROM accounts WHERE username LIKE '{prefix}%'"
    order_filter = f"SELECT order_id FROM orders WHERE account_id IN ({account_filter})"
    mysql(
        "START TRANSACTION;"
        f"DELETE FROM payments WHERE order_id IN ({order_filter});"
        f"DELETE FROM order_items WHERE order_id IN ({order_filter});"
        f"DELETE FROM orders WHERE account_id IN ({account_filter});"
        f"DELETE FROM cart_items WHERE account_id IN ({account_filter});"
        f"DELETE FROM auth_tokens WHERE account_id IN ({account_filter});"
        f"DELETE FROM user_coupons WHERE account_id IN ({account_filter});"
        f"DELETE FROM accounts WHERE username LIKE '{prefix}%';"
        f"DELETE FROM product_skus WHERE sku_id = '{sku_id}';"
        f"DELETE FROM products WHERE product_id = '{product_id}';"
        "COMMIT;"
    )


def main():
    run_id = secrets.token_hex(5)
    prefix = "stress" + run_id
    product_id = "stress_p_" + run_id
    sku_id = "stress_s_" + run_id
    capacity = RACE_ORDERS + TIMEOUT_ORDERS + 100
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "race_orders": RACE_ORDERS,
        "timeout_orders": TIMEOUT_ORDERS,
        "real_payment_gateway_calls": 0,
        "model_calls": 0,
        "passed": False,
    }
    try:
        mysql(
            "INSERT INTO products "
            "(product_id,merchant_id,name,brand,category_id,image_url,image_urls_json,price,market_price,"
            "stock_quantity,stock_status,tags_json,selling_points_json,recommend_reason,risk_notes_json,"
            "attributes_json,suitable_for_json,not_suitable_for_json,description,status,sort_order) VALUES "
            f"('{product_id}','m_001','库存并发测试商品','stress','c_phone','','[]',1.00,1.00,{capacity},"
            "'in_stock','[]','[]','','[]','[]','[]','[]','仅用于本地并发测试','active',99999);"
            "INSERT INTO product_skus "
            "(sku_id,product_id,sku_name,price,stock_quantity,stock_status,specs_json,is_default) VALUES "
            f"('{sku_id}','{product_id}','并发测试SKU',1.00,{capacity},'in_stock','[]',TRUE);"
        )
        initial_stock = stock(product_id, sku_id)
        require(initial_stock == (capacity, capacity), "stress product setup stock mismatch")

        race_records = create_orders(prefix + "r", RACE_ORDERS, product_id, sku_id)
        require(stock(product_id, sku_id) == (capacity - RACE_ORDERS,) * 2, "concurrent reserve mismatch")
        outcomes = race_pay_and_cancel(race_records)
        paid = sum(1 for result in outcomes.values() if result["pay"] == 200)
        canceled = RACE_ORDERS - paid
        expected_after_race = capacity - paid
        require(stock(product_id, sku_id) == (expected_after_race,) * 2, "race inventory mismatch")

        repeat_terminal_actions(race_records)
        require(stock(product_id, sku_id) == (expected_after_race,) * 2, "repeated action changed stock")

        timeout_records = create_orders(prefix + "t", TIMEOUT_ORDERS, product_id, sku_id)
        require(
            stock(product_id, sku_id) == (expected_after_race - TIMEOUT_ORDERS,) * 2,
            "timeout reserve mismatch",
        )
        mysql(
            "UPDATE orders SET payment_deadline_at=DATE_SUB(NOW(), INTERVAL 1 MINUTE) "
            f"WHERE account_id IN (SELECT account_id FROM accounts WHERE username LIKE '{prefix}t%') "
            "AND status='pending_payment';"
        )
        trigger_timeout_sweep(timeout_records)
        for record in timeout_records:
            status, order = api("/orders/" + record["order_id"], token=record["token"])
            require(status == 200 and order["status"] == "closed_timeout", "timeout order not closed")
        require(stock(product_id, sku_id) == (expected_after_race,) * 2, "timeout inventory mismatch")
        trigger_timeout_sweep(timeout_records)
        require(stock(product_id, sku_id) == (expected_after_race,) * 2, "repeated timeout changed stock")

        report.update(
            passed=True,
            paid_winners=paid,
            cancel_winners=canceled,
            assertions=[
                "reservation deducts exact product and SKU quantities",
                "pay/cancel has exactly one winner per order",
                "canceled inventory is restored exactly once",
                "paid inventory remains deducted",
                "repeated terminal actions are idempotent",
                "concurrent timeout sweep restores inventory exactly once",
            ],
        )
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        try:
            cleanup(prefix, product_id, sku_id)
            report["cleanup"] = "passed"
        except Exception as cleanup_error:
            report["cleanup"] = repr(cleanup_error)
        output = ROOT / ".local/stock-race-report.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
