import os
import time
import random
import tweepy
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import warnings

from google import genai
from google.genai import types

warnings.filterwarnings("ignore")

# =========================
# 基本設定（2ツリー固定・ゆる）
# =========================
TWEET_LIMIT = 130
MAX_TWEETS_IN_THREAD = 2
MAX_TOTAL_CHARS = TWEET_LIMIT * MAX_TWEETS_IN_THREAD  # 260

# 1日1回：朝固定（JST）
POST_TIMES = ["06:40"]

# 揺らぎ（±分）
JITTER_MINUTES = 5

# タイムゾーン（ここが最重要）
TZ = ZoneInfo("Asia/Tokyo")

# Gemini
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_TEMP_DRAFT = float(os.getenv("GEMINI_TEMP_DRAFT", "1.2"))
GEMINI_TEMP_POLISH = float(os.getenv("GEMINI_TEMP_POLISH", "0.3"))

# デプロイ即投稿フラグ（Trueでも「1日1回ガード」があるので安全）
DEPLOY_RUN = (os.getenv("DEPLOY_RUN", "0") == "1")

# =========================
# 永続ファイル（Railway/再起動でも守る）
# =========================
HISTORY_PATH = "post_history.json"          # モード交互・視点履歴
DAILY_STATE_PATH = "daily_post_state.json"  # 1日1回ガード

