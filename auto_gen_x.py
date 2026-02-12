import os
import time
import random
import schedule
import tweepy
import requests
import re
import json
from datetime import datetime, timedelta
import warnings

from google import genai
from google.genai import types

warnings.filterwarnings("ignore")

# =========================
# 基本設定（2ツリー固定・ゆる）
# =========================
TWEET_LIMIT = 130
MAX_TWEETS_IN_THREAD = 2
MAX_TOTAL_CHARS = TWEET_LIMIT * MAX_TWEETS_IN_THREAD  # 260

# 固定の基準時刻（ここは変えない）
POST_TIMES = ["12:30", "21:30"]

# 揺らぎ（±分）
JITTER_MINUTES = 7

# 視点ローテーション
VIEWPOINTS = ["安心", "反論", "暴露", "解説"]
HISTORY_PATH = "post_history.json"

# =========================
# 視点履歴
# =========================
def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"last_viewpoint": -1}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_viewpoint": -1}

def save_history(data):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def next_viewpoint():
    h = load_history()
    last = int(h.get("last_viewpoint", -1))
    idx = (last + 1) % len(VIEWPOINTS)
    vp = VIEWPOINTS[idx]
    h["last_viewpoint"] = idx
    h["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_history(h)
    return vp

# =========================
# Gemini：ほぼ自由に下書き（視点だけ指定）
# =========================
def gemini_draft(gemini_client, viewpoint: str) -> str:
    viewpoint_rule = {
        "安心": "安心させる視点。敵ではない/守りの反応/余白。結論は静かに。",
        "反論": "誤解への反論の視点。性格のせい・根性論をやさしく否定し、身体の反応に戻す。",
        "暴露": "図星を言う視点。ちゃんとしすぎ/我慢/力みを言語化して、責めずに救う。",
        "解説": "現象解説の視点。首・喉・呼吸・みぞおち等の具体→日常場面→『切り替え』の話へ。"
    }[viewpoint]

    prompt = f"""
あなたは「整体院コクリ」院長のナベジュン。
パニック障害と聴覚障害の当事者経験を背景に、
自律神経の不調や過緊張を“身体の反応”として扱う整体師です。

今回はの視点で、X投稿の下書きを1本書いてください。
文章構造は自由。短文を散らしすぎなくてOK。語る感じでもOK。

【今回の視点メモ】
{viewpoint_rule}

【ナベジュン憲法（必ず守る）】
・症状は敵ではなく、まず守りの反応として扱う
・「治す/完治/必ず」など断言しない（回復の土台を整える）
・強い刺激や押し付けの表現を避け、身体の安全を最優先
・否定しない／焦らせない／押し付けない
・精神論にしない（過緊張＝身体のシステム側の話として描く）
・最後は安心の余白で静かに締める（説教しない）

【ゆる条件】
・テーマ自由（思想、症状、日常の気づきなど）
・絵文字/ハッシュタグ/番号（1/2など）禁止
・売り込み禁止（予約/来院/価格/プロフィール誘導など禁止）
・最大{MAX_TOTAL_CHARS}文字以内（短いのはOK）
""".strip()

    r = gemini_client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=prompt,
        config=types.GenerateContentConfig(temperature=1.2)
    )
    return (r.text or "").strip()

# =========================
# ChatGPT：軽く整える（作り変えない）
# =========================
def chatgpt_polish(text: str) -> str:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return text

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")

    prompt = f"""
あなたはX投稿の編集者です。
下書きを自然に整えてください。
大きく作り変えず、温度は残す。

【やること】
・読みやすく整える
・不自然な重複があれば削る（同じ文を2回書かない）
・売り込みを入れない
・絵文字/ハッシュタグ/番号を入れない
・最大{MAX_TOTAL_CHARS}文字以内

完成文のみ出力。

【下書き】
{text}
""".strip()

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": prompt}

    try:
        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if isinstance(data.get("output_text"), str):
            out = data["output_text"].strip()
        else:
            out = ""
            for item in data.get("output", []):
                for c in item.get("content", []):
                    if c.get("type") in ("output_text", "text"):
                        out += c.get("text", "")
            out = (out or "").strip() or text

        if len(out) > MAX_TOTAL_CHARS:
            out = out[:MAX_TOTAL_CHARS].rstrip()

        return out

    except Exception:
        return text

