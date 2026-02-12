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

POST_HOUR = 6
OPENAI_MODEL = "gpt-5"

THREAD_SPLIT = 150  # 1ツイ目目安

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
# 気象データ取得
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
# トレンド判定ロジック
# =========================
def detect_trend(base, p12, p18, p24):
    values = [base, p12, p18, p24]
    diffs = [values[i+1] - values[i] for i in range(len(values)-1)]

    total_change = values[-1] - values[0]
    worst_drop = min(diffs)

    if worst_drop <= -1.5:
        return "急降下"
    if total_change <= -2:
        return "やや下降"
    return "安定"

# =========================
# 天気コメント
# =========================
def weather_impression(code, temp, humidity):
    if 71 <= code <= 77 and temp <= 3:
        return "雪やみぞれの可能性もありそうです。"
    if 51 <= code <= 67:
        return "しっとりした空模様になりそうです。"
    if code == 0:
        if temp >= 28:
            return "強い日差しになりそうです。"
        return "すっきり晴れそうな一日です。"
    if 1 <= code <= 3:
        if humidity >= 80 and temp >= 23:
            return "少し蒸しっとしそうな空気です。"
        return "くもりがちな空模様です。"
    return "落ち着いた空気の一日になりそうです。"

# =========================
# 投稿生成
# =========================
def generate_post(material):
    prompt = f"""
【仙台の天気痛・低気圧頭痛予報】{material['date']}

おはようございます。
整体院コクリの気圧予報です☀️

12時{material['h12']}hPa｜18時{material['h18']}hPa｜24時{material['h24']}hPa
朝6時の基準は{material['base']}hPa。

今日は【{material['trend']}】傾向です。
{material['impact']}
{material['weather_comment']}
穏やかな一日になりますように。
"""

    response = oa_client.responses.create(
        model=OPENAI_MODEL,
        input=prompt
    )
    return response.output_text.strip()

# =========================
# ツリー分割
# =========================
def split_into_thread(text):
    if len(text) <= THREAD_SPLIT:
        return [text]

    cut = text.rfind("\n", 0, THREAD_SPLIT)
    if cut < 20:
        cut = THREAD_SPLIT

    return [text[:cut].strip(), text[cut:].strip()]

# =========================
# 投稿処理
# =========================
def post_forecast():
    now = datetime.now(TZ)
    today = now.date()

    times, pressures, temps, hums, codes = fetch_weather()

    tmap = {
        datetime.fromisoformat(t).replace(tzinfo=TZ): {
            "pressure": float(p),
            "temp": float(tmp),
            "hum": float(h),
            "code": int(c)
        }
        for t, p, tmp, h, c in zip(times, pressures, temps, hums, codes)
    }

    def get_data(hour):
        if hour == 24:
            dt = datetime.combine(today + timedelta(days=1), dtime(0, 0), TZ)
        else:
            dt = datetime.combine(today, dtime(hour, 0), TZ)
        return tmap.get(dt, list(tmap.values())[0])

    base_dt = datetime.combine(today, dtime(6, 0), TZ)
    base_p = round(tmap.get(base_dt, list(tmap.values())[0])["pressure"])

    d12 = round(get_data(12)["pressure"])
    d18 = round(get_data(18)["pressure"])
    d24 = round(get_data(24)["pressure"])

    trend = detect_trend(base_p, d12, d18, d24)

    if trend == "急降下":
        impact = "気圧の変動がやや大きめです。"
    elif trend == "やや下降":
        impact = "敏感な方は少し注意が必要です。"
    else:
        impact = "体調への影響は少なそうです。"

    weather_comment = weather_impression(
        get_data(12)["code"],
        get_data(12)["temp"],
        get_data(12)["hum"]
    )

    material = {
        "date": now.strftime("%m月%d日"),
        "h12": d12,
        "h18": d18,
        "h24": d24,
        "base": base_p,
        "trend": trend,
        "impact": impact,
        "weather_comment": weather_comment
    }

    post_text = generate_post(material)

    parts = split_into_thread(post_text)

    first = x_client.create_tweet(text=parts[0])
    last_id = first.data["id"]

    if len(parts) > 1:
        x_client.create_tweet(
            text=parts[1],
            in_reply_to_tweet_id=last_id
        )

    print("投稿完了")

# =========================
# 常駐
# =========================
def run_bot():
    last_post_date = None
    print("気圧痛予報BOT 起動")

    if DEPLOY_RUN:
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
