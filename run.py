import os
import sys
import time
import subprocess

def spawn(cmd_list):
    # ログをRailwayの画面に流しつつ、環境変数を引き継いで起動
    return subprocess.Popen(
        cmd_list,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ.copy(),
    )

print("--- 整体院コクリ AI広報・営業部 統合システム始動 ---")

# 2つのボットを同時に起動
p1 = spawn([sys.executable, "auto_gen_x.py"])
p2 = spawn([sys.executable, "sendai_target_search.py"])

# どちらかが止まったら異常を検知して終了（Railwayが自動で全体を再起動してくれる）
while True:
    if p1.poll() is not None or p2.poll() is not None:
        print("警告：ボットのいずれかが停止しました。再起動を試みます。")
        sys.exit(1)
    time.sleep(10)
