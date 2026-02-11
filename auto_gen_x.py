import os
import time
import random
import schedule
import tweepy
import requests
import re
from datetime import datetime
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

POST_TIMES = ["07:30", "12:30", "18:30", "21:30"]

# =========================
# Gemini：ほぼ自由に下書き
# =========================
def gemini_draft(gemini_client) -> str:
    prompt = f"""
あなたは仙台で自律神経やパニック障害の不調をみる整体師。
X投稿の下書きを1本だけ自由に書いてください。

【ゆる条件】
・テーマは自由（思想、症状、日常の気づきなど）
・断言しすぎない（「必ず」「絶対」は避ける）
・売り込み禁止（予約/来院/価格/プロフィール誘導などは書かない）
・絵文字/ハッシュタグ/番号（1/2など）禁止
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

        draft = gemini_draft(gemini_client)
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
# スケジュール設定
# =========================
for t in POST_TIMES:
    schedule.every().day.at(t).do(job)

print(f"2ツリー固定 起動完了（1日{len(POST_TIMES)}回 / 130字×最大2）")

# デプロイ時に1回実行したくない場合は下の1行をコメントアウト
# job()

while True:
    schedule.run_pending()
    time.sleep(60)
