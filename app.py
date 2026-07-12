"""
CS 饰品行情监控后端
数据来源: csqaq.com 开放 API
功能: 定时采集 + 历史存储 + 异动检测 + 前端 API
"""

import json
import sqlite3
import smtplib
import time
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static")
CORS(app)

BASE_URL = "https://api.csqaq.com/api/v1"
DB_PATH = Path(__file__).parent / "cs_monitor.db"
API_TOKEN = "XGCWH1F7Y8U3P8X7L3F7G6X3"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "ApiToken": API_TOKEN}

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                smtp_server TEXT NOT NULL DEFAULT 'smtp.qq.com',
                smtp_port INTEGER NOT NULL DEFAULT 465,
                sender_email TEXT NOT NULL DEFAULT '',
                auth_code TEXT NOT NULL DEFAULT '',
                receiver_email TEXT NOT NULL DEFAULT '',
                enabled INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skin_monitor (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skin_id INTEGER NOT NULL UNIQUE,
                skin_name TEXT NOT NULL,
                current_price REAL DEFAULT 0,
                last_volume INTEGER DEFAULT 0,
                current_volume INTEGER DEFAULT 0,
                prev_volume INTEGER DEFAULT 0,
                alert_threshold INTEGER DEFAULT 15,
                vol_spike INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                last_checked TEXT,
                added_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS volume_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skin_id INTEGER NOT NULL,
                volume INTEGER NOT NULL,
                price REAL,
                captured_at TEXT NOT NULL
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
        # 发送邮件通知
        send_alert_email(msg)


# ─── 邮件通知 ────────────────────────────
def get_email_config() -> dict | None:
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM email_config WHERE enabled=1 LIMIT 1").fetchone()
        return dict(row) if row else None


def send_alert_email(message: str):
    """给已配置的邮箱发送异动提醒"""
    cfg = get_email_config()
    if not cfg or not cfg.get("receiver_email"):
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["sender_email"]
        msg["To"] = cfg["receiver_email"]
        is_up = "大涨" in message
        color = "#27ae60" if is_up else "#e74c3c"
        msg["Subject"] = f"[CS2] {message[:60]}"

        body = f"""
        <h2>CS2 饰品行情异动</h2>
        <table style="border-collapse:collapse;max-width:480px;">
        <tr style="background:{color};color:#fff;">
          <td style="padding:10px 14px;font-weight:bold;font-size:15px;">{message}</td>
        </tr>
        <tr><td style="padding:12px;border:1px solid #ddd;font-size:14px;">
          <p style="color:#888;margin-top:8px;font-size:11px;">监控系统 | csqaq.com</p>
          <p style="color:#aaa;font-size:11px;">{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
        </td></tr></table>
        """
        msg.attach(MIMEText(body, "html", "utf-8"))

        with smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"]) as server:
            server.login(cfg["sender_email"], cfg["auth_code"])
            server.sendmail(cfg["sender_email"], cfg["receiver_email"], msg.as_string())

        print(f"[EMAIL] 邮件已发送至 {cfg['receiver_email']}")
    except Exception as e:
        print(f"[EMAIL] 发送失败: {e}")



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
    """后台定时采集线程 (每 20 秒, 快速积累数据)"""
    while True:
        try:
            collect_and_store()
        except Exception as e:
            print(f"[ERROR] 采集异常: {e}")
        time.sleep(20)


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
    """获取某个指数的历史走势数据"""
    period = request.args.get("period", "24h")  # 1h / 24h / 7d
    period_map = {"1h": 1, "2h": 2, "24h": 24, "7d": 168}
    hours = period_map.get(period, 24)
    since = (datetime.now() - timedelta(hours=hours)).isoformat()

    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT market_index, captured_at FROM market_snapshots WHERE name_key=? AND captured_at>=? ORDER BY captured_at ASC",
            (name_key, since),
        ).fetchall()

    raw = [dict(r) for r in rows]
    if not raw:
        return jsonify({"points": [], "count": 0})

    # 对于短周期返回原始数据点, 长周期聚合成 OHLC
    if period in ("1h", "2h"):
        points = [{"t": r["captured_at"][5:19].replace("T", " "), "price": round(r["market_index"], 2)} for r in raw]
        return jsonify({"points": points, "count": len(points)})

    # 聚合为 OHLC bars
    # 分时(24h): 每10分钟一根K线; 日线(7d): 每2小时一根
    bucket_minutes = 10 if period == "24h" else 120
    ohlc = []
    bucket = []
    bucket_start = None
    for r in raw:
        ts = r["captured_at"]
        price = r["market_index"]
        if not bucket:
            bucket_start = ts
        bucket.append(price)

        # 检查是否应该切分
        if bucket_start:
            try:
                start_dt = datetime.fromisoformat(bucket_start)
                cur_dt = datetime.fromisoformat(ts)
                if (cur_dt - start_dt).total_seconds() >= bucket_minutes * 60:
                    ohlc.append({
                        "t": bucket_start[5:19].replace("T", " "),
                        "open": round(bucket[0], 2),
                        "close": round(bucket[-1], 2),
                        "high": round(max(bucket), 2),
                        "low": round(min(bucket), 2),
                    })
                    bucket = []
                    bucket_start = None
            except ValueError:
                pass

    # 处理最后剩余数据
    if bucket:
        ohlc.append({
            "t": bucket_start[5:19].replace("T", " ") if bucket_start else "",
            "open": round(bucket[0], 2),
            "close": round(bucket[-1], 2),
            "high": round(max(bucket), 2),
            "low": round(min(bucket), 2),
        })

    return jsonify({"points": ohlc, "count": len(ohlc), "raw_count": len(raw)})


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
            params={"text": q, "limitNum": 10},
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
    """获取饰品详情（价格、成交量、历史涨跌）"""
    try:
        r = requests.get(
            f"{BASE_URL}/info/good", params={"id": skin_id}, headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            goods = data.get("goods_info", {}) if data else {}
            # 整理历史涨跌
            history = []
            for label, days in [("1天", 1), ("7天", 7), ("15天", 15), ("30天", 30), ("90天", 90), ("180天", 180), ("365天", 365)]:
                rate = goods.get(f"sell_price_rate_{days}")
                chg = goods.get(f"sell_price_{days}")
                if rate is not None or chg is not None:
                    history.append({"label": label, "days": days, "rate": rate, "change": chg})
            goods["_price_history"] = history
            return jsonify(goods)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({})


@app.route("/api/skin/<int:skin_id>/chart")
def api_skin_chart(skin_id: int):
    """获取饰品价格走势 (分时/日线/周线)"""
    period = request.args.get("period", "7")  # 7=分时 30=日线 365=周线
    try:
        r = requests.post(
            f"{BASE_URL}/info/chart",
            json={
                "good_id": str(skin_id),
                "key": "sell_price",
                "platform": 1,
                "period": period,
                "style": "all_style",
            },
            headers=HEADERS,
            timeout=30,
        )
        if r.status_code != 200:
            return jsonify({"error": f"API 返回 {r.status_code}"}), 500

        data = r.json().get("data", {})
        timestamps = data.get("timestamp", [])
        prices = data.get("main_data", [])

        if not timestamps or not prices:
            return jsonify({"error": "无数据"}), 404

        # 转换时间戳为可读日期
        from datetime import timezone
        points = []
        for ts, price in zip(timestamps, prices):
            # ts is milliseconds
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            points.append({
                "t": dt.strftime("%Y-%m-%d %H:%M"),
                "ts": ts,
                "price": round(price, 2),
            })

        return jsonify({"points": points, "count": len(points)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── 皮肤价格监控 ───────────────────────

@app.route("/api/skin-monitor")
def api_skin_monitor_list():
    """列出所有监控的皮肤"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM skin_monitor ORDER BY added_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/skin-monitor/add", methods=["POST"])
def api_skin_monitor_add():
    """手动添加皮肤监控"""
    data = request.get_json()
    sid = data.get("skin_id")
    name = data.get("skin_name", str(sid))
    if not sid:
        return jsonify({"error": "缺少 skin_id"}), 400
    with sqlite3.connect(str(DB_PATH)) as conn:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO skin_monitor (skin_id, skin_name, enabled, added_at) VALUES (?,?,1,?)",
                (sid, name, datetime.now().isoformat()),
            )
            conn.commit()
            return jsonify({"ok": True, "msg": f"已添加 {name} 监控"})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/skin-monitor/volume")
def api_skin_volume_list():
    """查看所有皮肤成交量异动排行"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM skin_monitor WHERE current_volume>0 ORDER BY vol_spike DESC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/skin-monitor/remove", methods=["POST"])
def api_skin_monitor_remove():
    data = request.get_json()
    sid = data.get("skin_id")
    if not sid:
        return jsonify({"error": "缺少 skin_id"}), 400
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM skin_monitor WHERE skin_id=?", (sid,))
        conn.commit()
    return jsonify({"ok": True})


def get_volume_threshold(price: float) -> int:
    """根据价格区间返回成交量异动阈值"""
    if price <= 100:
        return 50
    elif price <= 1000:
        return 20
    elif price <= 10000:
        return 10
    else:
        return 5


def check_skin_volume():
    """后台巡检所有皮肤成交量异动"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        skins = conn.execute("SELECT * FROM skin_monitor WHERE enabled=1").fetchall()

    now = datetime.now().isoformat()
    alert_count = 0

    for skin in skins:
        try:
            r = requests.get(
                f"{BASE_URL}/info/good", params={"id": skin["skin_id"]}, headers=HEADERS, timeout=10
            )
            if r.status_code != 200:
                continue
            goods = r.json().get("data", {}).get("goods_info", {})
            if not goods:
                continue

            price = goods.get("buff_sell_price") or goods.get("yyyp_sell_price") or 0
            volume = goods.get("turnover_number") or 0

            if not price or not volume:
                continue

            # 记录成交量快照
            with sqlite3.connect(str(DB_PATH)) as conn2:
                conn2.execute(
                    "INSERT INTO volume_snapshots (skin_id, volume, price, captured_at) VALUES (?,?,?,?)",
                    (skin["skin_id"], volume, price, now),
                )

                # 查15分钟前的成交量
                since = (datetime.now() - timedelta(minutes=15)).isoformat()
                prev = conn2.execute(
                    "SELECT volume FROM volume_snapshots WHERE skin_id=? AND captured_at<=? ORDER BY captured_at ASC LIMIT 1",
                    (skin["skin_id"], since),
                ).fetchone()

                prev_vol = prev[0] if prev else 0
                vol_change = volume - prev_vol if prev_vol > 0 else 0

                threshold = get_volume_threshold(price)

                conn2.execute(
                    "UPDATE skin_monitor SET current_price=?, current_volume=?, prev_volume=?, vol_spike=?, alert_threshold=?, last_checked=? WHERE skin_id=?",
                    (price, volume, prev_vol, vol_change, threshold, now, skin["skin_id"]),
                )
                conn2.commit()

            # 判断异动
            if vol_change >= threshold:
                alert_count += 1
                direction = "放量暴涨" if price > (skin["current_price"] or price) else "放量异动"
                msg = f"[{direction}] {skin['skin_name']} 15分钟成交{vol_change}件 (阈值{threshold}件) | 当前¥{price:.2f}"
                _save_alert(skin["skin_name"], "volume_spike", msg)
                send_alert_email(msg)
                print(f"[VOLUME ALERT] {msg}")

            time.sleep(1.2)
        except Exception as e:
            print(f"[VOLUME_CHECK] {skin['skin_id']} 失败: {e}")

    if alert_count > 0:
        print(f"[VOLUME_CHECK] 本轮发现 {alert_count} 个异动")


def _save_alert(name_key, alert_type, message):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO alerts (name_key, alert_type, message, created_at) VALUES (?,?,?,?)",
            (name_key, alert_type, message, datetime.now().isoformat()),
        )
        conn.commit()


def auto_discover_all_mainstream():
    """全量搜索主流武器饰品，过滤无成交量的，加入监控池"""
    # 覆盖所有主流武器类型
    keywords = [
        # 步枪
        "AK-47", "M4A1", "M4A4", "AUG", "SG 553", "FAMAS", "Galil AR",
        # 狙击
        "AWP", "SSG 08", "SCAR-20", "G3SG1",
        # 冲锋枪
        "MAC-10", "MP9", "MP7", "P90", "UMP-45", "PP-Bizon",
        # 手枪
        "Desert Eagle", "USP-S", "Glock-18", "P250", "Five-SeveN", "CZ75-Auto", "Tec-9", "R8 Revolver",
        # 重武器
        "MAG-7", "Nova", "XM1014", "Sawed-Off", "M249", "Negev",
        # 刀
        "Karambit", "M9 Bayonet", "Butterfly Knife", "Skeleton Knife", "Bayonet",
        "Flip Knife", "Gut Knife", "Huntsman Knife", "Falchion Knife", "Bowie Knife",
        "Shadow Daggers", "Navaja Knife", "Stiletto Knife", "Talon Knife", "Ursus Knife",
        "Classic Knife", "Paracord Knife", "Survival Knife", "Nomad Knife",
        # 手套
        "Driver Gloves", "Specialist Gloves", "Sport Gloves", "Moto Gloves",
        "Hand Wraps", "Bloodhound Gloves", "Hydra Gloves",
    ]

    total_added = 0
    for kw in keywords:
        try:
            r = requests.get(
                f"{BASE_URL}/search/suggest", params={"text": kw, "limitNum": 10}, headers=HEADERS, timeout=10
            )
            if r.status_code != 200:
                continue
            items = r.json().get("data", [])
            for item in items:
                sid = int(item.get("id", 0))
                name = item.get("value", "")
                if not sid or not name:
                    continue

                with sqlite3.connect(str(DB_PATH)) as conn:
                    exists = conn.execute("SELECT 1 FROM skin_monitor WHERE skin_id=?", (sid,)).fetchone()
                    if not exists:
                        # 先查一下是否有成交量
                        try:
                            gr = requests.get(
                                f"{BASE_URL}/info/good", params={"id": sid}, headers=HEADERS, timeout=8
                            )
                            if gr.status_code == 200:
                                gd = gr.json().get("data", {}).get("goods_info", {})
                                turnover = gd.get("turnover_number") or 0
                                price = gd.get("buff_sell_price") or 0
                                if turnover > 0 and price > 0:
                                    threshold = get_volume_threshold(price)
                                    conn.execute(
                                        "INSERT INTO skin_monitor (skin_id, skin_name, current_price, current_volume, alert_threshold, enabled, added_at) VALUES (?,?,?,?,?,1,?)",
                                        (sid, name, price, turnover, threshold, datetime.now().isoformat()),
                                    )
                                    conn.commit()
                                    total_added += 1
                        except Exception:
                            pass
                time.sleep(0.15)
        except Exception as e:
            print(f"[DISCOVER] {kw} 失败: {e}")
        time.sleep(0.3)

    print(f"[DISCOVER] 全量发现完成，共 {total_added} 个有成交量的主流饰品")


def skin_monitor_loop():
    """后台成交量异动巡检 (每 5 分钟)"""
    print("[SKIN_MONITOR] 开始全量发现主流饰品...")
    auto_discover_all_mainstream()
    while True:
        try:
            check_skin_volume()
        except Exception as e:
            print(f"[SKIN_MONITOR] 巡检异常: {e}")
        time.sleep(300)


@app.route("/api/ranks", methods=["POST"])
def api_ranks():
    """涨幅榜 / 跌幅榜"""
    data = request.get_json() or {}
    rank_type = data.get("type", "rise")  # rise / fall
    page = data.get("page", 1)
    try:
        r = requests.post(
            f"{BASE_URL}/info/get_rank_list",
            json={"page": page, "pageSize": 20, "type": rank_type},
            headers=HEADERS,
            timeout=10,
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


@app.route("/api/email/config")
def api_get_email_config():
    """获取当前邮件配置(隐藏授权码)"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM email_config LIMIT 1").fetchone()
        if row:
            cfg = dict(row)
            cfg["auth_code"] = "***" if cfg.get("auth_code") else ""
            return jsonify(cfg)
        return jsonify({"enabled": 0, "sender_email": "", "receiver_email": ""})


@app.route("/api/email/config", methods=["POST"])
def api_set_email_config():
    """配置邮件通知"""
    data = request.get_json()
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM email_config")
        conn.execute(
            "INSERT INTO email_config (smtp_server, smtp_port, sender_email, auth_code, receiver_email, enabled) VALUES (?, ?, ?, ?, ?, 1)",
            (
                data.get("smtp_server", "smtp.qq.com"),
                data.get("smtp_port", 465),
                data.get("sender_email", ""),
                data.get("auth_code", ""),
                data.get("receiver_email", ""),
            ),
        )
        conn.commit()
    return jsonify({"ok": True, "msg": "邮件配置已保存"})


@app.route("/api/email/test", methods=["POST"])
def api_test_email():
    """发送测试邮件"""
    cfg = get_email_config()
    if not cfg or not cfg.get("receiver_email"):
        data = request.get_json()
        if data:
            sender = data.get("sender_email", "")
            auth = data.get("auth_code", "")
            receiver = data.get("receiver_email", "")
            if not sender or not auth or not receiver:
                return jsonify({"ok": False, "msg": "请先填写完整信息再测试"}), 400
            cfg = {"smtp_server": "smtp.qq.com", "smtp_port": 465,
                   "sender_email": sender, "auth_code": auth, "receiver_email": receiver}
        else:
            return jsonify({"ok": False, "msg": "请先配置邮箱"}), 400

    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["sender_email"]
        msg["To"] = cfg["receiver_email"]
        msg["Subject"] = "[CS2行情监控] 测试邮件"
        body = f"<h3>CS2 饰品行情监控</h3><p>邮件通知配置成功！</p><p style='color:#888'>{datetime.now()}</p>"
        msg.attach(MIMEText(body, "html", "utf-8"))

        with smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"]) as server:
            server.login(cfg["sender_email"], cfg["auth_code"])
            server.sendmail(cfg["sender_email"], cfg["receiver_email"], msg.as_string())

        return jsonify({"ok": True, "msg": "测试邮件已发送，请检查收件箱"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"发送失败: {str(e)}"}), 500


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


@app.route("/echarts.min.js")
def echarts_js():
    return send_from_directory(".", "echarts.min.js")


@app.route("/")
def index():
    return send_from_directory(".", "cs_monitor_frontend.html")


# ─── 启动 (模块加载时执行, gunicorn/直接运行都生效) ──
print("初始化数据库...")
init_db()
print("首次采集数据...")
try:
    collect_and_store()
    print("[OK] 首次采集完成")
except Exception as e:
    print(f"[WARN] 首次采集失败: {e}")
print("启动后台采集线程 (每 20 秒)...")
t = threading.Thread(target=collector_loop, daemon=True)
t.start()
print("启动皮肤监控线程 (每 5 分钟)...")
t2 = threading.Thread(target=skin_monitor_loop, daemon=True)
t2.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print(f"CS 饰品行情监控系统已启动 - 端口 {port}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
