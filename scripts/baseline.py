"""盤後基準更新：抓當日融資融券餘額＋收盤行情，滾動更新融資／融券成本，輸出 baseline.json 與 latest.json。

用法：
  python scripts/baseline.py                 # 更新今天（台北時間）；已更新過就跳過
  python scripts/baseline.py --date 20261118 # 指定日期
  python scripts/baseline.py --backfill 60   # 清空成本狀態，回補過去約 60 個交易日（第一次上線必跑）
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from common import (DATA_DIR, DEFAULT_MARGIN_RATE, SHARES_PATH, STATE_PATH, build_output,
                    clean, find_col, get_json, iter_tables, load_json, log, mis_indices,
                    now_tw, num, polite_sleep, save_json)

TWSE_REF = "https://www.twse.com.tw/zh/trading/margin/mi-margn.html"


def roc(d: dt.date) -> str:
    return f"{d.year - 1911}/{d:%m/%d}"


def is_security(code: str) -> bool:
    """只保留股票與 ETF（4 碼數字、或 00 開頭的 ETF 代號）。"""
    return code.isdigit() and len(code) == 4 or (code.startswith("00") and 4 <= len(code) <= 6)


# ---------------- 上市（證交所）----------------

def twse_margin(d: dt.date) -> dict | None:
    js = get_json("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
                  {"date": f"{d:%Y%m%d}", "selectType": "ALL", "response": "json"}, referer=TWSE_REF)
    if js.get("stat") != "OK":
        return None                                    # 休市或尚未公布
    if js.get("date") and js["date"] != f"{d:%Y%m%d}":
        return None
    tables = [t for t in iter_tables(js) if t[2] and len(t[2][0]) >= 15]
    if not tables:
        return None
    _, _, rows = max(tables, key=lambda t: len(t[2]))
    # 欄位順序：代號 名稱 | 融資 買進 賣出 現金償還 前日餘額 今日餘額 限額 | 融券 買進 賣出 現券償還 前日餘額 今日餘額 限額 | 資券互抵 註記
    out = {}
    for r in rows:
        code = clean(r[0])
        if not is_security(code):
            continue
        out[code] = {
            "name": clean(r[1]),
            "m_buy": num(r[2]) or 0, "m_prev": num(r[5]) or 0, "m_bal": num(r[6]) or 0,
            "s_sell": num(r[9]) or 0, "s_prev": num(r[11]) or 0, "s_bal": num(r[12]) or 0,
            "note": clean(r[15]) if len(r) > 15 else "",
        }
    return out


def twse_quotes(d: dt.date) -> dict:
    js = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                  {"date": f"{d:%Y%m%d}", "type": "ALLBUT0999", "response": "json"}, referer=TWSE_REF)
    for _, fields, rows in iter_tables(js):
        ic, ip = find_col(fields, "證券代號"), find_col(fields, "收盤價")
        if ic is None or ip is None:
            continue
        iname, ivol, iamt = find_col(fields, "證券名稱"), find_col(fields, "成交股數"), find_col(fields, "成交金額")
        isign, idiff = find_col(fields, "漲跌(+/-)"), find_col(fields, "漲跌價差")
        out = {}
        for r in rows:
            code = clean(r[ic])
            close = num(r[ip])
            if not is_security(code) or close is None:
                continue
            vol, amt = num(r[ivol]) if ivol is not None else None, num(r[iamt]) if iamt is not None else None
            diff = num(r[idiff]) if idiff is not None else None
            if diff is not None and isign is not None and "-" in clean(r[isign]):
                diff = -diff
            out[code] = {"name": clean(r[iname]) if iname is not None else "", "close": close,
                         "change": diff, "avg": (amt / vol) if (vol and amt) else close}
        return out
    return {}


# ---------------- 上櫃（櫃買中心）----------------
# 櫃買網站改版過，先試舊版（實測可用），失敗再試新版；欄位用名稱比對，找不到才用舊版預設順序。

TPEX_MARGIN_FIELDS = ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金",
                      "資使用率", "資限額", "前券餘額(張)", "券賣", "券買", "券償", "券餘額", "券屬證金",
                      "券使用率", "券限額", "資券相抵(張)", "備註"]
TPEX_QUOTE_FIELDS = ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價", "成交股數",
                     "成交金額(元)", "成交筆數"]


def _tpex_try(candidates):
    errs = []
    for url, params in candidates:
        try:
            js = get_json(url, params, tries=2)
            tabs = [t for t in iter_tables(js) if t[2]]
            if tabs:
                return max(tabs, key=lambda t: len(t[2]))
        except RuntimeError as e:
            errs.append(str(e))
        polite_sleep(1.5)
    if errs:
        log("櫃買端點皆失敗：", " | ".join(errs)[:300])
    return None


def tpex_margin(d: dt.date) -> dict:
    t = _tpex_try([
        ("https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php",
         {"l": "zh-tw", "o": "json", "d": roc(d)}),
        ("https://www.tpex.org.tw/www/zh-tw/margin/balance", {"date": f"{d:%Y/%m/%d}", "response": "json"}),
    ])
    if not t:
        return {}
    _, fields, rows = t
    fields = fields or TPEX_MARGIN_FIELDS
    c = {k: find_col(fields, *names) for k, names in {
        "code": ("代號", "證券代號"), "name": ("名稱", "證券名稱"),
        "m_prev": ("前資餘額(張)", "前資餘額"), "m_buy": ("資買",), "m_bal": ("資餘額",),
        "s_prev": ("前券餘額(張)", "前券餘額"), "s_sell": ("券賣",), "s_bal": ("券餘額",),
        "note": ("備註",)}.items()}
    if None in (c["code"], c["m_bal"], c["s_bal"]):
        log("櫃買融資欄位對不上，請檢查：", fields)
        return {}
    out = {}
    for r in rows:
        code = clean(r[c["code"]])
        if not is_security(code):
            continue
        g = lambda k: (num(r[c[k]]) or 0) if c[k] is not None else 0  # noqa: E731
        out[code] = {"name": clean(r[c["name"]]) if c["name"] is not None else "",
                     "m_buy": g("m_buy"), "m_prev": g("m_prev"), "m_bal": g("m_bal"),
                     "s_sell": g("s_sell"), "s_prev": g("s_prev"), "s_bal": g("s_bal"),
                     "note": clean(r[c["note"]]) if c["note"] is not None else ""}
    return out


def tpex_quotes(d: dt.date) -> dict:
    t = _tpex_try([
        ("https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php",
         {"l": "zh-tw", "o": "json", "d": roc(d), "se": "AL"}),
        ("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQ",
         {"date": f"{d:%Y/%m/%d}", "id": "", "response": "json"}),
    ])
    if not t:
        return {}
    _, fields, rows = t
    fields = fields or TPEX_QUOTE_FIELDS
    ic, ip = find_col(fields, "代號", "證券代號"), find_col(fields, "收盤", "收盤價")
    ichg, iavg = find_col(fields, "漲跌", "漲跌價差"), find_col(fields, "均價")
    ivol, iamt = find_col(fields, "成交股數"), find_col(fields, "成交金額(元)", "成交金額")
    if ic is None or ip is None:
        log("櫃買行情欄位對不上，請檢查：", fields)
        return {}
    out = {}
    for r in rows:
        code, close = clean(r[ic]), num(r[ip])
        if not is_security(code) or close is None:
            continue
        avg = num(r[iavg]) if iavg is not None else None
        if not avg and ivol is not None and iamt is not None:
            v, a = num(r[ivol]), num(r[iamt])
            avg = a / v if (v and a) else None
        out[code] = {"close": close, "change": num(r[ichg]) if ichg is not None else None,
                     "avg": avg or close}
    return out


# ---------------- 發行股數（算市值用，每週更新一次）----------------

def refresh_shares() -> dict:
    cache = load_json(SHARES_PATH, {})
    if cache.get("updated") and (now_tw().date() - dt.date.fromisoformat(cache["updated"])).days < 7:
        return cache.get("data", {})
    data = {}
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
                "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"):
        try:
            for x in get_json(url, tries=2):
                code = next((str(x[k]).strip() for k in ("公司代號", "SecuritiesCompanyCode", "CompanyCode") if k in x), None)
                sh = next((num(v) for k, v in x.items() if "股數" in k or k in ("IssueShares", "IssuedShares")), None)
                if code and sh:
                    data[code] = sh
        except (RuntimeError, TypeError) as e:
            log("發行股數抓取失敗（只影響市值欄）：", e)
        polite_sleep(1.5)
    if data:
        save_json(SHARES_PATH, {"updated": now_tw().date().isoformat(), "data": data})
        return data
    return cache.get("data", {})


# ---------------- 成本滾動 ----------------

def roll_cost(prev_cost, add, bal, px):
    """加權平均法：新增張數以當日均價計入，減少張數依平均成本扣除（不改變均價）。"""
    if bal <= 0:
        return None
    if px is None:
        return prev_cost
    if prev_cost is None:
        return px                      # 第一天沒有歷史 → 以當日均價起算（所以需要暖機）
    new = min(max(add, 0), bal)
    return ((bal - new) * prev_cost + new * px) / bal


def run_day(d: dt.date, state: dict) -> bool:
    log(f"處理 {d} …")
    tm = twse_margin(d)
    if not tm:
        log(f"{d} 證交所無融資資料（休市或尚未公布）")
        return False
    polite_sleep()
    tq = twse_quotes(d)
    polite_sleep()
    om = tpex_margin(d)
    polite_sleep()
    oq = tpex_quotes(d) if om else {}
    log(f"上市 融資 {len(tm)} / 行情 {len(tq)}；上櫃 融資 {len(om)} / 行情 {len(oq)}")

    stocks = state.setdefault("stocks", {})
    ds = f"{d:%Y%m%d}"
    for market, mg, qt in (("TWSE", tm, tq), ("TPEX", om, oq)):
        for code, r in mg.items():
            q = qt.get(code) or {}
            px = q.get("avg")
            s = stocks.get(code, {})
            stocks[code] = {
                "market": market, "name": r["name"] or q.get("name") or s.get("name", ""),
                "etf": code.startswith("00"),
                "m_bal": r["m_bal"], "m_chg": r["m_bal"] - r["m_prev"],
                "s_bal": r["s_bal"], "s_chg": r["s_bal"] - r["s_prev"],
                "m_cost": roll_cost(s.get("m_cost"), r["m_buy"], r["m_bal"], px),
                "s_cost": roll_cost(s.get("s_cost"), r["s_sell"], r["s_bal"], px),
                "m_rate": DEFAULT_MARGIN_RATE[market],
                "close": q.get("close"), "change": q.get("change"),
                "days": s.get("days", 0) + 1, "note": r["note"], "date": ds,
            }
    state["last_date"] = ds
    return True


def make_baseline(state: dict, shares: dict) -> dict:
    ds = state["last_date"]
    out = []
    for code, s in sorted(state["stocks"].items()):
        if s["date"] != ds or not s.get("close") or (s["m_bal"] <= 0 and s["s_bal"] <= 0):
            continue
        out.append({"code": code, "shares": shares.get(code), **{k: s[k] for k in (
            "name", "market", "etf", "close", "change", "m_bal", "m_chg", "s_bal", "s_chg",
            "m_cost", "s_cost", "m_rate", "days", "note")}})
    return {"date": ds, "stocks": out}


def publish(state: dict, *, with_index: bool):
    baseline = make_baseline(state, refresh_shares())
    save_json(DATA_DIR / "baseline.json", baseline)
    ds = baseline["date"]
    indices = mis_indices(ds) if with_index else {}
    latest = build_output(baseline, {s["code"]: s["close"] for s in baseline["stocks"]},
                          mode="close", snapshot=f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 收盤", indices=indices)
    save_json(DATA_DIR / "latest.json", latest)
    log(f"已輸出 {len(baseline['stocks'])} 檔，基準日 {ds}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD，預設今天")
    ap.add_argument("--backfill", type=int, help="回補 N 個交易日（會重建成本狀態）")
    a = ap.parse_args()

    if a.backfill:
        state = {}
        today = now_tw().date()
        days, d = [], today
        while len(days) < int(a.backfill * 1.15) + 5:      # 多抓一些平日，涵蓋國定假日
            if d.weekday() < 5:
                days.append(d)
            d -= dt.timedelta(days=1)
        ok = 0
        for d in reversed(days):
            try:
                ok += run_day(d, state)
            except RuntimeError as e:
                log("跳過：", e)
            polite_sleep()
            if ok and ok % 10 == 0:
                save_json(STATE_PATH, state)                  # 中途存檔，失敗可接著跑
        if not ok:
            sys.exit("回補失敗：一天都沒抓到")
        save_json(STATE_PATH, state)
        publish(state, with_index=False)
        return

    d = dt.datetime.strptime(a.date, "%Y%m%d").date() if a.date else now_tw().date()
    state = load_json(STATE_PATH, {})
    if not state.get("stocks"):
        sys.exit("尚無成本狀態，請先執行 --backfill 60")
    if state.get("last_date", "") >= f"{d:%Y%m%d}":
        log(f"{d} 已更新過，略過")
        return
    if run_day(d, state):
        save_json(STATE_PATH, state)
        publish(state, with_index=(d == now_tw().date()))


if __name__ == "__main__":
    main()
"""盤後基準更新：抓當日融資融券餘額＋收盤行情，滾動更新融資／融券成本，輸出 baseline.json 與 latest.json。

