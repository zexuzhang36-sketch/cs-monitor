"""
CS2 饰品行情监控系统 v2
数据源: csqaq.com API | 功能: 大盘指数 + 涨跌榜 + 热门排行 + K线 + 成交量异动 + QQ邮件通知
"""

import json, os, sqlite3, smtplib, time, threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=".")
CORS_IMPORTED = True
try:
    from flask_cors import CORS
except ImportError:
    CORS_IMPORTED = False

if CORS_IMPORTED:
    CORS(app)

BASE_URL = "https://api.csqaq.com/api/v1"
DB_PATH = Path(__file__).parent / "cs_monitor.db"
API_TOKEN = "XGCWH1F7Y8U3P8X7L3F7G6X3"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "ApiToken": API_TOKEN}

# 缓存
_cache = {}
_cache_ttl = {}

def cached(key, ttl=60):
    """简单内存缓存"""
    now = time.time()
    if key in _cache and _cache_ttl.get(key, 0) > now:
        return _cache[key]
    return None

def cache_set(key, data, ttl=60):
    _cache[key] = data
    _cache_ttl[key] = time.time() + ttl

# ─── 数据库 ─────────────────────────────
def init_db():
    with sqlite3.connect(str(DB_PATH)) as conn:
        for sql in [
            """CREATE TABLE IF NOT EXISTS market_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name_key TEXT NOT NULL, name TEXT NOT NULL,
                market_index REAL, chg_num REAL, chg_rate REAL, open REAL, close REAL,
                high REAL, low REAL, captured_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS alerts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name_key TEXT, alert_type TEXT,
                message TEXT NOT NULL, created_at TEXT NOT NULL, seen INTEGER DEFAULT 0)""",
            """CREATE INDEX IF NOT EXISTS idx_snapshots_nk ON market_snapshots(name_key, captured_at)""",
            """CREATE TABLE IF NOT EXISTS watchlist(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, threshold REAL DEFAULT 2.0, added_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS email_config(
                id INTEGER PRIMARY KEY AUTOINCREMENT, smtp_server TEXT DEFAULT 'smtp.qq.com',
                smtp_port INTEGER DEFAULT 465, sender_email TEXT, auth_code TEXT,
                receiver_email TEXT, enabled INTEGER DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS skin_monitor(
                id INTEGER PRIMARY KEY AUTOINCREMENT, skin_id INTEGER NOT NULL UNIQUE,
                skin_name TEXT, current_price REAL DEFAULT 0, current_volume INTEGER DEFAULT 0,
                prev_volume INTEGER DEFAULT 0, alert_threshold INTEGER DEFAULT 15,
                vol_spike INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
                last_checked TEXT, added_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS volume_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT, skin_id INTEGER NOT NULL,
                volume INTEGER NOT NULL, price REAL, captured_at TEXT NOT NULL)""",
        ]:
            conn.execute(sql)
        conn.commit()

# ─── 邮件 ────────────────────────────────
def get_email_config():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM email_config WHERE enabled=1 LIMIT 1").fetchone()
        return dict(r) if r else None

DINGTALK_TOKEN = "dcc66be677403ec3a76d66b4a33169e914d40bf64782eead62d6669e94fd5a8f"
DINGTALK_SECRET = "SECe8aea37f4a6e964b28b35d75880b826a0c1dddc8b3069108b3000382671b7c78"

