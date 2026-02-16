import os
import time
import json
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

import requests
import tweepy
from google import genai
from google.genai import types

# =========================
# ログ設定
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# =========================
# 環境変数チェック
# =========================
REQUIRED = ["X_BEARER_TOKEN", "API_KEY", "API_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET", "GEMINI_API_KEY"]
missing = [v for v in REQUIRED if not os.getenv(v)]
if missing:
    logging.error(f"不足環境変数: {missing}")
    raise SystemExit(1)

# =========================
# 基本設定
# =========================
TZ = ZoneInfo("Asia/Tokyo")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATE_PATH = os.path.join(BASE_DIR, "pressure_state.json")
BANNER_PATH = os.path.join(BASE_DIR, "pressurex.jpg")  # 固定画像

POST_HOUR = int(os.getenv("POST_HOUR", "6"))
TWEET_LIMIT = 128  # 返信側の安全マージン（親は短縮リトライで担保）

SENDAI_LAT = 38.2682
SENDAI_LON = 140.8694

OPEN_METEO_TIMEOUT = 15
NEAREST_MAX_DIFF_SEC = 3600  # 1時間以上ズレたデータは信用しない

# =========================
# クライアント初期化（v1.1:画像 / v2:投稿）
# =========================
try:
    auth = tweepy.OAuth1UserHandler(
        os.getenv("API_KEY"), os.getenv("API_SECRET"),
        os.getenv("ACCESS_TOKEN"), os.getenv("ACCESS_TOKEN_SECRET")
    )
    x_api_v1 = tweepy.API(auth)

    x_client = tweepy.Client(
        bearer_token=os.getenv("X_BEARER_TOKEN"),
        consumer_key=os.getenv("API_KEY"),
        consumer_secret=os.getenv("API_SECRET"),
        access_token=os.getenv("ACCESS_TOKEN"),
        access_token_secret=os.getenv("ACCESS_TOKEN_SECRET")
    )

    gen_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
except Exception as e:
    logging.error(f"クライアント初期化失敗: {e}")
    raise SystemExit(1)

# =========================
# 状態管理（attempt/success分離）
# =========================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logging.warning(f"状態ファイル読み込みエラー: {e}")
        return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logging.error(f"状態ファイル書き込みエラー: {e}")
        raise

def mark_attempt(today_str: str) -> None:
    state = load_state()
    state["last_attempt_date"] = today_str
    save_state(state)

def mark_success(today_str: str) -> None:
    state = load_state()
    state["last_success_date"] = today_str
    save_state(state)

def attempted_today(today_str: str) -> bool:
    return load_state().get("last_attempt_date") == today_str

def succeeded_today(today_str: str) -> bool:
    return load_state().get("last_success_date") == today_str

# =========================
# 文字数安全投稿（186厳密 + 画像対応）
# =========================
def is_tweet_too_long(err: tweepy.errors.Forbidden) -> bool:
    # 可能ならレスポンスJSONの code=186 を読む
    try:
        if getattr(err, "response", None) is not None:
            j = err.response.json()
            errors = j.get("errors", [])
            if errors and errors[0].get("code") == 186:
                return True
    except Exception:
        pass
    # フォールバック：文言
    msg = str(err).lower()
    return ("186" in msg) or ("too long" in msg)

def safe_post(text: str, reply_to: Optional[str] = None, media_id: Optional[str] = None) -> str:
    s = (text or "").strip()
    if not s:
        raise ValueError("空テキストは投稿できません")

    for i in range(5):
        try:
            kwargs: Dict[str, Any] = {"text": s, "user_auth": True}
            if reply_to:
                kwargs["in_reply_to_tweet_id"] = reply_to
            if media_id:
                kwargs["media_ids"] = [media_id]

            res = x_client.create_tweet(**kwargs)
            if not res or not res.data or "id" not in res.data:
                raise RuntimeError("create_tweet のレスポンスに id がありません")
            return res.data["id"]

        except tweepy.errors.Forbidden as e:
            if is_tweet_too_long(e):
                logging.warning(f"文字数オーバー。短縮再試行({i+1}/5) len={len(s)}")
                if len(s) <= 10:
                    raise RuntimeError("短縮余地がなく投稿できません") from e
                s = s[:-5]
                continue

            logging.error(f"Forbidden(短縮不可): {e}")
            raise

        except Exception as e:
            logging.error(f"投稿エラー: {e}")
            raise

    raise RuntimeError("文字数調整失敗（リトライ上限）")

