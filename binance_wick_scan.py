# 安装依赖 pip3 install requests
# 功能：检查 Binance U本位(USDT)永续合约刚走完的15分钟K线，满足以下条件则推送微信：
#   1) 该K线成交量(USDT) >= 前10根15分钟K线成交量均值的5倍
#   2) 上影线或下影线长度 ≈ 实体长度(|open-close|)
# 由 GitHub Actions / cron-job.org 在每小时的 00/15/30/45 分触发，每根K线只检查一次
import os
import json
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# 从测试号信息获取（与 wx_report.py 共用同一套 secrets）
appID = os.environ.get("APP_ID")
appSecret = os.environ.get("APP_SECRET")
openId = os.environ.get("OPEN_ID")
template_id = os.environ.get("TEMPLATE_ID")

# Binance U本位合约 API；如运行环境被地区封锁(451)，可设置 BINANCE_FAPI_HOST(S)
# 指向可用代理。BINANCE_FAPI_HOSTS 支持逗号分隔多个候选，逐个探测取第一个可用的
_default_hosts = "https://fapi.binance.com"
if os.environ.get("BINANCE_FAPI_HOST"):
    _default_hosts = os.environ["BINANCE_FAPI_HOST"]
FAPI_HOST_CANDIDATES = [h.strip().rstrip("/") for h in
                        os.environ.get("BINANCE_FAPI_HOSTS", _default_hosts).split(",")
                        if h.strip()]
FAPI_HOST = FAPI_HOST_CANDIDATES[0]   # 实际在 scan() 里探测后再确定
INTERVAL = "15m"
VOL_MULT = 5.0          # 放量倍数阈值
WICK_MIN, WICK_MAX = 0.8, 1.2   # 影线/实体比值的“几乎一样长”区间
MIN_BODY_RANGE = 0.15   # 实体至少占整根K线振幅的比例（过滤十字星噪声）


def get_access_token():
    # 获取access token的url
    url = 'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={}&secret={}' \
        .format(appID.strip(), appSecret.strip())
    response = requests.get(url).json()
    access_token = response.get('access_token')
    return access_token


def send_data(access_token, message):
    # touser 就是 openID，template_id 就是模板ID，data按模板占位符填写
    body = {
        "touser": openId.strip(),
        "template_id": template_id.strip(),
        "url": "https://www.binance.com/zh-CN/futures",
        "data": {
            "ssr": {
                "value": message
            },
            "vip": {
                "value": datetime.now().strftime("%Y年%m月%d日 %H:%M")
            },
            "today_note": {
                "value": "Binance 15m 量能+影线监控"
            }
        }
    }
    url = 'https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={}'.format(access_token)
    print(requests.post(url, json.dumps(body)).text)


def get_perp_symbols(session):
    """全部 TRADING 状态的 USDT 本位永续合约；逐个候选 host 探测，
    第一个能正常返回的 host 接管后续所有请求（写回全局 FAPI_HOST）"""
    global FAPI_HOST
    last_code = None
    for host in FAPI_HOST_CANDIDATES:
        try:
            r = session.get(f"{host}/fapi/v1/exchangeInfo", timeout=20)
        except requests.RequestException as e:
            print(f"host 不可达: {host} -> {e}")
            continue
        if r.status_code in (451, 403):
            print(f"host 被地区封锁: {host} -> HTTP {r.status_code}")
            last_code = r.status_code
            continue
        r.raise_for_status()
        FAPI_HOST = host
        print(f"使用 host: {host}")
        return [s["symbol"] for s in r.json().get("symbols", [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"]
    raise RuntimeError(
        f"所有候选 host 均不可用(最后状态码 {last_code})。"
        f"请设置环境变量 BINANCE_FAPI_HOSTS 指向可用的代理地址")


def check_symbol(sym, session):
    """返回匹配信息的 dict；不匹配返回 None；请求失败返回 ('fail', sym, 原因)"""
    r = session.get(f"{FAPI_HOST}/fapi/v1/klines",
                    params={"symbol": sym, "interval": INTERVAL, "limit": 12},
                    timeout=20)
    if r.status_code != 200:
        return ("fail", sym, r.status_code)
    ks = r.json()
    if not isinstance(ks, list) or len(ks) < 12:
        return ("fail", sym, "klines不足12根")
    # kline格式: [openTime, O, H, L, C, 量(币), closeTime, 额(USDT), ...]
    prev = ks[-2]                      # 上一根已完结的15分钟K线（最后一根是未走完的）
    vols = [float(k[7]) for k in ks[-12:-2]]   # 它之前10根的成交额(USDT)
    mean10 = sum(vols) / len(vols)
    vol_prev = float(prev[7])          # 成交额(USDT)；如想按币量比较可改为 prev[5]
    if mean10 <= 0 or vol_prev < VOL_MULT * mean10:
        return None
    o, h, l, c = (float(prev[i]) for i in (1, 2, 3, 4))
    body = abs(c - o)                  # open-close价差(实体)
    rng = h - l
    if rng <= 0 or body <= 0 or body < MIN_BODY_RANGE * rng:
        return None
    upper = h - max(o, c)              # 上影线
    lower = min(o, c) - l              # 下影线
    wicks = []
    if WICK_MIN <= upper / body <= WICK_MAX:
        wicks.append("上影线")
    if WICK_MIN <= lower / body <= WICK_MAX:
        wicks.append("下影线")
    if not wicks:
        return None
    return {"sym": sym, "ratio": vol_prev / mean10, "wicks": wicks,
            "o": o, "h": h, "l": l, "c": c, "vol": vol_prev}


def scan():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] 开始扫描 Binance USDT 永续 {INTERVAL} K线 ...")
    session = requests.Session()
    symbols = get_perp_symbols(session)
    print(f"共 {len(symbols)} 个合约")
    matches, failed = [], 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(check_symbol, s, session) for s in symbols]
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception:
                failed += 1
                continue
            if r is None:
                continue
            if isinstance(r, tuple) and r[0] == "fail":
                failed += 1
                if r[2] in (451, 403):
                    print(f"BLOCKED: {r[1]} -> HTTP {r[2]}")
                continue
            matches.append(r)
    matches.sort(key=lambda m: -m["ratio"])
    for m in matches:
        print(f"{m['sym']} 放量{m['ratio']:.1f}x {'/'.join(m['wicks'])} "
              f"O:{m['o']} H:{m['h']} L:{m['l']} C:{m['c']} 额:{m['vol']:.0f}")
    print(f"请求失败 {failed} 个，命中 {len(matches)} 个")
    if not matches:
        print("没有合约满足条件，不发送消息")
        return
    lines = [f"Binance 15m 放量>={VOL_MULT}x 且影线≈实体 共{len(matches)}个:"]
    for m in matches[:30]:  # 微信消息长度有限，按放量倍数降序只显示前30
        lines.append(f"{m['sym']} {m['ratio']:.1f}x {'/'.join(m['wicks'])} "
                     f"O:{m['o']} C:{m['c']}")
    if len(matches) > 30:
        lines.append(f"...其余 {len(matches) - 30} 个见运行日志")
    message = "\n".join(lines)
    print(message)
    access_token = get_access_token()
    send_data(access_token, message)


if __name__ == '__main__':
    import sys
    import time
    attempts = int(os.environ.get("SCAN_ATTEMPTS", "3"))
    for i in range(1, attempts + 1):
        try:
            scan()
            break
        except Exception:
            traceback.print_exc()
            if i == attempts:
                sys.exit(1)
            print(f"第 {i} 次失败，60 秒后重试 ...")
            time.sleep(60)
