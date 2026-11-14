#!/usr/bin/env python3
"""
KHH 週末直航機票監控
- 週五 KHH 出發 → 同週週日 返回 KHH
- 目的地：日本 / 韓國 / 中國 任一機場
- 直航（去回皆 0 轉機）
- 來回總價 < 門檻（預設 7500 TWD）→ Telegram 通知

兩種資料來源（可分開排程）：
  python flight_alert.py              → Travelpayouts（Aviasales 快取，快、免費）
  python flight_alert.py --google     → Google Flights 主動查詢（即時，需 pip install fast-flights）
兩者共用 sent.json（通知紀錄）與 docs/results.json（網頁資料），結果會合併。
"""
import datetime as dt
import json
import os
import random
import re
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
CURRENCY = "twd"
END_DATE = dt.date.fromisoformat(os.environ.get("END_DATE", "2027-12-31"))
COUNTRIES = set(os.environ.get("COUNTRIES", "JP,KR,CN").split(","))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "sent.json")
RESULTS_FILE = os.environ.get("RESULTS_FILE", "docs/results.json")

# Travelpayouts 掃描的航點（另外也會做「不指定目的地」的全面掃描）
DESTINATIONS = os.environ.get(
    "DESTINATIONS",
    "TYO,NRT,HND,OSA,KIX,FUK,OKA,NGO,SPK,CTS,KMJ,KOJ,SDJ,HIJ,TAK,OKJ,KMQ,FSZ,IBR,"
    "SEL,ICN,GMP,PUS,TAE,CJU,"
    "SHA,PVG,XMN,CAN,SZX,HGH,NKG,FOC,CTU,TFU,CKG,PEK,PKX,BJS,TSN,WUH,CSX,KMG,XIY,TAO,NGB,WNZ,CGO,HFE,SYX,HAK",
).split(",")

# Google Flights 主動查詢的航點（每個航點 × 每個週末 = 1 次查詢，所以只放高雄實際有直航的）
GF_DESTINATIONS = os.environ.get(
    "GF_DESTINATIONS",
    "NRT,HND,KIX,FUK,OKA,NGO,KMJ,CTS,ICN,PUS,TAE,PVG,XMN,SZX,CAN,HGH,NKG",
).split(",")

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
AIRPORTS_URL = "https://api.travelpayouts.com/data/en/airports.json"
CITIES_URL = "https://api.travelpayouts.com/data/en/cities.json"
FX_URL = "https://open.er-api.com/v6/latest/TWD"

FALLBACK_COUNTRY = {
    **{c: "JP" for c in "TYO NRT HND OSA KIX ITM UKB FUK OKA NGO SPK CTS KMJ KOJ SDJ HIJ TAK OKJ KMQ FSZ IBR".split()},
    **{c: "KR" for c in "SEL ICN GMP PUS TAE CJU".split()},
    **{c: "CN" for c in ("SHA PVG XMN CAN SZX HGH NKG FOC CTU TFU CKG PEK PKX BJS TSN WUH CSX KMG "
                         "XIY TAO NGB WNZ CGO HFE SYX HAK").split()},
}

TW = dt.timezone(dt.timedelta(hours=8))


# ---------- 共用工具 ----------
def http_get_json(url, params=None, headers=None, retries=3):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
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
    return sorted({(f.strftime("%Y-%m"), s.strftime("%Y-%m")) for f, s in wkends})


def dest_of(t):
    return t.get("destination_airport") or t["destination"]


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
    country = country_map.get(dest_of(t)) or country_map.get(t.get("destination"))
    return country in COUNTRIES


def key_of(t):
    # 同一目的地＋同一週末只保留最便宜的一筆（不論哪個來源、哪家航空）
    return f'{dest_of(t)}|{t["departure_at"][:10]}|{t["return_at"][:10]}'


def link_of(t):
    if t.get("url"):
        return t["url"]
    if t.get("link"):
        return "https://www.aviasales.com" + t["link"]
    return "https://www.aviasales.com"


