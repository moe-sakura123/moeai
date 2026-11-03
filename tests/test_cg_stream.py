#!/usr/bin/env python3
"""验证流式 chatStream 会下发 type=cg 帧。"""
import http.cookiejar
import json
import sqlite3
import urllib.request

BASE = "http://127.0.0.1:4173"
CID = "char-75dede73-10ca-4c10-9673-4291f6234e21"
CKEY = f"cg:images:11:{CID}"


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        with self.op.open(r, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")


db = sqlite3.connect(r'D:\ai_manager_workspace\missai_local\data\missai.db')
demo = {"id": "cg_demo_2", "imageId": "cg_demo_2",
        "url": "/image/img_1793356102257_87e7a1da.webp",
        "imageURL": "/image/img_1793356102257_87e7a1da.webp",
        "imageName": "流式演示CG", "isActive": True,
        "keywords": ["小满", "下雨"], "characterTags": [], "sceneTags": [],
        "emotionTags": [], "actionTags": [], "displayDuration": 0,
        "position": "center", "triggerOnce": False, "triggerSource": ["ai"], "scanDepth": 1}
db.execute("INSERT INTO config_cache(key,payload,updated_at) VALUES(?,?,?) "
           "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
           (CKEY, json.dumps([demo], ensure_ascii=False), "2026-10-31T00:00:00+08:00"))
db.commit()

c = Client()
j = c.req("POST", "/api/gva/base/login", {"userName": "admin", "passWord": "admin123456"})
tok = (j.get("data") or {}).get("token", "")
c.op.addheaders = [("x-token", tok)]
j = c.req("POST", "/api/gva/narratium/chatSession/create", {"characterId": CID, "fresh": False})
sid = (j.get("data") or {}).get("sessionId") or ""

# 流式 chatStream：手动读 SSE 文本
body = json.dumps({"sessionId": sid, "characterId": CID,
                   "message": "小满，外面下雨了，好冷呀", "modelRateId": 3001}).encode("utf-8")
r = urllib.request.Request(BASE + "/api/gva/narratium/chatSession/chatStream", data=body, method="POST")
r.add_header("Content-Type", "application/json")
r.add_header("x-token", tok)
r.add_header("Accept", "text/event-stream")
with urllib.request.urlopen(r, timeout=30) as resp:
    data = resp.read().decode("utf-8")
frames = [ln for ln in data.split("\n") if ln.startswith("data:")]
print("total data frames:", len(frames))
for ln in frames:
    raw = ln[5:].strip()
    if raw == "[DONE]":
        continue
    try:
        fr = json.loads(raw)
    except Exception:
        continue
    if fr.get("type") in ("cg", "done"):
        print("  frame type=", fr.get("type"), "cg=", json.dumps(fr.get("cg"))[:160])

db.execute("DELETE FROM config_cache WHERE key=?", (CKEY,))
db.commit()
print("cleaned up")
