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

    required_fields=["metadata","channel","coverage","access_matrix","snapshots","videos","posts","comments_summary","playlists","channel_sections","transcripts","sources","claims","metrics","calculations","risks","hypotheses","experiments","recommendations","benchmarks","knowledge_gaps","deltas","policy_checks","decision_queue","validation","executive_summary","beginner_plan","analysis","reproducibility","self_audit"]
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
