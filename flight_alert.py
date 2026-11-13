#!/usr/bin/env python3
"""
KHH 週末直航機票監控
- 週五 KHH 出發 → 同週週日 返回 KHH
- 目的地：日本 / 韓國 / 中國 任一機場
- 直航（去回皆 0 轉機）
- 來回總價 < 門檻（預設 7500 TWD）→ Telegram 通知
資料來源：Travelpayouts (Aviasales) Data API v3 prices_for_dates
只用 Python 標準函式庫，不需 pip install。
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# ---------- 設定（可用環境變數覆蓋） ----------
TP_TOKEN = os.environ.get("TP_TOKEN", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

ORIGIN = os.environ.get("ORIGIN", "KHH")
PRICE_LIMIT = int(os.environ.get("PRICE_LIMIT", "7500"))
CURRENCY = os.environ.get("CURRENCY", "twd")
END_DATE = dt.date.fromisoformat(os.environ.get("END_DATE", "2027-12-31"))
COUNTRIES = set(os.environ.get("COUNTRIES", "JP,KR,CN").split(","))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "sent.json")

# 高雄可能有直航的航點（另外也會做「不指定目的地」的全面掃描，兩者合併）
DESTINATIONS = os.environ.get(
    "DESTINATIONS",
    # 日本
    "TYO,NRT,HND,OSA,KIX,FUK,OKA,NGO,SPK,CTS,KMJ,KOJ,SDJ,HIJ,TAK,OKJ,KMQ,FSZ,IBR,"
    # 韓國
    "SEL,ICN,GMP,PUS,TAE,CJU,"
    # 中國
    "SHA,PVG,XMN,CAN,SZX,HGH,NKG,FOC,CTU,TFU,CKG,PEK,PKX,BJS,TSN,WUH,CSX,KMG,XIY,TAO,NGB,WNZ,CGO,HFE,SYX,HAK",
).split(",")

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
AIRPORTS_URL = "https://api.travelpayouts.com/data/en/airports.json"
CITIES_URL = "https://api.travelpayouts.com/data/en/cities.json"

# 備用國別表（下載 airports.json 失敗時使用）
FALLBACK_COUNTRY = {
    **{c: "JP" for c in "TYO NRT HND OSA KIX ITM UKB FUK OKA NGO SPK CTS KMJ KOJ SDJ HIJ TAK OKJ KMQ FSZ IBR".split()},
    **{c: "KR" for c in "SEL ICN GMP PUS TAE CJU".split()},
    **{c: "CN" for c in ("SHA PVG XMN CAN SZX HGH NKG FOC CTU TFU CKG PEK PKX BJS TSN WUH CSX KMG "
                         "XIY TAO NGB WNZ CGO HFE SYX HAK").split()},
}


def http_get_json(url, params=None, headers=None, retries=3):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def load_country_map():
    m = dict(FALLBACK_COUNTRY)
    for url in (CITIES_URL, AIRPORTS_URL):
        try:
            for row in http_get_json(url):
                if row.get("code") and row.get("country_code"):
                    m[row["code"]] = row["country_code"]
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 無法下載 {url}: {e}", file=sys.stderr)
    return m


def weekends(start, end):
    """回傳 [(週五, 週日)]，週五 >= start 且 週五 <= end。"""
    d = start + dt.timedelta(days=(4 - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append((d, d + dt.timedelta(days=2)))
        d += dt.timedelta(days=7)
    return out


def month_pairs(wkends):
    pairs = sorted({(f.strftime("%Y-%m"), s.strftime("%Y-%m")) for f, s in wkends})
    return pairs


def fetch(dep_month, ret_month, destination=None):
    params = {
        "origin": ORIGIN,
        "departure_at": dep_month,
        "return_at": ret_month,
        "one_way": "false",
        "direct": "true",
        "currency": CURRENCY,
        "sorting": "price",
        "limit": 1000,
        "page": 1,
        "token": TP_TOKEN,
    }
    if destination:
        params["destination"] = destination
    res = http_get_json(API, params, headers={"Accept-Encoding": "identity"})
    if not res.get("success", True):
        raise RuntimeError(res.get("error"))
    return res.get("data", [])


def matches(t, weekend_set, country_map):
    try:
        dep = dt.date.fromisoformat(t["departure_at"][:10])
        ret = dt.date.fromisoformat(t["return_at"][:10])
    except (KeyError, TypeError, ValueError):
        return False
    if (dep, ret) not in weekend_set:
        return False
    if t.get("transfers", 0) != 0 or t.get("return_transfers", 0) != 0:
        return False
    if float(t.get("price", 1e12)) >= PRICE_LIMIT:
        return False
    dest_ap = t.get("destination_airport") or t.get("destination")
    country = country_map.get(dest_ap) or country_map.get(t.get("destination"))
    return country in COUNTRIES


def key_of(t):
    return f'{t.get("destination_airport") or t["destination"]}|{t["departure_at"][:10]}|{t["return_at"][:10]}|{t.get("airline","")}'


def fmt(t):
    dep = t["departure_at"]
    ret = t["return_at"]
    link = "https://www.aviasales.com" + t["link"] if t.get("link") else "https://www.aviasales.com"
    return (
        f"✈️ {t.get('origin_airport', ORIGIN)} ⇄ {t.get('destination_airport') or t['destination']}  "
        f"<b>{int(float(t['price'])):,} {CURRENCY.upper()}</b>\n"
        f"去 {dep[:10]}(五) {dep[11:16]}  {t.get('airline','')}{t.get('flight_number','')}\n"
        f"回 {ret[:10]}(日) {ret[11:16]}\n"
        f'<a href="{link}">查看 / 訂票</a>'
    )


def send_telegram(text):
    if DRY_RUN or not TG_BOT_TOKEN:
        print("---- [通知預覽] ----\n" + text)
        return
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30).read()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state, today):
    # 清掉已過期的出發日
    state = {k: v for k, v in state.items() if k.split("|")[1] >= today.isoformat()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


RESULTS_FILE = os.environ.get("RESULTS_FILE", "docs/results.json")


def write_results(best, country_map, weekend_count):
    """輸出目前所有符合條件的票，給 GitHub Pages 網頁讀取。"""
    rows = []
    for t in sorted(best.values(), key=lambda x: float(x["price"])):
        dest = t.get("destination_airport") or t["destination"]
        rows.append({
            "dest": dest,
            "country": country_map.get(dest) or country_map.get(t.get("destination"), ""),
            "price": int(float(t["price"])),
            "dep": t["departure_at"][:16].replace("T", " "),
            "ret": t["return_at"][:16].replace("T", " "),
            "airline": t.get("airline", ""),
            "flight": f'{t.get("airline","")}{t.get("flight_number","")}',
            "link": "https://www.aviasales.com" + t["link"] if t.get("link") else "",
        })
    out = {
        "updated": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "limit": PRICE_LIMIT,
        "currency": CURRENCY.upper(),
        "weekends": weekend_count,
        "results": rows,
    }
    os.makedirs(os.path.dirname(RESULTS_FILE) or ".", exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


def run(fetch_fn=fetch, today=None, country_map=None):
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    wk = weekends(today, END_DATE)
    if not wk:
        print("監控區間已結束。")
        return []
    weekend_set = set(wk)
    country_map = country_map if country_map is not None else load_country_map()

    best = {}  # key -> ticket（同一組取最低價）
    calls = 0
    for dep_m, ret_m in month_pairs(wk):
        for dest in [None] + DESTINATIONS:
            try:
                tickets = fetch_fn(dep_m, ret_m, dest)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {dep_m}/{ret_m} {dest or '*'}: {e}", file=sys.stderr)
                tickets = []
            calls += 1
            for t in tickets:
                if matches(t, weekend_set, country_map):
                    k = key_of(t)
                    if k not in best or float(t["price"]) < float(best[k]["price"]):
                        best[k] = t
            if fetch_fn is fetch:
                time.sleep(0.4)

    state = load_state()
    alerts = []
    for k, t in sorted(best.items(), key=lambda kv: float(kv[1]["price"])):
        price = float(t["price"])
        # 第一次發現，或比上次通知再便宜時才通知
        if k not in state or price < state[k]:
            alerts.append(t)
            state[k] = price

    for t in alerts:
        send_telegram(fmt(t))
    save_state(state, today)
    write_results(best, country_map, len(wk))
    print(f"掃描 {len(wk)} 個週末、{calls} 次查詢；符合 {len(best)} 筆，新通知 {len(alerts)} 筆。")
    return alerts


if __name__ == "__main__":
    if not TP_TOKEN and not DRY_RUN:
        sys.exit("請設定 TP_TOKEN（Travelpayouts API token）")
    if "--test-telegram" in sys.argv:
        send_telegram("✅ KHH 機票監控：Telegram 通知測試成功")
        sys.exit(0)
    run()
