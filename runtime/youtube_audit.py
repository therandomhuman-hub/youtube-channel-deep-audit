#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_ROOT = "https://www.googleapis.com/youtube/v3"
UA = "YouTubeChannelDeepAudit/11.2-production"

# Google currently documents 1-unit costs for these read methods; search.list has
# a separate 100-calls/day bucket and each call costs 1 unit in that bucket.
METHOD_COSTS = {
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
    "playlists.list": 1,
    "channelSections.list": 1,
    "commentThreads.list": 1,
    "comments.list": 1,
    "search.list": 1,
}
SEARCH_CALL_BUDGET = 100
RETRYABLE = {429, 500, 502, 503, 504}
SECRET_RE = re.compile(
    r"(AIza[0-9A-Za-z_-]{20,}|(?:api[_-]?key|key)[=:][A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})",
    re.I,
)
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_text(value: Any) -> str:
    return SECRET_RE.sub("[REDACTED]", "" if value is None else str(value))


def credential_fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def plausible_key(secret: str) -> bool:
    return len(secret.strip()) >= 20 and not re.search(r"[\x00-\x1f\x7f\s]", secret)


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Checkpoint:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            self.data = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.data["updated_at"] = utc_now()
        atomic_write(self.path, self.data)


class Quota:
    def __init__(self, checkpoint: Checkpoint, run_budget: int) -> None:
        old = checkpoint.get("quota", {})
        self.run_budget = int(old.get("run_budget", run_budget))
        self.used = int(old.get("used", 0))
        self.search_calls = int(old.get("search_calls", 0))
        self.events = list(old.get("events", []))
        self.checkpoint = checkpoint

    def charge(self, method: str, attempt: int) -> bool:
        units = METHOD_COSTS.get(method, 1)
        allowed = (self.used + units <= self.run_budget) and (
            method != "search.list" or self.search_calls < SEARCH_CALL_BUDGET
        )
        self.events.append({
            "at": utc_now(),
            "method": method,
            "units": units,
            "attempt": attempt,
            "status": "AUTHORIZED" if allowed else "BLOCKED",
        })
        if not allowed:
            self.checkpoint.set("quota", self.summary())
            return False
        self.used += units
        if method == "search.list":
            self.search_calls += 1
        self.checkpoint.set("quota", self.summary())
        return True

    def summary(self) -> dict[str, Any]:
        return {
            "run_budget": self.run_budget,
            "used": self.used,
            "remaining": max(0, self.run_budget - self.used),
            "search_calls": self.search_calls,
            "search_call_budget": SEARCH_CALL_BUDGET,
            "event_count": len(self.events),
        }


class API:
    def __init__(self, key: str, quota: Quota, retries: int = 2, timeout: int = 30) -> None:
        if not plausible_key(key):
            raise ValueError("API key missing or syntactically invalid")
        self._key = key
        self.quota = quota
        self.retries = max(0, retries)
        self.timeout = timeout
        self.request_count = 0

    def get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in METHOD_COSTS:
            raise ValueError(f"Unsupported YouTube method: {method}")
        endpoint = method.split(".", 1)[0]
        last_error: dict[str, Any] = {}
        for attempt in range(1, self.retries + 2):
            if not self.quota.charge(method, attempt):
                return {"ok": False, "status": "QUOTA_BUDGET_BLOCKED"}
            q = {k: v for k, v in params.items() if v not in (None, "")}
            q["key"] = self._key
            url = f"{API_ROOT}/{endpoint}?{urlencode(q, doseq=True)}"
            self.request_count += 1
            try:
                req = Request(url, headers={"Accept": "application/json", "User-Agent": UA})
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return {"ok": True, "status": "SUCCESS", "data": data, "http_status": getattr(resp, "status", 200), "captured_at": utc_now()}
            except HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                reason = None
                message = None
                try:
                    err = (json.loads(raw).get("error") or {})
                    errors = err.get("errors") or []
                    reason = (errors[0].get("reason") if errors else None) or err.get("status")
                    message = err.get("message")
                except Exception:
                    pass
                last_error = {"http_status": exc.code, "reason": safe_text(reason), "message": safe_text(message)}
                if exc.code in RETRYABLE and attempt <= self.retries:
                    time.sleep(1.5 ** (attempt - 1))
                    continue
                return {"ok": False, "status": classify_http(exc.code, reason), "error": last_error, "captured_at": utc_now()}
            except (URLError, TimeoutError, OSError) as exc:
                last_error = {"type": type(exc).__name__, "message": safe_text(exc)}
                if attempt <= self.retries:
                    time.sleep(1.5 ** (attempt - 1))
                    continue
                return {"ok": False, "status": "NETWORK_ERROR", "error": last_error, "captured_at": utc_now()}
        return {"ok": False, "status": "API_ERROR", "error": last_error}


def classify_http(code: int, reason: str | None) -> str:
    r = (reason or "").lower()
    if code == 400 and "key" in r:
        return "API_KEY_INVALID"
    if code == 401:
        return "AUTHENTICATION_REQUIRED"
    if code == 403 and any(x in r for x in ("quota", "dailylimit", "ratelimit")):
        return "QUOTA_EXHAUSTED"
    if code == 403 and "comment" in r and "disabled" in r:
        return "COMMENTS_DISABLED"
    if code == 404:
        return "NOT_FOUND"
    if code == 429:
        return "RATE_LIMITED"
    if 500 <= code < 600:
        return "UPSTREAM_ERROR"
    return "HTTP_ERROR"


def extract_channel_ref(value: str) -> tuple[str, str]:
    value = value.strip()
    p = urlparse(value)
    if p.scheme and p.netloc:
        host = (p.hostname or "").lower()
        if host not in YT_HOSTS:
            raise ValueError("Only YouTube channel URLs are accepted")
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2 and parts[0] == "channel" and parts[1].startswith("UC"):
            return "id", parts[1]
        if parts and parts[0].startswith("@"):
            return "handle", parts[0]
        if len(parts) >= 2 and parts[0] == "user":
            return "username", parts[1]
        return "web", value
    if value.startswith("UC"):
        return "id", value
    if value.startswith("@"):
        return "handle", value
    return "name", value


