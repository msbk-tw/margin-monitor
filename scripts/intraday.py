"""盤中快照：用證交所 MIS 即時報價 × 最近一次盤後的融資成本／餘額，估算當下維持率。

用法：python scripts/intraday.py
休市日（MIS 回傳的不是今天的資料）會自動略過，不覆寫 latest.json。
"""
from __future__ import annotations

import sys

from common import DATA_DIR, build_output, load_json, log, mis_fetch, mis_indices, mis_price, now_tw, save_json


def main():
    baseline = load_json(DATA_DIR / "baseline.json", None)
    if not baseline or not baseline.get("stocks"):
        sys.exit("找不到 baseline.json，請先跑 baseline.py --backfill")

    now = now_tw()
    today = f"{now:%Y%m%d}"
    if baseline["date"] >= today:
        log("基準日已是今天（盤後資料已更新），盤中快照略過")
        return

    channels = [f"{'tse' if s['market'] == 'TWSE' else 'otc'}_{s['code']}.tw" for s in baseline["stocks"]]
    log(f"抓取 {len(channels)} 檔即時報價 …")
    msgs = mis_fetch(channels)
    if not any(m.get("d") == today for m in msgs.values()):
        log("MIS 沒有今天的報價（休市日？），略過")
        return

    prices = {s["code"]: mis_price(msgs.get(s["code"]), today) for s in baseline["stocks"]}
    prices = {k: v for k, v in prices.items() if v}
    log(f"有效報價 {len(prices)} / {len(channels)}")

    out = build_output(baseline, prices, mode="intraday", snapshot=f"{now:%Y-%m-%d %H:%M}",
                       indices=mis_indices(today))
    save_json(DATA_DIR / "latest.json", out)
    log("latest.json 已更新")


if __name__ == "__main__":
    main()
