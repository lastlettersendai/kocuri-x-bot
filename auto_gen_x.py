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
# 基本設定（ゆる）
# =========================
TWEET_LIMIT = 130               # 日本語140上限の保険で130
MAX_TWEETS_IN_THREAD = 3        # 最大3ツリー
MAX_TOTAL_CHARS = TWEET_LIMIT * MAX_TWEETS_IN_THREAD  # 390

MAX_TRIES = 6                   # 失敗しても止めない
POST_TIMES = ["07:30", "12:30", "18:30", "21:30"]  # 好きに増減OK

# =========================
# 便利：最低限の重複潰し（ゆる）
# ※「同じ行が連続で2回」だけ消す。文章をいじりすぎない。
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
# Gemini：自由に下書き
# =========================
def gemini_draft(gemini_client) -> str:
    prompt = f"""
あなたは仙台で自律神経やパニック障害の不調をみる整体師。
X投稿の下書きを1本だけ自由に書いてください。

【ゆる条件】
・テーマは自由（思想、症状、日常の気づき、体の反応など）
・断言しすぎない（「絶対」「必ず」は避ける）
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
# ChatGPT：軽く整えるだけ（縛りすぎない）
# =========================
def chatgpt_polish(text: str) -> str:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return text

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")

    prompt = f"""
あなたはX投稿の編集者です。
下書きを「人が書いた」自然な文章に軽く整えてください。
大きく作り変えない。温度は残す。

【やること（最低限）】
・読みやすく整える
・不自然な重複があれば削る（同じ文を2回書かない）
・売り込みは入れない
・絵文字/ハッシュタグ/番号（1/2など）を入れない
・最大{MAX_TOTAL_CHARS}文字以内

【出力】
完成文のみ（説明禁止）

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
# 130字×最大3ツリーに分割（番号は付けない）
# =========================
def split_into_thread(text: str):
    text = (text or "").strip()
    if not text:
        return []

    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS].rstrip()

    parts = []
    remaining = text

    while remaining and len(parts) < MAX_TWEETS_IN_THREAD:
        if len(remaining) <= TWEET_LIMIT:
            parts.append(remaining.strip())
            break

        window = remaining[:TWEET_LIMIT+1]

        # 句点や改行で自然に切る（なければ強制）
        candidates = []
        for m in re.finditer(r"\n", window):
            candidates.append(m.start())
        for m in re.finditer(r"[。！？!?]", window):
            candidates.append(m.end())

        cut = max(candidates) if candidates else TWEET_LIMIT
        if cut < 20:
            cut = TWEET_LIMIT

        part = remaining[:cut].strip()
        remaining = remaining[cut:].strip()

        if part:
            parts.append(part)

    # 余りが出たら最後に少しだけ詰める
    if remaining and parts:
        merged = (parts[-1] + "\n" + remaining).strip()
        parts[-1] = merged[:TWEET_LIMIT].rstrip()

    return [p for p in parts if p.strip()]

# =========================
# 1回の投稿処理
# =========================
def job():
    print(f"--- 投稿開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

    # Railwayの環境変数
    API_KEY = os.getenv("API_KEY")
    API_SECRET = os.getenv("API_SECRET")
    ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
    ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    missing = [k for k in ["API_KEY","API_SECRET","ACCESS_TOKEN","ACCESS_TOKEN_SECRET","GEMINI_API_KEY"] if not os.getenv(k)]
    if missing:
        print(f"環境変数不足: {missing}")
        return

    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)

        # 1) Geminiで自由下書き
        draft = gemini_draft(gemini_client)

        # 2) ChatGPTで軽く整える
        final = chatgpt_polish(draft)

        # 3) 連続重複行だけ最低限潰す（同文2連の事故対策）
        final = remove_consecutive_duplicate_lines(final)

        if not final:
            final = "ちゃんとしすぎる人ほど、体が先に止まる。"

        print("【完成文】\n", final)

        # 4) ツリー分割
        parts = split_into_thread(final)
        if not parts:
            print("生成に失敗しました（空）")
            return

        # 5) 投稿
        client_x = tweepy.Client(
            consumer_key=API_KEY,
            consumer_secret=API_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET
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
# スケジュール
# =========================
for t in POST_TIMES:
    schedule.every().day.at(t).do(job)

print(f"ゆる運用 起動完了（1日{len(POST_TIMES)}回 / 130字×最大3ツリー / 売り込みなし）")

# デプロイ時に1回
job()

while True:
    schedule.run_pending()
    time.sleep(60)