用法：
  python scripts/baseline.py                 # 更新今天（台北時間）；已更新過就跳過
  python scripts/baseline.py --date 20261118 # 指定日期
  python scripts/baseline.py --backfill 60   # 清空成本狀態，回補過去約 60 個交易日（第一次上線必跑）
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from common import (DATA_DIR, DEFAULT_MARGIN_RATE, SHARES_PATH, STATE_PATH, build_output,
                    clean, find_col, get_json, iter_tables, load_json, log, mis_indices,
                    now_tw, num, polite_sleep, save_json)

TWSE_REF = "https://www.twse.com.tw/zh/trading/margin/mi-margn.html"


def roc(d: dt.date) -> str:
    return f"{d.year - 1911}/{d:%m/%d}"


def is_security(code: str) -> bool:
    """只保留股票與 ETF（4 碼數字、或 00 開頭的 ETF 代號）。"""
    return code.isdigit() and len(code) == 4 or (code.startswith("00") and 4 <= len(code) <= 6)


# ---------------- 上市（證交所）----------------

def twse_margin(d: dt.date) -> dict | None:
    js = get_json("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
                  {"date": f"{d:%Y%m%d}", "selectType": "ALL", "response": "json"}, referer=TWSE_REF)
    if js.get("stat") != "OK":
        return None                                    # 休市或尚未公布
    if js.get("date") and js["date"] != f"{d:%Y%m%d}":
        return None
    tables = [t for t in iter_tables(js) if t[2] and len(t[2][0]) >= 15]
    if not tables:
        return None
    _, _, rows = max(tables, key=lambda t: len(t[2]))
    # 欄位順序：代號 名稱 | 融資 買進 賣出 現金償還 前日餘額 今日餘額 限額 | 融券 買進 賣出 現券償還 前日餘額 今日餘額 限額 | 資券互抵 註記
    out = {}
    for r in rows:
        code = clean(r[0])
        if not is_security(code):
            continue
        out[code] = {
            "name": clean(r[1]),
            "m_buy": num(r[2]) or 0, "m_prev": num(r[5]) or 0, "m_bal": num(r[6]) or 0,
            "s_sell": num(r[9]) or 0, "s_prev": num(r[11]) or 0, "s_bal": num(r[12]) or 0,
            "note": clean(r[15]) if len(r) > 15 else "",
        }
    return out


