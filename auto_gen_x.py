import os
import random
import time
import schedule
import tweepy
import json
import re
from datetime import datetime
import requests
from google import genai
from google.genai import types

# =========================
# 設定（思想モード固定）
# =========================
HISTORY_PATH = "post_history.json"
MAX_TRIES = 6
MIN_LEN, MAX_LEN = 220, 260
TWEET_LIMIT = 280  # 思想モードは1投稿完結

# =========================
# 履歴（類似回避）
# =========================
def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"posts": []}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"posts": []}

def save_history(data):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[。、，．・!！?？「」『』（）()\[\]【】]", "", text)
    return text.strip().lower()

def jaccard_similarity(a: str, b: str) -> float:
    def ngrams(s, n=3):
        s = normalize(s)
        return {s[i:i+n] for i in range(max(0, len(s)-n+1))}
    A = ngrams(a)
    B = ngrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)

def is_too_similar(candidate: str, history_posts: list, threshold=0.45) -> bool:
    for past in history_posts[-30:]:
        if jaccard_similarity(candidate, past) >= threshold:
            return True
    return False

# =========================
# Gemini：テーマ生成
# =========================
def generate_theme_from_gemini(gemini_client):
    prompt = """
整体師がXでフォロワーを増やすための
強い共感を生むテーマを40個出してください。

・症状名だけに限定しない
・性格や無意識の癖も含める
・少し苦い
・抽象語OK
・1行1テーマ
・解説不要
"""
    try:
        resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=1.2)
        )
        raw = (resp.text or "").strip()
        lines = [l.strip("・- ") for l in raw.split("\n") if len(l.strip()) > 5]
        return random.choice(lines) if lines else "ちゃんとしすぎる人の体の反応"
    except Exception:
        return "ちゃんとしすぎる人の体の反応"

# =========================
# ChatGPT：思想モード最終仕上げ
# =========================
def openai_final_edit(text: str) -> str:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        return text

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")

    prompt = f"""
あなたはX投稿のプロ編集者です。
下の文章を思想型アカウント向けに仕上げてください。
出力は完成文のみ。

【必須】
・日本語
・220〜260文字
・最初の2行はタイトルのように止める
・余白を残す
・やさしく少しだけ毒を入れる
・説明しすぎない
・ハウツー禁止
・予約導線禁止
・CS60禁止
・自律神経という単語は最大1回
・症状ワードは1〜2個まで
・断言しすぎない（必ず/絶対禁止）
・絵文字/番号/ハッシュタグ禁止

【元文章】
{text}
""".strip()

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"model": model, "input": prompt}

    try:
        r = requests.post("https://api.openai.com/v1/responses",
                          headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if "output_text" in data and isinstance(data["output_text"], str):
            return data["output_text"].strip()

        out = ""
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    out += c.get("text", "")
        return out.strip() if out else text

    except Exception:
        return text

# =========================
# 生成フロー（思想モード）
# =========================
def generate_tweet_text(gemini_client):
    history = load_history()
    history_posts = history.get("posts", [])

    for _ in range(MAX_TRIES):
        selected_theme = generate_theme_from_gemini(gemini_client)

        writer_prompt = f"""
あなたは整体師ナベジュン。
以下のテーマをもとにX投稿を作ってください。

テーマ：
{selected_theme}

【条件】
・220〜260文字
・思想7：症状2：自律神経1の割合
・ハウツー禁止
・売り込み禁止
・自律神経という単語は最大1回
・症状ワードは1〜2個
"""

        draft_resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=writer_prompt,
            config=types.GenerateContentConfig(temperature=1.1)
        )

        draft = (draft_resp.text or "").strip()
        if not draft:
            continue

        final = openai_final_edit(draft)
        if not final:
            continue

        n = len(final.replace("\n", ""))
        if not (MIN_LEN <= n <= MAX_LEN):
            continue

        if is_too_similar(final, history_posts):
            continue

        history_posts.append(final)
        history["posts"] = history_posts[-200:]
        history["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_history(history)

        return final

    return "ちゃんとしすぎる人ほど、体が先に止まる。"

# =========================
# 投稿処理
# =========================
def job():
    print(f"--- 投稿処理開始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

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
        tweet_text = generate_tweet_text(gemini_client)

        print(f"【生成内容】\n{tweet_text}")

        client_x = tweepy.Client(
            consumer_key=API_KEY,
            consumer_secret=API_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET
        )

        client_x.create_tweet(text=tweet_text)
        print("✅ 投稿成功！")

    except Exception as e:
        print(f"エラー発生: {e}")

# =========================
# スケジュール
# =========================
post_times = ["07:30", "12:30", "18:30", "21:30"]

for t in post_times:
    schedule.every().day.at(t).do(job)

print(f"思想モードAI広報 起動完了（1日{len(post_times)}回）")

job()

while True:
    schedule.run_pending()
    time.sleep(60)
