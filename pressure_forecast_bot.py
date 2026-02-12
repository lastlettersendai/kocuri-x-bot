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

OPENAI_MODEL = "gpt-5"
MAX_LEN = 135

# =========================
# クライアント初期化
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
# 気象データ取得（拡張）
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
def find_drop_band(times_dt, pressures):
    worst = None
    for i in range(len(pressures)-1):
        diff = pressures[i+1] - pressures[i]
        if diff <= DROP_PER_HOUR_THRESHOLD:
            start = times_dt[i]
            end = times_dt[i+1]
            total = pressures[i+1] - pressures[i]
            if not worst or total < worst[2]:
                worst = (start, end, total)
    return worst

# =========================
# 空気感判定（気温基準で雪防止）
# =========================
def weather_impression(code, temp, humidity):
    # 雪系
    if 71 <= code <= 77:
        if temp <= 3:
            return "雪やみぞれの可能性も。"
        else:
            return "冷たい雨になりそう。"

    # 雨系
    if 51 <= code <= 67:
        return "しっとりした空模様。"

    # 快晴
    if code == 0:
        if temp >= 28:
            return "強い日差しになりそう。"
        return "すっきり晴れそうな一日。"

    # 晴れ〜くもり
    if 1 <= code <= 3:
        if humidity >= 80 and temp >= 23:
            return "少し蒸しっとしそうな空気。"
        return "くもりがちな空模様。"

    return "落ち着いた空気の一日。"

# =========================
# 投稿文生成
# =========================
SYSTEM_PROMPT = """
あなたは仙台在住者向けの低気圧頭痛・気圧痛予報を作る専門家です。

条件：
・1行目固定：【仙台｜低気圧頭痛・気圧痛予報】
・2行目固定：おはようございます。本日の気圧痛予報です。
・12時、18時、24時の気圧を
  「12時1010hPa(-1)｜18時1010hPa(-1)｜24時1010hPa(-1)」形式で1行に
・朝6時の基準気圧を明記
・全体傾向を簡潔に説明
・weather_commentを自然に本文へ入れる
・怖がらせない
・生活指導は書かない
・最後はやさしく締める
・135文字以内
・完成文のみ出力
"""

def generate_post(material):
    response = oa_client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=json.dumps(material, ensure_ascii=False)
    )
    text = response.output_text.strip()
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

    today = now.date()
    base_dt = datetime.combine(today, dtime(6,0), TZ)
    tmap = {
        datetime.fromisoformat(t).replace(tzinfo=TZ): {
            "pressure": float(p),
            "temp": float(tmp),
            "hum": float(h),
            "code": int(c)
        }
        for t,p,tmp,h,c in zip(times, pressures, temps, hums, codes)
    }

    base_p = tmap.get(base_dt, list(tmap.values())[0])["pressure"]

    def get_data(h):
        dt = datetime.combine(today, dtime(h,0), TZ)
        return tmap.get(dt, list(tmap.values())[0])

    d12 = get_data(12)
    d18 = get_data(18)
    d24 = get_data(24)

    band = find_drop_band(times_dt, pressures)

    weather_comment = weather_impression(
        d12["code"], d12["temp"], d12["hum"]
    )

    material = {
        "h12": round(d12["pressure"]),
        "h18": round(d18["pressure"]),
        "h24": round(d24["pressure"]),
        "base": round(base_p),
        "has_drop": bool(band),
        "drop_diff": round(band[2]) if band else None,
        "weather_comment": weather_comment
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
