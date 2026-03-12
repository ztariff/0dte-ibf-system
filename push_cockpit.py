#!/usr/bin/env python3
"""
push_cockpit.py — Run on your trading machine while cockpit_feed.py is active.

Watches cockpit_state.json for changes and pushes them to GitHub every INTERVAL
seconds. This keeps the GitHub Pages version of the cockpit live (read-only view
from any device at https://ztariff.github.io/0dte-ibf-system/trading_cockpit.html).

Usage:
    python push_cockpit.py

Stop with Ctrl+C.
"""

import subprocess, time, os, datetime, hashlib

REPO      = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(REPO, "cockpit_state.json")
INTERVAL  = 60   # seconds between push attempts

def file_hash(path):
    try:
        return hashlib.md5(open(path, "rb").read()).hexdigest()
    except FileNotFoundError:
        return None

def git(*args):
    return subprocess.run(["git", "-C", REPO] + list(args),
                          capture_output=True, text=True)

def push_state():
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    if not os.path.exists(STATE_FILE):
        print(f"[{ts}] cockpit_state.json not found — is cockpit_feed.py running?")
        return

    git("add", "cockpit_state.json")
    diff = git("diff", "--staged", "--quiet")
    if diff.returncode == 0:
        print(f"[{ts}] No change in cockpit_state.json — skip push")
        return

    commit = git("commit", "-m", f"cockpit: live state {ts}")
    if commit.returncode != 0:
        print(f"[{ts}] Commit failed: {commit.stderr.strip()}")
        return

    push = git("push")
    if push.returncode == 0:
        print(f"[{ts}] ✓ Pushed — GitHub Pages updated")
    else:
        print(f"[{ts}] Push failed: {push.stderr.strip()}")

print("=== PHOENIX Cockpit Auto-Push ===")
print(f"Repo:     {REPO}")
print(f"Interval: {INTERVAL}s")
print(f"Live URL: https://ztariff.github.io/0dte-ibf-system/trading_cockpit.html")
print("Press Ctrl+C to stop.\n")

while True:
    try:
        push_state()
    except Exception as e:
        print(f"Error: {e}")
    time.sleep(INTERVAL)
