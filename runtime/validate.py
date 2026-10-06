#!/usr/bin/env python3
from __future__ import annotations

import argparse, csv, hashlib, json, math, re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator, FormatChecker
except Exception:
    Draft202012Validator = None
    FormatChecker = None

CLAIM_STATES={"VERIFIED","CREATOR_REPORTED","OBSERVED","INFERRED","UNVERIFIED","CONTRADICTED","WITHDRAWN"}
METRIC_STATES={"OBSERVED","DERIVED","ESTIMATED","REPORTED","UNAVAILABLE"}
SECRET_PATTERNS=[
    re.compile(r"(?i)AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?i)(?:api[_-]?key|key)\s*[=:]\s*[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._-]{20,}"),
]

class HTMLCheck(HTMLParser):
    VOID={"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}
    def __init__(self):
        super().__init__()
        self.stack=[]; self.html_lang=None; self.h1=0; self.external_scripts=0
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="html": self.html_lang=a.get("lang")
        if tag=="h1": self.h1+=1
        if tag=="script" and a.get("src"): self.external_scripts+=1
        if tag not in self.VOID: self.stack.append(tag)
    def handle_endtag(self,tag):
        if tag in self.VOID: return
        if tag in self.stack:
            i=len(self.stack)-1-self.stack[::-1].index(tag)
            self.stack=self.stack[:i]
    def handle_startendtag(self,tag,attrs):
        return

class VideoCardParser(HTMLParser):
    """Extract complete outer .video-card elements without regex-parsing nested HTML."""
    VOID={"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards=[]
        self._capture=None
    @staticmethod
    def _attrs(attrs):
        return {k:v for k,v in attrs if k}
    def handle_starttag(self,tag,attrs):
        a=self._attrs(attrs)
        if self._capture is not None:
            self._capture["html_parts"].append(self.get_starttag_text() or "")
        if tag=="details" and "video-card" in (a.get("class") or "").split():
            if self._capture is not None:
                self._capture["nested_details"] += 1
            else:
                self._capture={"video_id":a.get("data-video-id"),"html_parts":[self.get_starttag_text() or ""],"text_parts":[],"nested_details":0}
            return
        if self._capture is not None and tag=="details":
            self._capture["nested_details"] += 1
    def handle_data(self,data):
        if self._capture is not None:
            self._capture["html_parts"].append(data)
            self._capture["text_parts"].append(data)
    def handle_endtag(self,tag):
        if self._capture is None or tag in self.VOID:
            return
        self._capture["html_parts"].append(f"</{tag}>")
        if tag=="details":
            if self._capture["nested_details"]>0:
                self._capture["nested_details"]-=1
                return
            card=dict(self._capture)
            card["html"]="".join(card.pop("html_parts"))
            card["text"]=" ".join(card.pop("text_parts"))
            self.cards.append(card)
            self._capture=None

def parse_video_cards(doc:str)->list[dict[str,Any]]:
    parser=VideoCardParser()
    parser.feed(doc)
    parser.close()
    return parser.cards

def sha256(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""): h.update(c)
    return h.hexdigest()

def scan(text:str)->bool:
    return any(p.search(text) for p in SECRET_PATTERNS)

def validate(root:Path)->tuple[list[dict[str,Any]],list[dict[str,Any]]]:
    issues=[]; warnings=[]
    required=["audit.json","audit.html","video_inventory.csv","comments.csv","comments_coverage.csv","sources.csv","schema.json","config.yaml","release_manifest.json"]
    for name in required:
        if not (root/name).exists(): issues.append({"path":name,"message":"required artifact missing"})
    if issues: return issues,warnings
    try:
        report=json.loads((root/"audit.json").read_text(encoding="utf-8"))
        schema=json.loads((root/"schema.json").read_text(encoding="utf-8"))
    except Exception as e:
        return [{"path":"audit.json","message":"invalid JSON: "+str(e)}],warnings

    if Draft202012Validator:
        checker=Draft202012Validator(schema, format_checker=FormatChecker())
        for err in checker.iter_errors(report):
            path=".".join(str(x) for x in err.absolute_path) or "$"
            issues.append({"path":path,"message":"schema validation: "+err.message})
    else:
        warnings.append({"path":"schema","message":"jsonschema unavailable; semantic schema validation skipped"})

    required_fields=["metadata","channel","coverage","access_matrix","snapshots","videos","posts","comments_summary","playlists","channel_sections","transcripts","transcript_audit","sources","claims","metrics","calculations","risks","hypotheses","experiments","recommendations","benchmarks","knowledge_gaps","deltas","policy_checks","decision_queue","validation","executive_summary","beginner_plan","analysis","reproducibility","self_audit"]
    for f in required_fields:
        if f not in report: issues.append({"path":f,"message":"required report field missing"})
    if report.get("metadata",{}).get("input_contract",{}).get("api_key_exposed") is not False:
        issues.append({"path":"metadata.input_contract.api_key_exposed","message":"must be false"})
    if report.get("reproducibility",{}).get("credential_exposed") is not False:
        issues.append({"path":"reproducibility.credential_exposed","message":"must be false"})
    coverage=report.get("coverage",{})
    inventory_complete=bool(coverage.get("inventory",{}).get("complete"))
    details_complete=bool(coverage.get("video_details_complete", coverage.get("video_details",{}).get("complete")))
    status=report.get("validation",{}).get("status")
    if status=="COLLECTION_COMPLETE_TO_ACCESSIBLE_BOUNDARY" and not (inventory_complete and details_complete):
        issues.append({"path":"validation.status","message":"claims complete collection while inventory or video details are partial"})

    vids=[v.get("id") for v in report.get("videos",[]) if isinstance(v,dict)]
    transcript_audit=report.get("transcript_audit",{})
    target_count=transcript_audit.get("target_video_count")
    attempted_count=transcript_audit.get("attempted_video_count")
    if target_count not in (None,len(vids)):
        issues.append({"path":"transcript_audit.target_video_count","message":"does not match video count"})
    if target_count is not None and target_count == len(vids) and attempted_count != len(vids):
        issues.append({"path":"transcript_audit.attempted_video_count","message":"every discovered video requires a transcript/caption attempt"})
    full_count=transcript_audit.get("full_transcript_count")
    if isinstance(full_count,(int,float)) and (full_count<0 or full_count>len(vids)):
        issues.append({"path":"transcript_audit.full_transcript_count","message":"invalid full transcript count"})
    for i,v in enumerate(report.get("videos",[])):
        if not isinstance(v,dict):
            continue
        t=v.get("transcript_audit",{}) or {}
        if t.get("text_retained") is not False:
            issues.append({"path":f"videos[{i}].transcript_audit.text_retained","message":"full transcript text must not be persisted"})
        transcript_status=t.get("status") or v.get("transcript_status")
        allowed_transcript_statuses={
            "FULL_TRANSCRIPT_AVAILABLE","PARTIAL_TRANSCRIPT","NO_TRANSCRIPT_FOUND",
            "TRANSCRIPTS_DISABLED","TRANSCRIPT_REQUEST_BLOCKED",
            "AUTHENTICATION_OR_AGE_RESTRICTED","DEPENDENCY_UNAVAILABLE",
            "TRANSCRIPT_FETCH_ERROR","NOT_ATTEMPTED","TRANSCRIPT_PANEL_NO_SEGMENTS",
            "VIDEO_UNAVAILABLE"
        }
        if transcript_status not in allowed_transcript_statuses:
            issues.append({"path":f"videos[{i}].transcript_audit.status","message":"every discovered video requires an explicit transcript/caption attempt status"})
        if transcript_status == "NOT_ATTEMPTED":
            issues.append({"path":f"videos[{i}].transcript_audit.status","message":"transcript/caption attempt is mandatory for every discovered video"})
        a=v.get("video_analysis",{}) or {}
        if not a:
            issues.append({"path":f"videos[{i}].video_analysis","message":"every discovered video must have an individual audit"})
        original=a.get("original_evidence",{}) or {}
        audited=a.get("audited_findings", a.get("audited_evidence", {})) or {}
        guidance=a.get("beginner_guidance", a.get("professional_guidance", {})) or {}
        if not audited:
            issues.append({"path":f"videos[{i}].video_analysis.audited_findings","message":"visible AUDITED FINDINGS layer missing"})
        if not guidance:
            issues.append({"path":f"videos[{i}].video_analysis.beginner_guidance","message":"visible BEGINNER-FRIENDLY PROFESSIONAL GUIDANCE layer missing"})
        for field in ("video_id","url","title","description","evidence_boundary"):
            if field not in original:
                issues.append({"path":f"videos[{i}].video_analysis.original_evidence.{field}","message":"original public evidence field missing"})
        if a.get("decision") not in {"KEEP","TEST","MODIFY","AVOID","RESEARCH MORE"}:
            issues.append({"path":f"videos[{i}].video_analysis.decision","message":"invalid beginner decision"})
        if a.get("confidence") is None:
            issues.append({"path":f"videos[{i}].video_analysis.confidence","message":"per-video confidence missing"})
        rating=a.get("overall_beginner_rating")
        if rating is not None and (not isinstance(rating,(int,float)) or not 0<=rating<=10):
            issues.append({"path":f"videos[{i}].video_analysis.overall_beginner_rating","message":"must be between 0 and 10"})
        if a.get("confidence")=="HIGH" and t.get("status")!="FULL_TRANSCRIPT_AVAILABLE":
            issues.append({"path":f"videos[{i}].video_analysis.confidence","message":"HIGH confidence requires a full usable transcript"})
    if len(vids)!=len(set(vids)): issues.append({"path":"videos","message":"duplicate video IDs"})
    source_ids={s.get("source_id") for s in report.get("sources",[]) if isinstance(s,dict)}
    for i,c in enumerate(report.get("claims",[])):
        if c.get("status") not in CLAIM_STATES: issues.append({"path":f"claims[{i}]","message":"invalid claim status"})
        if c.get("status") not in {"UNVERIFIED","WITHDRAWN"} and not c.get("evidence_ids"):
            issues.append({"path":f"claims[{i}]","message":"claim lacks evidence_ids"})
        for eid in c.get("evidence_ids",[]):
            if eid not in source_ids: issues.append({"path":f"claims[{i}]","message":"unknown evidence id "+str(eid)})
    for i,m in enumerate(report.get("metrics",[])):
        if m.get("status") not in METRIC_STATES: issues.append({"path":f"metrics[{i}]","message":"invalid metric status"})
        v=m.get("value")
        if isinstance(v,float) and not math.isfinite(v): issues.append({"path":f"metrics[{i}]","message":"NaN/Infinity"})

    for p in root.rglob("*"):
        if not p.is_file() or p.name=="checkpoint.json": continue
        if p.suffix.lower() in {".json",".csv",".yaml",".yml",".html",".txt",".md",".ps1",".sh"}:
            txt=p.read_text(encoding="utf-8",errors="replace")
            if scan(txt): issues.append({"path":str(p.relative_to(root)),"message":"secret-like pattern detected"})

    doc=(root/"audit.html").read_text(encoding="utf-8",errors="replace")
    cards=parse_video_cards(doc)
    card_count=len(cards)
    if card_count != len(vids):
        issues.append({"path":"audit.html","message":f"video card count {card_count} does not match discovered video count {len(vids)}"})
    rendered_ids=[c.get("video_id") for c in cards]
    if any(not x for x in rendered_ids):
        issues.append({"path":"audit.html","message":"every video card must expose a non-empty data-video-id"})
    if len(rendered_ids)!=len(set(rendered_ids)):
        issues.append({"path":"audit.html","message":"duplicate video IDs rendered in HTML cards"})
    inventory_ids=[x for x in vids if x]
    if set(rendered_ids) != set(inventory_ids):
        missing=sorted(set(inventory_ids)-set(rendered_ids))
        extra=sorted(set(rendered_ids)-set(inventory_ids))
        issues.append({"path":"audit.html","message":f"HTML/inventory video ID mismatch; missing={missing[:20]} extra={extra[:20]}"})
    for i, card in enumerate(cards):
        plain=re.sub(r"<[^>]+>"," ",card.get("text","")).lower()
        for needle,label in (
            ("original public evidence","ORIGINAL PUBLIC EVIDENCE"),
            ("audited findings","AUDITED FINDINGS"),
            ("beginner-friendly professional guidance","BEGINNER-FRIENDLY PROFESSIONAL GUIDANCE"),
        ):
            if needle not in plain:
                issues.append({"path":f"video_cards[{i}]","message":f"mandatory visible layer missing: {label}"})
        if "transcript:" not in plain:
            issues.append({"path":f"video_cards[{i}]","message":"per-video transcript status is not visibly rendered"})
    hp=HTMLCheck(); hp.feed(doc)
    if not hp.html_lang: issues.append({"path":"audit.html","message":"missing html lang"})
    if hp.h1!=1: issues.append({"path":"audit.html","message":f"expected one H1, found {hp.h1}"})
    if hp.external_scripts: issues.append({"path":"audit.html","message":"external script source found"})
    if hp.stack: issues.append({"path":"audit.html","message":"unclosed HTML tags"})
    if "localStorage" not in doc: warnings.append({"path":"audit.html","message":"localStorage not detected"})
    if "window.print" not in doc: warnings.append({"path":"audit.html","message":"print control not detected"})

    with (root/"video_inventory.csv").open(encoding="utf-8") as f:
        rows=list(csv.DictReader(f))
    if len(rows)!=len(report.get("videos",[])): issues.append({"path":"video_inventory.csv","message":"row count mismatch"})
    with (root/"comments.csv").open(encoding="utf-8") as f:
        comment_rows=list(csv.DictReader(f))
    if any(not r.get("comment_id") for r in comment_rows): issues.append({"path":"comments.csv","message":"comment row without comment_id"})

    manifest=json.loads((root/"release_manifest.json").read_text(encoding="utf-8"))
    mutable={"checkpoint.json","collector.log","collector_exit_code.txt","validator_exit_code.txt","validation.json","release_status.txt","release_build.log","release_builder_exit_code.txt","channel_url.txt","request_body.txt"}
    for name,digest in manifest.get("files",{}).items():
        if name in mutable:
            continue
        p=root/name
        if not p.exists(): issues.append({"path":"release_manifest.json","message":"missing bound artifact "+name})
        elif sha256(p)!=digest: issues.append({"path":name,"message":"hash mismatch against release manifest"})
    if "credential_fingerprint" in json.dumps(report).lower():
        # Fingerprints are permitted only as non-reversible continuity metadata.
        pass
    return issues,warnings

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("package",type=Path)
    args=ap.parse_args()
    issues,warnings=validate(args.package)
    status="FAIL" if issues else ("WARN" if warnings else "PASS")
    print(json.dumps({"status":status,"issues":issues,"warnings":warnings},indent=2))
    return 1 if issues else 0

if __name__=="__main__":
    raise SystemExit(main())
