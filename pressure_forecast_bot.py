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
# パス基準（Railway等での相対パス事故防止）
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
BANNER_NAME = os.getenv("PRESSURE_BANNER_PATH", "pressurex.jpg")
BANNER_PATH = os.path.join(BASE_DIR, BANNER_NAME)

# =========================
# 設定
# =========================
TZ = ZoneInfo("Asia/Tokyo")
SENDAI_LAT = 38.2682
SENDAI_LON = 140.8694

POST_HOUR = int(os.getenv("POST_HOUR", "6"))
POST_WINDOW_MIN = int(os.getenv("POST_WINDOW_MIN", "10"))

# Xの上限は280だけど、安全に260で切る（URLなどの揺れ対策）
TWEET_LIMIT = 260

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_TEMP = float(os.getenv("GEMINI_TEMP", "0.6"))

STATE_PATH = os.getenv("PRESSURE_STATE_PATH", "pressure_state.json")

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
仙台向け気圧痛予報の「本文だけ」を書いてください。

【条件】
・2〜3文（必ず「。」で文を終える）
・湿度による体感を必ず1文入れる
・怖がらせない
・生活指導しない
・宣伝しない
・やさしく締める
・本文のみ出力
・100〜140文字程度（長くしすぎない）

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
# 文章を文末優先で分割（260文字）
# =========================
def split_by_sentence(text, limit=TWEET_LIMIT):
    text = (text or "").strip()
    if not text:
        return []

    parts = []
    rest = text

    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break

        window = rest[:limit]

        # 優先: 改行 > 句点 > 感嘆/疑問 > 読点
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("、"),
        )

        # さすがに短すぎる切り方は避ける
        if cut < 60:
            cut = limit

        parts.append(rest[:cut + (1 if cut != limit else 0)].strip())
        rest = rest[cut + (1 if cut != limit else 0):].strip()

    return [p for p in parts if p]

# =========================
# 投稿文生成（headとbodyを分ける）
# =========================
def build_head(material):
    today_str = now_jst().strftime("%m月%d日")
    head = (
        f"【仙台｜低気圧頭痛・気圧痛予報】{today_str}\n"
        f"おはようございます。整体院コクリの今日の気圧痛予報です {material['emoji']}\n\n"
        f"・12時{material['h12']}hPa({material['d12']:+d})\n"
        f"・18時{material['h18']}hPa({material['d18']:+d})\n"
        f"・24時{material['h24']}hPa({material['d24']:+d})\n"
        f"（朝6時の基準は{material['base']}hPa）"
    )
    return head.strip()

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

    material = {
        "h12": int(round(d12["pressure"])),
        "h18": int(round(d18["pressure"])),
        "h24": int(round(d24["pressure"])),

        "d12": int(round(d12["pressure"] - base_p)),
        "d18": int(round(d18["pressure"] - base_p)),
        "d24": int(round(d24["pressure"] - base_p)),

        "base": int(round(base_p)),

        "hum12": int(round(d12["hum"])),
        "hum18": int(round(d18["hum"])),
        "hum24": int(round(d24["hum"])),

        "trend": "少し下がる" if d24["pressure"] - base_p <= -2 else "安定"
    }

    material["emoji"] = code_to_emoji(d12["code"])

    head = build_head(material)
    body = gemini_body(material)

    # 本文が長い日だけ分割（基本は1本）
    body_parts = split_by_sentence(body, TWEET_LIMIT)

    # =========================
    # DEBUG（ログに必ず出す）
    # =========================
    print("=== DEBUG ===")
    print("Using banner:", BANNER_PATH)
    print("Exists:", os.path.exists(BANNER_PATH))
    print("Head len:", len(head))
    print("Body len:", len(body))
    print("Body parts:", len(body_parts))
    print("=============")

    # =========================
    # 画像アップロード（最初のツイートだけ）
    # =========================
    media_id = None
    try:
        if os.path.exists(BANNER_PATH):
            media = x_api_v1.media_upload(BANNER_PATH)
            media_id = getattr(media, "media_id_string", None) or str(media.media_id)
            print("uploaded media_id:", media_id)
        else:
            print("banner NOT FOUND")
    except Exception as e:
        print("media_upload ERROR:", e)
        media_id = None

    # =========================
    # 投稿（ツリー）
    # =========================
    if media_id:
        first = x_client.create_tweet(text=head, media_ids=[media_id])
    else:
        first = x_client.create_tweet(text=head)

    parent_id = first.data["id"]

    # 2ツイ目：本文（1〜n本）
    for p in body_parts:
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