def twse_quotes(d: dt.date) -> dict:
    js = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                  {"date": f"{d:%Y%m%d}", "type": "ALLBUT0999", "response": "json"}, referer=TWSE_REF)
    for _, fields, rows in iter_tables(js):
        ic, ip = find_col(fields, "證券代號"), find_col(fields, "收盤價")
        if ic is None or ip is None:
            continue
        iname, ivol, iamt = find_col(fields, "證券名稱"), find_col(fields, "成交股數"), find_col(fields, "成交金額")
        isign, idiff = find_col(fields, "漲跌(+/-)"), find_col(fields, "漲跌價差")
        out = {}
        for r in rows:
            code = clean(r[ic])
            close = num(r[ip])
            if not is_security(code) or close is None:
                continue
            vol, amt = num(r[ivol]) if ivol is not None else None, num(r[iamt]) if iamt is not None else None
            diff = num(r[idiff]) if idiff is not None else None
            if diff is not None and isign is not None and "-" in clean(r[isign]):
                diff = -diff
            out[code] = {"name": clean(r[iname]) if iname is not None else "", "close": close,
                         "change": diff, "avg": (amt / vol) if (vol and amt) else close}
        return out
    return {}


# ---------------- 上櫃（櫃買中心）----------------
# 櫃買網站改版過，新舊端點都試；欄位用名稱比對，找不到才用舊版預設順序。

