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
    return j["hourly"]["time"], j["hourly"]["surface_pressure"]

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
SYSTEM_PROMPT = """
あなたは仙台在住者向けの「低気圧頭痛・気圧痛予報」を作る専門家です。

条件：
・1行目固定：【仙台｜低気圧頭痛・気圧痛予報】
・2行目固定：おはようございます。本日の気圧痛予報です。
・12時、18時、24時の気圧を「12時1010hPa(-1)｜18時1010hPa(-1)｜24時1010hPa(-1)」の形式で1行にまとめる
・朝6時の基準気圧も明記する（例：朝の基準は1012hPa。）
・全体の傾向を一文で説明（横ばい／わずかに低め／やや下降／急降下など）
・急降下がある場合のみ時間帯と差分を補足してそのまま締める
・生活アドバイス（水分補給・保温など）は入れない
・最後はやさしく締める（例：無理せず、安心してお過ごしくださいね。）
・135文字以内（絶対）
・ハッシュタグなし
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
    times, pressures = fetch_pressure()
    times_dt = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in times]
    pressures = [float(p) for p in pressures]

    today = now.date()
    base_dt = datetime.combine(today, dtime(6,0), TZ)

    # 気圧マップ作成
    tmap = {datetime.fromisoformat(t).replace(tzinfo=TZ): float(p)
            for t,p in zip(times, pressures)}

    base_p = tmap.get(base_dt, pressures[0])

    def get_hpa(h):
        dt = datetime.combine(today, dtime(h,0), TZ)
        return round(tmap.get(dt, base_p))

    h12 = get_hpa(12)
    h18 = get_hpa(18)

    band = find_drop_band(times_dt, pressures)

    material = {
        "h12": h12,
        "h18": h18,
        "has_drop": bool(band),
        "drop_diff": round(band[2]) if band else None
    }

    post_text = generate_post(material)

    try:
        x_client.create_tweet(text=post_text)
        print("投稿完了:", post_text)
        return True
    except tweepy.errors.Forbidden as e:
        print("403 Forbidden:", e)
        return False
    except Exception as e:
        print("投稿エラー:", e)
        return False

# =========================
# 常駐ループ
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
