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

    # 30種類の「型」：専門家としての視点を強化
    patterns = [
        "常識を否定し、隠れた原因を暴く話", "病院で『異常なし』と言われる理由の正体",
        "パニック障害の予期不安を物理現象として語る話", "自律神経を整えるために『まず捨てるべき』習慣",
        "10年後の健康を左右する、今すぐできる細胞への投資", "脳の緊張が解けた瞬間に体がどう変わるかの描写",
        "『根性論』で解決しようとする危うさへの警鐘", "呼吸が浅い人が無意識に損をしていること",
        "多くの人が勘違いしている『本当の休み方』", "『誰に頼るか』で人生の価値が変わるという話",
        "マッサージで解決しない肩こりの裏にある脳の叫び", "『寝ても疲れが取れない』を放置した先にあるリスク",
        "なぜ『気合い』を入れるほど自律神経は乱れるのか", "体に溜まった『電気的なノイズ』を抜く重要性",
        "パニック障害を『性格のせい』にしている人への反論", "一流の経営者ほど『脳の休息』に投資している事実",
        "家族にも理解されない不調の『孤独』に寄り添う話", "『薬を減らしたい』と願う人が最初に見直すべきこと",
        "朝の絶望感を『明日の希望』に変えるステップ", "『休む＝サボる』という呪いを解く本質的な考え方",
        "感覚が鋭すぎる人が生きやすくなるための体の整え方", "冷え症と自律神経の密接すぎる関係について",
        "『なんとなく不調』は体が送っている最後のサイン", "仙台の冷え込みが心身に与えるダメージの回避法",
        "施術後の『驚くほど体が軽い』状態を維持する秘訣", "『いい人』ほど自律神経を壊しやすいという心理的側面",
        "脳の酸欠状態が招く、負の思考ループの止め方", "整体に行ってもすぐ戻る人が見落としている『根本』",
        "パニック障害を克服した先に見える『新しい人生』の話", "ナベジュンが毎日多くの方の体を見ていて確信したこと"
    ]

    # 10種類の「テーマ」：お悩みの解像度をアップ
    themes = [
        "夜中に目が覚める理由", "朝から体が重い原因", "イライラが止まらない脳の状態", 
        "呼吸が浅いサイン", "急な不安感と動悸", "天候や気圧による頭痛", 
        "人混みや電車での息苦しさ", "首から肩にかけての異常な詰まり", 
        "何をしても楽しくない心のガス欠", "手足の冷えと眠りの浅さ"
    ]

    selected_pattern = random.choice(patterns)
    selected_theme = random.choice(themes)

    # --- 専門家としての「強さ」と「共感」を両立させたプロンプト ---
    prompt = f"""
あなたは仙台で「どこに行っても良くならないパニック障害・自律神経の不調」を専門に救う、臨床経験豊富な整体師「ナベジュン」です。

「{selected_pattern}」という型を使い、「{selected_theme}」について、
読者が「自分のことだ！」と震えるような、鋭く本質的なX投稿を作成してください。

＜構成ルール：バズと共感の融合＞
1. 冒頭：1行目に「強力なフック（思わず手が止まる一言）」を置く。
2. 中盤：内容に応じて、以下のいずれかの形式をAIが選択してください。
   - 悩みに当てはまる特徴を「箇条書き」でリストアップする
   - 読者の心に深く潜り込むような「短い2〜3つの文章」で語る
3. 終盤：プロとしての「結論」または「救いの言葉」をズバッと1行で書く。

＜絶対ルール＞
1. 冒頭にタイトルや番号は一切書かない。
2. 挨拶（こんにちは等）は不要。いきなり核心を突く文章から始める。
3. 優しいだけでなく、プロとしての「断言」を交えて信頼感を出す。
4. 2〜3行ごとに必ず「空行」を入れ、スマホで読みやすくする。
5. 文字数は110〜134文字程度。
6. 語尾は「〜ます」「〜です」だけでなく、「〜なはず」「〜ですよね？」と変化をつけたり読んでいて引き込まれるようにしてください。
7. キーワード「自律神経」は自然な時だけでOK。「CS60」は3回に1回程度、解決策として混ぜる。
8. ハッシュタグは不要。
"""

    try:
        print(f"AI文章生成中... (型: {selected_pattern} / テーマ: {selected_theme})")
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents=prompt,
            config=types.GenerateContentConfig(temperature=1.0)
        )
        tweet_text = response.text.strip()

        print(f"【生成内容】\n{tweet_text}")

        client_x = tweepy.Client(
            consumer_key=API_KEY, consumer_secret=API_SECRET,
            access_token=ACCESS_TOKEN, access_token_secret=ACCESS_TOKEN_SECRET
        )
        client_x.create_tweet(text=tweet_text)
        print("✅ 投稿成功！")

    except Exception as e:
        print(f"エラー発生: {e}")

def post_with_delay():
    # 0〜30分のランダムな揺らぎ
    delay_minutes = random.randint(0, 30)
    print(f"--- 予約時刻。{delay_minutes}分後に実行します ---")
    time.sleep(delay_minutes * 60)
    job()

# 1日6回の投稿スケジュール（ナベジュンの野生の勘セット）
post_times = ["07:30", "10:00", "12:30", "15:30", "18:30", "21:30"]

for t in post_times:
    schedule.every().day.at(t).do(post_with_delay)

print(f"整体院コクリ AI広報部長 起動完了（1日{len(post_times)}回投稿＋揺らぎ設定）")

# 動作確認のため起動時に1回実行したい場合は、以下のコメントを外してください
job()

while True:
    schedule.run_pending()
    time.sleep(60)