TPEX_MARGIN_FIELDS = ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金",
                      "資使用率", "資限額", "前券餘額(張)", "券賣", "券買", "券償", "券餘額", "券屬證金",
                      "券使用率", "券限額", "資券相抵(張)", "備註"]
TPEX_QUOTE_FIELDS = ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價", "成交股數",
                     "成交金額(元)", "成交筆數"]


def _tpex_try(candidates):
    errs = []
    for url, params in candidates:
        try:
            js = get_json(url, params, tries=2)
            tabs = [t for t in iter_tables(js) if t[2]]
            if tabs:
                return max(tabs, key=lambda t: len(t[2]))
        except RuntimeError as e:
            errs.append(str(e))
        polite_sleep(1.5)
    if errs:
        log("櫃買端點皆失敗：", " | ".join(errs)[:300])
    return None


def tpex_margin(d: dt.date) -> dict:
    t = _tpex_try([
        ("https://www.tpex.org.tw/www/zh-tw/margin/balance", {"date": f"{d:%Y/%m/%d}", "response": "json"}),
        ("https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php",
         {"l": "zh-tw", "o": "json", "d": roc(d)}),
    ])
    if not t:
        return {}
    _, fields, rows = t
    fields = fields or TPEX_MARGIN_FIELDS
    c = {k: find_col(fields, *names) for k, names in {
        "code": ("代號", "證券代號"), "name": ("名稱", "證券名稱"),
        "m_prev": ("前資餘額(張)", "前資餘額"), "m_buy": ("資買",), "m_bal": ("資餘額",),
        "s_prev": ("前券餘額(張)", "前券餘額"), "s_sell": ("券賣",), "s_bal": ("券餘額",),
        "note": ("備註",)}.items()}
    if None in (c["code"], c["m_bal"], c["s_bal"]):
        log("櫃買融資欄位對不上，請檢查：", fields)
        return {}
    out = {}
    for r in rows:
        code = clean(r[c["code"]])
        if not is_security(code):
            continue
        g = lambda k: (num(r[c[k]]) or 0) if c[k] is not None else 0  # noqa: E731
        out[code] = {"name": clean(r[c["name"]]) if c["name"] is not None else "",
                     "m_buy": g("m_buy"), "m_prev": g("m_prev"), "m_bal": g("m_bal"),
                     "s_sell": g("s_sell"), "s_prev": g("s_prev"), "s_bal": g("s_bal"),
                     "note": clean(r[c["note"]]) if c["note"] is not None else ""}
    return out


