import os
import warnings
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

warnings.filterwarnings("ignore")

# =========================
# 設定
# =========================
HISTORY_PATH = "post_history.json"
MAX_TRIES = 8
SIM_THRESHOLD = 0.42

# 2ツイート固定前提（130×2に収めるため）
MIN_LEN, MAX_LEN = 220, 260
TWEET_LIMIT = 130

FORBIDDEN = [
    "本質", "人生", "価値", "投資", "救い",
    "景色が変わる", "細胞から書き換え",
    "必ず", "絶対", "100%", "確実", "完治", "治る", "治せる"
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

# 既存資産（選択だけ使う：縛りは弱め）
patterns = [
    "常識を否定し、隠れた原因を暴く話", "病院で『異常なし』と言われる理由の正体",
    "パニック障害の予期不安を物理現象として語る話", "自律神経を整えるために『まず捨てるべき』習慣",
    "『根性論』で解決しようとする危うさへの警鐘", "呼吸が浅い人が無意識に損をしていること",
    "マッサージで解決しない肩こりの裏にある脳の叫び", "『寝ても疲れが取れない』を放置した先にあるリスク",
    "なぜ『気合い』を入れるほど自律神経は乱れるのか", "パニック障害を『性格のせい』にしている人への反論",
    "家族にも理解されない不調の『孤独』に寄り添う話", "『薬を減らしたい』と願う人が最初に見直すべきこと",
    "朝の絶望感を『明日の希望』に変えるステップ", "感覚が鋭すぎる人が生きやすくなるための体の整え方",
    "冷え症と自律神経の密接すぎる関係について", "脳の酸欠状態が招く、負の思考ループの止め方",
    "整体に行ってもすぐ戻る人が見落としている『根本』", "ナベジュンが毎日多くの方の体を見ていて確信したこと"
]

themes = [
    "夜中に目が覚める理由", "朝から体が重い原因", "イライラが止まらない脳の状態",
    "呼吸が浅いサイン", "急な不安感と動悸", "天候や気圧による頭痛",
    "人混みや電車での息苦しさ", "首から肩にかけての異常な詰まり",
    "何をしても楽しくない心のガス欠", "手足の冷えと眠りの浅さ"
]

ANGLE = [
    "症状直前の体のサインを暴く",
    "日常場面から体反応に落とす",
    "誤解を1行で否定して身体理由に着地",
    "患者さんの口癖っぽい言い回しから入る",
    "まず止める癖を1つ提示して体の反応に結びつける"
]

OPENING_STYLE = [
    "1行目は短く刺す（7〜14文字）",
    "1行目は問いかけ（？で終える）",
    "1行目は意外な一言で始める",
    "1行目は断定（ただし『必ず/絶対』は禁止）"
]

ENDING_STYLE = [
    "最後は行動で締める（まず吐く息を長くする等）",
    "最後は安心で締める（体が守ってる反応です等）",
    "最後は専門家の結論で締める（順番は呼吸→首等）",
    "最後は短い救いで締める（戻せます。体から等）"
]

EDITOR_PERSONAS = [
    "削ぎ落としの名人。抽象と重複を徹底的に削る。",
    "バズ設計者。1行目を強化し、リズムを鋭く整える。",
    "臨床現場の編集者。身体具体を太くし、現実味を出す。",
    "共感設計者。感情の温度を少しだけ足して自然にする。",
    "CV志向の編集者。最後を行動につながる一文に整える。"
]

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

def contains_forbidden(text: str) -> bool:
    return any(w in text for w in FORBIDDEN)

def length_ok(text: str) -> bool:
    n = len(text.replace("\n",""))
    return MIN_LEN <= n <= MAX_LEN

# =========================
# 2ツイート固定分割
# =========================
def split_into_2_tweets(text: str, max_len=TWEET_LIMIT):
    text = (text or "").strip()
    if len(text) <= max_len:
        return [text]

    candidates = []
    for m in re.finditer(r"\n", text):
        candidates.append(m.start())
    for m in re.finditer(r"[。！？!?]", text):
        candidates.append(m.end())
    for m in re.finditer(r"\s", text):
        candidates.append(m.start())

    cut = min(candidates, key=lambda x: abs(x - max_len)) if candidates else max_len
    if cut < 30:
        cut = max_len

    part1 = text[:cut].strip()
    part2 = text[cut:].strip()

    if len(part1) > max_len:
        part1 = text[:max_len].strip()
        part2 = text[max_len:].strip()

    if len(part2) > max_len:
        part2 = part2[:max_len].rstrip()

    part1 = f"{part1}\n\n1/2"
    part2 = f"{part2}\n\n2/2"
    return [part1, part2]

# =========================
# OpenAI（ChatGPT）最終チェック＆添削
# =========================
def openai_final_edit(text: str, include_cs60: bool, include_reserve: bool) -> str:
    """
    Responses APIで最終添削。
    推奨：/v1/responses  [oai_citation:1‡OpenAI Platform](https://platform.openai.com/docs/api-reference/responses?utm_source=chatgpt.com)
    """
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        # キーが無いなら素通し（運用を止めない）
        return text

    # ※モデルは用途に応じて調整OK。ここでは最新系の例。
    # GPT-5.2はResponses API推奨  [oai_citation:2‡OpenAI Developers](https://developers.openai.com/api/docs/guides/latest-model/?utm_source=chatgpt.com)
    model = os.getenv("OPENAI_MODEL", "gpt-5.2")

    cs60_rule = "CS60は自然に1文だけ入れる。" if include_cs60 else "CS60は入れない。"
    reserve_rule = "最後に予約導線を1文だけ入れる。" if include_reserve else "予約導線は入れない。"

    prompt = f"""
あなたはX投稿のプロ編集者です。下の文章を「人間の文章」に最終仕上げしてください。
出力は「完成文のみ」。説明禁止。

【必須】
・日本語
・{MIN_LEN}〜{MAX_LEN}文字（改行は2〜4回）
・身体部位を3つ以上必ず入れる
・日常シーンを1つ必ず入れる
・初回で起きやすい具体変化を1つ必ず入れる
・断言しすぎない（必ず/絶対/100%は禁止）
・抽象語は禁止：{", ".join(FORBIDDEN)}
・タイトル/番号/ハッシュタグ/絵文字は禁止
・医療行為の断定禁止（治る保証、診断）

【追加ルール】
・{cs60_rule}
・{reserve_rule}
・同じ意味の重複を削る
・AIっぽい決まり文句（脳のSOS/限界サイン等）を避ける
・1行目を強くする（短く刺す/問い/意外性）

【元文章】
{text}
""".strip()

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "input": prompt
    }

    try:
        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        # Responses APIのtext取り出し（代表的パターン）
        # output_textがある場合はそれを優先
        if "output_text" in data and isinstance(data["output_text"], str):
            out = data["output_text"].strip()
            return out if out else text

        # 互換：output配列を走査
        out_text = ""
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    out_text += c.get("text", "")
        out_text = (out_text or "").strip()
        return out_text if out_text else text

    except Exception:
        return text

