"""
CS 饰品行情监控后端
数据来源: csqaq.com 开放 API
功能: 定时采集 + 历史存储 + 异动检测 + 前端 API
"""

import json
import os
import sqlite3
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

BASE_URL = "https://api.csqaq.com/api/v1"
DB_PATH = Path(__file__).parent / "cs_monitor.db"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# 指数中文名映射
INDEX_NAMES = {
    "init": "饰品指数",
    "lease": "租赁指数",
    "main_weapon": "百元主战",
    "agent": "探员指数",
    "no_painted": "原皮指数",
    "covert_weapon": "隐秘指数",
    "thousand_weapon": "千战指数",
    "sticker": "贴纸指数",
    "arms_race": "武库指数",
}


# ─── 数据库 ─────────────────────────────
def init_db():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_key TEXT NOT NULL,
                name TEXT NOT NULL,
                market_index REAL,
                chg_num REAL,
                chg_rate REAL,
                open REAL,
                close REAL,
                high REAL,
                low REAL,
                captured_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_key TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                seen INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_name_key
            ON market_snapshots(name_key, captured_at)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                threshold REAL DEFAULT 2.0,
                added_at TEXT NOT NULL
            )
        """)
        conn.commit()


# ─── 数据采集 ────────────────────────────
def fetch_current_data() -> dict | None:
    """从 csqaq API 获取实时行情"""
    try:
        r = requests.get(f"{BASE_URL}/current_data", headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json().get("data", {})
    except Exception as e:
        print(f"[ERROR] 数据获取失败: {e}")
    return None


def check_alerts(current: dict, previous: dict, name_key: str, name: str):
    """对比前后数据，检测异动"""
    cur_idx = current.get("market_index", 0)
    prev_idx = previous.get("market_index", 0) if previous else cur_idx

    if prev_idx == 0:
        return

    change_pct = (cur_idx - prev_idx) / prev_idx * 100

    # 检查自选列表中的自定义阈值
    with sqlite3.connect(str(DB_PATH)) as conn:
        wl = conn.execute(
            "SELECT threshold FROM watchlist WHERE name_key=?", (name_key,)
        ).fetchone()
        watch_threshold = wl[0] if wl else None

    threshold = watch_threshold if watch_threshold else 3.0

    if abs(change_pct) >= threshold:
        direction = "📈 大涨" if change_pct > 0 else "📉 大跌"
        tag = "[自选]" if watch_threshold else ""
        msg = f"{tag}[{direction}] {name} 当前 {cur_idx:.2f}，变动 {change_pct:+.2f}%"
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO alerts (name_key, alert_type, message, created_at) VALUES (?, ?, ?, ?)",
                (name_key, "big_move", msg, datetime.now().isoformat()),
            )
            conn.commit()
        print(f"[ALERT] {msg}")


def collect_and_store():
    """采集当前数据并存入数据库"""
    data = fetch_current_data()
    if not data:
        return

    now = datetime.now().isoformat()
    indices = data.get("sub_index_data", [])

    with sqlite3.connect(str(DB_PATH)) as conn:
        for item in indices:
            nk = item.get("name_key", "")
            name = INDEX_NAMES.get(nk, item.get("name", nk))

            # 取上一次记录用于异动检测
            prev = conn.execute(
                "SELECT market_index FROM market_snapshots WHERE name_key=? ORDER BY captured_at DESC LIMIT 1",
                (nk,),
            ).fetchone()

            conn.execute(
                """INSERT INTO market_snapshots
                   (name_key, name, market_index, chg_num, chg_rate, open, close, high, low, captured_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    nk, name,
                    item.get("market_index"), item.get("chg_num"), item.get("chg_rate"),
                    item.get("open"), item.get("close"), item.get("high"), item.get("low"),
                    now,
                ),
            )

            prev_data = {"market_index": prev[0]} if prev else None
            check_alerts(item, prev_data, nk, name)

        conn.commit()


def collector_loop():
    """后台定时采集线程 (每 60 秒)"""
    while True:
        try:
            collect_and_store()
        except Exception as e:
            print(f"[ERROR] 采集异常: {e}")
        time.sleep(60)


# ─── API 路由 ────────────────────────────

