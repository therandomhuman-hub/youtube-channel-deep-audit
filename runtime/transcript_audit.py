#!/usr/bin/env python3
"""Transcript retrieval and evidence-bounded per-video learning audit.

This module never persists the full transcript. It keeps only compact metadata,
hashes, bounded evidence excerpts, claim categories, and analysis signals.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Iterable


DEFAULT_LANGUAGES = ("en", "en-US", "en-GB", "en-IN")
MAX_CLAIMS = 12
MAX_EXCERPT_WORDS = 20

FINANCIAL_RE = re.compile(
    r"(?:\$\s?[\d,.]+\s?(?:k|m|b)?|[\d,.]+\s?(?:thousand|million|billion)\s+dollars?)",
    re.I,
)
NUMERIC_RESULT_RE = re.compile(
    r"\b[\d,.]+\s*(?:k|m|b|million|billion)?\s*(?:views?|subscribers?|subs?|followers?|downloads?|sales?|dollars?|revenue|members?)\b",
    re.I,
)
TIME_RESULT_RE = re.compile(
    r"\b(?:in|within|after|over|under)\s+\d+\s*(?:hours?|days?|weeks?|months?|years?)\b",
    re.I,
)
RESULT_VERB_RE = re.compile(
    r"\b(?:i|we)\s+(?:made|earned|generated|got|reached|grew|monetized|sold|built|hit|generated)\b",
    re.I,
)
PROOF_RE = re.compile(
    r"\b(?:dashboard|analytics|screenshot|proof|receipts?|statement|date range|shown on screen|show you|evidence|experiment|tested|test results?|source|sources|terms|policy)\b",
    re.I,
)
PROCESS_RE = re.compile(
    r"\b(?:step\s+\d+|first|second|third|then|next|finally|workflow|process|system|setup|settings?|prompt|template|checklist|install|click|upload|publish|measure|track)\b",
    re.I,
)
REPLICATION_RISK_RE = re.compile(
    r"\b(?:copy|copies|copied|borrow|borrowed|reupload|re-upload|scrape|scraped|steal|stolen|download .*clips?|same video|same content|change .*slightly|bypass|evade|beat the system|spam)\b",
    re.I,
)
MASS_PRODUCTION_RE = re.compile(
    r"\b(?:mass[- ]?produce|mass[- ]?production|automate everything|multiple channels|100 videos|50 videos|daily uploads?|3-5 videos? a day|3 to 5 videos? a day|hundreds of videos?)\b",
    re.I,
)
AI_RE = re.compile(r"\b(?:ai|chatgpt|claude|gemini|veo|sora|midjourney|runway|capcut|elevenlabs)\b", re.I)
JARGON_RE = re.compile(
    r"\b(?:ctr|cpm|rpm|retention|seo|ypp|api|hook rate|outlier|cvr|arpu|lifetime value|affiliate|arbitrage)\b",
    re.I,
)


def _normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", text, re.UNICODE)


def _excerpt(text: str, max_words: int = MAX_EXCERPT_WORDS) -> str:
    return " ".join(_words(text)[:max_words])


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", _normalize(text))
    return [p.strip() for p in parts if p.strip()]


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:240]}"


def _exception_status(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "transcriptsdisabled" in name or "subtitles are disabled" in msg:
        return "TRANSCRIPTS_DISABLED"
    if "notranscript" in name or "no transcript" in msg:
        return "NO_TRANSCRIPT_FOUND"
    if "videounavailable" in name or "video unavailable" in msg:
        return "VIDEO_UNAVAILABLE"
    if "requestblocked" in name or "blocked" in msg or "ip" in name and "block" in msg:
        return "TRANSCRIPT_REQUEST_BLOCKED"
    if "age" in msg or "sign in" in msg:
        return "AUTHENTICATION_OR_AGE_RESTRICTED"
    return "TRANSCRIPT_FETCH_ERROR"


def _candidate_claims(text: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for sentence in _sentences(text):
        low = sentence.lower()
        categories: list[str] = []
        if FINANCIAL_RE.search(sentence):
            categories.append("REVENUE_OR_FINANCIAL")
        if NUMERIC_RESULT_RE.search(sentence):
            categories.append("QUANTIFIED_RESULT")
        if TIME_RESULT_RE.search(sentence):
            categories.append("TIME_TO_RESULT")
        if RESULT_VERB_RE.search(sentence):
            categories.append("CREATOR_RESULT_REPORT")
        if not categories:
            continue

        if REPLICATION_RISK_RE.search(sentence):
            categories.append("REPLICATION_OR_POLICY_RISK")
        summary = "Creator reports a " + ", ".join(
            x.replace("_", " ").lower() for x in categories if x != "CREATOR_RESULT_REPORT"
        )
        if "CREATOR_RESULT_REPORT" in categories and len(categories) == 1:
            summary = "Creator reports a personal result or outcome."
        if any(c.get("summary") == summary for c in claims):
            continue
        claims.append(
            {
                "summary": summary,
                "categories": categories,
                "status": "CREATOR_REPORTED",
                "verification": "Creator statement detected in transcript; independent verification required.",
                "evidence_excerpt": _excerpt(sentence),
            }
        )
        if len(claims) >= MAX_CLAIMS:
            break
    return claims


def make_transcript_record(
    video_id: str,
    snippets: Iterable[dict[str, Any]],
    *,
    source: str,
    language: str | None = None,
    language_code: str | None = None,
    is_generated: bool | None = None,
) -> dict[str, Any]:
    raw_texts = []
    duration = 0.0
    count = 0
    for s in snippets:
        if isinstance(s, dict):
            text = _normalize(str(s.get("text", "")))
            if text:
                raw_texts.append(text)
            try:
                duration = max(duration, float(s.get("start", 0.0) or 0.0) + float(s.get("duration", 0.0) or 0.0))
            except Exception:
                pass
            count += 1
        else:
            text = _normalize(getattr(s, "text", ""))
            if text:
                raw_texts.append(text)
            try:
                duration = max(
                    duration,
                    float(getattr(s, "start", 0.0) or 0.0)
                    + float(getattr(s, "duration", 0.0) or 0.0),
                )
            except Exception:
                pass
            count += 1

    full_text = _normalize(" ".join(raw_texts))
    words = _words(full_text)
    claims = _candidate_claims(full_text)
    quantified = sum(
        1 for c in claims if any(x in c.get("categories", []) for x in ("REVENUE_OR_FINANCIAL", "QUANTIFIED_RESULT", "TIME_TO_RESULT"))
    )
    proof_hits = len(PROOF_RE.findall(full_text))
    process_hits = len(PROCESS_RE.findall(full_text))
    replication_hits = len(REPLICATION_RISK_RE.findall(full_text))
    mass_hits = len(MASS_PRODUCTION_RE.findall(full_text))
    jargon_hits = len(JARGON_RE.findall(full_text))
    ai_hits = len(AI_RE.findall(full_text))

    return {
        "video_id": video_id,
        "status": "FULL_TRANSCRIPT_AVAILABLE" if count else "TRANSCRIPT_PANEL_NO_SEGMENTS",
        "source": source,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "language": language,
        "language_code": language_code,
        "is_generated": is_generated,
        "segment_count": count,
        "word_count": len(words),
        "duration_seconds": round(duration, 2) if duration else None,
        "transcript_sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest() if full_text else None,
        "text_retained": False,
        "bounded_excerpt": _excerpt(full_text),
        "claim_count": len(claims),
        "quantified_claim_count": quantified,
        "proof_signal_count": proof_hits,
        "process_signal_count": process_hits,
        "replication_risk_signal_count": replication_hits,
        "mass_production_signal_count": mass_hits,
        "ai_signal_count": ai_hits,
        "jargon_signal_count": jargon_hits,
        "claims": claims,
    }


def audit_video_transcript(video_id: str, languages: Iterable[str] = DEFAULT_LANGUAGES) -> dict[str, Any]:
    base = {
        "video_id": video_id,
        "status": "NOT_ATTEMPTED",
        "source": None,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "text_retained": False,
    }
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception as exc:
        base.update({"status": "DEPENDENCY_UNAVAILABLE", "error": _safe_error(exc)})
        return base

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        chosen = None
        preferred = [x.lower() for x in languages]

        available = []
        for transcript in transcript_list:
            available.append(
                {
                    "language": getattr(transcript, "language", None),
                    "language_code": getattr(transcript, "language_code", None),
                    "is_generated": getattr(transcript, "is_generated", None),
                }
            )
            if getattr(transcript, "language_code", "").lower() in preferred:
                chosen = transcript
                if not getattr(transcript, "is_generated", False):
                    break

        if chosen is None:
            # Fall back to the first publicly enumerated transcript rather than
            # falsely reporting "none" merely because it is not English.
            chosen = next(iter(transcript_list), None)

        if chosen is None:
            base.update({"status": "NO_TRANSCRIPT_FOUND", "available_tracks": available})
            return base

        fetched = chosen.fetch()
        record = make_transcript_record(
            video_id,
            fetched,
            source="YOUTUBE_TRANSCRIPT_API_PUBLIC",
            language=getattr(chosen, "language", None),
            language_code=getattr(chosen, "language_code", None),
            is_generated=getattr(chosen, "is_generated", None),
        )
        record["available_tracks"] = available
        return record
    except Exception as exc:
        base.update({"status": _exception_status(exc), "error": _safe_error(exc)})
        return base


def audit_learning(
    *,
    title: str,
    description: str,
    transcript: dict[str, Any],
) -> dict[str, Any]:
    """Evidence-bounded beginner scoring from transcript signals + metadata.

    Scores measure learning value and safety, not whether the creator's claims
    are true. Numeric/financial claims remain creator-reported until independently
    verified elsewhere.
    """
    status = transcript.get("status", "")
    full = status == "FULL_TRANSCRIPT_AVAILABLE"
    words = int(transcript.get("word_count", 0) or 0)
    proof = int(transcript.get("proof_signal_count", 0) or 0)
    process = int(transcript.get("process_signal_count", 0) or 0)
    replication = int(transcript.get("replication_risk_signal_count", 0) or 0)
    mass = int(transcript.get("mass_production_signal_count", 0) or 0)
    jargon = int(transcript.get("jargon_signal_count", 0) or 0)
    quantified = int(transcript.get("quantified_claim_count", 0) or 0)
    claims = transcript.get("claims", []) or []

    if not full:
        return {
            "evidence_grade": "METADATA_OR_PARTIAL_ONLY",
            "confidence": "LOW",
            "practical_usefulness": 5,
            "evidence_discipline": 3,
            "beginner_accessibility": 5,
            "repeatability": 4,
            "originality_safety": 6 if replication == 0 else 3,
            "policy_safety": 3 if replication >= 2 or mass >= 2 else 6,
            "policy_risk": "UNKNOWN_OR_UNASSESSED" if status in {"NOT_ATTEMPTED", "DEPENDENCY_UNAVAILABLE"} else "MEDIUM",
            "overall_beginner_rating": 4.6,
            "decision": "RESEARCH MORE",
            "learning_mode": "DO_NOT_COPY_FROM_METADATA",
            "claim_evidence_gap": quantified + len(claims),
            "beginner_takeaway": "Do not treat title/description promises as instructions. Re-audit after obtaining the transcript.",
            "audit_basis": ["public title", "public description", "transcript unavailable or incomplete"],
        }

    usefulness = 6 + min(3, process // 12) + (1 if words >= 1000 else 0)
    usefulness = min(10, usefulness)

    evidence = 4 + min(4, proof // 3)
    if quantified and proof == 0:
        evidence -= 1
    evidence = max(2, min(10, evidence))

    accessibility = 7
    if jargon >= max(4, words // 250):
        accessibility -= 2
    if process >= 10:
        accessibility += 1
    if words < 600:
        accessibility -= 1
    accessibility = max(1, min(10, accessibility))

    repeatability = 5 + min(4, process // 10)
    if "step" in (title + " " + description).lower() or "system" in (title + " " + description).lower():
        repeatability += 1
    repeatability = min(10, repeatability)

    originality = 9
    if replication:
        originality -= min(5, 2 + replication)
    if mass:
        originality -= min(3, mass)
    originality = max(1, originality)

    policy_score = 10
    policy_risk = "Low"
    reasons = []
    if replication >= 2:
        policy_score -= 5
        policy_risk = "High"
        reasons.append("explicit reuse/copy/replication language detected in transcript")
    elif replication == 1:
        policy_score -= 3
        policy_risk = "Medium"
        reasons.append("replication/reuse language detected")
    if mass:
        policy_score -= min(4, mass)
        policy_risk = "High" if mass >= 2 else ("Medium" if policy_risk == "Low" else policy_risk)
        reasons.append("mass-production/automation language detected")
    if AI_RE.search(title + " " + description) and proof == 0:
        policy_score -= 1
        if policy_risk == "Low":
            policy_risk = "Medium"
        reasons.append("AI workflow claims need original/authentic implementation evidence")
    policy_score = max(1, policy_score)

    overall = round(
        usefulness * 0.24
        + evidence * 0.22
        + accessibility * 0.14
        + repeatability * 0.14
        + originality * 0.14
        + policy_score * 0.12,
        1,
    )

    if policy_risk == "High" and replication:
        decision = "AVOID"
        learning_mode = "STUDY_RISK_ONLY"
    elif policy_risk == "High":
        decision = "MODIFY"
        learning_mode = "LEARN_AND_REBUILD_ORIGINALLY"
    elif evidence < 5 or quantified >= 3 and proof == 0:
        decision = "TEST"
        learning_mode = "EXPERIMENT_WITH_VERIFICATION"
    elif overall >= 7.0:
        decision = "KEEP"
        learning_mode = "LEARN_AND_ADAPT"
    else:
        decision = "TEST"
        learning_mode = "EXPERIMENT_WITH_VERIFICATION"

    if reasons:
        safety_note = "; ".join(reasons)
    else:
        safety_note = "No major replication or mass-production signals detected in transcript."

    takeaway = (
        "Use the transcript to learn the problem, mechanism, steps and measurement logic; "
        "recreate the idea with original assets and verify every material claim."
    )
    if policy_risk == "High":
        takeaway = (
            "Do not copy the execution. Extract the underlying problem and turn it into an original, "
            "policy-safe experiment with explicit evidence."
        )

    return {
        "evidence_grade": "TRANSCRIPT_REVIEWED",
        "confidence": "HIGH",
        "practical_usefulness": usefulness,
        "evidence_discipline": evidence,
        "beginner_accessibility": accessibility,
        "repeatability": repeatability,
        "originality_safety": originality,
        "policy_safety": policy_score,
        "policy_risk": policy_risk,
        "policy_reasons": reasons,
        "overall_beginner_rating": overall,
        "decision": decision,
        "learning_mode": learning_mode,
        "claim_evidence_gap": quantified if proof else quantified + len(claims),
        "beginner_takeaway": takeaway,
        "audit_basis": [
            "public title",
            "public description",
            "full transcript analysis",
            "public performance counters",
        ],
        "safety_note": safety_note,
    }


def merge_transcript_records(primary: dict[str, Any] | None, fallback: dict[str, Any] | None) -> dict[str, Any]:
    """Prefer a successful primary record, otherwise use a browser-derived fallback."""
    if primary and primary.get("status") == "FULL_TRANSCRIPT_AVAILABLE":
        return primary
    if fallback and fallback.get("status") == "FULL_TRANSCRIPT_AVAILABLE":
        return fallback
    return primary or fallback or {"status": "NOT_CHECKED"}
