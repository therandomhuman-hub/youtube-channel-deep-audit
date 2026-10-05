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

from runtime.transcript_audit import audit_video_transcript, audit_learning, make_transcript_record, merge_transcript_records
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


def extract_claims_from_title(title: str) -> list[dict[str, Any]]:
    claims = []
    text = title or ""
    for value in re.findall(r"\$\s?[\d,.]+(?:K|M|k|m|B|b)?(?:/month|/mo)?", text):
        claims.append({
            "claim": f"Title presents outcome/revenue figure: {value}",
            "status": "CREATOR_REPORTED",
            "verification": "Creator headline claim; public API counters do not verify revenue or profit.",
        })
    for value, unit in re.findall(r"(?:in|within|over|under|after)\s+(\d+)\s+(hours?|days?|weeks?|months?|years?)", text, re.I):
        claims.append({
            "claim": f"Title presents time-to-result claim: {value} {unit}",
            "status": "CREATOR_REPORTED",
            "verification": "Creator headline claim; time-to-result requires case-specific evidence.",
        })
    if re.search(r"\b(copy|only|secret|guaranteed|monetized|billion|million|subscriber)\b", text, re.I):
        claims.append({
            "claim": "Title uses strong replication, exclusivity, scale or result framing.",
            "status": "OBSERVED",
            "verification": "Directly observed in public title.",
        })
    return claims

def per_video_analysis(video: dict[str, Any], comment_rows: list[dict[str, Any]], transcript: dict[str, Any]) -> dict[str, Any]:
    title = video.get("snippet", {}).get("title", "") or ""
    desc = video.get("snippet", {}).get("description", "") or ""
    stats = video.get("statistics", {}) or {}
    try:
        views = int(stats.get("viewCount", 0) or 0)
    except Exception:
        views = 0

    rows = [r for r in comment_rows if r.get("video_id") == video.get("id")]
    ctext = " ".join(str(r.get("text", "")) for r in rows).lower()
    learning = audit_learning(title=title, description=desc, transcript=transcript)
    title_claims = extract_claims_from_title(title)
    transcript_claims = transcript.get("claims", []) if isinstance(transcript, dict) else []

    resources = resource_hints(desc)
    tools = sorted(set(re.findall(
        r"\b(?:ChatGPT|Claude|Gemini|Canva|CapCut|VidIQ|TubeBuddy|Gumroad|WhatsApp|Instagram|Facebook|YouTube|TikTok|Veo|Sora|Runway|ElevenLabs)\b",
        title + " " + desc,
        re.I,
    )))

    workflow = {
        "input": "Audience problem or creator outcome, analyzed from title, description and transcript when available.",
        "research": "Transcript-derived process signals and public resource links; private research process remains unavailable.",
        "decision": "Strategy, mechanism and offer framing reconstructed from transcript rather than inferred from title alone.",
        "production": "Production steps reported in transcript are assessed for specificity; private labor/costs remain unavailable.",
        "packaging": "Outcome + platform/problem + mechanism/time + system/course/case-study framing.",
        "publishing": "Public upload metadata observed.",
        "measurement": "Public views/likes/comments observed; private CTR, retention, impressions and watch time unavailable.",
        "monetization": "Public product/resource links observed where present; conversion, revenue and profit are not independently verified.",
    }

    blind_spots = [
        "Private analytics (CTR, retention, impressions, watch time) unavailable.",
        "Failure rate and unsuccessful experiments are not observable from a public audit.",
        "True costs, labor, conversion rates, refunds and profit are not established.",
    ]
    if transcript.get("status") != "FULL_TRANSCRIPT_AVAILABLE":
        blind_spots.append("Transcript-level claim/workflow audit is incomplete for this video.")
    if learning.get("claim_evidence_gap", 0) > 0:
        blind_spots.append("Material creator-reported claims remain unverified unless independently supported by a public source.")
    if not resources:
        blind_spots.append("No classified outbound resource signal was found in the public description.")

    themes = [x for x in ["help", "niche", "prompt", "tool", "views", "start", "free", "works"] if x in ctext]
    public_evidence = {
        "views_observed": views,
        "likes_observed": stats.get("likeCount"),
        "comments_observed": stats.get("commentCount"),
        "comment_rows_collected": len(rows),
        "description_resource_count": len(resources),
    }

    combined_claims = []
    combined_claims.extend(title_claims)
    for c in transcript_claims:
        combined_claims.append({
            "claim": c.get("summary"),
            "status": c.get("status", "CREATOR_REPORTED"),
            "verification": c.get("verification"),
            "categories": c.get("categories", []),
            "source": transcript.get("source"),
        })

    return {
        **learning,
        "claims": combined_claims[:20],
        "title_claim_count": len(title_claims),
        "transcript_claim_count": len(transcript_claims),
        "tools": tools,
        "resources": resources,
        "comment_evidence": {"comment_count": len(rows), "themes": themes},
        "public_evidence": public_evidence,
        "blind_spots": blind_spots,
        "workflow": workflow,
        "beginner_takeaway": learning.get("beginner_takeaway"),
        "analysis_basis": learning.get("audit_basis", []) + ["public comments when available"],
    }

def comment_keyword_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    blob = " ".join(str(x.get("text","")) for x in rows).lower()
    terms = ["great","helpful","thanks","scam","works","doesn't work","expensive","link","tutorial","ai","youtube"]
    return {term: blob.count(term) for term in terms}