def tpex_quotes(d: dt.date) -> dict:
    t = _tpex_try([
        ("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQ",
         {"date": f"{d:%Y/%m/%d}", "id": "", "response": "json"}),
        ("https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php",
         {"l": "zh-tw", "o": "json", "d": roc(d), "se": "AL"}),
    ])
    if not t:
        return {}
    _, fields, rows = t
    fields = fields or TPEX_QUOTE_FIELDS
    ic, ip = find_col(fields, "代號", "證券代號"), find_col(fields, "收盤", "收盤價")
    ichg, iavg = find_col(fields, "漲跌", "漲跌價差"), find_col(fields, "均價")
    ivol, iamt = find_col(fields, "成交股數"), find_col(fields, "成交金額(元)", "成交金額")
    if ic is None or ip is None:
        log("櫃買行情欄位對不上，請檢查：", fields)
        return {}
    out = {}
    for r in rows:
        code, close = clean(r[ic]), num(r[ip])
        if not is_security(code) or close is None:
            continue
        avg = num(r[iavg]) if iavg is not None else None
        if not avg and ivol is not None and iamt is not None:
            v, a = num(r[ivol]), num(r[iamt])
            avg = a / v if (v and a) else None
        out[code] = {"close": close, "change": num(r[ichg]) if ichg is not None else None,
                     "avg": avg or close}
    return out


# ---------------- 發行股數（算市值用，每週更新一次）----------------

def refresh_shares() -> dict:
    cache = load_json(SHARES_PATH, {})
    if cache.get("updated") and (now_tw().date() - dt.date.fromisoformat(cache["updated"])).days < 7:
        return cache.get("data", {})
    data = {}
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
                "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"):
        try:
            for x in get_json(url, tries=2):
                code = next((str(x[k]).strip() for k in ("公司代號", "SecuritiesCompanyCode", "CompanyCode") if k in x), None)
                sh = next((num(v) for k, v in x.items() if "股數" in k or k in ("IssueShares", "IssuedShares")), None)
                if code and sh:
                    data[code] = sh
        except (RuntimeError, TypeError) as e:
            log("發行股數抓取失敗（只影響市值欄）：", e)
        polite_sleep(1.5)
    if data:
        save_json(SHARES_PATH, {"updated": now_tw().date().isoformat(), "data": data})
        return data
    return cache.get("data", {})


# ---------------- 成本滾動 ----------------

