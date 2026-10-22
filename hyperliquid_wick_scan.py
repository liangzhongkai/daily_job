#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描 Hyperliquid 永续合约：15m K 线上影线 > 实体*2 且 上影线 > 下影线*3，
且当根成交额 > 前12根均值*3（放量）。
数据源：Hyperliquid info API（metaAndAssetCtxs / candleSnapshot）。
注意：Hyperliquid K 线只有币本位成交量 v，美元成交额用 v*当根均价估算。
由外部 cron（cron-job.org 每15分钟）触发，GitHub Actions 仅做兜底限频。
"""
import time
import json
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== 微信推送配置（GitHub Secrets / 本机环境变量）=====
PUSH_URL = "https://sctapi.ftqq.com/{sendkey}.send"
SENDKEY = ""          # Server酱 sendkey
WECOM_WEBHOOK = ""    # 企业微信机器人 webhook（可选，二选一）

API = "https://api.hyperliquid.xyz/info"
INTERVAL = "15m"
N_BARS = 13           # 取13根：最后1根为待判定K线，前12根算均量
MIN_QUOTE_VOL = 50_000   # 最低美元成交额过滤
MAX_WORKERS = 6
EXCLUDED = {"BTC", "ETH"}   # 与 Binance 版保持一致：排除大市值


def http_json(payload, retry=3):
    data = json.dumps(payload).encode()
    for i in range(retry):
        try:
            req = urllib.request.Request(
                API, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            if i == retry - 1:
                raise
            time.sleep(1.5 * (i + 1))


def fetch_universe():
    """返回 [(coin, day_quote_vol), ...]，用 metaAndAssetCtxs 拿全部永续合约"""
    meta, ctxs = http_json({"type": "metaAndAssetCtxs"})
    out = []
    for info, ctx in zip(meta["universe"], ctxs):
        name = info.get("name", "")
        if info.get("isDelisted"):
            continue
        # dayNtlVlm 是24h美元成交额，用来过滤无流动性的死合约
        vol = float(ctx.get("dayNtlVlm", 0) or 0)
        if vol > 0:
            out.append((name, vol))
    return out


def fetch_candles(coin):
    """返回最近 N_BARS 根K线列表，每根含 o/h/l/c/base_v/quote_v（估算）"""
    now_ms = int(time.time() * 1000)
    start = now_ms - N_BARS * 15 * 60 * 1000 - 60_000
    req = {"type": "candleSnapshot", "req": {"coin": coin, "interval": INTERVAL,
                                             "startTime": start}}
    try:
        raw = http_json(req)
    except Exception:
        return None
    if not isinstance(raw, list) or len(raw) < N_BARS:
        return None
    bars = raw[-N_BARS:]
    out = []
    for b in bars:
        o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        v = float(b.get("v", 0) or 0)          # 币本位成交量
        avg_px = (o + h + l + c) / 4
        out.append({"o": o, "h": h, "l": l, "c": c,
                    "quote_v": v * avg_px})     # 估算美元成交额
    return out


def check_bar(bars, symbol):
    """判定最后一根K线是否满足：上影>实体*2、上影>下影*3、放量3倍"""
    last, prevs = bars[-1], bars[:-1]
    o, h, l, c = last["o"], last["h"], last["l"], last["c"]
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    if body == 0:
        return None
    if upper <= body * 2 or upper <= lower * 3:
        return None
    avg_vol = sum(b["quote_v"] for b in prevs) / len(prevs)
    if avg_vol <= 0 or last["quote_v"] < avg_vol * 3:
        return None
    if last["quote_v"] < MIN_QUOTE_VOL:
        return None
    return {
        "symbol": symbol,
        "pct_upper_body": round(upper / body, 1),
        "pct_upper_lower": round(upper / max(lower, 1e-12), 1),
        "vol_x": round(last["quote_v"] / avg_vol, 1),
        "quote_v": int(last["quote_v"]),
        "price": c,
    }


def scan():
    universe = fetch_universe()
    print(f"Hyperliquid 永续合约总数(未退市): {len(universe)}")
    hits = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_candles, name): name
                for name, _ in universe if name not in EXCLUDED}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                bars = fut.result()
            except Exception as e:
                print(f"[warn] {name} 拉取失败: {e}")
                continue
            if not bars:
                continue
            r = check_bar(bars, name)
            if r:
                hits.append(r)
    hits.sort(key=lambda x: -x["quote_v"])
    return hits


def ts():
    return datetime.now(timezone.utc).astimezone().strftime("%m-%d %H:%M")


def push_wechat(text):
    """Server酱推送到微信"""
    if not SENDKEY:
        print("[push] 未配置 SENDKEY，跳过 Server酱推送")
        return
    try:
        data = urllib.parse.urlencode({
            "title": "Hyperliquid 长上影放量",
            "desp": text}).encode()
        urllib.request.urlopen(
            PUSH_URL.format(sendkey=SENDKEY), data=data, timeout=15).read()
        print("[push] Server酱推送完成")
    except Exception as e:
        print(f"[push] Server酱推送失败: {e}")


def push_wecom(text):
    """企业微信机器人推送（可选）"""
    if not WECOM_WEBHOOK:
        return
    try:
        data = json.dumps({"msgtype": "text",
                           "text": {"content": text[:2000]}}).encode()
        req = urllib.request.Request(
            WECOM_WEBHOOK, data=data,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        print("[push] 企业微信推送完成")
    except Exception as e:
        print(f"[push] 企业微信推送失败: {e}")


def main():
    print(f"[{ts()}] 开始扫描 Hyperliquid {INTERVAL} 长上影+放量 ...")
    try:
        hits = scan()
    except Exception as e:
        print(f"[{ts()}] 扫描失败: {e}")
        raise
    if not hits:
        print(f"[{ts()}] 无符合条件的合约")
        return
    lines = [f"{h['symbol']}  上影/实体={h['pct_upper_body']}  "
             f"上影/下影={h['pct_upper_lower']}  放量={h['vol_x']}x  "
             f"成交额≈{h['quote_v']:,}$  价格={h['price']}" for h in hits]
    text = f"{ts()} Hyperliquid 15m 长上影放量 {len(hits)} 个:\n" + "\n".join(lines)
    print(text)
    push_wechat(text)
    push_wecom(text)


if __name__ == "__main__":
    import urllib.parse   # noqa: E402  仅供 push_wechat 使用
    main()
