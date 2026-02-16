import os
import time
import json
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

# 【修正1】日本語(全角)は140文字が限界のため、安全マージンをとって135に設定
TWEET_LIMIT = 135

STATE_PATH = os.getenv("PRESSURE_STATE_PATH", "pressure_state.json")
BANNER_NAME = os.getenv("PRESSURE_BANNER_PATH", "pressurex.jpg")
BANNER_PATH = os.path.join(BASE_DIR, BANNER_NAME)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash") # モデル名は適宜変更
GEMINI_TEMP = float(os.getenv("GEMINI_TEMP", "0.6"))

DEPLOY_RUN = (os.getenv("DEPLOY_RUN", "0") == "1")
FORCE_POST = (os.getenv("FORCE_POST", "0") == "1")

# =========================
# Xクライアント
# =========================
# ※ 認証情報が空の場合はエラーになるためチェック推奨
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
        # 区切り文字が見つからない、または先頭すぎる場合は強制カット
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
# 【修正3】医療系と誤判定されないよう安全設定を緩める
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
※120〜130文字程度
※本文のみ出力

総合レベル: {material["total_level"]}
""".strip()

    try:
        r = gen_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=GEMINI_TEMP,
                safety_settings=SAFETY_SETTINGS
            )
        )
        return (r.text or "").strip()
    except Exception as e:
        print(f"Gemini Body Error: {e}")
        return ""

def gemini_extra(material):
    prompt = f"""
あなたは天気予報キャスター。
仙台向け気圧痛予報の「追加のひとこと」だけを書いてください。

【条件】
・1〜2文、改行なし
・70〜100文字
・怖がらせない
・医療の断定や指示をしない
・宣伝しない
・内容は「今日は変動が強めなので、ゆったりめに」程度のやさしい注意喚起や、体感の補足にする
・本文のみ出力

気圧: {material["pressure_label"]}
総合レベル: {material["total_level"]}
""".strip()

    try:
        r = gen_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=GEMINI_TEMP,
                safety_settings=SAFETY_SETTINGS
            )
        )
        return (r.text or "").strip()
    except Exception as e:
        print(f"Gemini Extra Error: {e}")
        return ""

def build_head(today, base, h12, h18, h24, greeting="おはようございます。整体院コクリの今日の気圧痛予報です"):
    return (
        f"【仙台｜低気圧頭痛・気圧痛予報】{today.strftime('%m月%d日')}\n"
        f"{greeting}\n\n"
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

    try:
        times, pressures, temps, hums, dews = fetch_weather()
        times_dt = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in times]

        tmap = {}
        for t, p, tmp, h, dw in zip(times_dt, pressures, temps, hums, dews):
            # 【修正2】Open-Meteoの欠損値(None)対策
            if p is None or tmp is None:
                continue
            
            tmap[t] = {
                "pressure": float(p),
                "temp": float(tmp),
                "hum": float(h),
                "dew": float(dw) if dw is not None else 0.0,
            }
        
        if not tmap:
            print("Error: 有効な気象データがありません")
            return

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

        # 画像アップロード
        media_id = None
        try:
            if os.path.exists(BANNER_PATH):
                media = x_api_v1.media_upload(BANNER_PATH)
                media_id = getattr(media, "media_id_string", None) or str(media.media_id)
        except Exception as e:
            print("media_upload ERROR:", repr(e))
            media_id = None

        # 1ツイート目
        try:
            if media_id:
                first = x_client.create_tweet(text=head, media_ids=[media_id], user_auth=True)
            else:
                first = x_client.create_tweet(text=head, user_auth=True)
        except Exception as e:
            print("create_tweet(head) ERROR:", repr(e))
            return

        parent_id = first.data["id"]
        
        # 投稿成功とみなす
        set_last_post_date(today)

        # 本文生成
        body = gemini_body(material)
        if not body:
            # Gemini失敗時のフォールバック
            style = closing_style(total_level)
            tail = "ゆったりとお過ごしください。"
            if style == "安心": tail = "心ほどける時間を。"
            elif style == "軽い注意": tail = "無理せず丁寧に。"
            
            body = (
                f"気圧は{label}、振れ幅{day_range}hPaです。"
                f"気温差{temp_range}℃、露点最大{dew_max}℃。"
                f"{tail}"
            )

        # 分割投稿
        body_parts = split_by_sentence(body, TWEET_LIMIT)

        for p in body_parts:
            try:
                res = x_client.create_tweet(
                    text=p,
                    in_reply_to_tweet_id=parent_id,
                    user_auth=True
                )
                parent_id = res.data["id"]
            except tweepy.errors.Forbidden as e:
                print(f"reply Forbidden(403): Text length: {len(p)} / Error: {e}")
                break
            except Exception as e:
                print("reply ERROR:", repr(e))
                break

        # 追加のひとこと（レベル4以上）
        if total_level >= 4:
            extra = gemini_extra(material)
            if extra:
                try:
                    x_client.create_tweet(
                        text=extra,
                        in_reply_to_tweet_id=parent_id,
                        user_auth=True
                    )
                except Exception as e:
                    print("extra ERROR:", repr(e))

        print("投稿完了")

    except Exception as e:
        print("post_forecast FATAL ERROR:", repr(e))
        return

# =========================
# 常駐
# =========================
def run_bot():
    print("気圧痛予報BOT 起動")
    print("NOW(JST):", now_jst().isoformat())
    print("GEMINI_LIB:", genai.__version__ if hasattr(genai, "__version__") else "Unknown")
    
    if FORCE_POST:
        print("強制投稿モード")
        post_forecast()
        return

    if DEPLOY_RUN:
        if get_last_post_date() != now_jst().date():
            print("再起動時投稿")
            post_forecast()

    while True:
        now = now_jst()
        today = now.date()

        if get_last_post_date() != today and now.hour >= POST_HOUR:
            print(f"定時投稿: {now}")
            post_forecast()

        time.sleep(60)

if __name__ == "__main__":
    run_bot()