# =========================
# Gemini：ライター→編集者（ランダム）
# =========================
def build_writer_prompt(selected_pattern: str, selected_theme: str) -> str:
    angle = random.choice(ANGLE)
    opening = random.choice(OPENING_STYLE)
    ending = random.choice(ENDING_STYLE)

    return f"""
あなたは仙台・長町でパニック障害/自律神経の不調を専門にみる整体師「ナベジュン」。
X投稿を1本だけ作成してください。

【今回の参考（縛りすぎない）】
・ベース型：{selected_pattern}
・テーマ：{selected_theme}

【今回の作り方（必須）】
・角度：{angle}
・冒頭ルール：{opening}
・締め方：{ending}

【必須条件】
・{MIN_LEN}〜{MAX_LEN}文字
・改行は2〜4回
・身体部位を必ず3つ以上（喉/首の前/奥歯/肩/胸/みぞおち/横隔膜/肋骨など）
・日常シーンを必ず1つ（電車/布団/仕事中/人混み/朝など）
・初回で起きやすい具体変化を必ず1つ（息が下に入る/首前が緩む/眠気が来る等）
・抽象語は禁止：{", ".join(FORBIDDEN)}
・タイトル/番号/ハッシュタグ/絵文字は禁止
・医療行為の断定は禁止（治る保証、診断はしない）
・テンプレ表現（脳のSOS/限界サイン等）を避ける
""".strip()

def build_editor_prompt(draft: str, include_cs60: bool) -> str:
    persona = random.choice(EDITOR_PERSONAS)
    cs60_rule = f"・CS60を自然に1文だけ入れる（例：{random.choice(CS60_LINES)}）" if include_cs60 else "・CS60は入れない"

    return f"""
あなたは{persona}
以下のX投稿を120点にブラッシュアップしてください。
最終出力は「完成文のみ」。説明は不要。

【編集ルール】
・抽象と重複を削る
・AIっぽい言い回しを自然にする
・1行目を強くする
・2〜3行ごとに改行してリズム調整
・{MIN_LEN}〜{MAX_LEN}文字に収める
・身体の具体性は必ず残す（身体部位3つ以上）
・日常シーンは必ず残す
・初回の具体変化を必ず残す
・断言しすぎない（必ず/絶対禁止）
・禁止語は使わない：{", ".join(FORBIDDEN)}
{cs60_rule}

【元文章】
{draft}
""".strip()

