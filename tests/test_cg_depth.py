#!/usr/bin/env python3
"""验证 scanDepth：depth=3 能命中历史旧消息里的关键词，depth=1 只扫当前回复。"""
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


def seed(scan_depth, keyword="大麦茶"):
    demo = {"id": "cg_depth", "imageId": "cg_depth",
            "url": "/image/img_1793356102257_87e7a1da.webp",
            "imageURL": "/image/img_1793356102257_87e7a1da.webp",
            "imageName": "深度测试CG", "isActive": True,
            "keywords": [keyword], "characterTags": [], "sceneTags": [],
            "emotionTags": [], "actionTags": [], "displayDuration": 0,
            "position": "center", "triggerOnce": False,
            "triggerSource": ["ai"], "scanDepth": scan_depth}
    db.execute("INSERT INTO config_cache(key,payload,updated_at) VALUES(?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
               (CKEY, json.dumps([demo], ensure_ascii=False), "2026-10-31T00:00:00+08:00"))
    db.commit()


def clean():
    db.execute("DELETE FROM config_cache WHERE key=?", (CKEY,))
    db.commit()


db = sqlite3.connect(r'D:\ai_manager_workspace\missai_local\data\missai.db')
c = Client()
j = c.req("POST", "/api/gva/base/login", {"userName": "admin", "passWord": "admin123456"})
tok = (j.get("data") or {}).get("token", "")
c.op.addheaders = [("x-token", tok)]
j = c.req("POST", "/api/gva/narratium/chatSession/create", {"characterId": CID, "fresh": False})
sid = (j.get("data") or {}).get("sessionId") or ""
print("sid:", sid)


def chat(msg):
    return c.req("POST", "/api/gva/narratium/chatSession/chatSync",
                 {"sessionId": sid, "characterId": CID, "message": msg, "modelId": 3001})


clean()
# 1) depth=3：先让一条旧消息(assistant A)含"大麦茶"，再发无关消息 B
seed(3)
chat("来杯大麦茶吧")                       # assistant A 回显含"大麦茶"
r = chat("今天天气不错")                    # 当前回复不含"大麦茶"
cg3 = (r.get("data") or {}).get("cg") or []
print("depth=3, 关键词在历史(assistant A): cg =", json.dumps(cg3, ensure_ascii=False))
print("  -> depth=3 命中历史旧消息:", "PASS" if cg3 else "FAIL")

# 2) depth=1：只扫当前回复，历史里的关键词不再命中
clean()
seed(1)
r = chat("今天又是好天气")                   # 当前回复不含"大麦茶"
cg1 = (r.get("data") or {}).get("cg") or []
print("depth=1, 关键词只在历史里: cg =", json.dumps(cg1, ensure_ascii=False))
print("  -> depth=1 不看历史:", "PASS" if not cg1 else "FAIL")

# 3) depth=1，但当前回复含关键词 → 命中
clean()
seed(1)
r = chat("我要喝大麦茶")                    # 当前回复回显含"大麦茶"
cg1b = (r.get("data") or {}).get("cg") or []
print("depth=1, 关键词在当前回复: cg =", json.dumps(cg1b, ensure_ascii=False))
print("  -> depth=1 扫当前回复命中:", "PASS" if cg1b else "FAIL")

clean()
print("cleaned up")
