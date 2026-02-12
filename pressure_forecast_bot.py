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
POST_MINUTE = 0

OPENAI_MODEL = "gpt-5"

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
# 気圧取得
# =========================
def fetch_pressure():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={SENDAI_LAT}"
        f"&longitude={SENDAI_LON}"
        "&hourly=surface_pressure"
        "&timezone=Asia%2FTokyo"
        "&forecast_days=2"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    times = j["hourly"]["time"]
    pressures = j["hourly"]["surface_pressure"]
    return times, pressures

def build_map(times, pressures):
    tmap = {}
    for t, p in zip(times, pressures):
        dt = datetime.fromisoformat(t).replace(tzinfo=TZ)
        tmap[dt] = float(p)
    return tmap

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
# 投稿文生成
# =========================
SYSTEM_PROMPT = """あなたはX投稿のプロコピーライターです。
毎朝6:00時点の「仙台｜低気圧頭痛・気圧痛予報」を作ります。

条件：
・1行目固定：【仙台｜低気圧頭痛・気圧痛予報】
・2行目固定：おはようございます。本日の気圧痛予報です。
・12時/18時/24時のhPaと差分を1行でまとめる
・急降下帯があれば「急降下帯：◯時〜◯時（-XhPa）」を入れる
・最後は安心で締める
・140〜220文字
・ハッシュタグなし
"""

def generate_post(material):
    response = oa_client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=json.dumps(material, ensure_ascii=False)
    )
    return response.output_text.strip()

# =========================
# メイン投稿処理
# =========================
def post_forecast():
    now = datetime.now(TZ)
    times, pressures = fetch_pressure()
    times_dt = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in times]
    tmap = build_map(times, pressures)

    today = now.date()

    base = datetime.combine(today, dtime(6,0), TZ)
    base_p = tmap.get(base, pressures[0])

    def get_hpa(h):
        dt = datetime.combine(today, dtime(h,0), TZ)
        return tmap.get(dt, base_p)

    h12 = get_hpa(12)
    h18 = get_hpa(18)
    h24 = tmap.get(datetime.combine(today+timedelta(days=1), dtime(0,0), TZ), base_p)

    band = find_drop_band(times_dt, pressures)

    material = {
        "base": round(base_p),
        "points": [
            {"label":"12時","hpa":round(h12),"diff":round(h12-base_p)},
            {"label":"18時","hpa":round(h18),"diff":round(h18-base_p)},
            {"label":"24時","hpa":round(h24),"diff":round(h24-base_p)}
        ],
        "drop_band": {
            "start": band[0].hour if band else None,
            "end": band[1].hour if band else None,
            "drop": round(band[2]) if band else None
        }
    }

    post_text = generate_post(material)

    res = x_client.create_tweet(text=post_text)
    print("投稿完了:", post_text)
    return True

# =========================
# 常駐ループ
# =========================
def run_bot():
    last_post_date = None
    print("気圧痛予報BOT 起動")

    if DEPLOY_RUN:
        print("デプロイ即時投稿")
        post_forecast()

    while True:
        now = datetime.now(TZ)

        if now.hour == POST_HOUR and now.minute < 10:
            if last_post_date != now.date():
                post_forecast()
                last_post_date = now.date()

        time.sleep(30)

if __name__ == "__main__":
    run_bot()