def send_alert_notification(message: str):
    """钉钉机器人推送 (QQ邮箱备用)"""
    import hmac as _hmac, hashlib as _hashlib, base64 as _b64, urllib.parse as _urlp
    try:
        ts = str(round(time.time() * 1000))
        sign = _b64.b64encode(_hmac.new(DINGTALK_SECRET.encode(), f'{ts}\n{DINGTALK_SECRET}'.encode(), _hashlib.sha256).digest())
        url = f"https://oapi.dingtalk.com/robot/send?access_token={DINGTALK_TOKEN}&timestamp={ts}&sign={_urlp.quote_plus(sign.decode())}"
        r = requests.post(url, json={"msgtype":"markdown","markdown":{"title":"CS2行情异动","text":f"### CS2行情异动\n> {message}\n\n###### {datetime.now().strftime('%m-%d %H:%M')} | csqaq.com"}}, timeout=10)
        if r.status_code == 200: print(f"[DING] 已推送")
    except Exception as e: print(f"[DING] 失败: {e}")

    # QQ邮箱备用
    cfg = get_email_config()
    if not cfg or not cfg.get("receiver_email"): return
    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["sender_email"]
        msg["To"] = cfg["receiver_email"]
        msg["Subject"] = f"[CS2] {message[:60]}"
        msg.attach(MIMEText(f"<h2>CS2 行情异动</h2><p>{message}</p><p>{datetime.now()}</p>", "html", "utf-8"))
        if cfg["smtp_port"] == 587:
            server = smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"], timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"], timeout=15)
        server.login(cfg["sender_email"], cfg["auth_code"])
        server.sendmail(cfg["sender_email"], cfg["receiver_email"], msg.as_string())
        server.quit()
    except Exception as e: print(f"[EMAIL] 失败: {e}")

def _save_alert(nk, atype, msg):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("INSERT INTO alerts (name_key, alert_type, message, created_at) VALUES (?,?,?,?)",
                     (nk, atype, msg, datetime.now().isoformat()))
        conn.commit()

# ─── 数据采集 ────────────────────────────
def fetch_current_data():
    try:
        r = requests.get(f"{BASE_URL}/current_data", headers=HEADERS, timeout=15)
        return r.json().get("data", {}) if r.status_code == 200 else None
    except: return None

def get_volume_threshold(price: float) -> int:
    if price <= 100: return 50
    elif price <= 1000: return 20
    elif price <= 10000: return 10
    else: return 5

INDEX_NAMES = {
    "init": "饰品指数", "lease": "租赁指数", "main_weapon": "百元主战",
    "agent": "探员指数", "no_painted": "原皮指数", "covert_weapon": "隐秘指数",
    "thousand_weapon": "千战指数", "sticker": "贴纸指数", "gloves": "手套指数",
    "knives": "匕首指数", "doppler": "多普勒", "gamma_doppler": "伽玛多普勒",
    "first_generation": "一代手套", "second_generation": "二代手套", "third_generation": "三代手套",
    "charm": "挂件指数", "collection": "收藏品", "music_kits": "音乐盒",
    "24sh": "2024上海", "24ast": "2025奥斯汀", "24hg": "2024哥本", "23paris": "2023巴黎", "wk": "武库指数",
}

