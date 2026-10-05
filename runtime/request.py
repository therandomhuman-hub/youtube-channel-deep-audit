#!/usr/bin/env python3
from __future__ import annotations
import argparse, re
from urllib.parse import urlparse

SECRET_RE = re.compile(r"(AIza[0-9A-Za-z_-]{20,}|(?:api[_-]?key|key)\s*[=:]\s*[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})", re.I)
HOSTS = {"youtube.com","www.youtube.com","m.youtube.com"}

def extract_youtube_urls(body: str) -> list[str]:
    urls = re.findall(r"https?://[^\s<>()]+", body or "")
    out = []
    for url in urls:
        url = url.rstrip(".,);]}>")
        p = urlparse(url)
        if (p.hostname or "").lower() in HOSTS:
            out.append(url)
    return list(dict.fromkeys(out))

def is_channel_url(url: str) -> bool:
    p = urlparse(url)
    if (p.hostname or "").lower() not in HOSTS or p.scheme not in {"http","https"}:
        return False
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return True
    if parts[0].startswith("@"):
        return len(parts) == 1 or parts[1] in {"videos","shorts","live","playlists","posts","about"}
    if parts[0] == "channel" and len(parts) >= 2 and parts[1].startswith("UC"):
        return True
    if parts[0] == "user" and len(parts) >= 2:
        return True
    return False

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dispatch-url", default="")
    ap.add_argument("--issue-body", default="")
    args = ap.parse_args()

    if SECRET_RE.search(args.dispatch_url) or SECRET_RE.search(args.issue_body):
        raise SystemExit("SECURITY_BLOCK: secret-like material is not accepted in an audit request.")

    candidates = extract_youtube_urls(args.dispatch_url) + extract_youtube_urls(args.issue_body)
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        raise SystemExit("REQUEST_INVALID: exactly one YouTube channel URL must be supplied.")
    channel = candidates[0]
    if not is_channel_url(channel):
        raise SystemExit("REQUEST_INVALID: the target must be a YouTube channel URL or @handle URL, not a video URL.")
    print(channel)

if __name__ == "__main__":
    main()