# =========================
# 気象取得（キー/長さ厳密）
# =========================
def fetch_weather() -> Optional[Dict[str, Any]]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": SENDAI_LAT,
        "longitude": SENDAI_LON,
        "hourly": ["surface_pressure"],
        "timezone": "Asia/Tokyo",
        "forecast_days": 2,
    }
    try:
        r = requests.get(url, params=params, timeout=OPEN_METEO_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        hourly = data.get("hourly")
        if not hourly:
            raise ValueError("hourly がありません")
        if "time" not in hourly or "surface_pressure" not in hourly:
            raise ValueError("time / surface_pressure がありません")

        times = hourly["time"]
        pressures = hourly["surface_pressure"]
        if not isinstance(times, list) or not isinstance(pressures, list):
            raise ValueError("time/surface_pressure が list ではありません")
        if len(times) != len(pressures):
            raise ValueError(f"長さ不一致: time={len(times)} pressure={len(pressures)}")

        return hourly

    except Exception as e:
        logging.error(f"天気取得失敗: {e}")
        return None

def build_dt_list(times_str: List[str]) -> List[Optional[datetime]]:
    # インデックス整合性を崩さない：失敗は None を入れて長さ維持
    dt_list: List[Optional[datetime]] = []
    for t in times_str:
        try:
            dt = datetime.fromisoformat(t)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            dt_list.append(dt)
        except Exception:
            logging.warning(f"日時変換エラー: {t}")
            dt_list.append(None)
    return dt_list

def get_nearest_index(dt_list: List[Optional[datetime]], target_dt: datetime, max_diff_sec: int = NEAREST_MAX_DIFF_SEC) -> Optional[int]:
    candidates = []
    for i, dt in enumerate(dt_list):
        if dt is None:
            continue
        diff = abs((dt - target_dt).total_seconds())
        candidates.append((diff, i))

    if not candidates:
        logging.error("時刻リストに有効なdatetimeがありません")
        return None

    min_diff, best_i = min(candidates, key=lambda x: x[0])
    if min_diff > max_diff_sec:
        logging.error(f"指定時刻 {target_dt} のデータが見つかりません（最小誤差: {min_diff}秒）")
        return None
    return best_i

# =========================
# 表示ロジック
# =========================
def classify(delta: int) -> int:
    if abs(delta) >= 8:
        return 3
    if abs(delta) >= 5:
        return 2
    if abs(delta) >= 3:
        return 1
    return 0

def color(level: int) -> str:
    return ["🔵", "🟢", "🟡", "🔴"][level]

def label(level: int) -> str:
    return ["安定", "やや変動", "要注意", "警戒"][level]

def headline(level: int) -> str:
    if level == 0:
        return "今日は体が軽い日"
    if level == 1:
        return "今日は少し揺れやすい日"
    if level == 2:
        return "今日は頭が重くなりやすい日"
    return "今日は気圧変動大きめ"

# =========================
# Gemini本文（握りつぶさない）
# =========================
def generate_body(delta: int) -> str:
    prompt = f"""
あなたは仙台の整体師。
今日は気圧が{delta:+d}hPa変化します。

要件:
- 「気圧の上昇/下降で体がどう感じやすいか」を自然な日本語で
- 後頭部/こめかみ/だるさ/眠気 などを織り交ぜる
- 120文字以内
- 医療的断定はしない
- 宣伝はしない
- 出力は本文のみ
"""
    r = gen_client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        contents=prompt.strip(),
        config=types.GenerateContentConfig(temperature=0.7),
    )
    text = (r.text or "").strip()
    if not text:
        raise ValueError("Geminiレスポンスが空です")
    return text