def collect_and_store():
    data = fetch_current_data()
    if not data: return
    now = datetime.now().isoformat()
    with sqlite3.connect(str(DB_PATH)) as conn:
        for item in data.get("sub_index_data", []):
            nk = item.get("name_key", "")
            name = INDEX_NAMES.get(nk, item.get("name", nk))
            prev = conn.execute("SELECT market_index FROM market_snapshots WHERE name_key=? ORDER BY captured_at DESC LIMIT 1", (nk,)).fetchone()
            conn.execute("""INSERT INTO market_snapshots (name_key,name,market_index,chg_num,chg_rate,open,close,high,low,captured_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (nk, name, item.get("market_index"), item.get("chg_num"), item.get("chg_rate"),
                 item.get("open"), item.get("close"), item.get("high"), item.get("low"), now))
            prev_data = {"market_index": prev[0]} if prev else None
            check_index_alert(item, prev_data, nk, name)
        conn.commit()

def check_index_alert(cur, prev, nk, name):
    if not prev or not prev.get("market_index"): return
    cur_idx = cur.get("market_index", 0)
    prev_idx = prev["market_index"]
    if not prev_idx: return
    change_pct = (cur_idx - prev_idx) / prev_idx * 100
    with sqlite3.connect(str(DB_PATH)) as conn:
        wl = conn.execute("SELECT threshold FROM watchlist WHERE name_key=?", (nk,)).fetchone()
    threshold = wl[0] if wl else 3.0
    if abs(change_pct) >= threshold:
        direction = "大涨" if change_pct > 0 else "大跌"
        tag = "[自选]" if wl else ""
        msg = f"{tag}[{direction}] {name} {cur_idx:.2f} ({change_pct:+.2f}%)"
        _save_alert(nk, "big_move", msg)
        send_alert_notification(msg)

def collector_loop():
    while True:
        try: collect_and_store()
        except Exception as e: print(f"[COLLECT] {e}")
        time.sleep(20)

# ─── 皮肤成交量监控 ──────────────────────
def auto_discover_all_mainstream():
    """全量搜索主流武器饰品"""
    keywords = [
        "AK-47", "M4A1", "M4A4", "AUG", "SG 553", "FAMAS", "Galil AR",
        "AWP", "SSG 08", "SCAR-20", "G3SG1",
        "MAC-10", "MP9", "MP7", "P90", "UMP-45", "PP-Bizon",
        "Desert Eagle", "USP-S", "Glock-18", "P250", "Five-SeveN", "CZ75-Auto", "Tec-9",
        "MAG-7", "Nova", "XM1014", "Sawed-Off", "M249", "Negev",
        "Karambit", "M9 Bayonet", "Butterfly Knife", "Skeleton Knife", "Bayonet",
        "Flip Knife", "Gut Knife", "Huntsman Knife", "Falchion Knife", "Bowie Knife",
        "Shadow Daggers", "Navaja Knife", "Stiletto Knife", "Talon Knife", "Ursus Knife",
        "Classic Knife", "Paracord Knife", "Survival Knife", "Nomad Knife",
        "Driver Gloves", "Specialist Gloves", "Sport Gloves", "Moto Gloves",
        "Hand Wraps", "Bloodhound Gloves", "Hydra Gloves",
    ]
    added = 0
    for kw in keywords:
        try:
            r = requests.get(f"{BASE_URL}/search/suggest", params={"text": kw, "limitNum": 8}, headers=HEADERS, timeout=10)
            if r.status_code != 200: continue
            items = r.json().get("data", [])
            for item in items:
                sid = int(item.get("id", 0))
                name = item.get("value", "")
                if not sid or not name: continue
                with sqlite3.connect(str(DB_PATH)) as conn:
                    if conn.execute("SELECT 1 FROM skin_monitor WHERE skin_id=?", (sid,)).fetchone(): continue
                time.sleep(0.12)
                try:
                    gr = requests.get(f"{BASE_URL}/info/good", params={"id": sid}, headers=HEADERS, timeout=8)
                    if gr.status_code != 200: continue
                    gd = gr.json().get("data", {}).get("goods_info", {})
                    turnover = gd.get("turnover_number") or 0
                    price = gd.get("buff_sell_price") or 0
                    if turnover > 0 and price > 0:
                        with sqlite3.connect(str(DB_PATH)) as conn:
                            conn.execute("INSERT INTO skin_monitor (skin_id,skin_name,current_price,current_volume,alert_threshold,enabled,added_at) VALUES (?,?,?,?,?,1,?)",
                                (sid, name, price, turnover, get_volume_threshold(price), datetime.now().isoformat()))
                            conn.commit()
                        added += 1
                except: pass
        except Exception as e: print(f"[DISCOVER] {kw}: {e}")
        time.sleep(0.3)
    print(f"[DISCOVER] +{added} skins, total monitored")

def check_skin_volume():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        skins = conn.execute("SELECT * FROM skin_monitor WHERE enabled=1").fetchall()
    now = datetime.now().isoformat()
    alerts = 0
    for skin in skins:
        try:
            r = requests.get(f"{BASE_URL}/info/good", params={"id": skin["skin_id"]}, headers=HEADERS, timeout=10)
            if r.status_code != 200: continue
            gd = r.json().get("data", {}).get("goods_info", {})
            if not gd: continue
            price = gd.get("buff_sell_price") or 0
            volume = gd.get("turnover_number") or 0
            if not price or not volume: continue

            # 1小时成交量 = 当前总成交量 - 1小时前总成交量
            hour_ago = (datetime.now() - timedelta(hours=1)).isoformat()
            conn3 = sqlite3.connect(str(DB_PATH))
            prev_row = conn3.execute(
                "SELECT volume FROM volume_snapshots WHERE skin_id=? AND captured_at<=? ORDER BY captured_at DESC LIMIT 1",
                (skin["skin_id"], hour_ago)
            ).fetchone()
            conn3.close()
            prev_vol = prev_row[0] if prev_row else 0
            spike = volume - prev_vol if prev_vol > 0 else 0
            threshold = get_volume_threshold(price)

            with sqlite3.connect(str(DB_PATH)) as conn2:
                conn2.execute("INSERT INTO volume_snapshots (skin_id,volume,price,captured_at) VALUES (?,?,?,?)",
                              (skin["skin_id"], volume, price, now))
                conn2.execute("UPDATE skin_monitor SET current_price=?,current_volume=?,prev_volume=?,vol_spike=?,alert_threshold=?,last_checked=? WHERE skin_id=?",
                              (price, volume, prev_vol, spike, threshold, now, skin["skin_id"]))
                conn2.commit()

            # 价格异动检测 (±5%)
            price_change = 0
            prev_price = skin["current_price"] or 0
            if prev_price > 0:
                price_change = (price - prev_price) / prev_price * 100
            if abs(price_change) >= 5 and prev_price > 0:
                direction = "大涨" if price_change > 0 else "大跌"
                msg = f"[价格{direction}] {skin['skin_name']} {price_change:+.2f}% | ¥{prev_price:.2f}→¥{price:.2f}"
                _save_alert(skin["skin_name"], "price_move", msg)
                send_alert_notification(msg)
                print(f"[PRICE] {msg}")

            if spike >= threshold:
                alerts += 1
                direction = "放量暴涨" if price > (skin["current_price"] or price) else "放量异动"
                msg = f"[{direction}] {skin['skin_name']} 1h成交{spike}件(阈{threshold}) ¥{price:.2f}"
                _save_alert(skin["skin_name"], "volume_spike", msg)
                send_alert_notification(msg)
                print(f"[VOLUME] {msg}")
            time.sleep(1.1)
        except Exception as e: print(f"[VOLUME] err {skin['skin_id']}: {e}")
    if alerts: print(f"[VOLUME] {alerts} alerts")

def skin_monitor_loop():
    print("[SKIN_MONITOR] Discovering...")
    auto_discover_all_mainstream()
    while True:
        try: check_skin_volume()
        except Exception as e: print(f"[SKIN_MONITOR] {e}")
        time.sleep(300)

# ═══════════ API 路由 ═══════════

@app.route("/api/dashboard")
def api_dashboard():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        indices = conn.execute("""SELECT name_key,name,market_index,chg_num,chg_rate,captured_at FROM market_snapshots
            WHERE (name_key,captured_at) IN (SELECT name_key,MAX(captured_at) FROM market_snapshots GROUP BY name_key)
            ORDER BY ABS(chg_rate) DESC""").fetchall()
        alert_count = conn.execute("SELECT COUNT(*) FROM alerts WHERE seen=0").fetchone()[0]
        last_update = conn.execute("SELECT MAX(captured_at) FROM market_snapshots").fetchone()[0]
    return jsonify({"indices": [dict(r) for r in indices], "alert_count": alert_count, "last_update": last_update})

@app.route("/api/history/<name_key>")
def api_history(name_key: str):
    period = request.args.get("period", "24h")
    hours = {"1h": 1, "2h": 2, "24h": 24, "7d": 168}.get(period, 24)
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT market_index,captured_at FROM market_snapshots WHERE name_key=? AND captured_at>=? ORDER BY captured_at ASC",
                            (name_key, since)).fetchall()
    raw = [dict(r) for r in rows]
    if not raw: return jsonify({"points": [], "count": 0})
    if period in ("1h", "2h"):
        pts = [{"t": r["captured_at"][5:19].replace("T", " "), "price": round(r["market_index"], 2)} for r in raw]
        return jsonify({"points": pts, "count": len(pts)})
    # OHLC聚合
    bucket_m = 10 if period == "24h" else 120
    ohlc, bucket, bs = [], [], None
    for r in raw:
        price, ts = r["market_index"], r["captured_at"]
        if not bucket: bs = ts
        bucket.append(price)
        try:
            if (datetime.fromisoformat(ts) - datetime.fromisoformat(bs)).total_seconds() >= bucket_m * 60:
                ohlc.append({"t": bs[5:19].replace("T", " "), "open": round(bucket[0], 2),
                              "close": round(bucket[-1], 2), "high": round(max(bucket), 2), "low": round(min(bucket), 2)})
                bucket, bs = [], None
        except: pass
    if bucket:
        ohlc.append({"t": (bs or "")[5:19].replace("T", " "), "open": round(bucket[0], 2),
                      "close": round(bucket[-1], 2), "high": round(max(bucket), 2), "low": round(min(bucket), 2)})
    return jsonify({"points": ohlc, "count": len(ohlc), "raw_count": len(raw)})

@app.route("/api/indices")
def api_indices():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT name_key,name,market_index,chg_num,chg_rate,captured_at FROM market_snapshots
            WHERE (name_key,captured_at) IN (SELECT name_key,MAX(captured_at) FROM market_snapshots GROUP BY name_key)
            ORDER BY ABS(chg_rate) DESC""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts")
def api_alerts():
    limit = request.args.get("limit", 20, type=int)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts/seen", methods=["POST"])
def api_alerts_seen():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("UPDATE alerts SET seen=1 WHERE seen=0"); conn.commit()
    return jsonify({"ok": True})

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    if not q: return jsonify([])
    try:
        r = requests.get(f"{BASE_URL}/search/suggest", params={"text": q, "limitNum": 15}, headers=HEADERS, timeout=10)
        return jsonify(r.json().get("data", []) if r.status_code == 200 else [])
    except: return jsonify([])

@app.route("/api/skin/<int:skin_id>")
def api_skin_detail(skin_id: int):
    try:
        r = requests.get(f"{BASE_URL}/info/good", params={"id": skin_id}, headers=HEADERS, timeout=10)
        if r.status_code != 200: return jsonify({})
        gd = r.json().get("data", {}).get("goods_info", {})
        if not gd: return jsonify({})
        # 历史涨跌
        history = []
        for label, days in [("1天", 1), ("7天", 7), ("15天", 15), ("30天", 30), ("90天", 90), ("180天", 180), ("365天", 365)]:
            rate = gd.get(f"sell_price_rate_{days}")
            chg = gd.get(f"sell_price_{days}")
            if rate is not None: history.append({"label": label, "days": days, "rate": rate, "change": chg})
        gd["_price_history"] = history
        return jsonify(gd)
    except: return jsonify({})

@app.route("/api/skin/<int:skin_id>/chart")
def api_skin_chart(skin_id: int):
    period = request.args.get("period", "7")
    try:
        r = requests.post(f"{BASE_URL}/info/chart", json={"good_id": str(skin_id), "key": "sell_price",
                          "platform": 1, "period": period, "style": "all_style"}, headers=HEADERS, timeout=30)
        if r.status_code != 200: return jsonify({"error": "API failed"}), 500
        data = r.json().get("data", {})
        timestamps, prices = data.get("timestamp", []), data.get("main_data", [])
        if not timestamps: return jsonify({"error": "no data"}), 404
        points = []
        for ts, price in zip(timestamps, prices):
            dt = datetime.fromtimestamp(ts / 1000)
            points.append({"t": dt.strftime("%Y-%m-%d %H:%M"), "ts": ts, "price": round(price, 2)})
        return jsonify({"points": points, "count": len(points)})
    except Exception as e: return jsonify({"error": str(e)}), 500

# ─── 涨跌榜 + 热门推荐 ───────────────────

@app.route("/api/rank-list")
def api_rank_list():
    """涨跌榜 / 热门榜"""
    rtype = request.args.get("type", "rise")  # rise, fall, hot, volume
    page = request.args.get("page", 1, type=int)

    # 尝试从缓存获取
    cache_key = f"rank_{rtype}_{page}"
    cached_data = cached(cache_key, 120)
    if cached_data: return jsonify(cached_data)

    try:
        # use get_rank_list for rise/fall
        r = requests.post(f"{BASE_URL}/info/get_rank_list",
                          json={"page": page, "pageSize": 20, "type": rtype}, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", {})
            items = data.get("data", []) if isinstance(data, dict) else []
            result = []
            for item in items:
                result.append({
                    "id": item.get("id") or item.get("good_id", ""),
                    "name": item.get("name") or item.get("goodsName") or item.get("value", ""),
                    "price": item.get("salePrice") or item.get("price") or item.get("buff_sell_price", 0),
                    "chg_rate": item.get("sellPriceRate") or item.get("chg_rate") or item.get("rate", 0),
                    "chg_num": item.get("sell_price_1") or item.get("chg_num", 0),
                    "volume": item.get("turnoverNumber") or item.get("turnover_number", 0),
                })
            cache_set(cache_key, result, 120)
            return jsonify(result)
    except: pass
    return jsonify([])


@app.route("/api/hot-series")
def api_hot_series():
    """热门系列"""
    ck = cached("hot_series", 300)
    if ck: return jsonify(ck)
    try:
        r = requests.post(f"{BASE_URL}/info/get_series_list", json={}, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if isinstance(data, list):
                cache_set("hot_series", data, 300)
                return jsonify(data)
    except: pass
    return jsonify([])


@app.route("/api/arbitrage")
def api_arbitrage():
    """挂刀比例排行"""
    ck = cached("arbitrage", 300)
    if ck: return jsonify(ck)
    try:
        # use exchange_detail or search for popular items
        r = requests.post(f"{BASE_URL}/info/get_page_list",
                          json={"page": 1, "pageSize": 20, "sort": "arbitrage"},
                          headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if isinstance(data, dict): data = data.get("data", [])
            cache_set("arbitrage", data, 300)
            return jsonify(data)
    except: pass
    return jsonify([])

# ─── 自选监控 ────────────────────────────

@app.route("/api/watchlist")
def api_watchlist():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
    result = []
    for r in rows:
        item = dict(r)
        latest = conn.execute("SELECT market_index,chg_rate,captured_at FROM market_snapshots WHERE name_key=? ORDER BY captured_at DESC LIMIT 1",
                              (r["name_key"],)).fetchone()
        if latest:
            item["current_price"] = latest[0]; item["chg_rate"] = latest[1]; item["updated_at"] = latest[2]
        result.append(item)
    return jsonify(result)

@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    d = request.get_json()
    nk, name, th = d.get("name_key"), d.get("name", nk), d.get("threshold", 2.0)
    if not nk: return jsonify({"error": "missing"}), 400
    with sqlite3.connect(str(DB_PATH)) as conn:
        try:
            conn.execute("INSERT INTO watchlist (name_key,name,threshold,added_at) VALUES (?,?,?,?)",
                         (nk, name, th, datetime.now().isoformat())); conn.commit()
            return jsonify({"ok": True})
        except sqlite3.IntegrityError: return jsonify({"ok": False, "msg": "already exists"})

@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    d = request.get_json()
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM watchlist WHERE name_key=?", (d.get("name_key", ""),)); conn.commit()
    return jsonify({"ok": True})

@app.route("/api/compare")
def api_compare():
    keys = [k.strip() for k in request.args.get("keys", "").split(",") if k.strip()]
    hours = request.args.get("hours", 24, type=int)
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    result = {}
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        for nk in keys:
            rows = conn.execute("SELECT market_index,captured_at FROM market_snapshots WHERE name_key=? AND captured_at>=? ORDER BY captured_at ASC",
                                (nk, since)).fetchall()
            result[nk] = {"name": INDEX_NAMES.get(nk, nk), "data": [{"price": r["market_index"], "t": r["captured_at"][5:19]} for r in rows]}
    return jsonify(result)

# ─── 皮肤监控 ────────────────────────────

@app.route("/api/skin-monitor")
def api_skin_monitor_list():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM skin_monitor WHERE enabled=1 ORDER BY added_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/skin-monitor/volume")
def api_skin_volume_list():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM skin_monitor WHERE current_volume>0 ORDER BY vol_spike DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/skin-monitor/add", methods=["POST"])
def api_skin_monitor_add():
    d = request.get_json()
    sid = d.get("skin_id"); name = d.get("skin_name", str(sid))
    if not sid: return jsonify({"error": "missing"}), 400
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("INSERT OR REPLACE INTO skin_monitor (skin_id,skin_name,enabled,added_at) VALUES (?,?,1,?)",
                     (sid, name, datetime.now().isoformat())); conn.commit()
    return jsonify({"ok": True})

@app.route("/api/skin-monitor/remove", methods=["POST"])
def api_skin_monitor_remove():
    d = request.get_json()
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM skin_monitor WHERE skin_id=?", (d.get("skin_id", 0),)); conn.commit()
    return jsonify({"ok": True})

# ─── 邮箱配置 ────────────────────────────

@app.route("/api/email/config")
def api_email_config():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM email_config LIMIT 1").fetchone()
        if r:
            cfg = dict(r); cfg["auth_code"] = "***"
            return jsonify(cfg)
    return jsonify({"enabled": 0})

@app.route("/api/email/config", methods=["POST"])
def api_email_config_set():
    d = request.get_json()
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM email_config")
        conn.execute("INSERT INTO email_config (smtp_server,smtp_port,sender_email,auth_code,receiver_email,enabled) VALUES (?,?,?,?,?,1)",
                     (d.get("smtp_server","smtp.qq.com"), d.get("smtp_port",465), d.get("sender_email"), d.get("auth_code"), d.get("receiver_email", d.get("sender_email", ""))))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/email/test", methods=["POST"])
def api_email_test():
    d = request.get_json() or {}
    cfg = get_email_config()
    if not cfg or not cfg.get("receiver_email"):
        if d.get("sender_email") and d.get("auth_code"):
            cfg = {"smtp_server": "smtp.qq.com", "smtp_port": 465, "sender_email": d["sender_email"],
                   "auth_code": d["auth_code"], "receiver_email": d.get("receiver_email", d["sender_email"])}
        else: return jsonify({"ok": False, "msg": "请先配置邮箱"}), 400
    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["sender_email"]; msg["To"] = cfg["receiver_email"]
        msg["Subject"] = "[CS2] 测试邮件"
        msg.attach(MIMEText(f"<h3>CS2 行情监控</h3><p>配置成功!</p><p style='color:#888'>{datetime.now()}</p>", "html", "utf-8"))
        if cfg["smtp_port"] == 587:
            server = smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"], timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"], timeout=15)
        server.login(cfg["sender_email"], cfg["auth_code"])
        server.sendmail(cfg["sender_email"], cfg["receiver_email"], msg.as_string())
        server.quit()
        return jsonify({"ok": True, "msg": "已发送"})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

# ─── 静态文件 ────────────────────────────

@app.route("/echarts.min.js")
def echarts_js():
    return send_from_directory(".", "echarts.min.js")

@app.route("/")
def index():
    return send_from_directory(".", "cs_monitor_frontend.html")


# ─── 启动 ────────────────────────────────
init_db()
try: collect_and_store(); print("[OK] init collect")
except Exception as e: print(f"[WARN] init collect: {e}")
t_collect = threading.Thread(target=collector_loop, daemon=True); t_collect.start()
t_skin = threading.Thread(target=skin_monitor_loop, daemon=True); t_skin.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"CS2 Monitor v2 - :{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
