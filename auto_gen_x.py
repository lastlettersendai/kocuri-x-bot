print("プログラムを開始します...")

import warnings
import random
import tweepy
from google import genai

# 警告を非表示にする
warnings.filterwarnings("ignore")

patterns = [
    "1.【常識破壊】「実は◯◯は△△してるだけ」と本質を突く",
    "2.【裏側・真実】業界であまり言われない不都合な真実を暴露",
    "3.【数字×体験談】具体的な変化を数字で示し、信頼を築く",
    "4.【放置リスク】「今やらないと3年後詰む」と未来の危機を伝える",
    "5.【成功者の思考】自律神経が整っている人の判断基準（◎、△、×）",
    "6.【初心者救済】「三日坊主は意志の弱さじゃない」と寄り添う",
    "7.【テンプレ型】「整うための3ステップ」など保存したくなる箇条書き",
    "8.【違和感の言語化】「言葉にできない体の不調」を言語化する",
    "9.【比較・ランキング】疲れのレベル分けや、優先順位の比較",
    "10.【未完・リプ誘導】結論をあえて伏せ、読み手の興味を引く"
]

selected_pattern = random.choice(patterns)
themes = ["夜中に目が覚める理由", "朝から体が重い原因", "イライラが止まらない脳の状態", "呼吸が浅いサイン"]
selected_theme = random.choice(themes)

prompt = f"あなたは仙台の整体院コクリ店主です。型「{selected_pattern}」とテーマ「{selected_theme}」でX投稿を120文字以内で作成してください。「CS60」「自律神経」を入れ、ハッシュタグは禁止です。"

try:
    print(f"AI文章生成中... (型: {selected_pattern})")
    client_gemini = genai.Client(api_key=GEMINI_API_KEY)
    
    # モデル名を 'gemini-1.5-flash' に固定（これが最も確実です）
    response = client_gemini.models.generate_content(
        model='gemini-1.5-flash', 
        contents=prompt
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
