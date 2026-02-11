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

HISTORY_PATH = "post_history.json"
MAX_TRIES = 10

TWEET_LIMIT = 130
MAX_TWEETS_IN_THREAD = 3
MAX_TOTAL_CHARS = 390

SIM_THRESHOLD = 0.50
post_times = ["07:30", "12:30", "18:30", "21:30"]


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

def is_too_similar(candidate: str, history_posts: list, threshold=SIM_THRESHOLD) -> bool:
    for past in history_posts[-30:]:
        if jaccard_similarity(candidate, past) >= threshold:
            return True
    return False


def generate_theme_from_gemini(gemini_client):
    prompt = """
整体師がXで使える「お題」を40個。
抽象OK。症状でも性格でも仕事でも人間関係でもOK。
1行1テーマ。解説不要。
""".strip()

    try:
        resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=1.2)
        )
        raw = (resp.text or "").strip()
        lines = [l.strip("・- \t") for l in raw.split("\n") if len(l.strip()) > 3]
        return random.choice(lines) if lines else "気を抜けない人の体の反応"
    except Exception:
        return "気を抜けない人の体の反応"


def openai_final_edit(text: str) -> str:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        return text

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")

    prompt = f"""
あなたはX投稿の編集者です。
下書きを自然で読みやすい文章に整えてください。
出力は完成文のみ。説明は禁止。

【必須条件】
・日本語
・最大{MAX_TOTAL_CHARS}文字以内（短いのは可）
・売り込み禁止（予約/来院/価格/プロフィール誘導など）
・CS60禁止
・絵文字/ハッシュタグ/番号（1/2など）禁止
・体の具体を最低1つ入れる（首/喉/呼吸/胸/みぞおち/奥歯など）
・日常の場面を1つ入れる（仕事中/電車/布団など）
・最初の2行は短く止める（タイトル風）

【絶対禁止】
・同じ文を2回書かない
・同じ意味の言い換え反復もしない

【提出前に必ず内部で実施するチェック】
1. 同一文が含まれていないか確認
2. 同じ意味の文章が続いていないか確認
3. もしあれば片方を削除し、自然な流れに修正してから提出する

【下書き】
{text}
""".strip()

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model, "input": prompt}

    try:
        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if "output_text" in data and isinstance(data["output_text"], str):
            out = data["output_text"].strip()
        else:
            out = ""
            for item in data.get("output", []):
                for c in item.get("content", []):
                    if c.get("type") in ("output_text", "text"):
                        out += c.get("text", "")
            out = out.strip() if out else text

        if len(out) > MAX_TOTAL_CHARS:
            out = out[:MAX_TOTAL_CHARS].rstrip()

        return out

    except Exception:
        return text


def split_into_thread(text: str):
    text = (text or "").strip()
    if not text:
        return []

    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS].rstrip()

    parts, remaining = [], text

    while remaining and len(parts) < MAX_TWEETS_IN_THREAD:
        if len(remaining) <= TWEET_LIMIT:
            parts.append(remaining.strip())
            break

        window = remaining[:TWEET_LIMIT+1]
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

    if remaining and parts:
        parts[-1] = (parts[-1] + "\n" + remaining)[:TWEET_LIMIT].rstrip()

    return [p for p in parts if p.strip()]


def generate_post_text(gemini_client):
    history = load_history()
    history_posts = history.get("posts", [])
    last_candidate = ""

    for _ in range(MAX_TRIES):
        theme = generate_theme_from_gemini(gemini_client)

        # Geminiはかなり自由。ただし事故防止だけ入れる
        writer_prompt = f"""
お題：{theme}

X投稿の下書きを自由に作ってください。
文体も長さも自由。最大{MAX_TOTAL_CHARS}文字以内。

禁止：売り込み、予約誘導、CS60、絵文字、ハッシュタグ、番号（1/2など）
条件：体の具体を最低1つ、日常の場面を1つ
""".strip()

        draft_resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=writer_prompt,
            config=types.GenerateContentConfig(temperature=1.2)
        )
        draft = (draft_resp.text or "").strip()
        if not draft:
            continue

        final = openai_final_edit(draft).strip()
        if not final:
            continue

        last_candidate = final

        if is_too_similar(final, history_posts):
            continue

        history_posts.append(final)
        history["posts"] = history_posts[-200:]
        history["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_history(history)

        return final

    return last_candidate if last_candidate else "気を抜けない人ほど、体が先に止まる。"


def job():
    print(f"--- 投稿処理開始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    missing = [k for k in ["API_KEY","API_SECRET","ACCESS_TOKEN","ACCESS_TOKEN_SECRET","GEMINI_API_KEY"] if not os.getenv(k)]
    if missing:
        print(f"環境変数不足: {missing}")
        return

    try:
        gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        full_text = generate_post_text(gemini_client)
        print(f"【生成内容】\n{full_text}")

        client_x = tweepy.Client(
            consumer_key=os.getenv("API_KEY"),
            consumer_secret=os.getenv("API_SECRET"),
            access_token=os.getenv("ACCESS_TOKEN"),
            access_token_secret=os.getenv("ACCESS_TOKEN_SECRET")
        )

        parts = split_into_thread(full_text)
        if not parts:
            print("生成失敗")
            return

        first = client_x.create_tweet(text=parts[0])
        last_id = first.data["id"]

        for p in parts[1:]:
            r = client_x.create_tweet(text=p, in_reply_to_tweet_id=last_id)
            last_id = r.data["id"]

        print(f"✅ 投稿成功！（{len(parts)}ツリー）")

    except Exception as e:
        print(f"エラー発生: {e}")


for t in post_times:
    schedule.every().day.at(t).do(job)

print(f"自由下書き→ChatGPT整形 起動完了（1日{len(post_times)}回 / 最大3ツリー / 1ツイ130字）")

job()

while True:
    schedule.run_pending()
    time.sleep(60)
