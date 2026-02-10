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

    patterns = [
        "1.【常識破壊】「実は◯◯は△△してるだけ」と本質を突く",
        "2.【裏側・真実】業界であまり言われない不都合な真実を暴露",
        "3.【数字×体験談】具体的な変化を数字で示し、信頼を築く",
        "4.【放置リスク】今やらないと3年後詰む",
        "5.【成功者の思考】自律神経が整っている人の判断基準",
        "6.【初心者救済】「三日坊主は意志の弱さじゃない」"
    ]

    selected_pattern = random.choice(patterns)
    themes = ["夜中に目が覚める理由", "朝から体が重い原因", "イライラが止まらない脳の状態", "呼吸が浅いサイン"]
    selected_theme = random.choice(themes)

    prompt = f"あなたは仙台の整体院コクリ店主です。型「{selected_pattern}」とテーマ「{selected_theme}」でX投稿を120文字以内で作成してください。「CS60」「自律神経」を入れ、ハッシュタグは禁止です。"

    try:
        print(f"AI文章生成中... (最新モデル: gemini-3-flash-preview)")
        # 2026年標準のクライアント初期化
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # 2026年の標準モデル gemini-3-flash-preview を使用
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
        if "404" in str(e):
            print("💡 アドバイス: モデル名が古い可能性があります。リサーチ結果に基づきモデル名を更新してください。")

# --- 起動設定 ---
job()

schedule.every().day.at("09:30").do(job)

print("2026年版 AI広報部長、待機開始...")

while True:
    schedule.run_pending()
    time.sleep(60)
