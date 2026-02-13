import os
import time
import re
import json
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
import tweepy
from google import genai
from google.genai import types

# =========================
# 環境変数
# =========================
X_API_KEY = os.getenv("API_KEY")
X_API_SECRET = os.getenv("API_SECRET")
X_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
X_ACCESS_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEPLOY_RUN = (os.getenv("DEPLOY_RUN", "0") == "1")

# 画像バナー（固定 or 自動生成で上書きするファイル）
BANNER_PATH = os.getenv("PRESSURE_BANNER_PATH", "pressurex.jpg")

# =========================
# 設定
# =========================
TZ = ZoneInfo("Asia/Tokyo")
SENDAI_LAT = 38.2682
SENDAI_LON = 140.8694

POST_HOUR = int(os.getenv("POST_HOUR", "6"))
POST_WINDOW_MIN = int(os.getenv("POST_WINDOW_MIN", "10"))

MAX_TOTAL_LEN = 210
SINGLE_LIMIT = 130

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_TEMP = float(os.getenv("GEMINI_TEMP", "0.6"))

STATE_PATH = os.getenv("PRESSURE_STATE_PATH", "pressure_state.json")

# =========================
# クライアント
# =========================
# 投稿（v2）
x_client = tweepy.Client(
    bearer_token=X_BEARER_TOKEN,
    consumer_key=X_API_KEY,
    consumer_secret=X_API_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET
)

# 画像アップロード（v1.1）
x_api_v1 = tweepy.API(
    tweepy.OAuth1UserHandler(
        X_API_KEY,
        X_API_SECRET,
        X_ACCESS_TOKEN,
        X_ACCESS_SECRET,
    )
)

gen_client = genai.Client(api_key=GEMINI_API_KEY)

# =========================
# 時刻関連
# =========================
def now_jst():
    return datetime.now(TZ)

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_post_date": None}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_post_date": None}

def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_last_post_date():
    st = load_state()
    v = st.get("last_post_date")
    if not v:
        return None
    try:
        return datetime.fromisoformat(v).date()
    except Exception:
        return None

def set_last_post_date(d):
    st = load_state()
    st["last_post_date"] = datetime.combine(d, dtime(0, 0), TZ).isoformat()
    save_state(st)

def in_post_window(ref):
    today = ref.date()
    start = datetime.combine(today, dtime(POST_HOUR, 0), TZ)
    end = start + timedelta(minutes=POST_WINDOW_MIN)
    return start <= ref < end

# =========================
# 天気取得
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
# 天気マーク
# =========================
def code_to_emoji(code):
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
# Gemini本文
# =========================
def gemini_body(material):
    prompt = f"""
あなたは整体師。
仙台向け気圧痛予報の本文だけを書いてください。

【条件】
・2〜3文
・湿度による体感を必ず1文入れる
・怖がらせない
・生活指導しない
・宣伝しない
・やさしく締める
・本文のみ出力
・100文字前後

湿度:
12時{material["hum12"]}% / 18時{material["hum18"]}% / 24時{material["hum24"]}%
傾向: {material["trend"]}
""".strip()

    r = gen_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=GEMINI_TEMP)
    )
    return (r.text or "").strip()

# =========================
# 改行優先ツリー分割（複数対応）
# =========================
def split_thread(text):
    parts = []
    rest = text.strip()

    while rest:
        if len(rest) <= SINGLE_LIMIT:
            parts.append(rest)
            break

        window = rest[:SINGLE_LIMIT]
        cut = window.rfind("\n")

        if cut < 60:
            cut = -1
            for m in re.finditer(r"[。！？]", window):
                cut = m.end()

        if cut < 60:
            cut = SINGLE_LIMIT

        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()

    return parts

# =========================
# 投稿文生成
# =========================
def build_post(material):
    today_str = now_jst().strftime("%m月%d日")

    head = (
        f"【仙台｜低気圧頭痛・気圧痛予報】{today_str}\n"
        f"おはようございます。整体院コクリの今日の気圧痛予報です {material['emoji']}\n\n"
        f"・12時{material['h12']}hPa({material['d12']:+d})\n"
        f"・18時{material['h18']}hPa({material['d18']:+d})\n"
        f"・24時{material['h24']}hPa({material['d24']:+d})\n"
        f"（朝6時の基準は{material['base']}hPa）"
    )

    body = gemini_body(material)
    full = head + "\n\n" + body

    if len(full) > MAX_TOTAL_LEN:
        full = full[:MAX_TOTAL_LEN].rstrip()

    return full

# =========================
# 投稿処理
# =========================
def post_forecast():
    now = now_jst()
    today = now.date()

    times, pressures, temps, hums, codes = fetch_weather()
    times_dt = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in times]

    tmap = {}
    for tdt, p, tmp, h, c in zip(times_dt, pressures, temps, hums, codes):
        tmap[tdt] = {
            "pressure": float(p),
            "temp": float(tmp),
            "hum": float(h),
            "code": int(c),
        }

    base_dt = datetime.combine(today, dtime(6, 0), TZ)
    base_p = tmap.get(base_dt, next(iter(tmap.values())))["pressure"]

    def get_data(hour):
        if hour == 24:
            dt = datetime.combine(today + timedelta(days=1), dtime(0, 0), TZ)
        else:
            dt = datetime.combine(today, dtime(hour, 0), TZ)
        return tmap.get(dt, next(iter(tmap.values())))

    d12 = get_data(12)
    d18 = get_data(18)
    d24 = get_data(24)

    base = int(round(base_p))

    material = {
        "h12": int(round(d12["pressure"])),
        "h18": int(round(d18["pressure"])),
        "h24": int(round(d24["pressure"])),

        "d12": int(round(d12["pressure"] - base_p)),
        "d18": int(round(d18["pressure"] - base_p)),
        "d24": int(round(d24["pressure"] - base_p)),

        "base": base,

        "hum12": int(round(d12["hum"])),
        "hum18": int(round(d18["hum"])),
        "hum24": int(round(d24["hum"])),

        "trend": "少し下がる" if d24["pressure"] - base_p <= -2 else "安定"
    }

    material["emoji"] = code_to_emoji(d12["code"])

    post_text = build_post(material)
    parts = split_thread(post_text)

    # =========================
    # 画像アップロード（最初のツイートだけに付与）
    # =========================
    media_id = None
    if BANNER_PATH and os.path.exists(BANNER_PATH):
        try:
            media = x_api_v1.media_upload(BANNER_PATH)
            media_id = media.media_id
        except Exception as e:
            print(f"画像アップロード失敗（画像なしで投稿します）: {e}")
    else:
        print(f"バナー画像が見つかりません: {BANNER_PATH}（画像なしで投稿します）")

    # =========================
    # 投稿（ツリー対応）
    # =========================
    if media_id:
        first = x_client.create_tweet(text=parts[0], media_ids=[media_id])
    else:
        first = x_client.create_tweet(text=parts[0])

    parent_id = first.data["id"]

    # 2ツイート目以降
    for p in parts[1:]:
        res = x_client.create_tweet(text=p, in_reply_to_tweet_id=parent_id)
        parent_id = res.data["id"]

    set_last_post_date(today)
    print("投稿完了")

# =========================
# 常駐
# =========================
def run_bot():
    print("気圧痛予報BOT 起動")

    if DEPLOY_RUN:
        if get_last_post_date() != now_jst().date():
            post_forecast()

    while True:
        now = now_jst()
        if in_post_window(now) and get_last_post_date() != now.date():
            post_forecast()
        time.sleep(30)

if __name__ == "__main__":
    run_bot()