@app.route("/api/indices")
def api_indices():
    """获取最新大盘指数"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT name_key, name, market_index, chg_num, chg_rate, open, close, high, low, captured_at
            FROM market_snapshots
            WHERE (name_key, captured_at) IN (
                SELECT name_key, MAX(captured_at) FROM market_snapshots GROUP BY name_key
            )
            ORDER BY ABS(chg_rate) DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/<name_key>")
def api_history(name_key: str):
    """获取某个指数的历史数据"""
    hours = request.args.get("hours", 24, type=int)
    since = (datetime.now() - timedelta(hours=hours)).isoformat()

    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT market_index, captured_at FROM market_snapshots WHERE name_key=? AND captured_at>=? ORDER BY captured_at ASC",
            (name_key, since),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/alerts")
def api_alerts():
    """获取最近的异动提醒"""
    limit = request.args.get("limit", 20, type=int)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/alerts/seen", methods=["POST"])
def api_alerts_seen():
    """标记提醒为已读"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("UPDATE alerts SET seen=1 WHERE seen=0")
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/search")
def api_search():
    """搜索饰品 (需要 ApiToken)"""
    q = request.args.get("q", "")
    if not q:
        return jsonify([])
    try:
        r = requests.get(
            f"{BASE_URL}/search/suggest",
            params={"keyword": q, "limitNum": 10},
            headers=HEADERS,
            timeout=10,
        )
        if r.status_code == 200:
            return jsonify(r.json().get("data", []))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify([])


@app.route("/api/skin/<int:skin_id>")
def api_skin_detail(skin_id: int):
    """获取饰品详情"""
    try:
        r = requests.get(
            f"{BASE_URL}/info/good", params={"id": skin_id}, headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            return jsonify(r.json().get("data", {}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({})


@app.route("/api/dashboard")
def api_dashboard():
    """首页仪表盘汇总数据"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row

        # 最新指数
        indices = conn.execute("""
            SELECT name_key, name, market_index, chg_num, chg_rate, captured_at
            FROM market_snapshots
            WHERE (name_key, captured_at) IN (
                SELECT name_key, MAX(captured_at) FROM market_snapshots GROUP BY name_key
            )
            ORDER BY ABS(chg_rate) DESC
        """).fetchall()

        # 未读提醒数
        alert_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM alerts WHERE seen=0"
        ).fetchone()["cnt"]

        # 上次更新时间
        last_update = conn.execute(
            "SELECT MAX(captured_at) as t FROM market_snapshots"
        ).fetchone()["t"]

    return jsonify({
        "indices": [dict(r) for r in indices],
        "alert_count": alert_count,
        "last_update": last_update,
    })


# ─── 自选监控 ────────────────────────────

@app.route("/api/watchlist")
def api_watchlist():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()

    # 附上最新价格
    result = []
    for r in rows:
        nk = r["name_key"]
        latest = conn.execute(
            "SELECT market_index, chg_rate, captured_at FROM market_snapshots WHERE name_key=? ORDER BY captured_at DESC LIMIT 1",
            (nk,),
        ).fetchone()
        item = dict(r)
        item["current_price"] = latest["market_index"] if latest else None
        item["chg_rate"] = latest["chg_rate"] if latest else None
        item["updated_at"] = latest["captured_at"] if latest else None
        result.append(item)
    return jsonify(result)


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    data = request.get_json()
    name_key = data.get("name_key", "")
    name = data.get("name", name_key)
    threshold = data.get("threshold", 2.0)

    if not name_key:
        return jsonify({"error": "缺少 name_key"}), 400

    with sqlite3.connect(str(DB_PATH)) as conn:
        try:
            conn.execute(
                "INSERT INTO watchlist (name_key, name, threshold, added_at) VALUES (?, ?, ?, ?)",
                (name_key, name, threshold, datetime.now().isoformat()),
            )
            conn.commit()
            return jsonify({"ok": True, "msg": f"已添加 {name} 到监控列表"})
        except sqlite3.IntegrityError:
            return jsonify({"ok": False, "msg": f"{name} 已在监控列表中"})


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    data = request.get_json()
    name_key = data.get("name_key", "")
    if not name_key:
        return jsonify({"error": "缺少 name_key"}), 400

    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM watchlist WHERE name_key=?", (name_key,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/compare")
def api_compare():
    """多指数对比数据"""
    keys = request.args.get("keys", "").split(",")
    hours = request.args.get("hours", 24, type=int)
    since = (datetime.now() - timedelta(hours=hours)).isoformat()

    result = {}
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        for nk in keys:
            nk = nk.strip()
            if not nk:
                continue
            rows = conn.execute(
                "SELECT market_index, captured_at FROM market_snapshots WHERE name_key=? AND captured_at>=? ORDER BY captured_at ASC",
                (nk, since),
            ).fetchall()
            idx_info = INDEX_NAMES.get(nk, nk)
            result[nk] = {"name": idx_info, "data": [dict(r) for r in rows]}
    return jsonify(result)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ─── 启动 ────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"启动服务 - 端口 {port}")
    app.run(host="0.0.0.0", port=port, debug=False)


# ─── 启动初始化 (模块加载时执行) ────────
def _startup():
    print("初始化数据库...")
    init_db()
    print("首次采集数据...")
    collect_and_store()
    print("启动后台采集线程 (每 60 秒)...")
    t = threading.Thread(target=collector_loop, daemon=True)
    t.start()

_startup()
