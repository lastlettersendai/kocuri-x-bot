import os
import time
import json
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
TWEET_LIMIT = 260

STATE_PATH = os.getenv("PRESSURE_STATE_PATH", "pressure_state.json")
BANNER_NAME = os.getenv("PRESSURE_BANNER_PATH", "pressurex.jpg")
BANNER_PATH = os.path.join(BASE_DIR, BANNER_NAME)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
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
        if cut < 60:
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

    # 敏感寄り（コクリ仕様）
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
# Gemini 本文（キャスター風に締め方を変える）
# =========================
def gemini_body(material):
    style = closing_style(material["total_level"])

    prompt = f"""
あなたは天気予報キャスターのように、やさしい口調で仙台向け「気圧痛予報」の本文だけを書いてください。

【本文の型（固定）】
・3文固定、改行なし
・1文目：気圧が主役（方向と強さを短く。{material["pressure_label"]}／振れ幅{material["range"]}hPa／6→24差{material["delta"]:+d}hPa）
・2文目：補足（気温差{material["temp_range"]}℃、露点最大{material["dew_max"]}℃を“体感”として控えめに触れる）
・3文目：締め（{style} で締める）
  - 安心：落ち着いた一日になりそう／心ほどける時間を、など
  - 軽い注意：無理のない範囲で、いつもより丁寧に、など
  - 注意喚起：今日は揺れが出やすいかも。予定は詰めすぎず、ゆったりめに、など
※怖がらせない／宣伝しない／医療の断定や指示をしない
※120〜170文字程度
※本文のみ出力

総合レベル: {material["total_level"]}
""".strip()

    r = gen_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=GEMINI_TEMP)
    )
    return (r.text or "").strip()

# =========================
# Gemini 追加（総合4以上のみ：短い注意喚起/補足）
# =========================
def gemini_extra(material):
    prompt = f"""
あなたは天気予報キャスター。
仙台向け気圧痛予報の「追加のひとこと」だけを書いてください。

【条件】
・1〜2文、改行なし
・70〜130文字
・怖がらせない
・医療の断定や指示をしない
・宣伝しない
・内容は「今日は変動が強めなので、ゆったりめに」程度のやさしい注意喚起や、体感の補足にする
・本文のみ出力

気圧: {material["pressure_label"]}／振れ幅{material["range"]}hPa／6→24差{material["delta"]:+d}hPa
気温差: {material["temp_range"]}℃
露点最大: {material["dew_max"]}℃
総合レベル: {material["total_level"]}
""".strip()

    r = gen_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=GEMINI_TEMP)
    )
    return (r.text or "").strip()

# =========================
# 見出し（1ツリー目）
# =========================
def build_head(today, base, h12, h18, h24):
    return (
        f"【仙台｜低気圧頭痛・気圧痛予報】{today.strftime('%m月%d日')}\n"
        f"・12時{h12}hPa({h12-base:+d})\n"
        f"・18時{h18}hPa({h18-base:+d})\n"
        f"・24時{h24}hPa({h24-base:+d})\n"
        f"（朝6時基準 {base}hPa）"
    ).strip()

# =========================
# 投稿処理
# =========================
def post_forecast():
    now = now_jst()
    today = now.date()

    times, pressures, temps, hums, dews = fetch_weather()
    times_dt = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in times]

    tmap = {}
    for t, p, tmp, h, dw in zip(times_dt, pressures, temps, hums, dews):
        tmap[t] = {
            "pressure": float(p),
            "temp": float(tmp),
            "hum": float(h),
            "dew": float(dw),
        }

    # 基準（朝6時：最寄り）
    base_dt = datetime.combine(today, dtime(6, 0), TZ)
    base_key = get_closest(base_dt, tmap)
    base = int(round(tmap[base_key]["pressure"]))

    def get_hour(hour):
        if hour == 24:
            dt = datetime.combine(today + timedelta(days=1), dtime(0, 0), TZ)
        else:
            dt = datetime.combine(today, dtime(hour, 0), TZ)
        key = get_closest(dt, tmap)
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

    amplifier = classify_amplifier(temp_range, dew_max)
    total_level = pressure_level + amplifier

    material = {
        "pressure_label": label,
        "range": day_range,
        "delta": delta,
        "temp_range": temp_range,
        "dew_max": dew_max,
        "total_level": total_level,
    }

    head = build_head(today, base, h12, h18, h24)
    body = gemini_body(material)
    body_parts = split_by_sentence(body, TWEET_LIMIT)

    # 画像アップロード（1ツイート目だけ）
    media_id = None
    try:
        if os.path.exists(BANNER_PATH):
            media = x_api_v1.media_upload(BANNER_PATH)
            media_id = getattr(media, "media_id_string", None) or str(media.media_id)
    except Exception:
        media_id = None

    # 1ツリー目（数値）
    if media_id:
        first = x_client.create_tweet(text=head, media_ids=[media_id])
    else:
        first = x_client.create_tweet(text=head)

    parent_id = first.data["id"]

    # 2ツリー目（本文：常に）
    for p in body_parts:
        res = x_client.create_tweet(text=p, in_reply_to_tweet_id=parent_id)
        parent_id = res.data["id"]

    # 3ツリー目（総合4以上のみ：追加のひとこと）
    if total_level >= 4:
        extra = gemini_extra(material)
        extra = (extra or "").strip()
        if extra:
            x_client.create_tweet(text=extra, in_reply_to_tweet_id=parent_id)

    set_last_post_date(today)
    print("投稿完了")

# =========================
# 常駐
# =========================
def run_bot():
    print("気圧痛予報BOT 起動")
    print("NOW(JST):", now_jst().isoformat())
    print("LAST_POST_DATE:", get_last_post_date())
    print("DEPLOY_RUN:", DEPLOY_RUN)
    print("FORCE_POST:", FORCE_POST)

    # テストで今すぐ投稿したい時だけ
    if FORCE_POST:
        post_forecast()
        return

    # 起動時に、今日まだなら投稿（起動遅れ救済）
    if DEPLOY_RUN:
        if get_last_post_date() != now_jst().date():
            post_forecast()

    while True:
        now = now_jst()
        today = now.date()

        # 今日まだ投稿してなくて、投稿時刻を過ぎたら投稿
        if get_last_post_date() != today and now.hour >= POST_HOUR:
            post_forecast()

        time.sleep(60)

if __name__ == "__main__":
    run_bot()
