"""
YouTube comment collector for a digital ethnography of steel slag (staalslakken)
discussions in the Netherlands.

What it does
------------
1. Finds videos, either from a text file of video URLs/IDs you picked by hand
   (recommended: cheaper on quota and a deliberate ethnographic sample), or by
   a search query.
2. Downloads every public comment AND every reply, keeping the thread structure
   (who replied to whom, in what order), so threads can be read as conversations.
3. Pseudonymises authors: display names are dropped, channel IDs become stable
   codes (U001, U002, ...), and @mentions inside comment text become @USER.
4. Optionally translates every comment to English with DeepL (source language
   auto-detected, so English comments are left intact), keeping the original
   next to it and recording the detected language (NL, EN, ...) per comment.

Output (in --out folder)
------------------------
videos.csv    one row per video (title, channel, date, views, comment count)
comments.csv  one row per comment or reply, in reading order within each thread

Setup
-----
    pip install requests
    export YT_API_KEY="..."        # Google Cloud console -> enable "YouTube Data API v3" -> Credentials -> API key
    export DEEPL_API_KEY="..."     # optional, free tier at deepl.com/pro-api

Usage
-----
    # Easiest: paste your keys below (EASY MODE section) and press Run.
    # It searches YouTube for SEARCH_QUERIES, keeps relevant videos, and
    # scrapes all their comments automatically.

    # Videos you picked yourself (one URL or ID per line, # for comments):
    python youtube_comments.py --videos videos.txt --out data/ --translate

    # Or search (each search costs 100 of your 10,000 daily quota units):
    python youtube_comments.py --query "staalslakken" --max-videos 15 --out data/ --translate

Ethics notes
------------
- Only public comments are collected, through the official API.
- The salt file (.author_salt) links codes to real channel IDs. Keep it private,
  never share it with the data, and delete it once the project is graded.
- Quote paraphrased/translated text in your report, not verbatim Dutch.
"""

import argparse
import csv
import hashlib
import os
import re
import secrets
import sys
import time
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# EASY MODE: paste your keys here and press Run in your editor.
# With no command-line arguments, the script reads videos.txt from the same
# folder as this file and saves results to a "data" folder next to it.
# Do NOT push this file to GitHub with real keys in it.
# --------------------------------------------------------------------------- #
YT_API_KEY = "AIzaSyD4BWcF9TKUx9TIQNifUmCOX3_PzaCLUlY"       # e.g. "AIzaSy..."
DEEPL_API_KEY = "32174441-747e-4e5f-a65a-933a4700f23e:fx"    # e.g. "1234abcd-...:fx"  (leave empty to skip translation)

# What to search YouTube for when you press Run. Each search costs 100 of your
# 10,000 free daily quota units; comments cost about 1 unit per 100 comments.
SEARCH_QUERIES = [
    "staalslakken",
    "Graustabiel",
    "staalslakken Tata Steel",
    "Duomix staalslakken",
]
MAX_VIDEOS_PER_QUERY = 15

# Search results often include unrelated videos. Only videos whose title or
# description contains one of these words are kept. Set to [] to keep all.
KEEP_IF_CONTAINS = ["staalslak", "graustabiel", "duomix", "steel slag", "steelslag"]

# Optional: videos.txt next to this file (one URL per line) is ALWAYS added
# on top of the search results, and is never filtered out.

SCRIPT_DIR = Path(__file__).resolve().parent

YT_BASE = "https://www.googleapis.com/youtube/v3"
DEEPL_URL_FREE = "https://api-free.deepl.com/v2/translate"
DEEPL_URL_PRO = "https://api.deepl.com/v2/translate"

MENTION_RE = re.compile(r"@[\w.\-]+")
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")


# --------------------------------------------------------------------------- #
# YouTube API helpers
# --------------------------------------------------------------------------- #

