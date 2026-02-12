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
MAX_TOTAL_LEN = 210
SINGLE_POST_LIMIT = 130  # これを超えたらツリー

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
# 天気マーク判定
# =========================
def weather_emoji(code):
    if 71 <= code <= 77:
        return "❄️"
    if 51 <= code <= 67:
        return "☔"
    if code == 0:
        return "☀️"
    if 1 <= code <= 3:
        return "🌤"
    return "🌥"

# =========================
# 投稿文生成（OpenAI）
# =========================
def generate_post(material):

    today_str = datetime.now(TZ).strftime("%m月%d日")

    SYSTEM_PROMPT = f"""
あなたは仙台在住者向けの低気圧頭痛・気圧痛予報を作る専門家です。

必ず以下のフォーマットで出力してください。

【仙台｜低気圧頭痛・気圧痛予報】{today_str}
おはようございます。本日の気圧痛予報です {material["emoji"]}

12時{material["h12"]}hPa({material["d12"]:+d})｜18時{material["h18"]}hPa({material["d18"]:+d})｜24時{material["h24"]}hPa({material["d24"]:+d})
朝6時の基準は{material["base"]}hPa。

全体傾向を簡潔に説明。
怖がらせない。
生活指導しない。
やさしく締める。
210文字以内。
完成文のみ出力。
""".strip()

    resp = oa_client.responses.create(
        model=OPENAI_MODEL,
        input=SYSTEM_PROMPT
    )

    text = (resp.output_text or "").strip()

    if len(text) > MAX_TOTAL_LEN:
        text = text[:MAX_TOTAL_LEN]

    return text

# =========================
# ツリー分割
# =========================
def split_for_thread(text: str):
    if len(text) <= SINGLE_POST_LIMIT:
        return [text]

    first = text[:SINGLE_POST_LIMIT]
    second = text[SINGLE_POST_LIMIT:]

    return [first.strip(), second.strip()]

# =========================
# 投稿処理
# =========================
def post_forecast():

    now = datetime.now(TZ)
    times, pressures, temps, hums, codes = fetch_weather()

    times_dt = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in times]

    today = now.date()

    tmap = {}
    for tdt, p, tmp, h, c in zip(times_dt, pressures, temps, hums, codes):
        tmap[tdt] = {
            "pressure": float(p),
            "temp": float(tmp),
            "hum": float(h),
            "code": int(c)
        }

    base_dt = datetime.combine(today, dtime(6, 0), TZ)
    base_p = tmap.get(base_dt, next(iter(tmap.values())))["pressure"]

    def get_data(hour):
        if hour == 24:
            dt = datetime.combine(today + timedelta(days=1), dtime(0, 0), TZ)
        else:
            dt = datetime.combine(today, dtime(hour, 0), TZ)

        if dt in tmap:
            return tmap[dt]

        return next(iter(tmap.values()))

    d12 = get_data(12)
    d18 = get_data(18)
    d24 = get_data(24)

    material = {
        "h12": int(round(d12["pressure"])),
        "h18": int(round(d18["pressure"])),
        "h24": int(round(d24["pressure"])),
        "d12": int(round(d12["pressure"] - base_p)),
        "d18": int(round(d18["pressure"] - base_p)),
        "d24": int(round(d24["pressure"] - base_p)),
        "base": int(round(base_p)),
        "emoji": weather_emoji(d12["code"])
    }

    post_text = generate_post(material)
    parts = split_for_thread(post_text)

    try:
        first = x_client.create_tweet(text=parts[0])
        last_id = first.data["id"]

        if len(parts) > 1:
            x_client.create_tweet(
                text=parts[1],
                in_reply_to_tweet_id=last_id
            )

        print("投稿完了")
    except Exception as e:
        print("投稿エラー:", e)

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
