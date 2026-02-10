import os
import warnings
import random
import time
import schedule
import tweepy
from google import genai
from google.genai import types

# 警告を非表示にする
warnings.filterwarnings("ignore")

def job():
    print(f"--- 投稿処理開始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    # Railwayの環境変数から取得
    API_KEY = os.getenv("API_KEY")
    API_SECRET = os.getenv("API_SECRET")
    ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
    ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    # 型のリスト（タイトルを排除し、AIへの指示内容に簡略化しました）
    patterns = [
        "常識を覆す本質的な話",
        "業界のあまり知られていない真実",
        "具体的な変化と数字を交えた話",
        "放置するリスクと未来の話",
        "自律神経が整っている人の習慣",
        "三日坊主を励ます優しい話",
        "心身が整うためのステップ紹介",
        "言葉にできない不調の言語化",
        "不調レベルの比較や気づき",
        "続きが気になる興味深い話"
    ]

    selected_pattern = random.choice(patterns)
    themes = ["夜中に目が覚める理由", "朝から体が重い原因", "イライラが止まらない脳の状態", "呼吸が浅いサイン"]
    selected_theme = random.choice(themes)

    # --- 修正されたプロンプト部分 ---
    prompt = f"""
あなたは仙台の「整体院コクリ」店主です。
「{selected_pattern}」という方向性で、「{selected_theme}」についてのX投稿文を作成してください。

＜絶対ルール＞
1. 冒頭に番号や、「【型】」といったタイトルは絶対に書かないでください。
2. いきなり本題の文章から書き始めてください。
3. 信頼できる先生が優しく語りかけるような、自然な口調にしてください。
4. スマホで読みやすいよう、2〜3行ごとに必ず「空行（改行）」を入れてください。
5. 文字数は120文字以内。
6. 「CS60」「自律神経」のキーワードを必ず含めてください。
7. ハッシュタグは不要です。
"""

    try:
        print(f"AI文章生成中... (方向性: {selected_pattern})")
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # 2026年現役モデル gemini-3-flash-preview を使用
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=1.0
            )
        )
        tweet_text = response.text.strip()

        print(f"【生成内容】\n{tweet_text}")

        print("\nXへ投稿中...")
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

# --- 起動設定 ---
job()

# 毎日 09:30 に定期投稿
schedule.every().day.at("09:30").do(job)

print("2026年版 AI広報部長、待機中...")

while True:
    schedule.run_pending()
    time.sleep(60)
