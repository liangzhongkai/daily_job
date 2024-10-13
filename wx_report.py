# 安装依赖 pip3 install requests html5lib bs4 schedule
import os
import requests
import json
from datetime import datetime

# 从测试号信息获取
appID = os.environ.get("APP_ID")
appSecret = os.environ.get("APP_SECRET")
# 收信人ID即 用户列表中的微信号
openId = os.environ.get("OPEN_ID")
# 模板ID
template_id = os.environ.get("TEMPLATE_ID1")

def get_access_token():
    # 获取access token的url
    url = 'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={}&secret={}' \
        .format(appID.strip(), appSecret.strip())
    response = requests.get(url).json()
    print(response)
    access_token = response.get('access_token')
    return access_token


def get_daily_love():
    # 每日一句情话
    url = "https://api.lovelive.tools/api/SweetNothings/Serialization/Json"
    r = requests.get(url)
    all_dict = json.loads(r.text)
    sentence = all_dict['returnObj'][0]
    daily_love = sentence
    return daily_love

def get_data():
    date_str = '2026-03-12'
    # 将字符串转换为datetime对象
    given_date = datetime.strptime(date_str, '%Y-%m-%d')
    today = datetime.now()
    # 计算两个日期之间的差值
    delta = given_date - today
    return ["", delta.days]

def send_data(access_token, data):
    # touser 就是 openID
    # template_id 就是模板ID
    # url 就是点击模板跳转的url
    # data就按这种格式写，time和text就是之前{{time.DATA}}中的那个time，value就是你要替换DATA的值

    import datetime
    today = datetime.date.today()
    today_str = today.strftime("%Y年%m月%d日")

    body = {
        "touser": openId.strip(),
        "template_id": template_id.strip(),
        "url": "https://weixin.qq.com",
        "data": {
            "date": {
                "value": today_str
            },
            "ssr": {
                "value": data[0]
            },
            "vip": {
                "value": data[1]
            },
            "today_note": {
                "value": get_daily_love()
            }
        }
    }
    url = 'https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={}'.format(access_token)
    print(requests.post(url, json.dumps(body)).text)



def wx_report():
    # 1.获取access_token
    access_token = get_access_token()
    # 2. 获取数据
    data = get_data()
    print(f"data： {data}")
    # 3. 发送消息
    send_data(access_token, data)



if __name__ == '__main__':
    wx_report()
