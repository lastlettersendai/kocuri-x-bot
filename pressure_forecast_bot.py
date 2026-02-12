import os
import time
import re
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

# =========================
# 設定
# =========================
TZ = ZoneInfo("Asia/Tokyo")
SENDAI_LAT = 38.2682
SENDAI_LON = 140.8694

POST_HOUR = 6

# 文字数
MAX_TOTAL_LEN = 210
SINGLE_LIMIT = 130  # これ超えたらツリー

# Gemini
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_TEMP = float(os.getenv("GEMINI_TEMP", "0.6"))

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

gen_client = genai.Client(api_key=GEMINI_API_KEY)

# =========================
# Open-Meteo取得
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
# 天気マーク（1日の変化に強く：最悪を採用）
# =========================
def code_to_emoji(code: int) -> str:
    # Snow
    if 71 <= code <= 77:
        return "❄️"
    # Rain
    if 51 <= code <= 67:
        return "☔"
    # Clear
    if code == 0:
        return "☀️"
    # Partly cloudy
    if 1 <= code <= 3:
        return "🌤"
    # Others
    return "🌥"

def emoji_for_day(code12: int, code18: int, code24: int) -> str:
    # 荒れ度の優先順位：雪 > 雨 > くもり系 > 晴れ
    def severity(code: int) -> int:
        if 71 <= code <= 77:
            return 3
        if 51 <= code <= 67:
            return 2
        if 1 <= code <= 3:
            return 1
        if code == 0:
            return 0
        return 1

    codes = [code12, code18, code24]
    worst = max(codes, key=severity)
    return code_to_emoji(worst)

# =========================
# トレンド（簡易）
# =========================
def trend_label(base: int, p12: int, p18: int, p24: int) -> str:
    diffs = [p12 - base, p18 - base, p24 - base]
    worst = min(diffs)
    total = p24 - base

    # 「急降下」より強い言葉は避けたいならこの3段階が安定
    if worst <= -3:
        return "やや不安定"
    if total <= -2:
        return "少し下がる"
    return "安定"

# =========================
# Gemini：本文だけ生成（冒頭固定は触らせない）
# =========================
def gemini_body(material: dict) -> str:
    """
    material には数値などを全部渡す。
    返すのは「本文（朝6時基準の行より下）」だけ。
    """
    prompt = f"""
あなたは整体師の視点で、仙台向け「気圧痛予報」の本文だけを書きます。
次の固定部分（タイトル〜基準気圧）には触れません。繰り返しません。

【必須】
・本文は2〜3文
・湿度の影響コメントを1文に必ず入れる（高湿度=重だるさ/むくみ感、低湿度=喉・呼吸の浅さ/張り詰め感、のように“体感”で）
・怖がらせない／生活指導しない（ストレッチ、水分、入浴などの指示禁止）
・宣伝しない（予約・来院誘導禁止）
・やさしく締める
・「箇条書き」「見出し」「番号」禁止
・本文単体で80文字前後を目安（短めに）

【今日の材料（機械データ）】
傾向: {material["trend"]}
湿度: 12時{material["hum12"]}% / 18時{material["hum18"]}% / 24時{material["hum24"]}%
気温: 12時{material["temp12"]}℃ / 18時{material["temp18"]}℃ / 24時{material["temp24"]}℃
空模様コード: 12時{material["code12"]} / 18時{material["code18"]} / 24時{material["code24"]}

本文のみを出力してください。
""".strip()

    r = gen_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=GEMINI_TEMP)
    )
    return (r.text or "").strip()

# =========================
# 句点優先ツリー分割（130）
# =========================
def split_thread(text: str):
    if len(text) <= SINGLE_LIMIT:
        return [text]

    window = text[:SINGLE_LIMIT]
    cut = -1
    for m in re.finditer(r"[。！？]", window):
        cut = m.end()

    if cut < 60:
        cut = SINGLE_LIMIT

    return [text[:cut].strip(), text[cut:].strip()]

# =========================
# 投稿文生成（固定ヘッダ + Gemini本文）
# =========================
def build_post(material: dict) -> str:
    today_str = datetime.now(TZ).strftime("%m月%d日")

    head = (
        f"【仙台｜低気圧頭痛・気圧痛予報】{today_str}\n"
        f"おはようございます。整体院コクリの今日の気圧痛予報です {material['emoji']}\n\n"
        f"12時{material['h12']}hPa({material['d12']:+d})｜18時{material['h18']}hPa({material['d18']:+d})｜24時{material['h24']}hPa({material['d24']:+d})\n"
        f"朝6時の基準は{material['base']}hPa。\n"
    )

    body = gemini_body(material)

    full = (head + "\n" + body).strip()

    if len(full) > MAX_TOTAL_LEN:
        full = full[:MAX_TOTAL_LEN].rstrip()

    return full

# =========================
# 投稿処理
# =========================
def post_forecast():
    now = datetime.now(TZ)
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

    def get_data(hour: int):
        if hour == 24:
            dt = datetime.combine(today + timedelta(days=1), dtime(0, 0), TZ)
        else:
            dt = datetime.combine(today, dtime(hour, 0), TZ)
        return tmap.get(dt, next(iter(tmap.values())))

    d12 = get_data(12)
    d18 = get_data(18)
    d24 = get_data(24)

    h12 = int(round(d12["pressure"]))
    h18 = int(round(d18["pressure"]))
    h24 = int(round(d24["pressure"]))
    base = int(round(base_p))

    material = {
        "h12": h12,
        "h18": h18,
        "h24": h24,
        "d12": int(round(d12["pressure"] - base_p)),
        "d18": int(round(d18["pressure"] - base_p)),
        "d24": int(round(d24["pressure"] - base_p)),
        "base": base,

        "temp12": int(round(d12["temp"])),
        "temp18": int(round(d18["temp"])),
        "temp24": int(round(d24["temp"])),

        "hum12": int(round(d12["hum"])),
        "hum18": int(round(d18["hum"])),
        "hum24": int(round(d24["hum"])),

        "code12": int(d12["code"]),
        "code18": int(d18["code"]),
        "code24": int(d24["code"]),
    }

    material["emoji"] = emoji_for_day(material["code12"], material["code18"], material["code24"])
    material["trend"] = trend_label(base, h12, h18, h24)

    post_text = build_post(material)
    parts = split_thread(post_text)

    try:
        first = x_client.create_tweet(text=parts[0])
        last_id = first.data["id"]

        if len(parts) > 1:
            x_client.create_tweet(text=parts[1], in_reply_to_tweet_id=last_id)

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