def _youtube_player_response(page: Any) -> dict[str, Any] | None:
    """Extract YouTube player response from the live page context, then inline scripts."""
    try:
        value = page.evaluate("() => window.ytInitialPlayerResponse || null")
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    try:
        page.wait_for_function("() => !!window.ytInitialPlayerResponse", timeout=15000)
        value = page.evaluate("() => window.ytInitialPlayerResponse || null")
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    try:
        scripts = page.locator("script").all_text_contents()
    except Exception:
        return None
    decoder = json.JSONDecoder()
    for text_value in scripts:
        if "ytInitialPlayerResponse" not in text_value:
            continue
        m = re.search(r"ytInitialPlayerResponse\s*=\s*", text_value)
        if not m:
            continue
        start = text_value.find("{", m.end())
        if start < 0:
            continue
        try:
            obj, _ = decoder.raw_decode(text_value[start:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _public_caption_track(page: Any, video_id: str) -> dict[str, Any] | None:
    response = _youtube_player_response(page)
    if not response:
        return None
    tracks = (
        response.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )
    if not tracks:
        return None

    preferred = {"en": 0, "en-US": 1, "en-GB": 2, "en-IN": 3}
    ordered = sorted(
        tracks,
        key=lambda x: (
            0 if x.get("languageCode") in preferred else 1,
            0 if not x.get("kind") else 1,
            preferred.get(x.get("languageCode"), 99),
        ),
    )
    track = ordered[0] if ordered else None
    if not track or not track.get("baseUrl"):
        return None

    base_url = track["baseUrl"]
    try:
        body = page.evaluate(
            """async (url) => {
                const suffixes = [
                    (url.includes('?') ? '&' : '?') + 'fmt=json3',
                    (url.includes('?') ? '&' : '?') + 'fmt=srv3',
                    ''
                ];
                for (const suffix of suffixes) {
                    try {
                        const res = await fetch(url + suffix, {credentials: 'include'});
                        const text = await res.text();
                        if (text && text.trim().length) return text;
                    } catch (_) {}
                }
                return '';
            }""",
            base_url,
        )
    except Exception:
        body = ""
    if not body:
        return None

    snippets = []
    if body.lstrip().startswith("{"):
        try:
            payload = json.loads(body)
            for event in payload.get("events", []):
                segs = event.get("segs") or []
                text = "".join(str(seg.get("utf8", "")) for seg in segs).strip()
                if text:
                    snippets.append({
                        "text": re.sub(r"\s+", " ", text),
                        "start": float(event.get("tStartMs", 0)) / 1000.0,
                        "duration": float(event.get("dDurationMs", 0)) / 1000.0,
                    })
        except Exception:
            snippets = []

    if not snippets:
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(body)
            for node in root.findall(".//text"):
                text = "".join(node.itertext()).strip()
                if not text:
                    continue
                try:
                    start = float(node.attrib.get("start", 0) or 0)
                except Exception:
                    start = 0.0
                try:
                    duration = float(node.attrib.get("dur", 0) or 0)
                except Exception:
                    duration = 0.0
                snippets.append({
                    "text": re.sub(r"\s+", " ", text),
                    "start": start,
                    "duration": duration,
                })
        except Exception:
            snippets = []

    if not snippets:
        return None

    return {
        "video_id": video_id,
        "status": "FULL_TRANSCRIPT_AVAILABLE",
        "source": "YOUTUBE_PUBLIC_CAPTION_TRACK",
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "language": track.get("name", {}).get("simpleText") if isinstance(track.get("name"), dict) else None,
        "language_code": track.get("languageCode"),
        "is_generated": track.get("kind") == "asr",
        "available_tracks": [
            {
                "language": x.get("languageName") or x.get("name"),
                "language_code": x.get("languageCode"),
                "is_generated": x.get("kind") == "asr",
            }
            for x in tracks
        ],
        "_snippets": snippets,
    }


def browser_collect(
    channel_url: str,
    video_ids: list[str],
    transcript_limit: int,
    max_scrolls: int,
    transcript_ids: list[str] | None = None,
) -> dict[str, Any]:
    result = {
        "status": "PLAYWRIGHT_UNAVAILABLE",
        "shorts": [],
        "posts": [],
        "transcripts": [],
        "surface_checks": [],
        "limitations": [],
    }
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        result["limitations"].append("Playwright is unavailable.")
        return result
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(30000)
            base = channel_url.rstrip("/")

            for suffix in ["/", "/videos", "/shorts", "/live", "/playlists", "/posts"]:
                try:
                    page.goto(base + suffix, wait_until="domcontentloaded")
                    result["surface_checks"].append({
                        "surface": suffix,
                        "status": "OK",
                        "captured_at": utc_now(),
                    })
                except Exception as exc:
                    result["surface_checks"].append({
                        "surface": suffix,
                        "status": "UNAVAILABLE",
                        "error": safe_text(exc)[:500],
                        "captured_at": utc_now(),
                    })

            try:
                page.goto(base + "/shorts", wait_until="domcontentloaded")
                progressive_scroll(page, max_scrolls)
                hrefs = page.locator('a[href*="/shorts/"]').evaluate_all(
                    "els => els.map(e => e.href).filter(Boolean)"
                )
                seen = set()
                for pos, href in enumerate(hrefs, 1):
                    m = re.search(r"/shorts/([A-Za-z0-9_-]{6,})", href)
                    if m and m.group(1) not in seen:
                        seen.add(m.group(1))
                        result["shorts"].append({
                            "video_id": m.group(1),
                            "position": pos,
                            "captured_at": utc_now(),
                        })
            except Exception as exc:
                result["limitations"].append("Shorts: " + safe_text(exc)[:500])

            try:
                page.goto(base + "/posts", wait_until="domcontentloaded")
                progressive_scroll(page, max_scrolls)
                anchors = page.locator('a[href*="/post/"]').evaluate_all(
                    "(els) => els.map((e) => ({href: e.href, text: (e.innerText || e.textContent || '').trim()}))"
                )
                seen = set()
                for a in anchors:
                    href = a.get("href", "")
                    m = re.search(r"/post/([^?#/]+)", href)
                    if not m or m.group(1) in seen:
                        continue
                    seen.add(m.group(1))
                    preview = a.get("text", "")[:1200]
                    try:
                        loc = page.locator(f'a[href*="/post/{m.group(1)}"]').first
                        card = loc.locator(
                            "xpath=ancestor::*[self::ytd-rich-item-renderer or self::ytd-backstage-post-thread-renderer][1]"
                        )
                        preview = re.sub(r"\s+", " ", card.inner_text(timeout=3000)).strip()[:1200]
                    except Exception:
                        pass
                    result["posts"].append({
                        "post_id": m.group(1),
                        "url": href,
                        "text_preview": preview,
                        "captured_at": utc_now(),
                    })
            except Exception as exc:
                result["limitations"].append("Posts: " + safe_text(exc)[:500])

            ids = transcript_ids if transcript_ids is not None else video_ids[:max(0, transcript_limit)]
            for vid in ids:
                item = {
                    "video_id": vid,
                    "status": "UNKNOWN",
                    "captured_at": utc_now(),
                    "text_retained": False,
                }
                try:
                    page.goto(
                        f"https://www.youtube.com/watch?v={vid}",
                        wait_until="domcontentloaded",
                    )
                    page.wait_for_timeout(700)

                    transcript_result = page.evaluate(
                        """async () => {
                            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                            const selectors = [
                              'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"] .segment-text',
                              'transcript-segment-view-model .yt-core-attributed-string',
                              'ytd-transcript-segment-list-renderer .segment-text',
                              '#segments-container .segment-text',
                              '#segments-container yt-formatted-string'
                            ];
                            const textOf = (el) => (el?.innerText || el?.textContent || '').trim();

                            try {
                              const expand = document.querySelector('ytd-text-inline-expander #expand') || document.querySelector('#expand');
                              if (expand) expand.click();
                            } catch (_) {}

                            const getSegments = () => {
                              for (const sel of selectors) {
                                const got = [...document.querySelectorAll(sel)]
                                  .map(textOf).filter(Boolean);
                                if (got.length) return got;
                              }
                              return [];
                            };

                            let got = getSegments();
                            if (got.length) return got;

                            const findButton = () => {
                              const direct = document.querySelector('button[aria-label="Show transcript" i]');
                              if (direct) return direct;
                              const scopes = document.querySelectorAll(
                                'ytd-video-description-transcript-section-renderer, #structured-description, ytd-watch-metadata'
                              );
                              for (const scope of scopes) {
                                const buttons = scope.querySelectorAll('button, tp-yt-paper-button');
                                for (const b of buttons) {
                                  const label = (b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '');
                                  if (/transcript/i.test(label)) return b;
                                }
                              }
                              for (const b of document.querySelectorAll('button, tp-yt-paper-button')) {
                                if (/^show transcript$/i.test((b.textContent || '').trim())) return b;
                              }
                              return null;
                            };

                            const button = findButton();
                            if (button) {
                              button.click();
                              for (let i = 0; i < 24; i++) {
                                await sleep(400);
                                got = getSegments();
                                if (got.length) return got;
                              }
                            }
                            return [];
                        }"""
                    )

                    if isinstance(transcript_result, list) and transcript_result:
                        segments = [{"text": t, "start": 0, "duration": 0} for t in transcript_result]
                        item = make_transcript_record(
                            vid,
                            segments,
                            source="YOUTUBE_PUBLIC_TRANSCRIPT_UI",
                        )
                        item["captured_at"] = utc_now()
                    else:
                        cap = _public_caption_track(page, vid)
                        if cap:
                            snippets = cap.pop("_snippets")
                            item = make_transcript_record(
                                vid,
                                snippets,
                                source=cap.pop("source", "YOUTUBE_PUBLIC_CAPTION_TRACK"),
                                language=cap.pop("language", None),
                                language_code=cap.pop("language_code", None),
                                is_generated=cap.pop("is_generated", None),
                            )
                            item.update(cap)
                            item["captured_at"] = utc_now()
                        else:
                            item["status"] = "NOT_DETECTED"
                except Exception as exc:
                    item["status"] = "UNAVAILABLE"
                    item["error"] = safe_text(exc)[:500]
                result["transcripts"].append(item)

            browser.close()
        result["status"] = "SUCCESS"
    except Exception as exc:
        result["status"] = "PLAYWRIGHT_ERROR"
        result["limitations"].append(safe_text(exc)[:500])
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



def live_policy_checks() -> list[dict[str, Any]]:
    """Fetch a compact, current policy snapshot from official YouTube pages."""
    policy_urls = {
        "YPP_MONETIZATION": "https://support.google.com/youtube/answer/1311392",
        "PAID_PROMOTIONS": "https://support.google.com/youtube/answer/154235",
        "AI_DISCLOSURE": "https://support.google.com/youtube/answer/14328491",
        "SHORTS": "https://support.google.com/youtube/answer/15424877",
    }
    keywords = (
        "original", "authentic", "reused", "mass-produced", "repetitive",
        "altered", "synthetic", "disclose", "paid promotion", "shorts",
    )
    out: list[dict[str, Any]] = []
    for key, url in policy_urls.items():
        record = {
            "policy_id": key,
            "url": url,
            "status": "NOT_CHECKED",
            "captured_at": utc_now(),
        }
        try:
            req = Request(url, headers={"Accept": "text/html", "User-Agent": UA})
            with urlopen(req, timeout=20) as resp:
                raw = resp.read(3_000_000)
                body = raw.decode("utf-8", errors="replace")
                record["status"] = "LIVE_FETCHED"
                record["http_status"] = getattr(resp, "status", 200)
                record["content_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
                title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
                record["page_title"] = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()[:300] if title_match else None
                low = re.sub(r"<[^>]+>", " ", body).lower()
                record["matched_keywords"] = [kw for kw in keywords if kw in low]
        except HTTPError as exc:
            record["status"] = "HTTP_ERROR"
            record["http_status"] = exc.code
            record["error"] = safe_text(exc)[:300]
        except Exception as exc:
            record["status"] = "FETCH_ERROR"
            record["error"] = safe_text(exc)[:300]
        out.append(record)
    return out

def source_register(video_ids: list[str] | None = None, transcript_map: dict[str, dict[str, Any]] | None = None) -> list[dict[str, str]]:
    urls = {
        "SRC-YT-CHANNELS": "https://developers.google.com/youtube/v3/docs/channels",
        "SRC-YT-PLAYLISTITEMS": "https://developers.google.com/youtube/v3/docs/playlistItems/list",
        "SRC-YT-VIDEOS": "https://developers.google.com/youtube/v3/docs/videos/list",
        "SRC-YT-PLAYLISTS": "https://developers.google.com/youtube/v3/docs/playlists/list",
        "SRC-YT-SECTIONS": "https://developers.google.com/youtube/v3/docs/channelSections/list",
        "SRC-YT-COMMENTTHREADS": "https://developers.google.com/youtube/v3/docs/commentThreads/list",
        "SRC-YT-COMMENTS": "https://developers.google.com/youtube/v3/docs/comments/list",
        "SRC-YT-CAPTIONS": "https://developers.google.com/youtube/v3/docs/captions/list",
        "SRC-YT-ANALYTICS": "https://developers.google.com/youtube/analytics/reference",
        "SRC-YT-QUOTA": "https://developers.google.com/youtube/v3/determine_quota_cost",
        "SRC-YT-SHORTS": "https://support.google.com/youtube/answer/15424877",
        "SRC-YT-POSTS": "https://support.google.com/youtube/answer/9409631",
        "SRC-YT-MONETIZATION": "https://support.google.com/youtube/answer/1311392",
        "SRC-YT-PAID-PROMOTIONS": "https://support.google.com/youtube/answer/154235",
        "SRC-YT-AI-DISCLOSURE": "https://support.google.com/youtube/answer/14328491",
    }
    now = utc_now()
    rows = [
        {
            "source_id": k,
            "url": v,
            "role": "OFFICIAL_REFERENCE",
            "captured_at": now,
        }
        for k, v in urls.items()
    ]
    for vid in video_ids or []:
        rows.append({
            "source_id": f"VIDEO-{vid}",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "role": "PUBLIC_VIDEO_PAGE",
            "captured_at": now,
        })
        t = (transcript_map or {}).get(vid) or {}
        if t.get("status") == "FULL_TRANSCRIPT_AVAILABLE":
            rows.append({
                "source_id": f"TRANSCRIPT-{vid}",
                "url": t.get("source_url") or f"https://www.youtube.com/watch?v={vid}",
                "role": "PUBLIC_TRANSCRIPT_EVIDENCE",
                "captured_at": t.get("captured_at") or now,
            })
    return rows


def transcript_summary(transcripts: dict[str, dict[str, Any]], total_videos: int) -> dict[str, Any]:
    records = list(transcripts.values())
    full = sum(1 for x in records if x.get("status") == "FULL_TRANSCRIPT_AVAILABLE")
    partial = sum(1 for x in records if x.get("status") not in {"FULL_TRANSCRIPT_AVAILABLE", "NO_TRANSCRIPT_FOUND", "TRANSCRIPTS_DISABLED", "VIDEO_UNAVAILABLE", "TRANSCRIPT_REQUEST_BLOCKED", "AUTHENTICATION_OR_AGE_RESTRICTED", "DEPENDENCY_UNAVAILABLE", "TRANSCRIPT_FETCH_ERROR"} and x.get("status"))
    unavailable = sum(1 for x in records if x.get("status") in {
        "NO_TRANSCRIPT_FOUND", "TRANSCRIPTS_DISABLED", "VIDEO_UNAVAILABLE",
        "TRANSCRIPT_REQUEST_BLOCKED", "AUTHENTICATION_OR_AGE_RESTRICTED",
        "DEPENDENCY_UNAVAILABLE", "TRANSCRIPT_FETCH_ERROR",
    })
    not_attempted = max(0, total_videos - len(records))
    return {
        "target_video_count": total_videos,
        "attempted_video_count": len(records),
        "full_transcript_count": full,
        "partial_or_panel_count": partial,
        "unavailable_count": unavailable,
        "not_attempted_count": not_attempted,
        "coverage_percent": round((len(records) / total_videos) * 100, 1) if total_videos else 0,
        "full_coverage_percent": round((full / total_videos) * 100, 1) if total_videos else 0,
        "rule": "Transcript-level scoring is high-confidence only when a usable public transcript was actually retrieved and analyzed. Otherwise the video is explicitly low-confidence metadata/partial.",
    }


def build_claim_registry(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for v in videos:
        vid = v.get("id")
        a = v.get("video_analysis", {}) or {}
        for claim in a.get("claims", []) or []:
            status = claim.get("status")
            if status not in {"VERIFIED", "CREATOR_REPORTED", "OBSERVED", "INFERRED", "UNVERIFIED", "CONTRADICTED", "WITHDRAWN"}:
                status = "UNVERIFIED"
            evidence_ids = []
            if claim.get("source", "").startswith("YOUTUBE_"):
                evidence_ids = [f"TRANSCRIPT-{vid}"]
            else:
                evidence_ids = [f"VIDEO-{vid}"]
            out.append({
                "video_id": vid,
                "claim": claim.get("claim") or claim.get("summary"),
                "status": status,
                "evidence_ids": evidence_ids,
                "verification": claim.get("verification"),
                "categories": claim.get("categories", []),
            })
    return out

def render_html(report: dict[str, Any], path: Path) -> None:
    channel = report.get("channel", {})
    title = channel.get("snippet", {}).get("title") or "YouTube Channel Deep Audit"
    videos = report.get("videos", [])
    summary = report.get("transcript_audit", {})
    cards = []
    for i, v in enumerate(videos, 1):
        sn = v.get("snippet", {})
        st = v.get("statistics", {})
        a = v.get("video_analysis", {}) or {}
        t = v.get("transcript_audit", {}) or {}
        claims = a.get("claims", [])[:12]
        reasons = "; ".join(a.get("policy_reasons", [])) or "No major risk signal detected."
        transcript_line = (
            f"<b>{html.escape(str(t.get('status','NOT_CHECKED')))}</b> · "
            f"{t.get('word_count','—')} words · {t.get('segment_count','—')} segments · "
            f"{html.escape(str(t.get('language_code') or 'unknown'))} · "
            f"{'generated' if t.get('is_generated') else 'manual/unknown'}"
        )
        claim_html = "".join(
            f"<li>{html.escape(str(c.get('claim') or c.get('summary') or ''))} — "
            f"<b>{html.escape(str(c.get('status','')))}</b></li>"
            for c in claims
        ) or "<li>No transcript/title claim pattern extracted.</li>"
        blind = "".join(f"<li>{html.escape(str(x))}</li>" for x in a.get("blind_spots", []))
        cards.append(
            f"<details class='video-card'><summary><strong>#{i}</strong> {html.escape(str(sn.get('title','')))}</summary>"
            f"<div class='grid'>"
            f"<div><b>Published</b><br>{html.escape(str(sn.get('publishedAt','')))}</div>"
            f"<div><b>Views</b><br>{html.escape(str(st.get('viewCount','—')))}</div>"
            f"<div><b>Likes</b><br>{html.escape(str(st.get('likeCount','—')))}</div>"
            f"<div><b>Comments</b><br>{html.escape(str(st.get('commentCount','—')))}</div>"
            f"<div><b>Type</b><br>{html.escape(str(v.get('content_type','')))}</div>"
            f"<div><b>Overall beginner rating</b><br><span class='score'>{a.get('overall_beginner_rating','—')}/10</span></div>"
            f"</div>"
            f"<p><b>Decision:</b> {html.escape(str(a.get('decision','')))} · "
            f"<b>Learning mode:</b> {html.escape(str(a.get('learning_mode','')))} · "
            f"<b>Confidence:</b> {html.escape(str(a.get('confidence','')))}</p>"
            f"<p><b>Scores:</b> usefulness {a.get('practical_usefulness','—')}/10 · "
            f"evidence discipline {a.get('evidence_discipline','—')}/10 · "
            f"beginner accessibility {a.get('beginner_accessibility','—')}/10 · "
            f"repeatability {a.get('repeatability','—')}/10 · "
            f"originality safety {a.get('originality_safety','—')}/10 · "
            f"policy safety {a.get('policy_safety','—')}/10 · "
            f"<b>policy risk {html.escape(str(a.get('policy_risk','')))}</b></p>"
            f"<p><b>Transcript audit:</b> {transcript_line}</p>"
            f"<p><b>Transcript SHA-256:</b> {html.escape(str(t.get('transcript_sha256') or '—'))}</p>"
            f"<div class='two'><section><h3>Claims</h3><ul>{claim_html}</ul></section>"
            f"<section><h3>Policy / safety</h3><p>{html.escape(reasons)}</p>"
            f"<p><b>Claim-evidence gap:</b> {a.get('claim_evidence_gap','—')}</p></section></div>"
            f"<div class='two'><section><h3>Workflow reconstruction</h3><pre>{html.escape(json.dumps(a.get('workflow',{}),ensure_ascii=False,indent=2))}</pre></section>"
            f"<section><h3>Blind spots</h3><ul>{blind}</ul><h3>Beginner takeaway</h3><p>{html.escape(str(a.get('beginner_takeaway','')))}</p></section></div>"
            f"<p><b>Tools:</b> {html.escape(', '.join(a.get('tools',[])) or 'none detected')}</p>"
            f"<p><a href='https://www.youtube.com/watch?v={html.escape(str(v.get('id')))}' target='_blank' rel='noopener'>Open video</a></p>"
            f"</details>"
        )

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — Evidence-Bounded Deep Audit</title>
<style>
:root {{ font-family: system-ui,-apple-system,Segoe UI,sans-serif; color-scheme: light; --bg:#f5f7fb; --card:#fff; --ink:#152033; --muted:#5d6878; --line:#dbe2ec; }}
:root[data-theme="dark"] {{ color-scheme: dark; --bg:#0e131b; --card:#151c26; --ink:#eef3f8; --muted:#a9b5c6; --line:#293444; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); line-height:1.5; }}
main {{ max-width:1400px; margin:auto; padding:28px; }}
header,section,details {{ background:var(--card); border:1px solid var(--line); border-radius:16px; }}
header {{ padding:24px; margin-bottom:16px; }}
.grid {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin:16px 0; }}
.grid>div {{ padding:10px; border:1px solid var(--line); border-radius:10px; }}
.two {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
.two>section {{ padding:14px; }}
.video-card {{ margin:12px 0; padding:0 16px 16px; }}
.video-card summary {{ cursor:pointer; padding:16px 0; font-size:1.04rem; }}
pre {{ white-space:pre-wrap; overflow:auto; }}
.score {{ font-size:1.3rem; font-weight:800; }}
.toolbar {{ display:flex; gap:8px; flex-wrap:wrap; margin:12px 0; }}
input,button {{ padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:var(--card); color:var(--ink); }}
button {{ cursor:pointer; }}
.small {{ color:var(--muted); font-size:.93rem; }}
.badge {{ display:inline-block; padding:4px 8px; border:1px solid var(--line); border-radius:999px; margin-right:6px; }}
@media(max-width:900px) {{ .grid {{ grid-template-columns:repeat(2,1fr); }} .two {{ grid-template-columns:1fr; }} }}
@media print {{ .toolbar, a {{ display:none!important; }} details {{ break-inside:avoid; }} body {{ background:#fff; }} }}
</style>
</head>
<body>
<main>
<header>
<h1>{html.escape(title)} — Evidence-Bounded Deep Audit</h1>
<p class="small">Every video is scored separately. Transcript availability is never treated as proof of truth; financial, growth and time-to-result claims remain creator-reported until independently verified.</p>
<div class="toolbar"><input id="search" placeholder="Filter videos by title, risk, decision or claim…"><button id="theme">Toggle theme</button><button onclick="window.print()">Print</button></div>
<div class="badge">Videos: {len(videos)}</div>
<div class="badge">Full transcripts: {summary.get('full_transcript_count','—')}</div>
<div class="badge">Transcript coverage: {summary.get('coverage_percent','—')}%</div>
<div class="badge">Full transcript coverage: {summary.get('full_coverage_percent','—')}%</div>
</header>
<section style="padding:16px;margin-bottom:16px">
<h2>Transcript coverage</h2>
<pre>{html.escape(json.dumps(summary,ensure_ascii=False,indent=2))}</pre>
</section>
<section style="padding:16px">
<h2>Individual video audits</h2>
{''.join(cards)}
</section>
</main>
<script>
const root=document.documentElement;
const saved=localStorage.getItem('ytAuditTheme');
if(saved) root.setAttribute('data-theme',saved);
document.getElementById('theme').onclick=()=>{{
  const n=root.getAttribute('data-theme')==='dark'?'light':'dark';
  root.setAttribute('data-theme',n);
  localStorage.setItem('ytAuditTheme',n);
}};
document.getElementById('search').oninput=()=>{{
  const n=document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.video-card').forEach(c=>c.hidden=!c.innerText.toLowerCase().includes(n));
}};
</script>
</body>
</html>"""
    path.write_text(doc, encoding="utf-8")

def write_artifacts(out: Path, report: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    atomic_write(out / "audit.json", report)
    schema_source = Path(__file__).resolve().parents[1] / "schema.json"
    if schema_source.exists():
        (out / "schema.json").write_text(schema_source.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        raise RuntimeError("Canonical schema.json is missing from the repository.")

    (out / "config.yaml").write_text(
        "version: 11.3-production\nmode: DEEP\npublic_only: true\n"
        "credential_source: GITHUB_ACTIONS:YOUTUBE_API_KEY\n"
        "transcript_engine: youtube-transcript-api+public-ui\n"
        "transcript_full_text_persisted: false\n",
        encoding="utf-8",
    )

    csv_write(
        out / "video_inventory.csv",
        [
            {
                "video_id": v.get("id"),
                "title": v.get("snippet", {}).get("title"),
                "published_at": v.get("snippet", {}).get("publishedAt"),
                "views": v.get("statistics", {}).get("viewCount"),
                "likes": v.get("statistics", {}).get("likeCount"),
                "comments": v.get("statistics", {}).get("commentCount"),
                "duration": v.get("contentDetails", {}).get("duration"),
                "content_type": v.get("content_type"),
                "transcript_status": v.get("transcript_status"),
                "transcript_source": (v.get("transcript_audit") or {}).get("source"),
                "overall_beginner_rating": (v.get("video_analysis") or {}).get("overall_beginner_rating"),
                "audit_confidence": (v.get("video_analysis") or {}).get("confidence"),
            }
            for v in report.get("videos", [])
        ],
        [
            "video_id","title","published_at","views","likes","comments","duration",
            "content_type","transcript_status","transcript_source","overall_beginner_rating",
            "audit_confidence",
        ],
    )
    csv_write(
        out / "comments_coverage.csv",
        report.get("comments_summary", []),
        ["video_id","top_level_threads","replies_collected","complete","reply_pages","coverage"],
    )
    csv_write(out / "comments.csv", comments, ["video_id","kind","comment_id","text","published_at"])
    csv_write(out / "sources.csv", report.get("sources", []), ["source_id","url","role","captured_at"])
    csv_write(
        out / "video_analysis.csv",
        [
            {
                "video_id": v.get("id"),
                "title": v.get("snippet", {}).get("title"),
                "decision": (v.get("video_analysis") or {}).get("decision"),
                "overall_beginner_rating": (v.get("video_analysis") or {}).get("overall_beginner_rating"),
                "usefulness": (v.get("video_analysis") or {}).get("practical_usefulness"),
                "evidence_discipline": (v.get("video_analysis") or {}).get("evidence_discipline"),
                "beginner_accessibility": (v.get("video_analysis") or {}).get("beginner_accessibility"),
                "repeatability": (v.get("video_analysis") or {}).get("repeatability"),
                "originality_safety": (v.get("video_analysis") or {}).get("originality_safety"),
                "policy_safety": (v.get("video_analysis") or {}).get("policy_safety"),
                "policy_risk": (v.get("video_analysis") or {}).get("policy_risk"),
                "confidence": (v.get("video_analysis") or {}).get("confidence"),
                "claim_evidence_gap": (v.get("video_analysis") or {}).get("claim_evidence_gap"),
                "transcript_status": v.get("transcript_status"),
                "transcript_word_count": (v.get("transcript_audit") or {}).get("word_count"),
            }
            for v in report.get("videos", [])
        ],
        [
            "video_id","title","decision","overall_beginner_rating","usefulness",
            "evidence_discipline","beginner_accessibility","repeatability","originality_safety",
            "policy_safety","policy_risk","confidence","claim_evidence_gap","transcript_status",
            "transcript_word_count",
        ],
    )

    lines = [
        "# Video-by-Video Evidence-Bounded Analysis",
        "",
        f"Videos analyzed: {len(report.get('videos', []))}",
        f"Full transcripts: {report.get('transcript_audit', {}).get('full_transcript_count', 0)}",
        "",
    ]
    for i, v in enumerate(report.get("videos", []), 1):
        a = v.get("video_analysis", {}) or {}
        t = v.get("transcript_audit", {}) or {}
        lines += [
            f"## {i}. {v.get('snippet', {}).get('title', '')}",
            f"- Video ID: {v.get('id')}",
            f"- Views: {v.get('statistics', {}).get('viewCount')}",
            f"- Decision: {a.get('decision')}",
            f"- Overall beginner rating: {a.get('overall_beginner_rating')}/10",
            f"- Confidence: {a.get('confidence')}",
            f"- Evidence grade: {a.get('evidence_grade')}",
            f"- Transcript: {t.get('status')} · {t.get('word_count','—')} words · source {t.get('source') or 'none'}",
            f"- Scores: usefulness {a.get('practical_usefulness')}/10; evidence discipline {a.get('evidence_discipline')}/10; beginner accessibility {a.get('beginner_accessibility')}/10; repeatability {a.get('repeatability')}/10; originality safety {a.get('originality_safety')}/10; policy safety {a.get('policy_safety')}/10; policy risk {a.get('policy_risk')}",
            f"- Claim-evidence gap: {a.get('claim_evidence_gap')}",
            f"- Claim treatment: {json.dumps(a.get('claims', []), ensure_ascii=False)}",
            f"- Workflow: {json.dumps(a.get('workflow', {}), ensure_ascii=False)}",
            f"- Blind spots: {'; '.join(a.get('blind_spots', []))}",
            f"- Beginner takeaway: {a.get('beginner_takeaway')}",
            "",
        ]
    (out / "video_analysis.md").write_text("\n".join(lines), encoding="utf-8")
    render_html(report, out / "audit.html")

    manifest = {"schema_version": "11.3.0", "generated_at": utc_now(), "files": {}}
    for p in sorted(out.iterdir()):
        if p.is_file() and p.name not in {"release_manifest.json", "checkpoint.json"}:
            manifest["files"][p.name] = sha256_file(p)
    atomic_write(out / "release_manifest.json", manifest)

def csv_write(path: Path, rows: list[dict[str,Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)


def run(args: argparse.Namespace) -> int:
    skill = Path(args.skill)
    if not skill.exists() or skill.stat().st_size == 0:
        print(json.dumps({"status": "FAIL", "reason": "SKILL_FILE_UNREADABLE", "api_key_exposed": False}))
        return 2

    key = os.getenv("YOUTUBE_API_KEY")
    if not key or not plausible_key(key):
        print(json.dumps({"status": "FAIL", "reason": "API_KEY_MISSING_OR_INVALID", "api_key_exposed": False}))
        return 2

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint(out / "checkpoint.json")
    quota = Quota(checkpoint, args.quota_budget)
    api = API(key, quota, args.retries)
    started = checkpoint.get("started_at") or utc_now()

    checkpoint.set("started_at", started)
    checkpoint.set("channel_input", args.channel)
    checkpoint.set("credential_present", True)
    checkpoint.set("credential_exposed", False)
    checkpoint.set("skill_sha256", sha256_file(skill))

    channel, identity = resolve_channel(api, args.channel)
    if not channel:
        report = {
            "metadata": {
                "audit_id": "blocked",
                "methodology_version": "11.3-production",
                "schema_version": "11.3.0",
                "started_at": started,
                "completed_at": utc_now(),
                "canonical_channel_url": args.channel,
                "input_contract": {
                    "skill_file_present": True,
                    "channel_url_present": True,
                    "api_key_present": True,
                    "api_key_exposed": False,
                },
            },
            "channel": {},
            "coverage": {"status": "BLOCKED", "identity_resolution": identity},
            "access_matrix": [],
            "snapshots": [],
            "videos": [],
            "posts": [],
            "comments_summary": [],
            "playlists": [],
            "channel_sections": [],
            "transcripts": [],
            "transcript_audit": transcript_summary({}, 0),
            "sources": source_register(),
            "claims": [],
            "metrics": [],
            "calculations": [],
            "risks": [],
            "hypotheses": [],
            "experiments": [],
            "recommendations": [],
            "benchmarks": [],
            "knowledge_gaps": [],
            "deltas": [],
            "policy_checks": [],
            "decision_queue": {},
            "validation": {"status": "BLOCKED"},
            "executive_summary": {},
            "beginner_plan": beginner_plan(),
            "analysis": {},
            "reproducibility": {"quota": quota.summary(), "credential_present": True, "credential_exposed": False},
            "self_audit": {"api_key_exposed": False, "limitations": [identity]},
        }
        write_artifacts(out, report, [])
        return 3

    channel_id = channel["id"]
    canonical = f"https://www.youtube.com/channel/{channel_id}"
    checkpoint.set("channel_id", channel_id)
    checkpoint.set("channel", channel)
    uploads = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads:
        return 4

    inventory, inv_cov = paginate(
        api, checkpoint, "inventory", "playlistItems.list",
        {"part": "snippet,contentDetails,status", "playlistId": uploads, "maxResults": 50},
        args.max_pages,
    )
    video_ids = list(dict.fromkeys(
        [x.get("contentDetails", {}).get("videoId") for x in inventory if x.get("contentDetails", {}).get("videoId")]
    ))
    checkpoint.set("video_ids", video_ids)

    videos = checkpoint.get("videos", [])
    by_id = {v.get("id"): v for v in videos}
    missing = [x for x in video_ids if x not in by_id]
    parts = "id,snippet,contentDetails,statistics,status,topicDetails,recordingDetails,liveStreamingDetails,localizations,paidProductPlacementDetails,brandPartner"
    for i in range(0, len(missing), 50):
        group = missing[i:i + 50]
        res = api.get("videos.list", {"part": parts, "id": ",".join(group)})
        if not res.get("ok"):
            break
        for v in (res.get("data") or {}).get("items", []):
            by_id[v.get("id")] = v
        checkpoint.set("videos", list(by_id.values()))
    videos = list(by_id.values())

    playlists, playlist_cov = paginate(
        api, checkpoint, "playlists", "playlists.list",
        {"part": "snippet,contentDetails,status", "channelId": channel_id, "maxResults": 50},
        args.max_pages,
    )
    sections, section_cov = paginate(
        api, checkpoint, "sections", "channelSections.list",
        {"part": "snippet,contentDetails", "channelId": channel_id},
        args.max_pages,
    )

    # -------- Transcript engine: primary public transcript fetch + browser fallback --------
    transcript_cache = checkpoint.get("transcript_audits", {})
    if not isinstance(transcript_cache, dict):
        transcript_cache = {}

    transcript_target_ids = video_ids[:max(0, args.transcript_limit)]
    for vid in transcript_target_ids:
        record = transcript_cache.get(vid)
        if not record or record.get("status") in {"TRANSCRIPT_FETCH_ERROR", "TRANSCRIPT_REQUEST_BLOCKED", "NETWORK_ERROR"}:
            transcript_cache[vid] = audit_video_transcript(vid)
            checkpoint.set("transcript_audits", transcript_cache)
            time.sleep(0.25)

    browser_transcript_ids = [
        vid for vid in transcript_target_ids
        if transcript_cache.get(vid, {}).get("status") != "FULL_TRANSCRIPT_AVAILABLE"
    ]
    browser = (
        {
            "status": "SKIPPED",
            "shorts": [],
            "posts": [],
            "transcripts": [],
            "surface_checks": [],
            "limitations": ["Browser disabled."],
        }
        if args.skip_browser
        else browser_collect(
            canonical,
            video_ids,
            0,
            args.max_scrolls,
            transcript_ids=browser_transcript_ids,
        )
    )

    for fallback in browser.get("transcripts", []):
        vid = fallback.get("video_id")
        if not vid:
            continue
        transcript_cache[vid] = merge_transcript_records(transcript_cache.get(vid), fallback)

    checkpoint.set("transcript_audits", transcript_cache)

    short_ids = {x.get("video_id") for x in browser.get("shorts", [])}
    for v in videos:
        ctype, cmethod = classify_video(v, short_ids)
        desc = v.get("snippet", {}).get("description", "") or ""
        t = transcript_cache.get(v.get("id"), {"video_id": v.get("id"), "status": "NOT_ATTEMPTED", "text_retained": False})
        v["content_type"] = ctype
        v["classification_method"] = cmethod
        v["transcript_status"] = t.get("status", "NOT_CHECKED")
        v["transcript_audit"] = t
        v["_audit_hints"] = {"resources": resource_hints(desc), "keywords": keyword_hints(desc)}

    # -------- Exhaustive public comments --------
    comments_summary = checkpoint.get("comments_summary", [])
    comments = checkpoint.get("comments", [])
    completed = {
        x.get("video_id")
        for x in comments_summary
        if x.get("complete") or x.get("status") in {"COMMENTS_DISABLED", "UNAVAILABLE"}
    }
    if not args.skip_comments:
        for vid in video_ids:
            if vid in completed:
                continue
            threads, tcov = paginate(
                api, checkpoint, f"comments.{vid}.threads", "commentThreads.list",
                {"part": "id,snippet,replies", "videoId": vid, "maxResults": 100, "order": "time", "textFormat": "plainText"},
                args.comment_max_pages,
            )
            if tcov.get("stop_reason") == "COMMENTS_DISABLED":
                comments_summary.append({"video_id": vid, "status": "COMMENTS_DISABLED", "complete": True, "coverage": tcov})
                checkpoint.set("comments_summary", comments_summary)
                continue

            reply_map = {}
            replies_complete = True
            reply_pages = 0
            for thread in threads:
                top = (thread.get("snippet") or {}).get("topLevelComment") or {}
                parent = top.get("id")
                inline = (thread.get("replies") or {}).get("comments", [])
                for r in inline:
                    if r.get("id"):
                        reply_map[r["id"]] = r
                total = int((thread.get("snippet") or {}).get("totalReplyCount") or 0)
                if parent and total > len(inline):
                    reps, rcov = paginate(
                        api, checkpoint, f"comments.{vid}.replies.{parent}", "comments.list",
                        {"part": "id,snippet", "parentId": parent, "maxResults": 100, "textFormat": "plainText"},
                        args.comment_max_pages,
                    )
                    reply_pages += rcov["pages"]
                    replies_complete = replies_complete and rcov["complete"]
                    for r in reps:
                        if r.get("id"):
                            reply_map[r["id"]] = r

            seen = {r.get("comment_id") for r in comments if r.get("comment_id")}
            for thread in threads:
                top = (thread.get("snippet") or {}).get("topLevelComment") or {}
                sn = top.get("snippet") or {}
                cid = top.get("id")
                if cid and cid not in seen:
                    comments.append({
                        "video_id": vid,
                        "kind": "top_level",
                        "comment_id": cid,
                        "text": sn.get("textOriginal") or sn.get("textDisplay"),
                        "published_at": sn.get("publishedAt"),
                    })
                    seen.add(cid)
            for r in reply_map.values():
                rid = r.get("id")
                rs = r.get("snippet") or {}
                if rid and rid not in seen:
                    comments.append({
                        "video_id": vid,
                        "kind": "reply",
                        "comment_id": rid,
                        "text": rs.get("textOriginal") or rs.get("textDisplay"),
                        "published_at": rs.get("publishedAt"),
                    })
                    seen.add(rid)

            comments_summary.append({
                "video_id": vid,
                "top_level_threads": len(threads),
                "replies_collected": len(reply_map),
                "reply_pages": reply_pages,
                "complete": tcov["complete"] and replies_complete,
                "coverage": tcov,
            })
            checkpoint.set("comments_summary", comments_summary)
            checkpoint.set("comments", comments)
            if quota.used >= quota.run_budget:
                break

    for v in videos:
        t = transcript_cache.get(v.get("id"), {"video_id": v.get("id"), "status": "NOT_ATTEMPTED", "text_retained": False})
        v["video_analysis"] = per_video_analysis(v, comments, t)

    policy_checks = live_policy_checks()

    captured = utc_now()
    transcript_report = transcript_summary(transcript_cache, len(video_ids))
    final_sources = source_register(video_ids, transcript_cache)
    report = {
        "metadata": {
            "audit_id": f"{channel_id}-{captured.replace(':','').replace('+00:00','Z')}",
            "methodology_version": "11.3-production",
            "schema_version": "11.3.0",
            "started_at": started,
            "completed_at": captured,
            "canonical_channel_url": canonical,
            "input_contract": {
                "skill_file_present": True,
                "channel_url_present": True,
                "api_key_present": True,
                "api_key_exposed": False,
            },
        },
        "channel": channel,
        "channel_id": channel_id,
        "coverage": {
            "inventory": inv_cov,
            "video_details": {"discovered": len(video_ids), "collected": len(videos), "complete": len(videos) == len(video_ids)},
            "playlists": playlist_cov,
            "channel_sections": section_cov,
            "public_comments": {
                "video_records": len(comments_summary),
                "complete_videos": sum(1 for x in comments_summary if x.get("complete")),
            },
            "browser": browser,
            "transcripts": transcript_report,
            "public_access_boundary": "Maximum legitimately accessible public/API/browser evidence observed during this run.",
        },
        "access_matrix": [
            {"surface": "uploads", "mode": "YOUTUBE_DATA_API", "status": "SUCCESS" if inv_cov.get("complete") else "PARTIAL"},
            {"surface": "video_details", "mode": "YOUTUBE_DATA_API", "status": "SUCCESS" if len(videos) == len(video_ids) else "PARTIAL"},
            {"surface": "playlists", "mode": "YOUTUBE_DATA_API", "status": "SUCCESS" if playlist_cov.get("complete") else "PARTIAL"},
            {"surface": "channel_sections", "mode": "YOUTUBE_DATA_API", "status": "SUCCESS" if section_cov.get("complete") else "PARTIAL"},
            {"surface": "comments", "mode": "YOUTUBE_DATA_API", "status": "PARTIAL_OR_COMPLETE_PER_VIDEO"},
            {"surface": "posts", "mode": "PUBLIC_BROWSER", "status": browser.get("status")},
            {"surface": "shorts", "mode": "PUBLIC_BROWSER", "status": browser.get("status")},
            {"surface": "transcripts", "mode": "PUBLIC_TRANSCRIPT + PUBLIC_BROWSER_FALLBACK", "status": "COMPLETE" if transcript_report.get("full_transcript_count") == len(video_ids) else "PARTIAL"},
            {"surface": "private_analytics", "mode": "OWNER_AUTH_REQUIRED", "status": "UNAVAILABLE"},
        ],
        "snapshots": [{
            "captured_at": captured,
            "inventory_count": len(video_ids),
            "video_count": len(videos),
            "quota": quota.summary(),
            "transcript_audit": transcript_report,
        }],
        "videos": sorted(videos, key=lambda x: x.get("snippet", {}).get("publishedAt") or "", reverse=True),
        "posts": browser.get("posts", []),
        "comments_summary": comments_summary,
        "playlists": playlists,
        "channel_sections": sections,
        "transcripts": list(transcript_cache.values()),
        "transcript_audit": transcript_report,
        "sources": final_sources,
        "claims": [],
        "metrics": [],
        "calculations": [],
        "risks": [],
        "hypotheses": [],
        "experiments": [],
        "recommendations": [],
        "benchmarks": [],
        "knowledge_gaps": [],
        "deltas": [],
        "policy_checks": policy_checks,
        "decision_queue": {},
        "validation": {
            "status": "COLLECTION_COMPLETE_TO_ACCESSIBLE_BOUNDARY"
            if inv_cov.get("complete") and len(videos) == len(video_ids)
            else "PARTIAL_TO_ACCESSIBLE_BOUNDARY"
        },
        "executive_summary": {},
        "beginner_plan": beginner_plan(),
        "analysis": {
            "performance": performance_metrics(videos),
            "policy_baseline": policy_checks,
            "comment_keywords": comment_keyword_summary(comments),
            "transcript_audit": transcript_report,
        },
        "reproducibility": {
            "collector_version": "11.3-production",
            "transcript_engine": "youtube-transcript-api 1.2.x + public transcript UI fallback",
            "skill_sha256": sha256_file(skill),
            "credential_present": True,
            "credential_exposed": False,
            "credential_fingerprint": credential_fingerprint(key),
            "quota": quota.summary(),
            "api_request_count": api.request_count,
            "full_transcript_text_persisted": False,
        },
        "self_audit": {
            "api_key_exposed": False,
            "inventory_complete": inv_cov.get("complete"),
            "video_details_complete": len(videos) == len(video_ids),
            "comment_complete_videos": sum(1 for x in comments_summary if x.get("complete")),
            "transcript_attempted_videos": transcript_report.get("attempted_video_count"),
            "full_transcript_videos": transcript_report.get("full_transcript_count"),
            "transcript_full_text_persisted": False,
            "limitations": browser.get("limitations", []) + (
                ["Transcript coverage incomplete; affected videos remain low-confidence."]
                if transcript_report.get("full_transcript_count") != len(video_ids) else []
            ),
        },
    }

    # Build top-level claim registry after all video analyses exist.
    report["claims"] = build_claim_registry(report["videos"])
    write_artifacts(out, report, comments)
    checkpoint.set("completed_at", captured)
    checkpoint.set("status", "COMPLETED" if inv_cov.get("complete") else "PARTIAL")

    print(json.dumps({
        "status": "SUCCESS",
        "channel_id": channel_id,
        "videos": len(videos),
        "posts": len(report["posts"]),
        "comments": len(comments),
        "full_transcripts": transcript_report.get("full_transcript_count"),
        "transcript_attempted": transcript_report.get("attempted_video_count"),
        "quota": quota.summary(),
        "api_key_exposed": False,
    }, indent=2))
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