def fmt(t):
    dep, ret = t["departure_at"], t["return_at"]
    src = "Google Flights" if t.get("source") == "google" else "Aviasales"
    return (
        f"✈️ {ORIGIN} ⇄ {dest_of(t)}  <b>{int(float(t['price'])):,} TWD</b>\n"
        f"去 {dep[:10]}(五) {dep[11:16]}  {t.get('airline','')}{t.get('flight_number','')}\n"
        f"回 {ret[:10]}(日) {ret[11:16]}\n"
        f"來源：{src}\n"
        f'<a href="{link_of(t)}">查看 / 訂票</a>'
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
    state = {k: v for k, v in state.items() if k.split("|")[1] >= today.isoformat()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def write_results(best, country_map, weekend_count, source, today):
    """更新網頁資料：只替換本來源的結果，保留另一個來源的結果。"""
    try:
        with open(RESULTS_FILE, encoding="utf-8") as f:
            old = json.load(f).get("results", [])
    except (FileNotFoundError, json.JSONDecodeError):
        old = []
    keep = [r for r in old
            if r.get("source", "aviasales") != source
            and r["dep"][:10] >= today.isoformat()
            and r["price"] < PRICE_LIMIT]

    rows = []
    for t in best.values():
        d = dest_of(t)
        rows.append({
            "dest": d,
            "country": country_map.get(d) or country_map.get(t.get("destination"), ""),
            "price": int(float(t["price"])),
            "dep": t["departure_at"][:16].replace("T", " "),
            "ret": t["return_at"][:16].replace("T", " "),
            "airline": t.get("airline", ""),
            "flight": f'{t.get("airline","")}{t.get("flight_number","")}',
            "link": link_of(t),
            "source": source,
        })

    # 合併：同目的地同週末只留最便宜
    merged = {}
    for r in keep + rows:
        k = f'{r["dest"]}|{r["dep"][:10]}'
        if k not in merged or r["price"] < merged[k]["price"]:
            merged[k] = r
    out = {
        "updated": dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M"),
        "limit": PRICE_LIMIT,
        "currency": "TWD",
        "weekends": weekend_count,
        "results": sorted(merged.values(), key=lambda r: r["price"]),
    }
    os.makedirs(os.path.dirname(RESULTS_FILE) or ".", exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


# ---------- 來源 1：Travelpayouts（Aviasales 快取） ----------
def tp_fetch(dep_month, ret_month, destination=None):
    params = {
        "origin": ORIGIN, "departure_at": dep_month, "return_at": ret_month,
        "one_way": "false", "direct": "true", "currency": CURRENCY,
        "sorting": "price", "limit": 1000, "page": 1, "token": TP_TOKEN,
    }
    if destination:
        params["destination"] = destination
    res = http_get_json(API, params, headers={"Accept-Encoding": "identity"})
    if not res.get("success", True):
        raise RuntimeError(res.get("error"))
    tickets = res.get("data", [])
    for t in tickets:
        t["source"] = "aviasales"
    return tickets


def scan_travelpayouts(wk, fetch_fn=tp_fetch):
    calls = 0
    for dep_m, ret_m in month_pairs(wk):
        for dest in [None] + DESTINATIONS:
            try:
                yield from fetch_fn(dep_m, ret_m, dest)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {dep_m}/{ret_m} {dest or '*'}: {e}", file=sys.stderr)
            calls += 1
            if fetch_fn is tp_fetch:
                time.sleep(0.4)
    print(f"Travelpayouts 查詢 {calls} 次")


# ---------- 來源 2：Google Flights 主動查詢（fast-flights） ----------
CUR_SYMBOLS = [("NT$", "TWD"), ("US$", "USD"), ("HK$", "HKD"), ("CA$", "CAD"), ("A$", "AUD"),
               ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("₩", "KRW"), ("$", "USD")]


def load_fx():
    """回傳 {幣別: 1 TWD 可換多少該幣別}。"""
    try:
        rates = http_get_json(FX_URL)["rates"]
        if rates.get("USD"):
            return rates
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 匯率下載失敗，使用預設值: {e}", file=sys.stderr)
    return {"TWD": 1, "USD": 1 / 32.0, "JPY": 4.7, "KRW": 43.0, "EUR": 1 / 35.0}


def price_to_twd(text, fx):
    if not text:
        return None
    text = str(text).strip()
    cur = "USD"
    for sym, code in CUR_SYMBOLS:
        if text.startswith(sym) or sym in text:
            cur = code
            break
    m = re.search(r"[\d][\d,\.]*", text)
    if not m:
        return None
    amount = float(m.group(0).replace(",", ""))
    rate = fx.get(cur)
    if not rate:
        return None
    return amount / rate


def parse_clock(s):
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)?", s or "")
    if not m:
        return ""
    h, mi, ap = int(m.group(1)), m.group(2), m.group(3)
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mi}"


def gf_query(dep, ret, dest):
    """回傳 fast-flights 的 flights 列表（round-trip、去回都限直航）。"""
    from fast_flights import FlightData, Passengers, get_flights  # noqa: WPS433
    result = get_flights(
        flight_data=[
            FlightData(date=dep.isoformat(), from_airport=ORIGIN, to_airport=dest, max_stops=0),
            FlightData(date=ret.isoformat(), from_airport=dest, to_airport=ORIGIN, max_stops=0),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),
        fetch_mode="fallback",
    )
    return list(getattr(result, "flights", []) or [])


def scan_google(wk, query_fn=gf_query, fx=None):
    fx = fx or load_fx()
    calls = errors = streak = 0
    for dep, ret in wk:
        for dest in GF_DESTINATIONS:
            calls += 1
            try:
                flights = query_fn(dep, ret, dest)
                streak = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                streak += 1
                msg = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
                print(f"[warn] Google {dep} {dest}: {msg}", file=sys.stderr)
                if streak >= 20:
                    print("[error] 連續 20 次失敗（可能被 Google 暫時擋下），提前結束本次 Google 掃描", file=sys.stderr)
                    print(f"Google Flights 查詢 {calls} 次，失敗 {errors} 次")
                    return
                flights = []
            for f in flights:
                stops = getattr(f, "stops", None)
                if stops not in (0, "0", None):
                    continue
                twd = price_to_twd(getattr(f, "price", ""), fx)
                if twd is None:
                    continue
                q = urllib.parse.quote(f"Flights to {dest} from {ORIGIN} on {dep} through {ret} nonstop")
                yield {
                    "source": "google",
                    "origin": ORIGIN,
                    "destination": dest,
                    "destination_airport": dest,
                    "price": round(twd),
                    "airline": getattr(f, "name", "") or "",
                    "flight_number": "",
                    "departure_at": f"{dep.isoformat()}T{parse_clock(getattr(f, 'departure', ''))}",
                    "return_at": f"{ret.isoformat()}T",
                    "transfers": 0,
                    "return_transfers": 0,
                    "url": f"https://www.google.com/travel/flights?q={q}&curr=TWD&hl=zh-TW",
                }
            if query_fn is gf_query:
                time.sleep(random.uniform(1.0, 2.5))
    print(f"Google Flights 查詢 {calls} 次，失敗 {errors} 次")


# ---------- 主流程 ----------
def run(source="aviasales", tickets_fn=None, today=None, country_map=None):
    today = today or dt.datetime.now(TW).date()
    wk = weekends(today, END_DATE)
    if not wk:
        print("監控區間已結束。")
        return []
    weekend_set = set(wk)
    country_map = country_map if country_map is not None else load_country_map()

    if tickets_fn is None:
        tickets_fn = scan_google if source == "google" else scan_travelpayouts

    best = {}
    for t in tickets_fn(wk):
        if matches(t, weekend_set, country_map):
            k = key_of(t)
            if k not in best or float(t["price"]) < float(best[k]["price"]):
                best[k] = t

    state = load_state()
    alerts = []
    for k, t in sorted(best.items(), key=lambda kv: float(kv[1]["price"])):
        price = float(t["price"])
        prev = state.get(k)
        # 舊版紀錄的 key 多了航空公司欄位，順便相容
        if prev is None:
            prev = min((v for kk, v in state.items() if kk.startswith(k + "|")), default=None)
        if prev is None or price < prev:
            alerts.append(t)
            state[k] = price

    for t in alerts:
        send_telegram(fmt(t))
    save_state(state, today)
    write_results(best, country_map, len(wk), source, today)
    print(f"[{source}] 掃描 {len(wk)} 個週末；符合 {len(best)} 筆，新通知 {len(alerts)} 筆。")
    return alerts


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        send_telegram("✅ KHH 機票監控：Telegram 通知測試成功")
        sys.exit(0)
    if "--google" in sys.argv:
        run("google")
    else:
        if not TP_TOKEN and not DRY_RUN:
            sys.exit("請設定 TP_TOKEN（Travelpayouts API token）")
        run("aviasales")
