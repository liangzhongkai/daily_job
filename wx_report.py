# 安装依赖 pip3 install requests schedule selenium
# 功能：每10分钟打开 https://app.hyperliquid.xyz/fundingComparison，
#       切换到 Hourly，解析表格中 Hyperliquid / Binance-HL Arb 两列，
#       任一列绝对值 >= 0.02% 时通过微信模板消息推送。
import os
import time
import json
import traceback
from datetime import datetime

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# 从测试号信息获取
appID = os.environ.get("APP_ID")
appSecret = os.environ.get("APP_SECRET")
# 收信人ID即 用户列表中的微信号
openId = os.environ.get("OPEN_ID")
# 模板ID
template_id = os.environ.get("TEMPLATE_ID")

# 目标页面与筛选阈值
FUNDING_URL = "https://app.hyperliquid.xyz/fundingComparison"
# 阈值：0.02%（页面上显示的是百分数字符串，直接和 0.02 比较）
THRESHOLD_PERCENT = 0.02
# 需要检查的列标题（不看正负号，取绝对值）
TARGET_COLUMNS = ["Hyperliquid", "Binance-HL Arb"]


def get_access_token():
    # 获取access token的url
    url = 'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={}&secret={}' \
        .format(appID.strip(), appSecret.strip())
    response = requests.get(url).json()
    print(response)
    access_token = response.get('access_token')
    return access_token


def send_data(access_token, message):
    # touser 就是 openID
    # template_id 就是模板ID
    # url 就是点击模板跳转的url
    # data按模板中 {{ssr.DATA}} 等占位符填写

    today_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    body = {
        "touser": openId.strip(),
        "template_id": template_id.strip(),
        "url": FUNDING_URL,
        "data": {
            "ssr": {
                "value": message
            },
            "vip": {
                "value": today_str
            },
            "today_note": {
                "value": "Funding rate alert"
            }
        }
    }
    url = 'https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={}'.format(access_token)
    print(requests.post(url, json.dumps(body)).text)


def build_driver():
    # Windows 上用无头 Edge，Linux（GitHub Actions runner）上用预装的 Chrome
    options = webdriver.EdgeOptions() if os.name == "nt" else webdriver.ChromeOptions()
    if os.name != "nt":
        options.binary_location = "/usr/bin/google-chrome"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,2400")
    options.add_argument("--log-level=3")
    if os.name == "nt":
        return webdriver.Edge(options=options)
    return webdriver.Chrome(options=options)


def parse_percent(text):
    """把 '0.0250%' / '-0.0312%' / '—' 之类解析成浮点百分数，失败返回 None"""
    if not text:
        return None
    text = text.strip().replace('%', '').replace(',', '')
    try:
        return float(text)
    except ValueError:
        return None


def parse_rows_generic(driver):
    """通用行列解析：按文本定位表头单元格 → 其所在行为表头行 →
    同容器的兄弟行为数据行。不依赖 <table>/<th>/<td> 标签。"""
    rows = []
    header_cells = driver.find_elements(
        By.XPATH, "//*[normalize-space(text())='Binance-HL Arb']")
    for hc in header_cells:
        # 向上找一个"行"元素：其直接子元素数 >= 4（一行有多列）
        row_el = hc
        header_texts = []
        for _ in range(5):
            row_el = row_el.find_element(By.XPATH, "..")
            children = row_el.find_elements(By.XPATH, "./*")
            header_texts = [c.text.strip() for c in children]
            if len(children) >= 4 and "Binance-HL Arb" in header_texts:
                break
        if "Binance-HL Arb" not in header_texts:
            continue
        col_idx = {}
        for name in TARGET_COLUMNS:
            if name in header_texts:
                col_idx[name] = header_texts.index(name)
        if len(col_idx) < len(TARGET_COLUMNS):
            continue
        # 数据行 = 行元素在父容器中的后续兄弟
        data_rows = row_el.find_elements(By.XPATH, "./following-sibling::*")
        for tr in data_rows:
            cells = [c.text.strip() for c in tr.find_elements(By.XPATH, "./*")]
            if len(cells) <= max(col_idx.values()):
                continue
            coin = cells[0].lstrip("☆★").strip()
            hl = parse_percent(cells[col_idx["Hyperliquid"]])
            arb = parse_percent(cells[col_idx["Binance-HL Arb"]])
            if coin:
                rows.append((coin, hl, arb))
        if rows:
            break
    return rows