# =========================
# 投稿メインプロセス
# =========================
def post_forecast() -> bool:
    now = datetime.now(TZ)
    today = now.date()
    today_str = str(today)

    hourly = fetch_weather()
    if not hourly:
        return False

    times_str = hourly["time"]
    pressures = hourly["surface_pressure"]
    dt_list = build_dt_list(times_str)

    t06 = datetime.combine(today, dtime(6, 0), tzinfo=TZ)
    t12 = datetime.combine(today, dtime(12, 0), tzinfo=TZ)
    t18 = datetime.combine(today, dtime(18, 0), tzinfo=TZ)
    t24 = datetime.combine(today + timedelta(days=1), dtime(0, 0), tzinfo=TZ)

    i06 = get_nearest_index(dt_list, t06)
    i12 = get_nearest_index(dt_list, t12)
    i18 = get_nearest_index(dt_list, t18)
    i24 = get_nearest_index(dt_list, t24)

    if None in [i06, i12, i18, i24]:
        logging.error("必要な時刻のデータが揃わないため投稿中止")
        return False

    # ✅ 「投稿できる前提が揃った」段階で attempt を刻む（品質UP）
    mark_attempt(today_str)

    base = int(round(pressures[i06]))  # type: ignore[index]
    h12  = int(round(pressures[i12]))  # type: ignore[index]
    h18  = int(round(pressures[i18]))  # type: ignore[index]
    h24  = int(round(pressures[i24]))  # type: ignore[index]

    delta = h24 - base
    lvl = classify(delta)

    head_text = (
        f"【仙台｜低気圧頭痛・天気痛予報】{today.strftime('%m/%d')}\n\n"
        f"{color(lvl)} {label(lvl)}｜{headline(lvl)}\n\n"
        f"朝6時 {base}hPa\n"
        f"→ 夜にかけて {delta:+d}hPa\n\n"
        f"・12時 {h12}hPa({h12-base:+d})\n"
        f"・18時 {h18}hPa({h18-base:+d})\n"
        f"・24時 {h24}hPa({h24-base:+d})"
    )

    # 画像アップロード
    media_id: Optional[str] = None
    if os.path.exists(BANNER_PATH):
        try:
            media = x_api_v1.media_upload(BANNER_PATH)
            # ✅ v2 へ渡すのは string が安全
            media_id = getattr(media, "media_id_string", None) or str(media.media_id)
            logging.info("画像アップロード成功")
        except Exception as e:
            logging.error(f"画像アップロード失敗: {e}")

    # 投稿（失敗は握りつぶさない）
    parent = safe_post(head_text, media_id=media_id)
    body = generate_body(delta)
    safe_post(body, reply_to=parent)

    mark_success(today_str)
    logging.info("=== 投稿完了 ===")
    return True

# =========================
# メインループ
# =========================
def run_bot() -> None:
    logging.info(f"BOT起動 [Single Image Version] (POST_HOUR: {POST_HOUR})")

    while True:
        try:
            now = datetime.now(TZ)
            today_str = str(now.date())

            # 稼働確認ログ（1時間おき）
            if now.minute == 0 and now.second == 0:
                logging.info("BOT稼働中...")

            # 今日すでに試行済みなら二度と打たない（成功/失敗問わず）
            if (not attempted_today(today_str)) and now.hour >= POST_HOUR:
                logging.info(f"投稿判定: {now.isoformat()}")
                ok = post_forecast()
                logging.info(f"結果: {'SUCCESS' if ok else 'SKIP/FAIL'}")

            # ✅ 分境界に同期（処理時間ぶんのズレを吸収）
            now2 = datetime.now(TZ)
            sleep_sec = 60 - now2.second
            if sleep_sec <= 0:
                sleep_sec = 1
            time.sleep(sleep_sec)

        except KeyboardInterrupt:
            logging.info("手動停止")
            break
        except Exception as e:
            # 例外は必ずスタックトレース付きで残す
            logging.exception(f"ループ例外: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
