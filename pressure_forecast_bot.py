import os
import time
import json
import re
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
import tweepy
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
TWEET_LIMIT = 135
REPLY_WAIT_SEC = float(os.getenv("REPLY_WAIT_SEC", "2.5"))

STATE_PATH = os.getenv("PRESSURE_STATE_PATH", "pressure_state.json")
BANNER_NAME = os.getenv("PRESSURE_BANNER_PATH", "pressurex.jpg")
BANNER_PATH = os.path.join(BASE_DIR, BANNER_NAME)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
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
            return json.load(f)
    except Exception:
        return {"last_post_date": None, "last_body": "", "last_extra": ""}

def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

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
    return st.get("last_body", ""), st.get("last_extra", "")

def set_last_texts(body, extra):
    st = load_state()
    st["last_body"] = body
    st["last_extra"] = extra
    save_state(st)

# =========================
# 天気取得
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
# 補助関数（AIの文字数オーバー対策）
# =========================
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
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("、"),
            window.rfind(" "),
        )
        if cut < 10:
            cut = limit

        take_len = cut + (1 if cut != limit else 0)
        parts.append(rest[:take_len].strip())
        rest = rest[take_len:].strip()

    return [p for p in parts if p]

# =========================
# 判定ロジック
# =========================
def classify_pressure(base, h12, h18, h24):
    vals = [base, h12, h18, h24]
    day_range = max(vals) - min(vals)
    delta = h24 - base

    if day_range >= 8 or abs(delta) >= 7:
        return 2, "変化大", day_range, delta
    elif day_range >= 5 or abs(delta) >= 4:
        return 1, "やや変化", day_range, delta
    return 0, "穏やか", day_range, delta

def classify_amplifier(temp_range, dew_max):
    score = 0
    if temp_range >= 7:
        score += 1
    if dew_max >= 16:
        score += 1
    return score

def closing_style(total_level):
    if total_level <= 1:
        return "安心"
    if total_level <= 3:
        return "軽い注意"
    return "注意喚起"

# =========================
# Gemini 設定＆生成ロジック
# =========================
SAFETY_SETTINGS = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
]

def gemini_generate(prompt: str) -> str:
    try:
        r = gen_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=GEMINI_TEMP,
                safety_settings=SAFETY_SETTINGS
            )
        )
        text = (r.text or "").strip()
        return re.sub(r"\s+", " ", text)
    except Exception as e:
        print("Gemini error:", repr(e))
        return ""

def gemini_body(material, prev_body: str = "", mmdd: str = ""):
    prompt = f"""
あなたは天気予報キャスターです。以下のデータを使って、気圧痛に悩む方向けのX投稿文を120文字程度で作成してください。

【データ】
・気圧変化：{material['pressure_label']}（振れ幅 {material['range']}hPa / 6→24時差 {material['delta']:+d}hPa）
・気温差：{material['temp_range']}℃
・露点最大：{material['dew_max']}℃
・アドバイス基準：{closing_style(material['total_level'])}

【執筆ルール（厳守）】
・文頭に【】などの見出しや肩書きは付けないこと。
・「露点」という専門用語は絶対に使わないこと。
・代わりに露点の数値を参考に、体感に翻訳して自然に組み込むこと。
・です/ます調
・未来語（明日・週末など）禁止
・前回（{prev_body if prev_body else "なし"}）とは違う表現
・今日({mmdd})の内容として書く
・改行なし
"""
    return gemini_generate(prompt)

def gemini_extra(material, prev_extra: str = "", mmdd: str = ""):
    prompt = f"""
気圧変動が強めの日の追加のひとことを70文字程度で作成してください。
です/ます調。不安を煽らない。
前回（{prev_extra if prev_extra else "なし"}）とは違う表現にする。
改行なし。
"""
    return gemini_generate(prompt)

