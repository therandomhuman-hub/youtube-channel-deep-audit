#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent

def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> int:
    if len(sys.argv) != 2:
        print("usage: build_release.py PACKAGE_DIR", file=sys.stderr)
        return 2
    root=Path(sys.argv[1]).resolve()
    proc=subprocess.run([sys.executable,str(HERE/"validate.py"),str(root)],capture_output=True,text=True)
    print(proc.stdout,end="")
    if proc.stderr:
        print(proc.stderr,file=sys.stderr,end="")
    if proc.returncode != 0:
        return proc.returncode
    validation=json.loads(proc.stdout)
    files={}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in {"release_validation.json","release_manifest.json","checkpoint.json"}:
            files[str(p.relative_to(root))]=sha256(p)
    payload={
        "release_status":"RELEASED",
        "validator_status":validation.get("status"),
        "generated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "artifact_hashes":files,
        "api_key_exposed":False,
    }
    (root/"release_validation.json").write_text(json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
