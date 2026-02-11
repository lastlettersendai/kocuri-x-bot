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
# 設定（130字×最大3ツリー）
# =========================
HISTORY_PATH = "post_history.json"
MAX_TRIES = 10

TWEET_LIMIT = 130             # 1ポストの安全上限（保険）
MAX_TWEETS_IN_THREAD = 3      # 最大3ツリー
MAX_TOTAL_CHARS = 390         # 130×3

SIM_THRESHOLD = 0.50          # 類似回避（ほどほど）

post_times = ["07:30", "12:30", "18:30", "21:30"]


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

def is_too_similar(candidate: str, history_posts: list, threshold=SIM_THRESHOLD) -> bool:
    for past in history_posts[-30:]:
        if jaccard_similarity(candidate, past) >= threshold:
            return True
    return False


# =========================
# Gemini：テーマ生成（自由にお題出し）
# =========================
def generate_theme_from_gemini(gemini_client):
    prompt = """
整体師がXでフォロワーを増やすための
強い共感を生む「お題」を40個出してください。

・症状名だけに限定しない
・性格、無意識の癖、人間関係、仕事のしんどさも含める
・抽象語OK
・少し苦い
・1行1テーマ
・解説不要
""".strip()

    try:
        resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=1.2)
        )
        raw = (resp.text or "").strip()
        lines = [l.strip("・- \t") for l in raw.split("\n") if len(l.strip()) > 3]
        return random.choice(lines) if lines else "ちゃんとしすぎる人の体の反応"
    except Exception:
        return "ちゃんとしすぎる人の体の反応"


# =========================
# ChatGPT：最終整形（最大390字・売り込みなし）
# =========================
def openai_final_edit(text: str) -> str:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        return text

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")

    prompt = f"""
あなたはX投稿のプロ編集者です。
下の文章を「思想7：症状2：自律神経1」で仕上げてください。
出力は完成文のみ。説明禁止。

【必須】
・日本語
・最初の2行はタイトルのように止める（例：「〜って、たいてい〜」など）
・余白を残す（改行は2〜6回まで）
・やさしく少しだけ毒を入れる
・説明しすぎない（ハウツー禁止）
・売り込み禁止（予約/来院/プロフィール誘導/宣伝/価格など全部なし）
・CS60禁止
・自律神経という単語は最大1回
・症状ワードは1〜2個まで（首/喉/呼吸/動悸/みぞおち等）
・断言しすぎない（必ず/絶対/100%は禁止）
・絵文字/ハッシュタグ/番号（1/2等）禁止
・全体の長さは最大{MAX_TOTAL_CHARS}文字以内（短いのはOK）

【元文章】
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

        # 念のため上限だけ守る（途中で切るのが嫌なら、ここは削ってもOK）
        if len(out) > MAX_TOTAL_CHARS:
            out = out[:MAX_TOTAL_CHARS].rstrip()

        return out

    except Exception:
        return text


# =========================
# 分割：最大3ツリー、1ポスト130字、番号は付けない
# =========================
def split_into_thread(text: str, max_len=TWEET_LIMIT, max_parts=MAX_TWEETS_IN_THREAD):
    text = (text or "").strip()
    if not text:
        return []

    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS].rstrip()

    parts = []
    remaining = text

    while remaining and len(parts) < max_parts:
        if len(remaining) <= max_len:
            parts.append(remaining.strip())
            break

        window = remaining[:max_len+1]

        # なるべく自然に切る（句点/改行優先）
        candidates = []
        for m in re.finditer(r"\n", window):
            candidates.append(m.start())
        for m in re.finditer(r"[。！？!?]", window):
            candidates.append(m.end())

        if candidates:
            cut = max(candidates)
        else:
            cut = max_len

        if cut < 20:
            cut = max_len

        part = remaining[:cut].strip()
        remaining = remaining[cut:].strip()

        if part:
            parts.append(part)

    # まだ残ってるなら最後に詰める（超過分はカット）
    if remaining and parts:
        last = (parts[-1] + "\n" + remaining).strip()
        parts[-1] = last[:max_len].rstrip()

    return [p for p in parts if p.strip()][:max_parts]


# =========================
# 生成フロー：Gemini（お題→下書き）→ChatGPT（整形）
# =========================
def generate_post_text(gemini_client):
    history = load_history()
    history_posts = history.get("posts", [])

    last_candidate = ""

    for _ in range(MAX_TRIES):
        theme = generate_theme_from_gemini(gemini_client)

        writer_prompt = f"""
あなたは整体師ナベジュン。
以下の「お題」から、X投稿の下書きを作ってください。

お題：
{theme}

【条件】
・思想7：症状2：自律神経1
・売り込み禁止／ハウツー禁止
・CS60禁止
・自律神経という単語は最大1回
・症状ワードは1〜2個
・最初の2行はタイトルのように止める
・絵文字/ハッシュタグ/番号（1/2等）禁止
・長さはお任せ（ただし最大{MAX_TOTAL_CHARS}文字以内）
""".strip()

        draft_resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=writer_prompt,
            config=types.GenerateContentConfig(temperature=1.1)
        )
        draft = (draft_resp.text or "").strip()
        if not draft:
            continue

        final = openai_final_edit(draft).strip()
        if not final:
            continue

        if len(final) > MAX_TOTAL_CHARS:
            final = final[:MAX_TOTAL_CHARS].rstrip()

        last_candidate = final

        if is_too_similar(final, history_posts):
            continue

        history_posts.append(final)
        history["posts"] = history_posts[-200:]
        history["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_history(history)

        return final

    # どうしてもダメなら「最後に作れた候補」を返す（毎回固定文にならない）
    if last_candidate:
        return last_candidate

    # それすら無理なら超短い保険（ここに来る確率はかなり低い）
    return "ちゃんとしすぎる人って。\n\nだいたい、止まれない。"


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
        full_text = generate_post_text(gemini_client)

        print(f"【生成内容（全文）】\n{full_text}")

        client_x = tweepy.Client(
            consumer_key=API_KEY,
            consumer_secret=API_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET
        )

        parts = split_into_thread(full_text, max_len=TWEET_LIMIT, max_parts=MAX_TWEETS_IN_THREAD)
        if not parts:
            print("生成失敗（空）")
            return

        first = client_x.create_tweet(text=parts[0])
        last_id = first.data["id"]

        for p in parts[1:]:
            r = client_x.create_tweet(text=p, in_reply_to_tweet_id=last_id)
            last_id = r.data["id"]

        print(f"✅ 投稿成功！（{len(parts)}ツイート / 1ツイ最大{TWEET_LIMIT}字）")

    except Exception as e:
        print(f"エラー発生: {e}")


# =========================
# スケジュール
# =========================
for t in post_times:
    schedule.every().day.at(t).do(job)

print(f"思想モードAI広報 起動完了（1日{len(post_times)}回 / 最大{MAX_TWEETS_IN_THREAD}ツリー / 1ツイ{TWEET_LIMIT}字）")

# 起動時に1回実行
job()

while True:
    schedule.run_pending()
    time.sleep(60)
