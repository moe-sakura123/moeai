#!/usr/bin/env python3
"""端到端本地聊天测试：登录 → 建会话 → 读世界书 → 聊天（本地桩 LLM）。"""
import http.cookiejar
import json
import urllib.request

BASE = "http://127.0.0.1:4173"
CID = "char-75dede73-10ca-4c10-9673-4291f6234e21"   # 樱花书屋的兼职少女


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        with self.op.open(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")


c = Client()

# 1. 登录 admin
st, j = c.req("POST", "/api/gva/base/login",
              {"userName": "admin", "passWord": "admin123456"})
print("1) login:", st, j.get("msg"), "uid=", (j.get("data") or {}).get("user", {}).get("id"))
token = (j.get("data") or {}).get("token", "")
headers_auth = {}

# 2. 建/取会话
st, j = c.req("POST", "/api/gva/narratium/chatSession/create",
              {"characterId": CID, "fresh": False})
print("2) create session:", st, "sid=", (j.get("data") or {}).get("sessionId"),
      "title=", (j.get("data") or {}).get("title"))
sid = (j.get("data") or {}).get("sessionId") or ""
# token 也可用 x-token
c.op.addheaders = [("x-token", token)]

# 3. 读该角色的世界书条目（确认库里有哪些）
st, j = c.req("GET", "/api/gva/narratiumWorldbookEntry/getEntriesByCharacterID?characterID=" + CID)
entries = (j.get("data") or [])
print("3) worldbook entries:", st, "count=", len(entries))
for e in entries:
    print("    key=", (e.get("key") or [])[:3], "enabled=", e.get("enabled"),
          "order=", e.get("order"))

# 4. 发一条聊天（chatSync，模型指向本地桩 rateId=3001）
st, j = c.req("POST", "/api/gva/narratium/chatSession/chatSync",
              {"sessionId": sid, "characterId": CID,
               "message": "测试：小满你好，今天下雨了吗？", "modelId": 3001})
print("4) chatSync:", st)
d = j.get("data") or {}
print("   reply:", repr(d.get("message"))[:200])
print("   src:", d.get("src"), "| assistantMessageId:", d.get("assistantMessageId"),
      "| userMessageId:", d.get("userMessageId"))
