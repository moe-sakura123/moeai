#!/usr/bin/env python3
"""本地 OpenAI 兼容桩：监听 127.0.0.1:53373，把收到的完整 messages 记入日志，
返回确定性的回复。用于展示本地聊天时服务端拼装了什么提示词。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "logs" / "stub_llm_capture.log"
MODELS = ["gpt-selftest-mini", "gpt-selftest-pro", "gpt-selftest-long"]


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/").endswith("/models"):
            return self._send({"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
        self._send({"error": {"message": "not found " + self.path}}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        if not self.path.split("?")[0].rstrip("/").endswith("/chat/completions"):
            return self._send({"error": {"message": "not found " + self.path}}, 404)
        # 记录收到的完整 messages
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("===REQUEST=== model=" + str(payload.get("model")) +
                    " stream=" + str(payload.get("stream")) + "\n")
            for m in payload.get("messages") or []:
                f.write(json.dumps({"role": m.get("role"), "content": m.get("content")},
                                   ensure_ascii=False)[:2000] + "\n")
            f.write("\n")
        model = str(payload.get("model") or "")
        last = ""
        sys_len = 0
        for m in payload.get("messages") or []:
            if m.get("role") == "user":
                last = str(m.get("content") or "")
            if m.get("role") == "system":
                sys_len = len(str(m.get("content") or ""))
        text = f"MOCK[{model}]|sys={sys_len}|收到={last[:60]}"
        self._send({"id": "cmpl-local", "object": "chat.completion", "model": model,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}]})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 53373), Stub)
    print("stub listening on 127.0.0.1:53373")
    srv.serve_forever()


if __name__ == "__main__":
    main()
