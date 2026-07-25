"""
notify.py  --  Telegram push helper used by every alert script.
Reads TG_TOKEN + TG_CHAT from env (GitHub Actions secrets). If absent, prints
the message instead of sending (so scripts are safe to run locally).
"""
from __future__ import annotations
import os, json, urllib.request, urllib.parse

def telegram(text: str, token: str | None = None, chat: str | None = None, thread: int | None = None) -> bool:
    token = token or os.environ.get("TG_TOKEN")
    chat  = chat  or os.environ.get("TG_CHAT")
    if not token or not chat:
        print("[notify] no TG creds set; message would be:\n" + "-"*50 + "\n" + text[:1500] + "\n" + "-"*50)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if thread is not None:
        payload["message_thread_id"] = thread     # post into a forum/group Topic
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as r:
            json.load(r)
        return True
    except Exception as e:
        print("[notify] telegram send failed:", e)
        return False

def get_chat_id(token: str) -> None:
    """One-off helper: after you message your new bot, run this to print your chat id."""
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20) as r:
        d = json.load(r)
    for u in d.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        c = m.get("chat", {})
        if c: print("chat_id:", c.get("id"), "| name:", c.get("first_name") or c.get("title"))

def get_topics(token: str) -> None:
    """One-off helper: create a Topic, post any message in it, then run this to
    print each Topic's message_thread_id so you can add it to topics.json."""
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20) as r:
        d = json.load(r)
    found = {}
    for u in d.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        tid = m.get("message_thread_id")
        if tid is None:
            continue
        name = (m.get("forum_topic_created") or {}).get("name")
        if tid not in found or name:
            found[tid] = name or found.get(tid) or "?"
    if not found:
        print("no topic messages in the last ~24h; post in the Topic, then rerun")
    for tid, name in sorted(found.items()):
        print(f"thread_id: {tid} | topic: {name}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "chatid":
        get_chat_id(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "topics":
        get_topics(sys.argv[2])
    else:
        telegram("Test from Mithril alerts. If you see this, Telegram delivery works.")
