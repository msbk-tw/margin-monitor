"""共用工具：HTTP、數字解析、表格解析、JSON 存取、維持率計算。"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
import time
from pathlib import Path

import requests

TW = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "docs" / "data"          # 前端讀取的公開資料
STATE_DIR = ROOT / "state"                 # 融資成本累積狀態（每日滾動）
STATE_PATH = STATE_DIR / "cost_state.json"
SHARES_PATH = STATE_DIR / "shares.json"

# ---- 規則參數（可依實際狀況調整）----
CALL_LINE = 130.0            # 追繳線
NEAR_LINE = 140.0            # 接近追繳區間上緣
SHORT_MARGIN = 0.9           # 融券保證金成數
DEFAULT_MARGIN_RATE = {"TWSE": 0.6, "TPEX": 0.5}   # 融資成數：上市 6 成、上櫃 5 成
WARMUP_DAYS = 20             # 成本累積少於此天數，前端標示「暖機中」

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain",
    "Accept-Language": "zh-TW,zh;q=0.9",
})


def now_tw() -> dt.datetime:
    return dt.datetime.now(TW)


def log(*args):
    print(f"[{now_tw():%H:%M:%S}]", *args, flush=True)


def get_json(url: str, params: dict | None = None, *, tries: int = 3,
             pause: float = 3.0, referer: str | None = None):
    """GET 並解析 JSON；失敗自動重試（證交所對頻率很敏感）。"""
    last = None
    for i in range(tries):
        try:
            headers = {"Referer": referer} if referer else None
            r = SESSION.get(url, params=params, timeout=25, headers=headers)
            r.raise_for_status()
            txt = r.text.strip()
            if not txt or txt[0] not in "{[":
                raise ValueError(f"非 JSON 回應：{txt[:80]!r}")
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(pause * (i + 1))
    raise RuntimeError(f"抓取失敗 {url} {params}: {last}")


def polite_sleep(sec: float = 3.5):
    """證交所約每 5 秒 3 次以上就可能封鎖 IP，抓完一支 API 休息一下。"""
    time.sleep(sec + random.random())


_TAG = re.compile(r"<[^>]+>")


def clean(s) -> str:
    return _TAG.sub("", str(s)).replace("　", "").strip()


def num(v):
    """'1,234' → 1234.0；'--'、'-'、空字串 → None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = clean(v).replace(",", "").replace("X", "").replace(" ", "")
    if s in ("", "-", "--", "---", "除權息", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def iter_tables(js):
    """把證交所／櫃買各種 JSON 版型統一成 [(title, fields, rows)]。"""
    out = []
    if isinstance(js, dict):
        for t in js.get("tables") or []:
            if isinstance(t, dict) and t.get("data"):
                out.append((t.get("title", ""), t.get("fields") or [], t["data"]))
        if js.get("data") and isinstance(js["data"], list):
            out.append((js.get("title", ""), js.get("fields") or [], js["data"]))
        if js.get("aaData"):
            out.append(("", js.get("fields") or [], js["aaData"]))
        for k, v in js.items():          # 舊版 MI_INDEX: data1..data9 / fields1..fields9
            if re.fullmatch(r"data\d+", k) and isinstance(v, list) and v:
                n = k[4:]
                out.append((js.get(f"subtitle{n}", ""), js.get(f"fields{n}") or [], v))
    elif isinstance(js, list) and js and isinstance(js[0], dict):
        keys = list(js[0].keys())
        out.append(("", keys, [[x.get(k) for k in keys] for x in js]))
    return out


def find_col(fields, *names):
    """依欄位名稱找索引：先完全比對，再做包含比對。"""
    cf = [clean(f).replace(" ", "") for f in fields]
    for n in names:
        if n in cf:
            return cf.index(n)
    for n in names:
        for i, f in enumerate(cf):
            if n in f:
                return i
    return None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj, *, compact: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    kw = {"separators": (",", ":")} if compact else {"indent": 1}
    tmp.write_text(json.dumps(obj, ensure_ascii=False, **kw), encoding="utf-8")
    tmp.replace(path)


# ---------------- 維持率計算 ----------------

def maintenance(st: dict, px):
    """回傳 (融資維持率, 融券維持率, 整體=兩者較低)，單位 %。

    融資維持率 ≈ 現價 / (融資成本 × 融資成數)
    融券維持率 ≈ (擔保價款 + 保證金) / 現價市值 ≈ 融券成本 × (1 + 0.9) / 現價
    成本皆為推估，非券商帳戶實際數字。
    """
    m = s = None
    if px and px > 0:
        if st.get("m_bal", 0) > 0 and st.get("m_cost"):
            m = px / (st["m_cost"] * st["m_rate"]) * 100
        if st.get("s_bal", 0) > 0 and st.get("s_cost"):
            s = st["s_cost"] * (1 + SHORT_MARGIN) / px * 100
    vals = [x for x in (m, s) if x is not None]
    return m, s, (min(vals) if vals else None)


def status(r):
    if r is None:
        return None
    if r < CALL_LINE:
        return "call"
    if r < NEAR_LINE:
        return "near"
    return "ok"


def r2(x, n=2):
    return None if x is None else round(x, n)


def build_output(baseline: dict, prices: dict, *, mode: str, snapshot: str,
                 indices: dict | None = None) -> dict:
    """baseline + 價格 → 前端用的 latest.json。prices: {code: 現價}；缺價者不估算。"""
    rows = []
    agg = {mk: {"mv": 0.0, "loan": 0.0, "n": 0, "quoted": 0, "call": 0, "near": 0}
           for mk in ("TWSE", "TPEX")}
    for st in baseline["stocks"]:
        px = prices.get(st["code"])
        m, s, o = maintenance(st, px)
        a = agg[st["market"]]
        if st["m_bal"] > 0:
            a["n"] += 1
            if px:
                a["quoted"] += 1
                if st.get("m_cost"):
                    a["mv"] += st["m_bal"] * px
                    a["loan"] += st["m_bal"] * st["m_cost"] * st["m_rate"]
        so = status(o)
        if so in ("call", "near"):
            a[so] += 1
        prev = st["close"]
        chg = (px - prev) if (px and mode == "intraday" and prev) else (st.get("change") if px else None)
        base_prev = (px - chg) if (px and chg is not None) else None
        rows.append({
            "c": st["code"], "n": st["name"], "mk": st["market"], "etf": st["etf"],
            "px": r2(px), "chg": r2(chg),
            "pct": r2(chg / base_prev * 100) if (chg is not None and base_prev) else None,
            "cap": r2(px * st["shares"] / 1e8, 1) if (px and st.get("shares")) else None,
            "mr": r2(m, 1), "sr": r2(s, 1), "or": r2(o, 1), "st": so,
            "mb": int(st["m_bal"]), "mc": int(st["m_chg"]),
            "sb": int(st["s_bal"]),
            "ssr": r2(st["s_bal"] / st["m_bal"] * 100) if st["m_bal"] > 0 else None,
            "w": st.get("days", 0) < WARMUP_DAYS,
        })
    markets = {}
    for mk, a in agg.items():
        markets[mk] = {
            "ratio": r2(a["mv"] / a["loan"] * 100) if a["loan"] else None,
            "coverage": r2(a["quoted"] / a["n"] * 100, 1) if a["n"] else None,
            "call": a["call"], "near": a["near"],
            "index": (indices or {}).get(mk),
        }
    return {
        "mode": mode,                       # intraday / close
        "snapshot": snapshot,               # 本次報價時間
        "baseline_date": baseline["date"],  # 融資餘額基準日
        "generated_at": now_tw().isoformat(timespec="seconds"),
        "rules": {"call": CALL_LINE, "near": NEAR_LINE,
                  "margin_rate": DEFAULT_MARGIN_RATE, "short_margin": SHORT_MARGIN},
        "markets": markets,
        "rows": rows,
    }


# ---------------- MIS 即時報價（盤中、指數）----------------

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"
MIS_BATCH = 60


def mis_fetch(channels: list[str]) -> dict:
    """channels 例：['tse_2330.tw', 'otc_6488.tw', 'tse_t00.tw']，回傳 {代號: msg}。"""
    out = {}
    try:
        SESSION.get(MIS_HOME, timeout=15)   # 先拿 cookie
    except requests.RequestException:
        pass
    for i in range(0, len(channels), MIS_BATCH):
        part = channels[i:i + MIS_BATCH]
        js = get_json(MIS_URL, {"ex_ch": "|".join(part), "json": "1", "delay": "0",
                                "_": int(time.time() * 1000)}, referer=MIS_HOME)
        for m in js.get("msgArray") or []:
            out[str(m.get("c", "")).strip()] = m
        time.sleep(1.2)
    return out


def mis_price(m: dict, today: str):
    """取最新成交價；非今日資料或尚無成交 → None（不顯示過期價）。"""
    if not m or m.get("d") != today:
        return None
    z = num(m.get("z"))
    if z:
        return z
    # 兩筆成交之間 z 可能是 '-'：今天已有成交量時，用最佳買價當近似
    if (num(m.get("v")) or 0) > 0:
        b = (m.get("b") or "").split("_")[0]
        return num(b) or None
    return None


def mis_indices(today: str) -> dict:
    """加權指數 t00、櫃買指數 o00。"""
    try:
        msgs = mis_fetch(["tse_t00.tw", "otc_o00.tw"])
    except RuntimeError as e:
        log("指數抓取失敗：", e)
        return {}
    out = {}
    for mk, code in (("TWSE", "t00"), ("TPEX", "o00")):
        m = msgs.get(code)
        px, y = mis_price(m, today), num((m or {}).get("y"))
        if px and y:
            out[mk] = {"value": round(px, 2), "chg": round(px - y, 2),
                       "pct": round((px - y) / y * 100, 2)}
    return out
