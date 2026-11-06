"""Long-running demo: reports progress and asks a question on the phone.

Shows the three hub calls a script can make:
    progress(percent, text)   -> visible in /jobs and /status (no notification)
    ask(question, options)    -> buttons on the phone, waits for your tap
    notify(text)              -> a push message
Run it with:  ahub run long-demo steps=10
"""
import argparse
import time

from agenthub.client import ask, notify, progress

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--steps", type=int, default=10)
a = p.parse_args()

for i in range(1, a.steps + 1):
    time.sleep(1)
    progress(100 * i / a.steps, f"step {i}/{a.steps}")
    print(f"step {i}/{a.steps}", flush=True)
    if i == a.steps // 2:
        answer = ask("Halfway there. Continue?", options=["Continue", "Stop"], timeout_s=600, default="Continue")
        print(f"user said: {answer}")
        if answer == "Stop":
            notify("long-demo stopped at your request")
            raise SystemExit(0)
print("all done")