# =========================
# 投稿処理
# =========================
def post_forecast():
    now = now_jst()
    today = now.date()
    mmdd = now.strftime("%m/%d")

    try:
        times, pressures, temps, hums, dews = fetch_weather()
        times_dt = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in times]

        tmap = {}
        for t, p, tmp, h, dw in zip(times_dt, pressures, temps, hums, dews):
            if p is None or tmp is None:
                continue
            tmap[t] = {
                "pressure": float(p),
                "temp": float(tmp),
                "dew": float(dw) if dw else 0.0,
            }

        base_dt = datetime.combine(today, dtime(6, 0), TZ)
        base_key = min(tmap.keys(), key=lambda k: abs((k - base_dt).total_seconds()))
        base = int(round(tmap[base_key]["pressure"]))

        def get_hour(hour):
            if hour == 24:
                dt = datetime.combine(today + timedelta(days=1), dtime(0, 0), TZ)
            else:
                dt = datetime.combine(today, dtime(hour, 0), TZ)
            key = min(tmap.keys(), key=lambda k: abs((k - dt).total_seconds()))
            return tmap[key]

        d12 = get_hour(12)
        d18 = get_hour(18)
        d24 = get_hour(24)

        h12 = int(round(d12["pressure"]))
        h18 = int(round(d18["pressure"]))
        h24 = int(round(d24["pressure"]))

        pressure_level, label, day_range, delta = classify_pressure(base, h12, h18, h24)

        temp_vals = [d12["temp"], d18["temp"], d24["temp"]]
        temp_range = int(round(max(temp_vals) - min(temp_vals)))
        dew_max = int(round(max(d12["dew"], d18["dew"], d24["dew"])))

        total_level = pressure_level + classify_amplifier(temp_range, dew_max)

        material = {
            "pressure_label": label,
            "range": day_range,
            "delta": delta,
            "temp_range": temp_range,
            "dew_max": dew_max,
            "total_level": total_level,
        }

        head = (
            f"【仙台｜低気圧頭痛・気圧痛予報】{today.strftime('%m月%d日')}\n"
            f"おはようございます。整体院コクリの今日の気圧痛予報です\n\n"
            f"・12時{h12}hPa({h12-base:+d})\n"
            f"・18時{h18}hPa({h18-base:+d})\n"
            f"・24時{h24}hPa({h24-base:+d})\n"
            f"（朝6時基準 {base}hPa）"
        )

        prev_body, prev_extra = get_last_texts()

        # 本文とタグの生成
        body = gemini_body(material, prev_body, mmdd)
        # 万が一AIが空文字を返した時の安全装置
        if not body:
            body = f"今日は気圧変化が{label}で、振れ幅{day_range}hPaです。気温差は{temp_range}℃です。無理のない範囲でお過ごしください。"
            
        extra = gemini_extra(material, prev_extra, mmdd) if total_level >= 4 else ""
        uniq_tag = f" ({mmdd} Δ{delta:+d} R{day_range})"

        # 140文字対策：タグを含めてもオーバーしないように安全に分割する
        safe_limit = TWEET_LIMIT - len(uniq_tag)
        body_parts = split_by_sentence(body, safe_limit)
        if body_parts:
            body_parts[-1] += uniq_tag # 最後のパーツに必ずタグをつける
        else:
            body_parts = [uniq_tag]

        # 画像アップロード（安全な取得とエラーハンドリング）
        media_id = None
        if os.path.exists(BANNER_PATH):
            try:
                media = x_api_v1.media_upload(BANNER_PATH)
                media_id = getattr(media, "media_id_string", None) or str(media.media_id)
            except Exception as e:
                print("media_upload error:", repr(e))
                media_id = None

        # API仕様変更に対応した安全な辞書渡し
        tweet_params = {"text": head, "user_auth": True}
        if media_id:
            tweet_params["media_ids"] = [media_id]

        first = x_client.create_tweet(**tweet_params)
        parent_id = str(first.data["id"])
        
        time.sleep(REPLY_WAIT_SEC)
        ok = True

        # 本文（2ツイート目以降）を投稿
        for p in body_parts:
            try:
                res = x_client.create_tweet(
                    text=p,
                    in_reply_to_tweet_id=parent_id,
                    user_auth=True
                )
                parent_id = str(res.data["id"])
                time.sleep(REPLY_WAIT_SEC)
            except Exception as e:
                print("reply error:", repr(e))
                ok = False
                break

        # 追加のひとこと（条件次第）
        if ok and extra:
            extra_parts = split_by_sentence(extra, TWEET_LIMIT)
            for ep in extra_parts:
                try:
                    res = x_client.create_tweet(
                        text=ep,
                        in_reply_to_tweet_id=parent_id,
                        user_auth=True
                    )
                    parent_id = str(res.data["id"])
                    time.sleep(REPLY_WAIT_SEC)
                except Exception as e:
                    print("extra error:", repr(e))
                    ok = False
                    break

        if ok:
            set_last_post_date(today)
            set_last_texts(body, extra)
            print("投稿完了")

    except Exception as e:
        print("FATAL:", repr(e))

# =========================
# 常駐
# =========================
def run_bot():
    print("BOT起動:", now_jst())

    if FORCE_POST:
        post_forecast()
        return

    if DEPLOY_RUN:
        if get_last_post_date() != now_jst().date():
            post_forecast()

    while True:
        if get_last_post_date() != now_jst().date() and now_jst().hour >= POST_HOUR:
            post_forecast()
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