def roll_cost(prev_cost, add, bal, px):
    """加權平均法：新增張數以當日均價計入，減少張數依平均成本扣除（不改變均價）。"""
    if bal <= 0:
        return None
    if px is None:
        return prev_cost
    if prev_cost is None:
        return px                      # 第一天沒有歷史 → 以當日均價起算（所以需要暖機）
    new = min(max(add, 0), bal)
    return ((bal - new) * prev_cost + new * px) / bal


def run_day(d: dt.date, state: dict) -> bool:
    log(f"處理 {d} …")
    tm = twse_margin(d)
    if not tm:
        log(f"{d} 證交所無融資資料（休市或尚未公布）")
        return False
    polite_sleep()
    tq = twse_quotes(d)
    polite_sleep()
    om = tpex_margin(d)
    polite_sleep()
    oq = tpex_quotes(d) if om else {}
    log(f"上市 融資 {len(tm)} / 行情 {len(tq)}；上櫃 融資 {len(om)} / 行情 {len(oq)}")

    stocks = state.setdefault("stocks", {})
    ds = f"{d:%Y%m%d}"
    for market, mg, qt in (("TWSE", tm, tq), ("TPEX", om, oq)):
        for code, r in mg.items():
            q = qt.get(code) or {}
            px = q.get("avg")
            s = stocks.get(code, {})
            stocks[code] = {
                "market": market, "name": r["name"] or q.get("name") or s.get("name", ""),
                "etf": code.startswith("00"),
                "m_bal": r["m_bal"], "m_chg": r["m_bal"] - r["m_prev"],
                "s_bal": r["s_bal"], "s_chg": r["s_bal"] - r["s_prev"],
                "m_cost": roll_cost(s.get("m_cost"), r["m_buy"], r["m_bal"], px),
                "s_cost": roll_cost(s.get("s_cost"), r["s_sell"], r["s_bal"], px),
                "m_rate": DEFAULT_MARGIN_RATE[market],
                "close": q.get("close"), "change": q.get("change"),
                "days": s.get("days", 0) + 1, "note": r["note"], "date": ds,
            }
    state["last_date"] = ds
    return True


def make_baseline(state: dict, shares: dict) -> dict:
    ds = state["last_date"]
    out = []
    for code, s in sorted(state["stocks"].items()):
        if s["date"] != ds or not s.get("close") or (s["m_bal"] <= 0 and s["s_bal"] <= 0):
            continue
        out.append({"code": code, "shares": shares.get(code), **{k: s[k] for k in (
            "name", "market", "etf", "close", "change", "m_bal", "m_chg", "s_bal", "s_chg",
            "m_cost", "s_cost", "m_rate", "days", "note")}})
    return {"date": ds, "stocks": out}


def publish(state: dict, *, with_index: bool):
    baseline = make_baseline(state, refresh_shares())
    save_json(DATA_DIR / "baseline.json", baseline)
    ds = baseline["date"]
    indices = mis_indices(ds) if with_index else {}
    latest = build_output(baseline, {s["code"]: s["close"] for s in baseline["stocks"]},
                          mode="close", snapshot=f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 收盤", indices=indices)
    save_json(DATA_DIR / "latest.json", latest)
    log(f"已輸出 {len(baseline['stocks'])} 檔，基準日 {ds}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD，預設今天")
    ap.add_argument("--backfill", type=int, help="回補 N 個交易日（會重建成本狀態）")
    a = ap.parse_args()

    if a.backfill:
        state = {}
        today = now_tw().date()
        days, d = [], today
        while len(days) < int(a.backfill * 1.15) + 5:      # 多抓一些平日，涵蓋國定假日
            if d.weekday() < 5:
                days.append(d)
            d -= dt.timedelta(days=1)
        ok = 0
        for d in reversed(days):
            try:
                ok += run_day(d, state)
            except RuntimeError as e:
                log("跳過：", e)
            polite_sleep()
            if ok and ok % 10 == 0:
                save_json(STATE_PATH, state)                  # 中途存檔，失敗可接著跑
        if not ok:
            sys.exit("回補失敗：一天都沒抓到")
        save_json(STATE_PATH, state)
        publish(state, with_index=False)
        return

    d = dt.datetime.strptime(a.date, "%Y%m%d").date() if a.date else now_tw().date()
    state = load_json(STATE_PATH, {})
    if not state.get("stocks"):
        sys.exit("尚無成本狀態，請先執行 --backfill 60")
    if state.get("last_date", "") >= f"{d:%Y%m%d}":
        log(f"{d} 已更新過，略過")
        return
    if run_day(d, state):
        save_json(STATE_PATH, state)
        publish(state, with_index=(d == now_tw().date()))


if __name__ == "__main__":
    main()
