import os
import warnings
import random
import time
import schedule
import tweepy
import json
import re
from datetime import datetime
from google import genai
from google.genai import types

warnings.filterwarnings("ignore")

# =========================
# 設定
# =========================
HISTORY_PATH = "post_history.json"
MAX_TRIES = 8
SIM_THRESHOLD = 0.42

MIN_LEN, MAX_LEN = 220, 260
TWEET_LIMIT = 130

FORBIDDEN = [
    "本質", "人生", "価値", "投資",
    "景色が変わる", "細胞から書き換え",
    "必ず", "絶対", "100%", "確実", "完治", "治る"
]

CS60_LINES = [
    "必要な時はCS60で抜けない重さに逃げ道を作ります。",
    "整体で警戒が落ちた後にCS60を使うこともあります。",
    "どうしても抜けない重さにはCS60を併用します。"
]

RESERVE_LINES = [
    "詳しい流れは固定ポストにまとめました。",
    "長町で静かに整えています。必要な方はどうぞ。",
    "予約はプロフィールのリンクから可能です。",
    "今つらい方は一度ご相談ください。"
]

patterns = [
    "常識を否定し、隠れた原因を暴く話",
    "病院で『異常なし』と言われる理由の正体",
    "呼吸が浅い人が無意識に損をしていること",
    "整体に行ってもすぐ戻る人が見落としていること"
]

themes = [
    "夜中に目が覚める理由",
    "急な不安感と動悸",
    "人混みや電車での息苦しさ",
    "首から肩にかけての詰まり"
]

EDITOR_PERSONAS = [
    "削ぎ落としの名人。抽象を削る。",
    "バズ設計者。1行目を強くする。",
    "臨床現場の編集者。身体具体を太くする。",
    "共感設計者。温度を少し足す。"
]

# =========================
# 履歴管理
# =========================
def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"posts": []}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"posts": []}

def save_history(data):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

def normalize(text):
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[。、，．・!！?？「」『』（）()\[\]【】]", "", text)
    return text.lower().strip()

def jaccard_similarity(a, b):
    def ngrams(s, n=3):
        s = normalize(s)
        return {s[i:i+n] for i in range(max(0, len(s)-n+1))}
    A = ngrams(a)
    B = ngrams(b)
    if not A or not B:
        return 0
    return len(A & B) / len(A | B)

def is_too_similar(text, history_posts):
    for past in history_posts[-30:]:
        if jaccard_similarity(text, past) >= SIM_THRESHOLD:
            return True
    return False

def contains_forbidden(text):
    return any(w in text for w in FORBIDDEN)

def length_ok(text):
    n = len(text.replace("\n",""))
    return MIN_LEN <= n <= MAX_LEN

# =========================
# 2ツイート固定分割
# =========================
def split_into_2_tweets(text):
    if len(text) <= TWEET_LIMIT:
        return [text]

    candidates = []
    for m in re.finditer(r"\n", text):
        candidates.append(m.start())
    for m in re.finditer(r"[。！？!?]", text):
        candidates.append(m.end())

    cut = min(candidates, key=lambda x: abs(x - TWEET_LIMIT)) if candidates else TWEET_LIMIT
    if cut < 30:
        cut = TWEET_LIMIT

    part1 = text[:cut].strip()
    part2 = text[cut:].strip()

    part1 = part1[:TWEET_LIMIT]
    part2 = part2[:TWEET_LIMIT]

    part1 += "\n\n1/2"
    part2 += "\n\n2/2"

    return [part1, part2]

# =========================
# 生成処理
# =========================
def generate_tweet_text(client, selected_pattern, selected_theme):
    history = load_history()
    posts = history.get("posts", [])

    # CS60は25%確率
    include_cs60 = random.random() < 0.25

    # 2本目に予約導線を入れるかもランダム（50%）
    include_reserve = random.random() < 0.5

    for _ in range(MAX_TRIES):

        writer_prompt = f"""
あなたは仙台で自律神経とパニック障害を専門にみる整体師ナベジュン。
X投稿を作成してください。

・{MIN_LEN}〜{MAX_LEN}文字
・身体部位を3つ以上
・日常シーンを1つ
・初回で起きる具体変化を1つ
・抽象語禁止
"""

        draft = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=writer_prompt,
            config=types.GenerateContentConfig(temperature=1.1)
        ).text.strip()

        editor_prompt = f"""
あなたは{random.choice(EDITOR_PERSONAS)}
以下を120点に編集してください。

・抽象削除
・身体具体を太く
・{MIN_LEN}〜{MAX_LEN}文字
・禁止語使用禁止
{'・CS60を自然に1文入れる' if include_cs60 else '・CS60は入れない'}

{draft}
"""

        final = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=editor_prompt,
            config=types.GenerateContentConfig(temperature=0.9)
        ).text.strip()

        if not final:
            continue
        if contains_forbidden(final):
            continue
        if not length_ok(final):
            continue
        if is_too_similar(final, posts):
            continue

        # 予約導線を最後に追加（ランダム）
        if include_reserve:
            final += "\n\n" + random.choice(RESERVE_LINES)

        posts.append(final)
        history["posts"] = posts[-200:]
        history["updated_at"] = datetime.now().isoformat()
        save_history(history)

        return final

    return final

# =========================
# メイン処理
# =========================
def job():
    print(f"--- 投稿処理開始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    API_KEY = os.getenv("API_KEY")
    API_SECRET = os.getenv("API_SECRET")
    ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
    ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    client = genai.Client(api_key=GEMINI_API_KEY)
    selected_pattern = random.choice(patterns)
    selected_theme = random.choice(themes)

    tweet_text = generate_tweet_text(client, selected_pattern, selected_theme)

    print(f"【生成内容】\n{tweet_text}")

    client_x = tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET
    )

    parts = split_into_2_tweets(tweet_text)

    if len(parts) == 1:
        client_x.create_tweet(text=parts[0])
    else:
        first = client_x.create_tweet(text=parts[0])
        first_id = first.data["id"]
        client_x.create_tweet(text=parts[1], in_reply_to_tweet_id=first_id)

    print("✅ 投稿成功！")

def post_with_delay():
    delay_minutes = random.randint(0, 30)
    time.sleep(delay_minutes * 60)
    job()

post_times = ["07:30", "10:00", "12:30", "15:30", "18:30", "21:30"]

for t in post_times:
    schedule.every().day.at(t).do(post_with_delay)

print("整体院コクリ AI広報部長 起動完了")

job()

while True:
    schedule.run_pending()
    time.sleep(60)