# =========================
# 1日1回ガード（最重要：同日2回を物理的に防止）
# =========================
def load_daily_state():
    if not os.path.exists(DAILY_STATE_PATH):
        return {"last_post_date": None}
    try:
        with open(DAILY_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_post_date": None}

def save_daily_state(st):
    try:
        with open(DAILY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def last_post_date():
    st = load_daily_state()
    v = st.get("last_post_date")
    if not v:
        return None
    try:
        return datetime.fromisoformat(v).date()
    except Exception:
        return None

def mark_posted_today():
    st = load_daily_state()
    st["last_post_date"] = datetime.now(TZ).isoformat(timespec="seconds")
    save_daily_state(st)

# =========================
# 履歴（思想⇄身体交互・視点ローテ）
# =========================
def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {
            "last_mode": "身体",          # 次は思想から始めるなら "身体" を初期に
            "last_viewpoint_思想": -1,
            "last_viewpoint_身体": -1
        }
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            h = json.load(f)
        # 互換性
        h.setdefault("last_mode", "身体")
        h.setdefault("last_viewpoint_思想", -1)
        h.setdefault("last_viewpoint_身体", -1)
        return h
    except Exception:
        return {
            "last_mode": "身体",
            "last_viewpoint_思想": -1,
            "last_viewpoint_身体": -1
        }

def save_history(h):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# 思想⇄身体を交互にする
def next_mode():
    h = load_history()
    last = h.get("last_mode", "身体")
    mode = "思想" if last == "身体" else "身体"
    h["last_mode"] = mode
    h["updated_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    save_history(h)
    return mode

# モードごとに視点を回す（思想は3種、身体は解説中心）
VIEWPOINTS_THOUGHT = ["安心", "反論", "暴露"]
VIEWPOINTS_BODY = ["解説"]  # ここ増やしたければ ["解説","解説2"] みたいにしてOK

def next_viewpoint(mode: str):
    h = load_history()
    if mode == "思想":
        arr = VIEWPOINTS_THOUGHT
        key = "last_viewpoint_思想"
    else:
        arr = VIEWPOINTS_BODY
        key = "last_viewpoint_身体"

    last = int(h.get(key, -1))
    idx = (last + 1) % len(arr)
    vp = arr[idx]
    h[key] = idx
    h["updated_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    save_history(h)
    return vp

# =========================
# 禁止ワード（頻度コントロール）
# =========================
FREQ_WORDS = ["余白", "生存戦略"]

def dynamic_avoid_words():
    """
    80%の確率で抑制（=ほぼ出ないが、たまに出る）
    """
    avoid = []
    for w in FREQ_WORDS:
        if random.random() < 0.8:
            avoid.append(w)
    return avoid

# =========================
# Gemini：下書き（思想/身体モードで分岐）
# =========================
def gemini_draft(gemini_client, mode: str, viewpoint: str) -> str:
    viewpoint_rule = {
        "安心": "安心させる視点。敵ではない/守りの反応。説教せず静かに。",
        "反論": "誤解への反論。性格のせい・根性論をやさしく否定し、身体の反応に戻す。",
        "暴露": "図星を言う。ちゃんとしすぎ/我慢/力みを言語化して、責めずに救う。",
        "解説": "現象解説。首・喉・呼吸・みぞおち等の具体→日常場面→『切り替え』へ。"
    }.get(viewpoint, "やさしく、身体の反応として描く。")

    avoid_words = dynamic_avoid_words()
    avoid_line = f"・次の語は原則使わない（必要なら言い換え）: {'、'.join(avoid_words)}" if avoid_words else ""

    mode_block = ""
    if mode == "思想":
        mode_block = """
【思想モード】
・抽象から入ってOK
・ただし説教しない（断言しない）
・身体の描写は「少し触れる」程度でOK
・“体の反応”という視点に戻して締める
""".strip()
    else:
        mode_block = """
【身体翻訳モード】
・必ず具体部位を1つ以上出す（喉/首/みぞおち/呼吸/背中 など）
・抽象語でまとめすぎない（体の描写→日常場面→安心、の順）
・思想ワードを増やしすぎない（説明は短く）
""".strip()

    prompt = f"""
あなたは「整体院コクリ」院長のナベジュン。
パニック障害と聴覚障害の当事者経験を背景に、
自律神経の不調や過緊張を“身体の反応”として扱う整体師です。

今回は【{mode}】で、X投稿の下書きを1本書いてください。
文章構造は自由。短文を散らしすぎなくてOK。語る感じでもOK。

【今回の視点メモ】
{viewpoint_rule}

{mode_block}

【ナベジュン憲法（必ず守る）】
・症状は敵ではなく、まず守りの反応として扱う
・「治す/完治/必ず」など断言しない（回復の土台を整える）
・強い刺激や押し付けの表現を避け、身体の安全を最優先
・否定しない／焦らせない／押し付けない
・精神論にしない（過緊張＝身体のシステム側の話として描く）
・最後は安心で静かに締める（説教しない）

【条件】
・絵文字/ハッシュタグ/番号（1/2など）禁止
・売り込み禁止（予約/来院/価格/プロフィール誘導など禁止）
・最大{MAX_TOTAL_CHARS}文字以内（短いのはOK）
{avoid_line}

本文のみ出力。
""".strip()

    r = gemini_client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=GEMINI_TEMP_DRAFT)
    )
    return (r.text or "").strip()

# =========================
# Gemini：整える（頻出語をさらに抑制）
# =========================
def gemini_polish(gemini_client, text: str) -> str:
    if not text:
        return text

    avoid_words = dynamic_avoid_words()
    avoid_line = f"・次の語はできるだけ使わない（言い換え優先）: {'、'.join(avoid_words)}" if avoid_words else ""

    prompt = f"""
あなたはX投稿のプロの編集者です。
下書きを自然に整えてください。大きく作り変えず、温度は残してください。

【やること】
・読みやすく整える
・不自然な重複があれば削る（同じ文を2回書かない）
・売り込みを入れない
・絵文字/ハッシュタグ/番号を入れない
{avoid_line}
・最大{MAX_TOTAL_CHARS}文字以内

完成文のみ出力。

【下書き】
{text}
""".strip()

    try:
        r = gemini_client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=GEMINI_TEMP_POLISH)
        )
        out = (r.text or "").strip() or text
        if len(out) > MAX_TOTAL_CHARS:
            out = out[:MAX_TOTAL_CHARS].rstrip()
        return out
    except Exception:
        return text

# =========================
# 連続同一行だけ最小限で潰す（保険）
# =========================
def remove_consecutive_duplicate_lines(text: str) -> str:
    if not text:
        return text
    lines = [l.rstrip() for l in text.split("\n")]
    out = []
    prev = None
    for l in lines:
        if l and prev == l:
            continue
        out.append(l)
        if l:
            prev = l
    return "\n".join(out).strip()

# =========================
# 2ツリー固定の分割（余りmergeなし）
# =========================
def split_into_thread(text: str):
    text = (text or "").strip()
    if not text:
        return []

    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS].rstrip()

    if len(text) <= TWEET_LIMIT:
        return [text]

    window = text[:TWEET_LIMIT]
    cut = -1
    for m in re.finditer(r"[\n。！？!?]", window):
        cut = m.end()

    if cut < 20:
        cut = TWEET_LIMIT

    part1 = text[:cut].strip()
    part2 = text[cut:].strip()
    return [p for p in [part1, part2] if p]

