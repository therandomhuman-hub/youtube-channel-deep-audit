import argparse, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse
import requests

API = "https://www.googleapis.com/youtube/v3"

COSTS = {
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
    "commentThreads.list": 1,
    "comments.list": 1,
    "playlists.list": 1,
    "channelSections.list": 1,
    "search.list": 1,
}
SEARCH_CALL_LIMIT = 100

class Quota:
    def __init__(self, path):
        self.path=Path(path); self.data=json.loads(self.path.read_text()) if self.path.exists() else {"units":0,"search_calls":0}
    def charge(self, method):
        self.data["units"] += COSTS.get(method,1)
        if method=="search.list": self.data["search_calls"] += 1
        self.path.parent.mkdir(parents=True,exist_ok=True); self.path.write_text(json.dumps(self.data,indent=2))
        if self.data["search_calls"] > SEARCH_CALL_LIMIT: raise RuntimeError("search.list daily call budget exceeded")

def channel_id_from_url(url):
    p=urlparse(url)
    if p.scheme not in ("http","https") or not p.netloc: raise ValueError("Invalid channel URL")
    m=re.search(r"/channel/(UC[\w-]+)",p.path)
    if m: return m.group(1)
    return None

class YouTube:
    def __init__(self,key,quota):
        self.key=key; self.q=quota; self.s=requests.Session()
    def get(self,method,params):
        self.q.charge(method)
        p=dict(params); p["key"]=self.key
        r=self.s.get(API+"/"+method.split(".")[0],params=p,timeout=30)
        if r.status_code>=400:
            raise RuntimeError(f"{method} failed: HTTP {r.status_code}: {r.text[:500]}")
        return r.json()

def paged(yt, method, params, item_key):
    token=None
    while True:
        p=dict(params)
        if token: p["pageToken"]=token
        data=yt.get(method,p)
        for item in data.get("items",[]): yield item
        token=data.get("nextPageToken")
        if not token: break

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--channel-url",required=True)
    ap.add_argument("--out",default="audit")
    ap.add_argument("--api-key-env",default="YOUTUBE_API_KEY")
    args=ap.parse_args()
    key=os.environ.get(args.api_key_env)
    if not key: raise SystemExit(f"Missing secret environment variable {args.api_key_env}")
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    q=Quota(out/"quota.json"); yt=YouTube(key,q)
    cid=channel_id_from_url(args.channel_url)
    if not cid: raise SystemExit("This runtime currently requires a /channel/UC... URL. Resolve @handles upstream before execution.")
    ch=yt.get("channels.list",{"part":"snippet,contentDetails,statistics,status,topicDetails","id":cid})
    if not ch.get("items"): raise SystemExit("Channel not found or inaccessible")
    channel=ch["items"][0]; uploads=channel["contentDetails"]["relatedPlaylists"]["uploads"]
    videos=[x["contentDetails"]["videoId"] for x in paged(yt,"playlistItems.list",{"part":"contentDetails,snippet","playlistId":uploads,"maxResults":50},"items")]
    unique=list(dict.fromkeys(videos))
    records=[]
    for i in range(0,len(unique),50):
        ids=unique[i:i+50]
        data=yt.get("videos.list",{"part":"snippet,contentDetails,statistics,status,topicDetails,liveStreamingDetails,localizations,recordingDetails,paidProductPlacementDetails,brandPartner","id":",".join(ids)})
        records.extend(data.get("items",[]))
    result={
        "schema_version":"11.2-runtime-1",
        "channel_url":args.channel_url,
        "channel_id":cid,
        "captured_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "credential":{"source":args.api_key_env,"present":True,"value":"[REDACTED]"},
        "channel":channel,
        "coverage":{"uploads_inventory":True,"video_count_discovered":len(unique),"video_count_metadata":len(records),
                    "public_data_only":True,"owner_analytics":False},
        "videos":records,
        "quota":q.data,
    }
    (out/"audit.json").write_text(json.dumps(result,indent=2,ensure_ascii=False))
    (out/"README.txt").write_text("Audit completed. API credential value is not written to artifacts.\n")
    print(json.dumps({"status":"success","channel_id":cid,"videos":len(records),"out":str(out),"quota":q.data}))

if __name__=="__main__": main()