def web_resolve(url: str) -> tuple[str | None, dict[str, Any]]:
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            canonical = None
            cm = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', body, re.I)
            if cm:
                canonical = cm.group(1)
            for pattern in [
                r'"channelId":"(UC[a-zA-Z0-9_-]{22})"',
                r'"externalId":"(UC[a-zA-Z0-9_-]{22})"',
                r'https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})',
                r'https://youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})',
            ]:
                m = re.search(pattern, body)
                if m:
                    return m.group(1), {"status": "SUCCESS", "http_status": resp.status, "canonical_url": canonical}
            return None, {"status": "CHANNEL_ID_NOT_FOUND", "http_status": resp.status, "canonical_url": canonical}
    except Exception as exc:
        return None, {"status": "WEB_ERROR", "error": safe_text(exc)}


def resolve_channel(api: API, supplied: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    mode, ref = extract_channel_ref(supplied)
    parts = "id,snippet,contentDetails,statistics,brandingSettings,topicDetails,status,localizations"
    if mode == "web":
        cid, meta = web_resolve(ref)
        if not cid:
            return None, {"status": "IDENTITY_UNRESOLVED", "mode": mode, "web_resolution": meta}
        res = api.get("channels.list", {"part": parts, "id": cid})
    elif mode == "name":
        res = api.get("search.list", {"part": "snippet", "q": ref, "type": "channel", "maxResults": 5})
        if not res.get("ok"):
            return None, {"status": res.get("status"), "mode": mode}
        candidates = (res.get("data") or {}).get("items", [])
        if len(candidates) != 1:
            return None, {"status": "AMBIGUOUS_IDENTITY", "mode": mode, "candidates": candidates}
        cid = candidates[0].get("snippet", {}).get("channelId") or candidates[0].get("id", {}).get("channelId")
        res = api.get("channels.list", {"part": parts, "id": cid})
    else:
        params = {"part": parts}
        if mode == "id":
            params["id"] = ref
        elif mode == "handle":
            params["forHandle"] = ref
        elif mode == "username":
            params["forUsername"] = ref
        res = api.get("channels.list", params)
    if not res.get("ok"):
        return None, {"status": res.get("status"), "mode": mode, "api": {"error": res.get("error")}}
    items = (res.get("data") or {}).get("items", [])
    if len(items) != 1:
        return None, {"status": "CHANNEL_NOT_FOUND", "mode": mode, "item_count": len(items)}
    return items[0], {"status": "SUCCESS", "mode": mode}


def paginate(api: API, checkpoint: Checkpoint, phase: str, method: str, params: dict[str, Any], max_pages: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if checkpoint.get(f"{phase}.complete"):
        cached = checkpoint.get(f"{phase}.items", [])
        return cached, {"complete": True, "pages": 0, "resumed": True, "item_count": len(cached), "stop_reason": "CHECKPOINT_COMPLETE"}
    items = list(checkpoint.get(f"{phase}.items", []))
    seen = {x.get("id") for x in items if isinstance(x, dict) and x.get("id")}
    token = checkpoint.get(f"{phase}.nextPageToken") or None
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            return items, {"complete": False, "pages": pages, "resumed": bool(token), "item_count": len(items), "stop_reason": "MAX_PAGES"}
        q = dict(params)
        if token:
            q["pageToken"] = token
        res = api.get(method, q)
        pages += 1
        if not res.get("ok"):
            checkpoint.set(f"{phase}.nextPageToken", token or "")
            return items, {"complete": False, "pages": pages, "resumed": bool(token), "item_count": len(items), "stop_reason": res.get("status")}
        data = res.get("data") or {}
        for item in data.get("items", []):
            ident = item.get("id") if isinstance(item, dict) else None
            if ident and ident in seen:
                continue
            if ident:
                seen.add(ident)
            items.append(item)
        token = data.get("nextPageToken")
        checkpoint.set(f"{phase}.items", items)
        checkpoint.set(f"{phase}.nextPageToken", token or "")
        if not token:
            checkpoint.set(f"{phase}.complete", True)
            return items, {"complete": True, "pages": pages, "resumed": bool(checkpoint.get(f"{phase}.items")) and pages > 1, "item_count": len(items), "stop_reason": "EXHAUSTED"}


def iso_duration_seconds(value: str) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    h, mi, s = [int(x or 0) for x in m.groups()]
    return h * 3600 + mi * 60 + s


def classify_video(video: dict[str, Any], shorts_ids: set[str]) -> tuple[str, str]:
    vid = video.get("id")
    if vid in shorts_ids:
        return "SHORT", "PUBLIC_SHORTS_SURFACE"
    if video.get("liveStreamingDetails"):
        return "LIVE", "FIRST_PARTY_LIVE_METADATA"
    title = (video.get("snippet", {}).get("title") or "").lower()
    if "premiere" in title:
        return "PREMIERE", "TITLE_HEURISTIC"
    return "LONG_FORM", "DEFAULT_VIDEO_FORMAT"


def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>()]+", text or "")


def resource_hints(description: str) -> list[dict[str, str]]:
    out = []
    for url in extract_urls(description):
        low = url.lower()
        category = "UNCLASSIFIED"
        if any(x in low for x in ("affiliate", "amzn", "amazon", "bit.ly", "linktr.ee", "kit.co")):
            category = "PROMOTIONAL_OR_AFFILIATE"
        elif any(x in low for x in ("course", "academy", "gumroad", "stan.store", "shop")):
            category = "PRODUCT_OR_STORE"
        elif any(x in low for x in ("discord", "telegram", "newsletter", "substack", "community")):
            category = "COMMUNITY_OR_AUDIENCE"
        out.append({"url": url[:1000], "category": category})
    return out


def keyword_hints(text: str) -> list[str]:
    lower = (text or "").lower()
    terms = ["sponsor","affiliate","course","newsletter","discord","community","free guide","link in description","subscribe","join","gumroad","store","download","discount","promo","use my code"]
    return [x for x in terms if x in lower]


def performance_metrics(videos: list[dict[str, Any]]) -> dict[str, Any]:
    values = []
    for v in videos:
        try:
            values.append(int(v.get("statistics", {}).get("viewCount")))
        except Exception:
            pass
    median = statistics.median(values) if values else None
    mean = statistics.mean(values) if values else None
    threshold = median * 5 if median else None
    outliers = [v for v in videos if threshold and int(v.get("statistics", {}).get("viewCount", 0) or 0) >= threshold]
    return {
        "video_count": len(videos),
        "views_observed_n": len(values),
        "median_views": median,
        "mean_views": mean,
        "outlier_threshold_5x_median": threshold,
        "outlier_count": len(outliers),
        "outlier_ratio": (len(outliers) / len(videos)) if videos else None,
        "top_videos": sorted(
            [{"id":v.get("id"),"title":v.get("snippet",{}).get("title"),"views":int(v.get("statistics",{}).get("viewCount",0) or 0)} for v in videos],
            key=lambda x:x["views"], reverse=True
        )[:10],
    }


def comment_keyword_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    blob = " ".join(str(x.get("text","")) for x in rows).lower()
    terms = ["great","helpful","thanks","scam","works","doesn't work","expensive","link","tutorial","ai","youtube"]
    return {term: blob.count(term) for term in terms}


def browser_collect(channel_url: str, video_ids: list[str], transcript_limit: int, max_scrolls: int) -> dict[str, Any]:
    result = {"status":"PLAYWRIGHT_UNAVAILABLE","shorts":[],"posts":[],"transcripts":[],"surface_checks":[],"limitations":[]}
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        result["limitations"].append("Playwright is unavailable.")
        return result
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width":1440,"height":1000})
            page.set_default_timeout(30000)
            base = channel_url.rstrip("/")
            for suffix in ["/","/videos","/shorts","/live","/playlists","/posts"]:
                try:
                    page.goto(base+suffix, wait_until="domcontentloaded")
                    result["surface_checks"].append({"surface":suffix,"status":"OK","captured_at":utc_now()})
                except Exception as exc:
                    result["surface_checks"].append({"surface":suffix,"status":"UNAVAILABLE","error":safe_text(exc)[:500],"captured_at":utc_now()})
            try:
                page.goto(base+"/shorts", wait_until="domcontentloaded")
                progressive_scroll(page,max_scrolls)
                hrefs=page.locator('a[href*="/shorts/"]').evaluate_all("els => els.map(e => e.href).filter(Boolean)")
                seen=set()
                for pos, href in enumerate(hrefs,1):
                    m=re.search(r"/shorts/([A-Za-z0-9_-]{6,})",href)
                    if m and m.group(1) not in seen:
                        seen.add(m.group(1)); result["shorts"].append({"video_id":m.group(1),"position":pos,"captured_at":utc_now()})
            except Exception as exc:
                result["limitations"].append("Shorts: "+safe_text(exc)[:500])
            try:
                page.goto(base+"/posts", wait_until="domcontentloaded")
                progressive_scroll(page,max_scrolls)
                anchors=page.locator('a[href*="/post/"]').evaluate_all("els => els.map(e => ({href:e.href,text:(e.innerText||e.textContent||"").trim()}))")
                seen=set()
                for a in anchors:
                    href=a.get("href","")
                    m=re.search(r"/post/([^?#/]+)",href)
                    if not m or m.group(1) in seen:
                        continue
                    seen.add(m.group(1))
                    preview=a.get("text","")[:1200]
                    try:
                        loc=page.locator(f'a[href*="/post/{m.group(1)}"]').first
                        card=loc.locator("xpath=ancestor::*[self::ytd-rich-item-renderer or self::ytd-backstage-post-thread-renderer][1]")
                        preview=re.sub(r"\s+"," ",card.inner_text(timeout=3000)).strip()[:1200]
                    except Exception:
                        pass
                    result["posts"].append({"post_id":m.group(1),"url":href,"text_preview":preview,"captured_at":utc_now()})
            except Exception as exc:
                result["limitations"].append("Posts: "+safe_text(exc)[:500])
            for vid in video_ids[:max(0,transcript_limit)]:
                item={"video_id":vid,"status":"UNKNOWN","captured_at":utc_now(),"text_retained":False}
                try:
                    page.goto(f"https://www.youtube.com/watch?v={vid}",wait_until="domcontentloaded")
                    buttons=page.locator('button[aria-label*="transcript" i], tp-yt-paper-button[aria-label*="transcript" i]')
                    if buttons.count()==0:
                        item["status"]="NOT_DETECTED"
                    else:
                        buttons.first.click(); page.wait_for_timeout(750)
                        n=page.locator("ytd-transcript-segment-renderer").count()
                        item["status"]="PUBLIC_TRANSCRIPT_AVAILABLE" if n else "TRANSCRIPT_PANEL_NO_SEGMENTS"
                        item["segment_count"]=n
                except Exception as exc:
                    item["status"]="UNAVAILABLE"; item["error"]=safe_text(exc)[:500]
                result["transcripts"].append(item)
            browser.close()
        result["status"]="SUCCESS"
        if transcript_limit < len(video_ids):
            result["limitations"].append(f"Transcript availability checked for {transcript_limit} of {len(video_ids)} videos.")
    except Exception as exc:
        result["status"]="PLAYWRIGHT_ERROR"; result["limitations"].append(safe_text(exc)[:500])
    return result


def progressive_scroll(page: Any, max_scrolls: int) -> None:
    previous=-1; stable=0
    for _ in range(max_scrolls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(700)
        height=page.evaluate("document.body.scrollHeight")
        if height==previous:
            stable+=1
            if stable>=4: break
        else:
            stable=0
        previous=height


def source_register() -> list[dict[str,str]]:
    urls={
        "SRC-YT-CHANNELS":"https://developers.google.com/youtube/v3/docs/channels",
        "SRC-YT-PLAYLISTITEMS":"https://developers.google.com/youtube/v3/docs/playlistItems/list",
        "SRC-YT-VIDEOS":"https://developers.google.com/youtube/v3/docs/videos/list",
        "SRC-YT-PLAYLISTS":"https://developers.google.com/youtube/v3/docs/playlists/list",
        "SRC-YT-SECTIONS":"https://developers.google.com/youtube/v3/docs/channelSections/list",
        "SRC-YT-COMMENTTHREADS":"https://developers.google.com/youtube/v3/docs/commentThreads/list",
        "SRC-YT-COMMENTS":"https://developers.google.com/youtube/v3/docs/comments/list",
        "SRC-YT-CAPTIONS":"https://developers.google.com/youtube/v3/docs/captions/list",
        "SRC-YT-ANALYTICS":"https://developers.google.com/youtube/analytics/reference",
        "SRC-YT-QUOTA":"https://developers.google.com/youtube/v3/determine_quota_cost",
        "SRC-YT-SHORTS":"https://support.google.com/youtube/answer/15424877",
        "SRC-YT-POSTS":"https://support.google.com/youtube/answer/9409631",
        "SRC-YT-VIEWS":"https://developers.google.com/youtube/v3/docs/videos",
    }
    now=utc_now()
    return [{"source_id":k,"url":v,"role":"OFFICIAL_REFERENCE","captured_at":now} for k,v in urls.items()]


def render_html(report: dict[str,Any], path: Path) -> None:
    channel=report.get("channel",{})
    title=channel.get("snippet",{}).get("title") or "YouTube Channel Deep Audit"
    videos=report.get("videos",[])
    perf=report.get("analysis",{}).get("performance",{})
    data=json.dumps({"videos":videos},ensure_ascii=False).replace("<","\\u003c").replace("</script","<\\/script")
    cards=[]
    for i,v in enumerate(videos,1):
        sn=v.get("snippet",{}); st=v.get("statistics",{}); hints=v.get("_audit_hints",{})
        cards.append(
            "<details class='video-card'><summary><strong>#%d</strong> %s</summary>"
            "<div class='cardgrid'><div><b>Published</b><br>%s</div><div><b>Views</b><br>%s</div><div><b>Likes</b><br>%s</div>"
            "<div><b>Comments</b><br>%s</div><div><b>Type</b><br>%s</div><div><b>Transcript</b><br>%s</div></div>"
            "<p><b>Resources:</b> %s</p><p><b>Signals:</b> %s</p><p><a href='https://www.youtube.com/watch?v=%s' target='_blank' rel='noreferrer noopener'>Open video</a></p></details>"
            % (i,html.escape(sn.get("title","")),html.escape(str(sn.get("publishedAt","—"))),html.escape(str(st.get("viewCount","—"))),
               html.escape(str(st.get("likeCount","—"))),html.escape(str(st.get("commentCount","—"))),html.escape(str(v.get("content_type","UNKNOWN"))),
               html.escape(str(v.get("transcript_status","NOT_CHECKED"))),html.escape(json.dumps(hints.get("resources",[]),ensure_ascii=False)[:1800]),
               html.escape(", ".join(hints.get("keywords",[])) or "none detected"),html.escape(v.get("id","")))
        )
    sources="".join(
        f"<tr><td>{html.escape(str(s.get('source_id','')))}</td><td><a href='{html.escape(str(s.get('url','')))}'>{html.escape(str(s.get('url','')))}</a></td><td>{html.escape(str(s.get('role','')))}</td></tr>"
        for s in report.get("sources",[])
    )
    tracker="".join(f"<label><input type='checkbox' data-day='{i}'> Day {i}</label>" for i in range(1,31))
    doc=f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — Deep Audit</title>
<style>
:root{{--bg:#fff;--fg:#171717;--muted:#666;--panel:#f5f5f7;--border:#d8d8dc;--accent:#6b5cff}}
[data-theme='dark']{{--bg:#111;--fg:#eee;--muted:#aaa;--panel:#1b1b1e;--border:#333;--accent:#8c80ff}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif}}
aside{{position:fixed;left:0;top:0;bottom:0;width:230px;padding:18px;border-right:1px solid var(--border);background:var(--bg);overflow:auto}}
aside a{{display:block;padding:8px 0;color:inherit;text-decoration:none}} main{{margin-left:250px;max-width:1400px;padding:28px}}
section{{margin:0 0 42px}} h1{{font-size:34px}} .muted{{color:var(--muted)}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.panel{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px}} button,input,select{{font:inherit}}
button{{padding:9px 12px;border:1px solid var(--border);border-radius:9px;background:var(--panel);color:var(--fg);cursor:pointer}}
input[type=search],input[type=number]{{padding:10px;width:100%;border:1px solid var(--border);border-radius:9px;background:var(--bg);color:var(--fg)}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:9px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}}
.video-card{{border:1px solid var(--border);border-radius:12px;padding:10px;margin:9px 0;background:var(--panel)}} summary{{cursor:pointer}}
.cardgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:12px 0}}
.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}} label{{display:block;margin:7px 0}}
.bar{{height:10px;background:var(--panel);border-radius:999px;overflow:hidden}} .bar>span{{display:block;height:100%;width:0;background:var(--accent)}}
.badge{{display:inline-block;padding:4px 8px;border-radius:999px;border:1px solid var(--border);font-size:12px}}
@media(max-width:850px){{aside{{position:sticky;width:auto;height:auto;border-right:0;border-bottom:1px solid var(--border);z-index:3}}main{{margin:0;padding:18px}}aside nav{{display:flex;gap:10px;overflow:auto}}aside a{{white-space:nowrap}}}}
@media print{{aside,button,#interactive{{display:none!important}}main{{margin:0;max-width:none}}}}
</style></head>
<body>
<aside><nav aria-label="Audit navigation"><a href="#summary">Summary</a><a href="#start">Start Here</a><a href="#videos">Videos</a><a href="#research">Research</a><a href="#ideas">Idea Scorer</a><a href="#decision">Decision Tree</a><a href="#tracker">30-Day</a><a href="#sources">Sources</a></nav>
<div style="margin-top:14px"><button id="theme">Toggle theme</button> <button onclick="window.print()">Print / PDF</button></div></aside>
<main>
<section id="summary"><span class="badge">Production Deep Audit 11.2</span><h1>{html.escape(title)}</h1>
<p class="muted">Captured {html.escape(str(report.get("metadata",{}).get("completed_at","")))} · maximum legitimately accessible public/API/browser boundary.</p>
<div class="grid"><div class="panel"><b>Videos</b><div>{len(videos)}</div></div><div class="panel"><b>Median views</b><div>{html.escape(str(perf.get("median_views","—")))}</div></div><div class="panel"><b>Outliers</b><div>{html.escape(str(perf.get("outlier_count","—")))}</div></div><div class="panel"><b>Posts</b><div>{len(report.get("posts",[]))}</div></div><div class="panel"><b>Comment videos</b><div>{len(report.get("comments_summary",[]))}</div></div></div></section>
<section id="start"><div class="panel"><h2>Start Here — 10 Minutes</h2><ol><li>Pick one audience.</li><li>Find three relevant channels.</li><li>Review ten videos per channel.</li><li>Find five outliers.</li><li>Write the common pattern.</li><li>Create one original test idea.</li></ol></div></section>
<section id="videos"><h2>Video-by-Video Inventory</h2><p class="muted">Search/filter is only a convenience; inventory completeness is defined by the collection record.</p><input id="search" type="search" placeholder="Search titles, descriptions, IDs..." autocomplete="off"><div id="cards">{''.join(cards)}</div></section>
<section id="research"><h2>Research Analyzer</h2><div class="two"><div class="panel">
<label>Video views<input id="views" type="number" value="10000" min="0"></label><label>Typical views<input id="typical" type="number" value="{int(perf.get("median_views") or 1000)}" min="0"></label><label>Age in days<input id="age" type="number" value="30" min="0.01" step="0.01"></label>
<p><b>Outlier ratio:</b> <span id="out"></span></p><p><b>Views/day:</b> <span id="vpd"></span></p><p><b>Views/hour:</b> <span id="vph"></span></p><p><b>Research signal:</b> <span id="signal"></span></p><p class="muted">Heuristic only; not causal or predictive.</p>
</div><div class="panel"><b>Use responsibly</b><p>Keep format, age, topic and context comparable. A high ratio is a research signal, not proof of why a video won.</p></div></div></section>
<section id="ideas"><h2>Idea Scorer</h2><div class="panel" id="idea-box"></div></section>
<section id="decision"><h2>Decision Tree</h2><div class="panel"><label>Recent demand? <select id="d1"><option value="y">Yes</option><option value="n">No</option></select></label><label>Original angle? <select id="d2"><option value="y">Yes</option><option value="n">No</option></select></label><label>Safe/original production? <select id="d3"><option value="y">Yes</option><option value="n">No</option></select></label><label>Clear promise? <select id="d4"><option value="y">Yes</option><option value="n">No</option></select></label><button id="decide">Decide</button><p><b>Result:</b> <span id="decision-result">—</span></p></div></section>
<section id="tracker"><h2>30-Day Beginner Tracker</h2><div class="panel"><p><b><span id="count">0</span> / 30</b></p><div class="bar"><span id="progress"></span></div><div class="two" style="margin-top:14px">{tracker}</div><button id="reset" style="margin-top:12px">Reset Progress</button></div></section>
<section id="sources"><h2>Sources / Evidence Register</h2><div class="panel"><table><thead><tr><th>Source</th><th>URL</th><th>Role</th></tr></thead><tbody>{sources}</tbody></table></div></section>
<section><h2>Beginner FAQ</h2><div class="two"><div class="panel"><b>Expensive tools?</b><p>Not required. Validate the workflow before increasing costs.</p></div><div class="panel"><b>Copy viral videos?</b><p>Study demand and structure; create original expression and evidence.</p></div><div class="panel"><b>First videos fail?</b><p>Treat them as experiments and change one major variable at a time.</p></div><div class="panel"><b>Show your face?</b><p>No. A faceless format still needs original value and audience fit.</p></div></div></section>
<section><h2>Final One-Page Playbook</h2><div class="panel"><b>Research → Outlier → Pattern → Original idea → Script → Production → Packaging → Publish → Measure → Decide.</b><p>Separate observations, verified claims, creator-reported claims and recommendations. Never hide incomplete evidence.</p></div></section>
</main>
<script>
const reportData=${data};
const root=document.documentElement, theme=document.getElementById('theme');
const savedTheme=localStorage.getItem('ytAuditTheme'); if(savedTheme) root.setAttribute('data-theme',savedTheme);
theme.onclick=()=>{{const n=root.getAttribute('data-theme')==='dark'?'light':'dark';root.setAttribute('data-theme',n);localStorage.setItem('ytAuditTheme',n)}};
document.getElementById('search').oninput=()=>{{const n=document.getElementById('search').value.toLowerCase();document.querySelectorAll('.video-card').forEach(c=>c.hidden=!c.innerText.toLowerCase().includes(n))}};
function calc(){{const v=+views.value||0,t=+typical.value||0,a=+age.value||.01;out.textContent=t?(v/t).toFixed(2)+'x':'—';vpd.textContent=(v/a).toFixed(2);vph.textContent=(v/(a*24)).toFixed(2);signal.textContent=t?Math.min(100,Math.round(50+10*Math.log10(Math.max(1,v/t))))+'/100':'—'}}
['views','typical','age'].forEach(id=>document.getElementById(id).oninput=calc);calc();
const names=['Demand','Recent evidence','Outlier strength','Audience fit','Originality','Production simplicity','Monetization potential','Policy/copyright safety'];
idea-box.innerHTML='<p>Score each 1–5.</p>'+names.map((n,i)=>'<label>'+n+' <input class="idea" type="number" min="1" max="5" value="3"></label>').join('')+'<p><b>Total:</b> <span id="ideaTotal">24</span> / 40</p><p><b>Recommendation:</b> <span id="ideaRec">TEST</span></p>';
function score(){{const s=[...document.querySelectorAll('.idea')].reduce((a,x)=>a+(+x.value||0),0);ideaTotal.textContent=s;ideaRec.textContent=s>=32?'MAKE IT':s>=25?'TEST':s>=18?'REDESIGN':'AVOID'}};document.querySelectorAll('.idea').forEach(x=>x.oninput=score);
decide.onclick=()=>{{const a=[d1.value,d2.value,d3.value,d4.value];decisionResult.textContent=d3.value==='n'?'AVOID':a.every(x=>x==='y')?'MAKE IT':d2.value==='n'?'REDESIGN':'RESEARCH MORE'}};
const trackerKey='ytAuditTracker', boxes=[...document.querySelectorAll('[data-day]')], saved=JSON.parse(localStorage.getItem(trackerKey)||'[]');boxes.forEach(b=>b.checked=saved.includes(+b.dataset.day));
function save(){{const days=boxes.filter(b=>b.checked).map(b=>+b.dataset.day);localStorage.setItem(trackerKey,JSON.stringify(days));count.textContent=days.length;progress.style.width=(days.length/30*100)+'%'}}boxes.forEach(b=>b.onchange=save);save();reset.onclick=()=>{{localStorage.removeItem(trackerKey);boxes.forEach(b=>b.checked=false);save()}};
</script></body></html>"""
    path.write_text(doc, encoding="utf-8")


def write_artifacts(out: Path, report: dict[str,Any], comments: list[dict[str,Any]]) -> None:
    atomic_write(out/"audit.json", report)
    atomic_write(out/"schema.json", {
        "$schema":"https://json-schema.org/draft/2020-12/schema",
        "type":"object",
        "required":["metadata","channel","coverage","access_matrix","snapshots","videos","posts","comments_summary","playlists","channel_sections","transcripts","sources","claims","metrics","calculations","risks","hypotheses","experiments","recommendations","benchmarks","knowledge_gaps","deltas","policy_checks","decision_queue","validation","executive_summary","beginner_plan","analysis","reproducibility","self_audit"],
        "properties":{"videos":{"type":"array"},"claims":{"type":"array"},"metrics":{"type":"array"},"sources":{"type":"array"},"metadata":{"type":"object"},"validation":{"type":"object"}}
    })
    (out/"config.yaml").write_text("version: 11.2\nmode: DEEP\npublic_only: true\ncredential_source: GITHUB_ACTIONS:YOUTUBE_API_KEY\n", encoding="utf-8")
    csv_write(out/"video_inventory.csv",[
        {"video_id":v.get("id"),"title":v.get("snippet",{}).get("title"),"published_at":v.get("snippet",{}).get("publishedAt"),"views":v.get("statistics",{}).get("viewCount"),
         "likes":v.get("statistics",{}).get("likeCount"),"comments":v.get("statistics",{}).get("commentCount"),"duration":v.get("contentDetails",{}).get("duration"),
         "content_type":v.get("content_type"),"transcript_status":v.get("transcript_status")}
        for v in report.get("videos",[])
    ],["video_id","title","published_at","views","likes","comments","duration","content_type","transcript_status"])
    csv_write(out/"comments_coverage.csv",report.get("comments_summary",[]),["video_id","top_level_threads","replies_collected","complete","reply_pages","coverage"])
    csv_write(out/"comments.csv",comments,["video_id","kind","comment_id","text","published_at"])
    csv_write(out/"sources.csv",report.get("sources",[]),["source_id","url","role","captured_at"])
    render_html(report,out/"audit.html")
    manifest={"schema_version":"11.2.0","generated_at":utc_now(),"files":{}}
    for p in sorted(out.iterdir()):
        if p.is_file() and p.name not in {"release_manifest.json","checkpoint.json"}:
            manifest["files"][p.name]=sha256_file(p)
    atomic_write(out/"release_manifest.json",manifest)


def csv_write(path: Path, rows: list[dict[str,Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)


def run(args: argparse.Namespace) -> int:
    skill=Path(args.skill)
    if not skill.exists() or skill.stat().st_size==0:
        print(json.dumps({"status":"FAIL","reason":"SKILL_FILE_UNREADABLE","api_key_exposed":False})); return 2
    key=os.getenv("YOUTUBE_API_KEY")
    if not key or not plausible_key(key):
        print(json.dumps({"status":"FAIL","reason":"API_KEY_MISSING_OR_INVALID","api_key_exposed":False})); return 2
    out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
    checkpoint=Checkpoint(out/"checkpoint.json")
    quota=Quota(checkpoint,args.quota_budget)
    api=API(key,quota,args.retries)
    started=checkpoint.get("started_at") or utc_now()
    checkpoint.set("started_at",started)
    checkpoint.set("channel_input",args.channel)
    checkpoint.set("credential_present",True)
    checkpoint.set("credential_exposed",False)
    checkpoint.set("skill_sha256",sha256_file(skill))

    channel,identity=resolve_channel(api,args.channel)
    if not channel:
        report={
            "metadata":{"methodology_version":"11.2-production","schema_version":"11.2.0","started_at":started,"completed_at":utc_now(),"canonical_channel_url":args.channel,
                        "input_contract":{"skill_file_present":True,"channel_url_present":True,"api_key_present":True,"api_key_exposed":False}},
            "channel":{},"coverage":{"status":"BLOCKED","identity_resolution":identity},"access_matrix":[],"snapshots":[],"videos":[],"posts":[],"comments_summary":[],"playlists":[],"channel_sections":[],"transcripts":[],
            "sources":source_register(),"claims":[],"metrics":[],"calculations":[],"risks":[],"hypotheses":[],"experiments":[],"recommendations":[],"benchmarks":[],"knowledge_gaps":[],"deltas":[],"policy_checks":[],
            "decision_queue":{},"validation":{"status":"BLOCKED"},"executive_summary":{},"beginner_plan":beginner_plan(),"analysis":{},
            "reproducibility":{"quota":quota.summary(),"credential_present":True,"credential_exposed":False},"self_audit":{"api_key_exposed":False,"limitations":[identity]}
        }
        write_artifacts(out,report,[])
        return 3

    channel_id=channel["id"]
    canonical=f"https://www.youtube.com/channel/{channel_id}"
    checkpoint.set("channel_id",channel_id); checkpoint.set("channel",channel)
    uploads=channel.get("contentDetails",{}).get("relatedPlaylists",{}).get("uploads")
    if not uploads:
        return 4

    inventory,inv_cov=paginate(api,checkpoint,"inventory","playlistItems.list",{"part":"snippet,contentDetails,status","playlistId":uploads,"maxResults":50},args.max_pages)
    video_ids=list(dict.fromkeys([x.get("contentDetails",{}).get("videoId") for x in inventory if x.get("contentDetails",{}).get("videoId")]))
    checkpoint.set("video_ids",video_ids)

    videos=checkpoint.get("videos",[])
    by_id={v.get("id"):v for v in videos}
    missing=[x for x in video_ids if x not in by_id]
    parts="id,snippet,contentDetails,statistics,status,topicDetails,recordingDetails,liveStreamingDetails,localizations,paidProductPlacementDetails,brandPartner"
    for i in range(0,len(missing),50):
        group=missing[i:i+50]
        res=api.get("videos.list",{"part":parts,"id":",".join(group)})
        if not res.get("ok"):
            break
        for v in (res.get("data") or {}).get("items",[]):
            by_id[v.get("id")]=v
        checkpoint.set("videos",list(by_id.values()))
    videos=list(by_id.values())

    playlists,playlist_cov=paginate(api,checkpoint,"playlists","playlists.list",{"part":"snippet,contentDetails,status","channelId":channel_id,"maxResults":50},args.max_pages)
    sections,section_cov=paginate(api,checkpoint,"sections","channelSections.list",{"part":"snippet,contentDetails","channelId":channel_id},args.max_pages)

    browser={"status":"SKIPPED","shorts":[],"posts":[],"transcripts":[],"limitations":["Browser disabled."]} if args.skip_browser else browser_collect(canonical,video_ids,args.transcript_limit,args.max_scrolls)
    short_ids={x.get("video_id") for x in browser.get("shorts",[])}
    for v in videos:
        ctype,cmethod=classify_video(v,short_ids)
        desc=v.get("snippet",{}).get("description","") or ""
        v["content_type"]=ctype; v["classification_method"]=cmethod
        v["transcript_status"]=next((x.get("status") for x in browser.get("transcripts",[]) if x.get("video_id")==v.get("id")),"NOT_CHECKED")
        v["_audit_hints"]={"resources":resource_hints(desc),"keywords":keyword_hints(desc)}

    comments_summary=checkpoint.get("comments_summary",[])
    comments=checkpoint.get("comments",[])
    completed={x.get("video_id") for x in comments_summary if x.get("complete") or x.get("status") in {"COMMENTS_DISABLED","UNAVAILABLE"}}
    if not args.skip_comments:
        for vid in video_ids:
            if vid in completed:
                continue
            threads,tcov=paginate(api,checkpoint,f"comments.{vid}.threads","commentThreads.list",{"part":"id,snippet,replies","videoId":vid,"maxResults":100,"order":"time","textFormat":"plainText"},args.comment_max_pages)
            if tcov.get("stop_reason")=="COMMENTS_DISABLED":
                summary={"video_id":vid,"status":"COMMENTS_DISABLED","complete":True,"coverage":tcov}
                comments_summary.append(summary); checkpoint.set("comments_summary",comments_summary); continue
            reply_map={}; replies_complete=True; reply_pages=0
            for thread in threads:
                top=(thread.get("snippet") or {}).get("topLevelComment") or {}
                parent=top.get("id"); inline=(thread.get("replies") or {}).get("comments",[])
                for r in inline:
                    if r.get("id"): reply_map[r["id"]]=r
                total=int((thread.get("snippet") or {}).get("totalReplyCount") or 0)
                if parent and total>len(inline):
                    reps,rcov=paginate(api,checkpoint,f"comments.{vid}.replies.{parent}","comments.list",{"part":"id,snippet","parentId":parent,"maxResults":100,"textFormat":"plainText"},args.comment_max_pages)
                    reply_pages += rcov["pages"]; replies_complete = replies_complete and rcov["complete"]
                    for r in reps:
                        if r.get("id"): reply_map[r["id"]]=r
            seen={r.get("comment_id") for r in comments if r.get("comment_id")}
            for thread in threads:
                top=(thread.get("snippet") or {}).get("topLevelComment") or {}; sn=top.get("snippet") or {}; cid=top.get("id")
                if cid and cid not in seen:
                    comments.append({"video_id":vid,"kind":"top_level","comment_id":cid,"text":sn.get("textOriginal") or sn.get("textDisplay"),"published_at":sn.get("publishedAt")}); seen.add(cid)
            for r in reply_map.values():
                rid=r.get("id"); rs=r.get("snippet") or {}
                if rid and rid not in seen:
                    comments.append({"video_id":vid,"kind":"reply","comment_id":rid,"text":rs.get("textOriginal") or rs.get("textDisplay"),"published_at":rs.get("publishedAt")}); seen.add(rid)
            summary={"video_id":vid,"top_level_threads":len(threads),"replies_collected":len(reply_map),"reply_pages":reply_pages,"complete":tcov["complete"] and replies_complete,"coverage":tcov}
            comments_summary.append(summary)
            checkpoint.set("comments_summary",comments_summary); checkpoint.set("comments",comments)
            if quota.used>=quota.run_budget:
                break

    captured=utc_now()
    report={
        "metadata":{"audit_id":f"{channel_id}-{captured.replace(':','').replace('+00:00','Z')}","methodology_version":"11.2-production","schema_version":"11.2.0",
                    "started_at":started,"completed_at":captured,"canonical_channel_url":canonical,
                    "input_contract":{"skill_file_present":True,"channel_url_present":True,"api_key_present":True,"api_key_exposed":False}},
        "channel":channel,
        "coverage":{"inventory":inv_cov,"video_details":{"discovered":len(video_ids),"collected":len(videos),"complete":len(videos)==len(video_ids)},
                   "playlists":playlist_cov,"channel_sections":section_cov,"public_comments":{"video_records":len(comments_summary),"complete_videos":sum(1 for x in comments_summary if x.get("complete"))},
                   "browser":browser,"public_access_boundary":"maximum legitimately accessible public/API/browser evidence observed during this run"},
        "access_matrix":[
            {"surface":"uploads","mode":"YOUTUBE_DATA_API","status":"SUCCESS" if inv_cov.get("complete") else "PARTIAL"},
            {"surface":"video_details","mode":"YOUTUBE_DATA_API","status":"SUCCESS" if len(videos)==len(video_ids) else "PARTIAL"},
            {"surface":"playlists","mode":"YOUTUBE_DATA_API","status":"SUCCESS" if playlist_cov.get("complete") else "PARTIAL"},
            {"surface":"channel_sections","mode":"YOUTUBE_DATA_API","status":"SUCCESS" if section_cov.get("complete") else "PARTIAL"},
            {"surface":"comments","mode":"YOUTUBE_DATA_API","status":"PARTIAL_OR_COMPLETE_PER_VIDEO"},
            {"surface":"posts","mode":"PUBLIC_BROWSER","status":browser.get("status")},
            {"surface":"shorts","mode":"PUBLIC_BROWSER","status":browser.get("status")},
            {"surface":"transcripts","mode":"PUBLIC_BROWSER","status":browser.get("status")},
            {"surface":"private_analytics","mode":"OWNER_AUTH_REQUIRED","status":"UNAVAILABLE"},
        ],
        "snapshots":[{"captured_at":captured,"inventory_count":len(video_ids),"video_count":len(videos),"quota":quota.summary()}],
        "videos":sorted(videos,key=lambda x:x.get("snippet",{}).get("publishedAt") or "",reverse=True),
        "posts":browser.get("posts",[]),"comments_summary":comments_summary,"playlists":playlists,"channel_sections":sections,"transcripts":browser.get("transcripts",[]),
        "sources":source_register(),"claims":[],"metrics":[],"calculations":[],"risks":[],"hypotheses":[],"experiments":[],"recommendations":[],"benchmarks":[],"knowledge_gaps":[],"deltas":[],"policy_checks":[],
        "decision_queue":{},"validation":{"status":"COLLECTION_COMPLETE_TO_ACCESSIBLE_BOUNDARY"},"executive_summary":{},"beginner_plan":beginner_plan(),
        "analysis":{"performance":performance_metrics(videos),"comment_keywords":comment_keyword_summary(comments)},
        "reproducibility":{"collector_version":"11.2-production","skill_sha256":sha256_file(skill),"credential_present":True,"credential_exposed":False,
                           "credential_fingerprint":credential_fingerprint(key),"quota":quota.summary(),"api_request_count":api.request_count},
        "self_audit":{"api_key_exposed":False,"inventory_complete":inv_cov.get("complete"),"video_details_complete":len(videos)==len(video_ids),
                      "comment_complete_videos":sum(1 for x in comments_summary if x.get("complete")),"limitations":browser.get("limitations",[])}
    }
    write_artifacts(out,report,comments)
    checkpoint.set("completed_at",captured); checkpoint.set("status","COMPLETED" if inv_cov.get("complete") else "PARTIAL")
    print(json.dumps({"status":"SUCCESS","channel_id":channel_id,"videos":len(videos),"posts":len(report["posts"]),"comments":len(comments),"quota":quota.summary(),"api_key_exposed":False},indent=2))
    return 0


def beginner_plan() -> dict[str,Any]:
    return {"first_10_minutes":["Pick one audience","Find 3 relevant channels","Find 10 videos per channel","Identify 5 outliers","Write why they worked","Create 1 original idea"],
            "days_1_3":"Choose one niche and audience.","days_4_6":"Research ~50 videos.","days_7_8":"Identify ~10 outliers.","day_9":"Create 10 idea candidates.","day_10":"Score the ideas.","days_11_16":"Script → produce → package → publish.","days_17_22":"Produce the next 3–4 videos.","days_23_28":"Identify repeated patterns.","days_29_30":"Kill / Continue / Double Down."}


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--skill",required=True)
    ap.add_argument("--channel",required=True)
    ap.add_argument("--output",default="audit")
    ap.add_argument("--quota-budget",type=int,default=9000)
    ap.add_argument("--retries",type=int,default=2)
    ap.add_argument("--max-pages",type=int,default=None)
    ap.add_argument("--comment-max-pages",type=int,default=None)
    ap.add_argument("--transcript-limit",type=int,default=100)
    ap.add_argument("--max-scrolls",type=int,default=100)
    ap.add_argument("--skip-browser",action="store_true")
    ap.add_argument("--skip-comments",action="store_true")
    args=ap.parse_args()
    try:
        return run(args)
    except Exception as exc:
        print(json.dumps({"status":"FAIL","reason":"UNHANDLED_RUNTIME_ERROR","message":safe_text(exc),"api_key_exposed":False}))
        return 10


if __name__=="__main__":
    raise SystemExit(main())
