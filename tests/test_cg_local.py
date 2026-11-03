#!/usr/bin/env python3
"""验证 CG 关键词触发：临时塞一条 CG 配置 → 发聊天 → 看返回是否带 cg。测完删除该配置。"""
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


# 1. 塞一条演示 CG（关键词 小满 / 下雨，指向现有图片）
db = sqlite3.connect(r'D:\ai_manager_workspace\missai_local\data\missai.db')
demo = {
    "id": "cg_demo_1", "imageId": "cg_demo_1",
    "url": "/image/img_1793356102257_87e7a1da.webp",
    "imageURL": "/image/img_1793356102257_87e7a1da.webp",
    "imageName": "演示CG", "isActive": True,
    "keywords": ["小满", "下雨"], "characterTags": [], "sceneTags": [],
    "emotionTags": [], "actionTags": [], "displayDuration": 0,
    "position": "center", "triggerOnce": False, "triggerSource": ["ai"], "scanDepth": 1,
}
db.execute("INSERT INTO config_cache(key,payload,updated_at) VALUES(?,?,?) "
           "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
           (CKEY, json.dumps([demo], ensure_ascii=False), "2026-10-31T00:00:00+08:00"))
db.commit()
print("seeded cg:", CKEY)

c = Client()
j = c.req("POST", "/api/gva/base/login", {"userName": "admin", "passWord": "admin123456"})
token = (j.get("data") or {}).get("token", "")
c.op.addheaders = [("x-token", token)]
# 建会话
j = c.req("POST", "/api/gva/narratium/chatSession/create", {"characterId": CID, "fresh": False})
sid = (j.get("data") or {}).get("sessionId") or ""
# 发聊天
j = c.req("POST", "/api/gva/narratium/chatSession/chatSync",
          {"sessionId": sid, "characterId": CID,
           "message": "小满，外面下雨了，好冷呀", "modelId": 3001})
d = j.get("data") or {}
print("reply:", repr(d.get("message"))[:120])
print("cg:", json.dumps(d.get("cg"), ensure_ascii=False))

# 2. 测完删除演示 CG 配置
db.execute("DELETE FROM config_cache WHERE key=?", (CKEY,))
db.commit()
print("cleaned up cg config")