def fetch_funding_rows():
    """打开页面，点击 Hourly，返回 [(coin, hyperliquid, binance_hl_arb), ...]"""
    driver = build_driver()
    rows = []
    try:
        driver.get(FUNDING_URL)
        wait = WebDriverWait(driver, 45)

        # Hourly 是自定义下拉（styled-components div，非原生 select）：
        # 先点击触发器展开，再点击弹出的选项
        try:
            triggers = [e for e in driver.find_elements(
                By.XPATH, "//*[normalize-space(text())='Hourly']") if e.is_displayed()]
            if triggers:
                trigger = triggers[0]
                driver.execute_script("arguments[0].click();", trigger)
                time.sleep(1)
                # 展开后会出现选项列表；点击除触发器以外的 Hourly 选项
                for opt in driver.find_elements(
                        By.XPATH, "//*[normalize-space(text())='Hourly']"):
                    if opt.is_displayed() and opt != trigger:
                        driver.execute_script("arguments[0].click();", opt)
                        print(f"已点击 Hourly 选项: <{opt.tag_name}> {opt.get_attribute('class')!r}")
                        break
                else:
                    print("展开后未找到 Hourly 选项（可能默认已是 Hourly）")
            else:
                print("未找到 Hourly 触发器，按页面默认档位继续")
            time.sleep(2)
        except Exception as e:
            print(f"选择 Hourly 失败，按页面默认档位继续: {e}")

        # 等表头出现（表头不一定是 <th>，直接按文本等）
        try:
            wait.until(EC.presence_of_element_located(
                (By.XPATH, "//*[normalize-space(text())='Binance-HL Arb']")))
        except TimeoutException:
            # 保存页面快照供调试（GitHub Actions 里作为 artifact 下载）
            with open("funding_debug.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            driver.save_screenshot("funding_debug.png")
            print("DEBUG: 页面标题 =", driver.title)
            print("DEBUG: body 前2000字符 =\n", driver.page_source[:2000])
            raise RuntimeError("45秒内表头未渲染，快照已保存 funding_debug.html/png")

        rows = parse_rows_generic(driver)
    finally:
        driver.quit()
    return rows


def filter_alerts(rows):
    """筛选 Hyperliquid 或 Binance-HL Arb 绝对值 >= 阈值的币种，并按两列绝对值降序排序"""
    alerts = []
    for coin, hl, arb in rows:
        if (hl is not None and abs(hl) >= THRESHOLD_PERCENT) or \
           (arb is not None and abs(arb) >= THRESHOLD_PERCENT):
            alerts.append((coin, hl, arb))
    # 先按 Hyperliquid 绝对值降序，再按 Binance-HL Arb 绝对值降序
    alerts.sort(key=lambda r: (abs(r[1]) if r[1] is not None else 0,
                               abs(r[2]) if r[2] is not None else 0),
                reverse=True)
    return alerts


def fmt(v):
    return "N/A" if v is None else f"{v:+.4f}%"


def funding_alert_job():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] 开始检查 funding rate ...")
    try:
        rows = fetch_funding_rows()
        print(f"共解析到 {len(rows)} 个币种")
        if not rows:
            print("未解析到表格数据，可能页面结构变化或加载失败")
            return
        alerts = filter_alerts(rows)
        if not alerts:
            print("没有币种达到阈值，不发送消息")
            return

        lines = [f" funding alert (Hourly, |rate|>={THRESHOLD_PERCENT}%):"]
        for coin, hl, arb in alerts:
            lines.append(f"{coin}: HL={fmt(hl)}, Binance-HL Arb={fmt(arb)}")
        message = "\n".join(lines)
        print(message)

        # 1.获取access_token 2.发送消息
        access_token = get_access_token()
        send_data(access_token, message)
    except Exception:
        traceback.print_exc()


# 由 GitHub Actions 每10分钟触发一次，每次运行只检查并推送一轮
funding_alert_job()
