import os
import time
import json
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
import tweepy
from openai import OpenAI

# =========================
# 環境変数
# =========================
X_API_KEY = os.getenv("API_KEY")
X_API_SECRET = os.getenv("API_SECRET")
X_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
X_ACCESS_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEPLOY_RUN = (os.getenv("DEPLOY_RUN", "0") == "1")

# =========================
# 設定
# =========================
TZ = ZoneInfo("Asia/Tokyo")
SENDAI_LAT = 38.2682
SENDAI_LON = 140.8694

DROP_PER_HOUR_THRESHOLD = -1.5
POST_HOUR = 6

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
MAX_LEN = 135

# =========================
# クライアント
# =========================
x_client = tweepy.Client(
    bearer_token=X_BEARER_TOKEN,
    consumer_key=X_API_KEY,
    consumer_secret=X_API_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET
)

oa_client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# Open-Meteo 取得
# =========================
def fetch_weather():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={SENDAI_LAT}"
        f"&longitude={SENDAI_LON}"
        "&hourly=surface_pressure,temperature_2m,relative_humidity_2m,weathercode"
        "&timezone=Asia%2FTokyo"
        "&forecast_days=2"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    j = r.json()

    return (
        j["hourly"]["time"],
        j["hourly"]["surface_pressure"],
        j["hourly"]["temperature_2m"],
        j["hourly"]["relative_humidity_2m"],
        j["hourly"]["weathercode"],
    )

# =========================
# 急降下検出
# =========================
def find_drop(times_dt, pressures):
    for i in range(len(pressures)-1):
        diff = pressures[i+1] - pressures[i]
        if diff <= DROP_PER_HOUR_THRESHOLD:
            return diff
    return None

# =========================
# 天気絵文字判定
# =========================
def weather_emoji(code, temp):
    if 71 <= code <= 77 and temp <= 3:
        return "❄️"
    if 51 <= code <= 67:
        return "🌧"
    if code == 0:
        return "☀️"
    if 1 <= code <= 3:
        return "⛅"
    return "☁️"

# =========================
# 投稿生成プロンプト
# =========================
SYSTEM_PROMPT = """
あなたは仙台在住者向けの天気痛・低気圧頭痛予報を作成する専門家です。

必ず以下のフォーマットと改行位置を守って出力してください。

【仙台の天気痛・低気圧頭痛予報】{date}

おはようございます。
整体院コクリの気圧予報です{weather_emoji}

12時{h12}hPa｜18時{h18}hPa｜24時{h24}hPa
朝6時の基準は{base}hPa。

今日は{trend}
{impact}
{weather_comment}

【厳守ルール】

・必ず135文字以内
・完成文のみ出力
・絵文字は1つだけ
・怖がらせない
・生活アドバイスを書かない
・宣伝しない
"""

def generate_post(material):
    resp = oa_client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=json.dumps(material, ensure_ascii=False)
    )
    text = resp.output_text.strip()
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN]
    return text

# =========================
# 投稿処理
# =========================
def post_forecast():
    now = datetime.now(TZ)
    times, pressures, temps, hums, codes = fetch_weather()

    times_dt = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in times]
    pressures = [float(p) for p in pressures]
    temps = [float(t) for t in temps]
    codes = [int(c) for c in codes]

    today = now.date()

    tmap = {
        times_dt[i]: {
            "pressure": pressures[i],
            "temp": temps[i],
            "code": codes[i]
        }
        for i in range(len(times_dt))
    }

    base_dt = datetime.combine(today, dtime(6,0), TZ)
    base_p = tmap.get(base_dt, list(tmap.values())[0])["pressure"]

    def get_data(hour):
        if hour == 24:
            dt = datetime.combine(today + timedelta(days=1), dtime(0,0), TZ)
        else:
            dt = datetime.combine(today, dtime(hour,0), TZ)

        if dt in tmap:
            return tmap[dt]

        return list(tmap.values())[0]

    d12 = get_data(12)
    d18 = get_data(18)
    d24 = get_data(24)

    drop = find_drop(times_dt, pressures)

    if drop:
        trend = "【やや下がる】傾向です。"
        impact = "敏感な方は少し注意が必要です。"
    else:
        trend = "【安定】傾向です。"
        impact = "体調への影響は少なそうです。"

    weather_comment = "穏やかな一日になりそうですね。"

    emoji = weather_emoji(d12["code"], d12["temp"])

    material = {
        "date": now.strftime("%m月%d日"),
        "h12": round(d12["pressure"]),
        "h18": round(d18["pressure"]),
        "h24": round(d24["pressure"]),
        "base": round(base_p),
        "trend": trend,
        "impact": impact,
        "weather_comment": weather_comment,
        "weather_emoji": emoji
    }

    post_text = generate_post(material)

    try:
        x_client.create_tweet(text=post_text)
        print("投稿完了:", post_text)
    except Exception as e:
        print("投稿エラー:", e)

# =========================
# 常駐
# =========================
def run_bot():
    last_post_date = None
    print("気圧痛予報BOT 起動")

    if DEPLOY_RUN:
        print("デプロイ即時投稿")
        post_forecast()
        last_post_date = datetime.now(TZ).date()

    while True:
        now = datetime.now(TZ)
        if now.hour == POST_HOUR and now.minute < 10:
            if last_post_date != now.date():
                post_forecast()
                last_post_date = now.date()
        time.sleep(30)

if __name__ == "__main__":
    run_bot()