def yt_get(endpoint, key, **params):
    """GET a YouTube Data API endpoint, retrying briefly on transient errors."""
    params["key"] = key
    for attempt in range(4):
        r = requests.get(f"{YT_BASE}/{endpoint}", params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 403:
            reason = ""
            try:
                reason = r.json()["error"]["errors"][0]["reason"]
            except Exception:
                pass
            if reason == "commentsDisabled":
                return None
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                sys.exit("YouTube quota used up for today. Try again tomorrow (resets at midnight Pacific time).")
            sys.exit(f"YouTube API refused the request ({reason or r.text[:200]}). Check your API key.")
        if r.status_code in (500, 503):
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return None
        sys.exit(f"YouTube API error {r.status_code}: {r.text[:300]}")
    sys.exit("YouTube API kept failing; try again later.")


def parse_video_ids(path):
    ids = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = VIDEO_ID_RE.search(line)
        if m:
            ids.append(m.group(1))
        elif re.fullmatch(r"[A-Za-z0-9_-]{11}", line):
            ids.append(line)
        else:
            print(f"  ! could not read a video ID from: {line}")
    return list(dict.fromkeys(ids))  # dedupe, keep order


def search_videos(query, key, max_videos):
    ids, token = [], None
    while len(ids) < max_videos:
        data = yt_get(
            "search", key, part="id", q=query, type="video",
            maxResults=min(50, max_videos - len(ids)),
            regionCode="NL", relevanceLanguage="nl", order="relevance",
            **({"pageToken": token} if token else {}),
        )
        if not data:
            break
        ids += [it["id"]["videoId"] for it in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return list(dict.fromkeys(ids))[:max_videos]


def video_details(video_ids, key):
    rows = []
    for i in range(0, len(video_ids), 50):
        data = yt_get("videos", key, part="snippet,statistics", id=",".join(video_ids[i:i + 50]))
        for it in (data or {}).get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            rows.append({
                "video_id": it["id"],
                "title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),  # channels are organisations (NOS etc.), not private people
                "published_at": sn.get("publishedAt", ""),
                "view_count": st.get("viewCount", ""),
                "comment_count": st.get("commentCount", ""),
                "url": f"https://www.youtube.com/watch?v={it['id']}",
                "_text": (sn.get("title", "") + " " + sn.get("description", "")).lower(),
            })
    return rows


def all_replies(parent_id, key):
    replies, token = [], None
    while True:
        data = yt_get("comments", key, part="snippet", parentId=parent_id,
                      maxResults=100, textFormat="plainText",
                      **({"pageToken": token} if token else {}))
        if not data:
            break
        replies += data.get("items", [])
        token = data.get("nextPageToken")
        if not token:
            break
    replies.sort(key=lambda c: c["snippet"].get("publishedAt", ""))
    return replies


def video_comments(video_id, key):
    """Yield (top_level_comment, [replies]) for every thread on a video."""
    token = None
    while True:
        data = yt_get("commentThreads", key, part="snippet,replies", videoId=video_id,
                      maxResults=100, textFormat="plainText", order="time",
                      **({"pageToken": token} if token else {}))
        if data is None:
            print(f"  - comments disabled or video unavailable: {video_id}")
            return
        for th in data.get("items", []):
            top = th["snippet"]["topLevelComment"]
            n_replies = th["snippet"].get("totalReplyCount", 0)
            inline = th.get("replies", {}).get("comments", [])
            # The thread endpoint only includes up to 5 replies; fetch the rest.
            replies = all_replies(top["id"], key) if n_replies > len(inline) else \
                sorted(inline, key=lambda c: c["snippet"].get("publishedAt", ""))
            yield top, replies
        token = data.get("nextPageToken")
        if not token:
            return


# --------------------------------------------------------------------------- #
# Pseudonymisation
# --------------------------------------------------------------------------- #

class Pseudonymiser:
    """Maps real channel IDs to stable codes (U001...). Same person -> same code."""

    def __init__(self, salt_path):
        salt_path = Path(salt_path)
        if salt_path.exists():
            self.salt = salt_path.read_text().strip()
        else:
            self.salt = secrets.token_hex(16)
            salt_path.write_text(self.salt)
        self.codes = {}

    def code(self, channel_id, fallback_name=""):
        raw = channel_id or f"name:{fallback_name}"
        h = hashlib.sha256((self.salt + raw).encode()).hexdigest()
        if h not in self.codes:
            self.codes[h] = f"U{len(self.codes) + 1:03d}"
        return self.codes[h]


def clean_text(text):
    return MENTION_RE.sub("@USER", (text or "").replace("\r", " ").strip())


# --------------------------------------------------------------------------- #
# DeepL translation
# --------------------------------------------------------------------------- #

def translate_all(texts, deepl_key, batch=40):
    url = DEEPL_URL_FREE if deepl_key.endswith(":fx") else DEEPL_URL_PRO
    out = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        to_send = [t if t else " " for t in chunk]
        for attempt in range(4):
            r = requests.post(url, headers={"Authorization": f"DeepL-Auth-Key {deepl_key}"},
                              data={"text": to_send, "target_lang": "EN-GB"}, timeout=60)
            if r.status_code == 200:
                out += [(t["text"], t.get("detected_source_language", "")) if src else ("", "")
                        for t, src in zip(r.json()["translations"], chunk)]
                break
            if r.status_code == 456:
                print("  ! DeepL monthly character limit reached; remaining rows left untranslated.")
                return out + [("", "")] * (len(texts) - len(out))
            if r.status_code in (429, 500, 503):
                time.sleep(2 ** attempt)
                continue
            print(f"  ! DeepL error {r.status_code}: {r.text[:200]}; leaving this batch untranslated.")
            out += [("", "")] * len(chunk)
            break
        else:
            out += [("", "")] * len(chunk)
        print(f"  translated {min(i + batch, len(texts))}/{len(texts)}")
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def comment_row(c, video_id, thread_id, parent_id, position, pseudo):
    sn = c["snippet"]
    author_id = (sn.get("authorChannelId") or {}).get("value", "")
    return {
        "video_id": video_id,
        "thread_id": thread_id,
        "comment_id": c["id"],
        "parent_id": parent_id,
        "is_reply": bool(parent_id),
        "position_in_thread": position,
        "author_code": pseudo.code(author_id, sn.get("authorDisplayName", "")),
        "published_at": sn.get("publishedAt", ""),
        "like_count": sn.get("likeCount", 0),
        "reply_count": "",
        "text_nl": clean_text(sn.get("textOriginal") or sn.get("textDisplay")),
        "text_en": "",
        "detected_lang": "",
    }


def main():
    ap = argparse.ArgumentParser(description="Collect pseudonymised YouTube comments for digital ethnography.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--videos", help="text file with one YouTube URL or video ID per line")
    src.add_argument("--query", help="search query, e.g. 'staalslakken'")
    ap.add_argument("--max-videos", type=int, default=10, help="with --query: how many videos (default 10)")
    ap.add_argument("--out", default=None, help="output folder (default: 'data' next to this script)")
    ap.add_argument("--translate", action="store_true", help="add English translations via DeepL")
    args = ap.parse_args()

    yt_key = os.environ.get("YT_API_KEY") or YT_API_KEY.strip()
    deepl_key = os.environ.get("DEEPL_API_KEY") or DEEPL_API_KEY.strip()
    if not yt_key:
        sys.exit("No YouTube key found. Paste it into YT_API_KEY near the top of this file.")

    found_by = {}  # video_id -> list of how it was found

    def add(ids, label):
        for i in ids:
            found_by.setdefault(i, [])
            if label not in found_by[i]:
                found_by[i].append(label)

    easy_mode = not args.videos and not args.query
    if easy_mode:
        # Pressed Run with no arguments: search automatically, plus videos.txt if present.
        args.translate = bool(deepl_key)
        for q in SEARCH_QUERIES:
            hits = search_videos(q, yt_key, MAX_VIDEOS_PER_QUERY)
            print(f"Search '{q}': {len(hits)} videos")
            add(hits, f"search: {q}")
        vt = SCRIPT_DIR / "videos.txt"
        if vt.exists():
            hand = parse_video_ids(vt)
            print(f"videos.txt: {len(hand)} videos")
            add(hand, "videos.txt")
        if not args.translate:
            print("No DeepL key: skipping translation")
    elif args.query:
        add(search_videos(args.query, yt_key, args.max_videos), f"search: {args.query}")
    else:
        add(parse_video_ids(args.videos), "videos.txt")

    if args.out is None:
        args.out = str(SCRIPT_DIR / "data")
    if args.translate and not deepl_key:
        sys.exit("--translate needs a DeepL key. Paste it into DEEPL_API_KEY near the top of this file.")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pseudo = Pseudonymiser(out / ".author_salt")

    if not found_by:
        sys.exit("No videos found.")

    videos = video_details(list(found_by), yt_key)
    kept, dropped = [], []
    for v in videos:
        v["found_by"] = "; ".join(found_by.get(v["video_id"], []))
        v["collected_on"] = time.strftime("%Y-%m-%d")
        hand_picked = "videos.txt" in found_by.get(v["video_id"], [])
        relevant = not KEEP_IF_CONTAINS or any(w.lower() in v["_text"] for w in KEEP_IF_CONTAINS)
        (kept if (hand_picked or relevant) else dropped).append(v)
    for v in kept + dropped:
        del v["_text"]
    for v in dropped:
        print(f"  skipped (no slag keyword in title/description): {v['title'][:70]}")
    videos = kept
    if not videos:
        sys.exit("No relevant videos found. Check SEARCH_QUERIES / KEEP_IF_CONTAINS.")
    print(f"\n{len(videos)} relevant videos")
    rows = []
    for v in videos:
        print(f"- {v['title'][:70]}  ({v['comment_count'] or '?'} comments)")
        for top, replies in video_comments(v["video_id"], yt_key):
            top_row = comment_row(top, v["video_id"], top["id"], "", 0, pseudo)
            top_row["reply_count"] = len(replies)
            rows.append(top_row)
            for n, rep in enumerate(replies, start=1):
                rows.append(comment_row(rep, v["video_id"], top["id"], top["id"], n, pseudo))

    if args.translate and rows:
        print(f"Translating {len(rows)} comments with DeepL...")
        for r, (en, lang) in zip(rows, translate_all([r["text_nl"] for r in rows], deepl_key)):
            r["text_en"] = en
            r["detected_lang"] = lang

    with open(out / "videos.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(videos[0].keys()))
        w.writeheader()
        w.writerows(videos)

    fields = ["video_id", "thread_id", "comment_id", "parent_id", "is_reply", "position_in_thread",
              "author_code", "published_at", "like_count", "reply_count", "text_nl", "text_en", "detected_lang"]
    with open(out / "comments.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"\nDone: {len(videos)} videos, {len(rows)} comments, {len(pseudo.codes)} distinct authors")
    print(f"Saved to {out.resolve()}/videos.csv and comments.csv")
    print("Keep .author_salt private; it is the only link back to real accounts.")


if __name__ == "__main__":
    main()