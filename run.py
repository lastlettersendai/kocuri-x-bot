import os
import sys
import time
import subprocess

def spawn(cmd):
    return subprocess.Popen(
        cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ.copy(),
    )

p1 = spawn([sys.executable, "auto_gen_x.py"])
p2 = spawn([sys.executable, "sendai_target_search.py"])

while True:
    r1 = p1.poll()
    r2 = p2.poll()
    if r1 is not None or r2 is not None:
        raise SystemExit(f"child exited: auto_gen={r1}, target_search={r2}")
    time.sleep(2)