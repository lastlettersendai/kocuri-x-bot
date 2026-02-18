import os
import time
import json
import re
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
import tweepy
# 最新のSDK: pip install google-genai
from google import genai
from google.genai import types

# =========================
# 基本設定
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Asia/Tokyo")

SENDAI_LAT = 38.2682
SENDAI_LON = 140.8694

POST_HOUR = int(os.getenv("POST_HOUR", "6"))

# 日本語(全角)は140文字が限界のため、安全マージンをとって135に設定
TWEET_LIMIT = 135

STATE_PATH = os.getenv("PRESSURE_STATE_PATH", "pressure_state.json")
BANNER_NAME = os.getenv("PRESSURE_BANNER_PATH", "pressurex.jpg")
BANNER_PATH = os.path.join(BASE_DIR, BANNER_NAME)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")  # モデル名は適宜変更
GEMINI_TEMP = float(os.getenv("GEMINI_TEMP", "0.6"))

DEPLOY_RUN = (os.getenv("DEPLOY_RUN", "0") == "1")
FORCE_POST = (os.getenv("FORCE_POST", "0") == "1")

# =========================
# Xクライアント
# =========================
x_client = tweepy.Client(
    bearer_token=os.getenv("X_BEARER_TOKEN"),
    consumer_key=os.getenv("API_KEY"),
    consumer_secret=os.getenv("API_SECRET"),
    access_token=os.getenv("ACCESS_TOKEN"),
    access_token_secret=os.getenv("ACCESS_TOKEN_SECRET")
)

x_api_v1 = tweepy.API(
    tweepy.OAuth1UserHandler(
        os.getenv("API_KEY"),
        os.getenv("API_SECRET"),
        os.getenv("ACCESS_TOKEN"),
        os.getenv("ACCESS_TOKEN_SECRET"),
    )
)

gen_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# =========================
# 状態管理
# =========================
def now_jst():
    return datetime.now(TZ)

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_post_date": None, "last_body": "", "last_extra": ""}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f) or {}
            st.setdefault("last_post_date", None)
            st.setdefault("last_body", "")
            st.setdefault("last_extra", "")
            return st
    except Exception:
        return {"last_post_date": None, "last_body": "", "last_extra": ""}

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

def get_last_texts():
    st = load_state()
    return (st.get("last_body", "") or "").strip(), (st.get("last_extra", "") or "").strip()

def set_last_texts(body: str, extra: str):
    st = load_state()
    st["last_body"] = (body or "").strip()
    st["last_extra"] = (extra or "").strip()
    save_state(st)

# =========================
# 天気取得（露点含む）
# =========================
def fetch_weather():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={SENDAI_LAT}"
        f"&longitude={SENDAI_LON}"
        "&hourly=surface_pressure,temperature_2m,relative_humidity_2m,dewpoint_2m"
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
        j["hourly"]["dewpoint_2m"],
    )

# =========================
# 補助
# =========================
def get_closest(target_dt, tmap):
    if not tmap:
        raise ValueError("Weather data is empty")
    return min(tmap.keys(), key=lambda k: abs((k - target_dt).total_seconds()))

def split_by_sentence(text, limit=TWEET_LIMIT):
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break

        window = rest[:limit]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("、"),
        )
        if cut < 10:
            cut = limit

        take_len = cut + (1 if cut != limit else 0)
        parts.append(rest[:take_len].strip())
        rest = rest[take_len:].strip()

    return [p for p in parts if p]

# =========================
# コクリ仕様 判定ロジック
# =========================
def classify_pressure(base, h12, h18, h24):
    vals = [base, h12, h18, h24]
    day_range = max(vals) - min(vals)
    delta = h24 - base

    if day_range >= 8 or abs(delta) >= 7:
        level = 2
        label = "変化大"
    elif day_range >= 5 or abs(delta) >= 4:
        level = 1
        label = "やや変化"
    else:
        level = 0
        label = "穏やか"

    return level, label, day_range, delta

def classify_amplifier(temp_range, dew_max):
    score = 0
    if temp_range >= 7:
        score += 1
    if dew_max >= 16:
        score += 1
    return score

def closing_style(total_level: int) -> str:
    if total_level <= 1:
        return "安心"
    if total_level <= 3:
        return "軽い注意"
    return "注意喚起"

# =========================
# Gemini 設定
# =========================
SAFETY_SETTINGS = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH
    ),
]

# 口語が混ざったら弾く（再生成のトリガ）
BANNED_PHRASES = [
    "かもね", "だよ", "だね", "してね", "じゃん", "みたい",
    "あなた", "みなさん"
]
BANNED_RE = re.compile("|".join(map(re.escape, BANNED_PHRASES)))

def looks_bad_tone(text: str) -> bool:
    if not text:
        return True
    if "\n" in text:
        return True  # 改行なしルール
    if BANNED_RE.search(text):
        return True
    # です・ますが全く無いのも危険（フランク寄り）
    if ("です" not in text) and ("ます" not in text):
        return True
    return False

def _gemini_generate(prompt: str, temperature: float):
    r = gen_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=temperature,
            safety_settings=SAFETY_SETTINGS
        )
    )
    return (r.text or "").strip()

def gemini_body(material, prev_body: str = ""):
    style = closing_style(material["total_level"])

    prompt = f"""
あなたは天気予報キャスターです。仙台向け「気圧痛予報」の本文だけを書いてください。

【本文の型（固定）】
・3文固定、改行なし
・1文目：気圧が主役（方向と強さを短く。{material["pressure_label"]}／振れ幅{material["range"]}hPa／6→24差{material["delta"]:+d}hPa）
・2文目：補足（気温差{material["temp_range"]}℃、露点最大{material["dew_max"]}℃を“体感”として控えめに触れる）
・3文目：締め（{style} で締める）
  - 安心：落ち着いた一日になりそう／心ほどける時間を、など
  - 軽い注意：無理のない範囲で／いつもより丁寧に、など
  - 注意喚起：予定は詰めすぎず／ゆったりめに、など

【口調の厳守】
・必ず「です／ます」調で統一（です・ますを必ず入れる）
・禁止語：「〜かもね」「〜だね」「〜だよ」「〜してね」「みたい」「〜じゃん」
・二人称（あなた／みなさん）禁止
・怖がらせない／宣伝しない／医療の断定や指示をしない
・120〜130文字程度
・本文のみ出力
