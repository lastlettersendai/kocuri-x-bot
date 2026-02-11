import os
import time
import random
from datetime import datetime
import tweepy
from google import genai
from google.genai import types

# --- Railwayの環境変数から取得（プログラムには直接書かない） ---
X_API_KEY = os.getenv("API_KEY")
X_API_SECRET = os.getenv("API_SECRET")
X_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
X_ACCESS_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN") # 検索に必要なので追加
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- 運用ルール ---
TARGET_ACCOUNT = "sendai_tushin"
DAILY_LIMIT = 50
MODEL_NAME = "gemini-3-flash-preview"

# クライアント初期化
gen_client = genai.Client(api_key=GEMINI_API_KEY)
x_client = tweepy.Client(
    bearer_token=X_BEARER_TOKEN,
    consumer_key=X_API_KEY,
    consumer_secret=X_API_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET
)

def ask_gemini_if_target(profile):
    """Gemini 3 Flash にターゲット判定を依頼"""
    prompt = f"""
    以下のXユーザーが「仙台市（太白区・若林区・宮城野区・青葉区）」に住んでおり、
    かつ「自律神経、疲れ、肩こり、頭痛」などの悩みを持っていそうか判定してください。
    【プロフィール】: {profile}
    回答は必ず「YES」か「NO」の1単語だけで答えてください。
    """
    try:
        response = gen_client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt
        )
        return "YES" in response.text.upper()
    except Exception as e:
        print(f"Gemini Error: {e}")
        return False

def run_bot():
    today_likes = 0
    print(f"[{datetime.now()}] 2026年型いいね集客システム始動。目標:{DAILY_LIMIT}件")

    while today_likes < DAILY_LIMIT:
        now_hour = datetime.now().hour
        # 夜間（23時〜7時）はスリープ
        if not (7 <= now_hour < 23):
            print("夜間モード：待機中...")
            time.sleep(1800)
            continue

        try:
            target_user = x_client.get_user(username=TARGET_ACCOUNT)
            tweets = x_client.get_users_tweets(target_user.data.id, max_results=5)
            
            if tweets.data:
                users = x_client.get_retweeters(tweets.data[0].id, user_fields=["description"])
                
                if users.data:
                    for u in users.data:
                        if today_likes >= DAILY_LIMIT: break
                        
                        if ask_gemini_if_target(u.description or ""):
                            try:
                                x_client.like(tweets.data[0].id)
                                today_likes += 1
                                print(f"[{today_likes}] {u.username} さんを判定→いいね完了")
                                # 人間らしい待ち時間（3〜8分）
                                time.sleep(random.randint(180, 480))
                            except: continue

            # コスト節約：30分休憩
            print("巡回完了。30分休憩します。")
            time.sleep(1800)

        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(600)

if __name__ == "__main__":
    run_bot()