# =========================
# 連続同一行だけ最小限で潰す（保険）
# =========================
def remove_consecutive_duplicate_lines(text: str) -> str:
    if not text:
        return text
    lines = [l.rstrip() for l in text.split("\n")]
    out = []
    prev = None
    for l in lines:
        if l and prev == l:
            continue
        out.append(l)
        if l:
            prev = l
    return "\n".join(out).strip()

# =========================
# 2ツリー固定の分割（余りmergeなし）
# =========================
def split_into_thread(text: str):
    text = (text or "").strip()
    if not text:
        return []

    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS].rstrip()

    if len(text) <= TWEET_LIMIT:
        return [text]

    window = text[:TWEET_LIMIT]
    cut = -1
    for m in re.finditer(r"[\n。！？!?]", window):
        cut = m.end()

    if cut < 20:
        cut = TWEET_LIMIT

    part1 = text[:cut].strip()
    part2 = text[cut:].strip()

    return [p for p in [part1, part2] if p]

# =========================
# 投稿処理
# =========================
def job():
    print(f"--- 投稿開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

    missing = [k for k in ["API_KEY","API_SECRET","ACCESS_TOKEN","ACCESS_TOKEN_SECRET","GEMINI_API_KEY"] if not os.getenv(k)]
    if missing:
        print(f"環境変数不足: {missing}")
        return

    try:
        gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        viewpoint = next_viewpoint()
        print(f"【今回の視点】{viewpoint}")

        draft = gemini_draft(gemini_client, viewpoint=viewpoint)
        final = chatgpt_polish(draft)
        final = remove_consecutive_duplicate_lines(final)

        if not final:
            final = "ちゃんとしすぎる人ほど、体が先に止まる。"

        print("【完成文】\n", final)

        parts = split_into_thread(final)
        if not parts:
            print("生成失敗（空）")
            return

        client_x = tweepy.Client(
            consumer_key=os.getenv("API_KEY"),
            consumer_secret=os.getenv("API_SECRET"),
            access_token=os.getenv("ACCESS_TOKEN"),
            access_token_secret=os.getenv("ACCESS_TOKEN_SECRET")
        )

        first = client_x.create_tweet(text=parts[0])
        last_id = first.data["id"]

        for p in parts[1:]:
            resp = client_x.create_tweet(text=p, in_reply_to_tweet_id=last_id)
            last_id = resp.data["id"]

        print(f"✅ 投稿成功！（{len(parts)}ツリー）")

    except Exception as e:
        print(f"エラー: {e}")

# =========================
# 揺らぎスケジュール（毎日作り直す）
# =========================
def jitter_time_str(base_hhmm: str, jitter_minutes: int) -> str:
    """
    base_hhmm (例 '12:30') に対して ±jitter_minutes の範囲でランダムに揺らす。
    返り値は 'HH:MM'。
    """
    h, m = map(int, base_hhmm.split(":"))
    base = datetime(2000, 1, 1, h, m)
    offset = random.randint(-jitter_minutes, jitter_minutes)
    t = base + timedelta(minutes=offset)
    return t.strftime("%H:%M")

def schedule_today_with_jitter():
    """
    当日分の投稿を、基準POST_TIMESからランダムに揺らして登録する。
    schedule.clear('posts')で毎日作り直す前提。
    """
    schedule.clear('posts')
    actual_times = []
    for base in POST_TIMES:
        actual = jitter_time_str(base, JITTER_MINUTES)
        schedule.every().day.at(actual).do(job).tag('posts')
        actual_times.append((base, actual))
    print("📌 本日の投稿時刻（揺らぎ適用）:", ", ".join([f"{b}→{a}" for b, a in actual_times]))

def reschedule_job():
    """
    日付が変わったら当日分の投稿時刻を作り直す。
    00:01に実行。
    """
    schedule_today_with_jitter()

# =========================
# 起動
# =========================
print(f"2ツリー固定×視点ローテ 起動完了（1日{len(POST_TIMES)}回 / 130字×最大2 / 4視点）")
print(f"揺らぎ：±{JITTER_MINUTES}分 / 基準時刻: {POST_TIMES}")

# 当日分を登録
schedule_today_with_jitter()

# 毎日0:01に翌日の揺らぎを作り直す（タグごと作り直し）
schedule.every().day.at("00:01").do(reschedule_job)

# デプロイ時に即投稿したい場合だけ（任意）
if os.getenv("DEPLOY_RUN", "0") == "1":
    job()

while True:
    schedule.run_pending()
    time.sleep(30)