# =========================
# 投稿処理（1日1回ガード込み）
# =========================
def job():
    # ---- 1日1回ガード（最初に判定） ----
    today = datetime.now(TZ).date()
    if last_post_date() == today:
        print("🛑 今日はすでに投稿済みなのでスキップ")
        return

    print(f"--- 投稿開始(JST): {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} ---")

    missing = [k for k in ["API_KEY","API_SECRET","ACCESS_TOKEN","ACCESS_TOKEN_SECRET","GEMINI_API_KEY"] if not os.getenv(k)]
    if missing:
        print(f"環境変数不足: {missing}")
        return

    try:
        gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        # 思想⇄身体を交互
        mode = next_mode()
        viewpoint = next_viewpoint(mode)
        print(f"【今回】mode={mode} / viewpoint={viewpoint}")

        draft = gemini_draft(gemini_client, mode=mode, viewpoint=viewpoint)
        final = gemini_polish(gemini_client, draft)
        final = remove_consecutive_duplicate_lines(final)

        if not final:
            final = "ちゃんとしすぎる人ほど、体が先に止まる。"

        print("【完成文】\n", final)

        parts = split_into_thread(final)
        if not parts:
            print("生成失敗（空）")
            return

        client_x = tweepy.Client(
            consumer_key=os.getenv("API_KEY"),
            consumer_secret=os.getenv("API_SECRET"),
            access_token=os.getenv("ACCESS_TOKEN"),
            access_token_secret=os.getenv("ACCESS_TOKEN_SECRET")
        )

        first = client_x.create_tweet(text=parts[0])
        last_id = first.data["id"]

        for p in parts[1:]:
            resp = client_x.create_tweet(text=p, in_reply_to_tweet_id=last_id)
            last_id = resp.data["id"]

        print(f"✅ 投稿成功！（{len(parts)}ツリー）")

        # ---- 成功したら今日投稿済みにする ----
        mark_posted_today()

    except Exception as e:
        print(f"エラー: {e}")

# =========================
# JST固定：毎日「指定時刻（±揺らぎ）」の実行時刻を作る
# =========================
def parse_hhmm(hhmm: str):
    h, m = map(int, hhmm.split(":"))
    return h, m

def make_jittered_run_times_for_date(day_date):
    runs = []
    for base in POST_TIMES:
        h, m = parse_hhmm(base)
        base_dt = datetime(day_date.year, day_date.month, day_date.day, h, m, tzinfo=TZ)
        offset = random.randint(-JITTER_MINUTES, JITTER_MINUTES)
        run_dt = base_dt + timedelta(minutes=offset)
        runs.append((base, run_dt))
    runs.sort(key=lambda x: x[1])
    return runs

def print_today_schedule(runs):
    s = ", ".join([f"{b}→{dt.strftime('%H:%M')}" for b, dt in runs])
    print(f"📌 本日の投稿時刻（JST/揺らぎ適用）: {s}")

# =========================
# 起動（scheduleを使わない）
# =========================
print(f"JST固定 起動完了（1日{len(POST_TIMES)}回 / 130字×最大2 / 思想⇄身体交互）")
print(f"揺らぎ：±{JITTER_MINUTES}分 / 基準時刻: {POST_TIMES}")
print(f"DEPLOY_RUN: {DEPLOY_RUN}")
print(f"LAST_POST_DATE: {last_post_date()}")

# デプロイ時に即投稿（任意）
# ※ 1日1回ガードがあるので、同日に二重投稿は起きない
if DEPLOY_RUN:
    job()

today = datetime.now(TZ).date()
runs = make_jittered_run_times_for_date(today)
print_today_schedule(runs)

done = set()  # run_dt.isoformat() を入れる（その日の予定枠の実行済み）

while True:
    now = datetime.now(TZ)

    # 日付が変わったら翌日分を作り直す
    if now.date() != today:
        today = now.date()
        runs = make_jittered_run_times_for_date(today)
        done.clear()
        print_today_schedule(runs)

    for base, run_dt in runs:
        key = run_dt.isoformat()
        if key in done:
            continue

        # run_dt〜run_dt+5分の間に拾えればOK
        if run_dt <= now <= (run_dt + timedelta(minutes=5)):
            print(f"⏰ 実行(JST): base={base} / run={run_dt.strftime('%H:%M')} / now={now.strftime('%H:%M:%S')}")
            job()
            done.add(key)

        # 取り逃し救済（ただし job() 内で1日1回ガードが効く）
        elif now > (run_dt + timedelta(minutes=5)):
            print(f"⚠️ 取り逃し救済(JST): base={base} / run={run_dt.strftime('%H:%M')} / now={now.strftime('%H:%M:%S')}")
            job()
            done.add(key)

    time.sleep(20)
