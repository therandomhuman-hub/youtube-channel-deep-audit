#!/usr/bin/env python3
from __future__ import annotations
import argparse, re
from urllib.parse import urlparse

SECRET_RE=re.compile(r"(AIza[0-9A-Za-z_-]{20,}|(?:api[_-]?key|key)[=:][A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})",re.I)
YOUTUBE_HOSTS={"youtube.com","www.youtube.com","m.youtube.com","youtu.be"}

def extract(body: str) -> list[str]:
    urls=re.findall(r"https?://[^\s<>()]+",body or "")
    return [u.rstrip(".,);]}>") for u in urls if (urlparse(u).hostname or "").lower() in YOUTUBE_HOSTS]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--dispatch-url",default="")
    ap.add_argument("--issue-body",default="")
    a=ap.parse_args()
    candidates=extract(a.dispatch_url)+extract(a.issue_body)
    if SECRET_RE.search(a.dispatch_url) or SECRET_RE.search(a.issue_body):
        raise SystemExit("SECURITY_BLOCK: secret-like material is not accepted in an audit request")
    # Require exactly one YouTube target to eliminate ambiguous/multi-target requests.
    candidates=list(dict.fromkeys(candidates))
    if len(candidates)!=1:
        raise SystemExit("REQUEST_INVALID: provide exactly one YouTube channel URL")
    u=candidates[0]
    p=urlparse(u)
    if p.scheme not in {"http","https"} or (p.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise SystemExit("REQUEST_INVALID: URL is not a YouTube URL")
    print(u)

if __name__=="__main__":
    main()