def generate_tweet_text(gemini_client, selected_pattern, selected_theme):
    history = load_history()
    history_posts = history.get("posts", [])

    # CS60：25%確率
    include_cs60 = (random.random() < 0.25)

    # 予約導線：ランダム（例：50%）
    include_reserve = (random.random() < 0.5)

    last_final = None

    for _ in range(MAX_TRIES):
        # 1) Gemini（ライター）
        writer_prompt = build_writer_prompt(selected_pattern, selected_theme)
        draft_resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=writer_prompt,
            config=types.GenerateContentConfig(temperature=1.1)
        )
        draft = (draft_resp.text or "").strip()

        # 2) Gemini（編集者）
        editor_prompt = build_editor_prompt(draft, include_cs60=include_cs60)
        final_resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=editor_prompt,
            config=types.GenerateContentConfig(temperature=0.9)
        )
        final = (final_resp.text or "").strip()
        last_final = final

        if not final:
            continue
        if contains_forbidden(final):
            continue
        if not length_ok(final):
            continue
        if is_too_similar(final, history_posts):
            continue

        # 3) 予約導線は「文章末尾に付与」（ツリー前提なのでOK）
        if include_reserve:
            final = final.strip() + "\n\n" + random.choice(RESERVE_LINES)

        # 4) ChatGPTで最終添削（完全自動）
        final = openai_final_edit(final, include_cs60=include_cs60, include_reserve=include_reserve)

        # 最終バリデーション（ChatGPT後も一応）
        if contains_forbidden(final):
            continue
        # 予約文付与や最終編集で多少ズレてもツリーで吸収できるが、範囲は維持
        if not length_ok(final):
            continue
        if is_too_similar(final, history_posts):
            continue

        # 履歴保存
        history_posts.append(final)
        history["posts"] = history_posts[-200:]
        history["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_history(history)

        return final

    # どうしても通らない場合：止めずに投稿
    fallback = last_final if last_final else "電車で急に苦しくなる人。\n\n喉・首の前・みぞおちが固まって、吐けてないことが多いです。\n\n初回は息が下に入る感じが出やすい。\nまず吐く息を長く。"
    history_posts.append(fallback)
    history["posts"] = history_posts[-200:]
    history["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_history(history)
    return fallback

# =========================
# メイン処理
# =========================
def job():
    print(f"--- 投稿処理開始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    # Railwayの環境変数
    API_KEY = os.getenv("API_KEY")
    API_SECRET = os.getenv("API_SECRET")
    ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
    ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    missing = [k for k in ["API_KEY","API_SECRET","ACCESS_TOKEN","ACCESS_TOKEN_SECRET","GEMINI_API_KEY"] if not os.getenv(k)]
    if missing:
        print(f"エラー: 環境変数が不足しています: {missing}")
        return

    selected_pattern = random.choice(patterns)
    selected_theme = random.choice(themes)

    try:
        print(f"AI文章生成中... (型: {selected_pattern} / テーマ: {selected_theme})")

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        tweet_text = generate_tweet_text(gemini_client, selected_pattern, selected_theme)

        print(f"【生成内容】\n{tweet_text}")

        client_x = tweepy.Client(
            consumer_key=API_KEY, consumer_secret=API_SECRET,
            access_token=ACCESS_TOKEN, access_token_secret=ACCESS_TOKEN_SECRET
        )

        parts = split_into_2_tweets(tweet_text, max_len=TWEET_LIMIT)

        if len(parts) == 1:
            client_x.create_tweet(text=parts[0])
        else:
            first = client_x.create_tweet(text=parts[0])
            first_id = first.data["id"]
            client_x.create_tweet(text=parts[1], in_reply_to_tweet_id=first_id)

        print("✅ 投稿成功！")

    except Exception as e:
        print(f"エラー発生: {e}")

def post_with_delay():
    delay_minutes = random.randint(0, 30)
    print(f"--- 予約時刻。{delay_minutes}分後に実行します ---")
    time.sleep(delay_minutes * 60)
    job()

# 1日6回投稿
post_times = ["07:30", "10:00", "12:30", "15:30", "18:30", "21:30"]

for t in post_times:
    schedule.every().day.at(t).do(post_with_delay)

print(f"整体院コクリ AI広報部長 起動完了（1日{len(post_times)}回投稿＋揺らぎ設定）")

# 起動時に1回実行（＝デプロイ時に出力する）
job()

while True:
    schedule.run_pending()
    time.sleep(60)
