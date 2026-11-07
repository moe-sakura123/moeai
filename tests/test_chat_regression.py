#!/usr/bin/env python3
"""聊天管线回归基线（阶段 0/1）：不依赖线上服务，使用临时 SQLite。

覆盖：
  1) 世界书按用户隔离（系统条目 + 当前用户，其他用户不可见）
  2) 记忆按会话隔离 + 会话内游标 + 删除会话同步清理
  3) 状态面板落库合并（缺字段沿用上一轮）
  4) 分区 token 统计 + 历史预算
  5) prompt 快照扩展字段（turn_id / finish_reason / sections / hash）
  6) 失败路径删除孤立 user 消息

运行： python tests/test_chat_regression.py
"""
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import prompt_builder  # noqa: E402
import server  # noqa: E402

CID = "regression-char"
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def setup_db(tmp):
    db.DB_PATH = Path(tmp) / "regression.db"
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._local.conn = None
    db.init()
    db.ensure_admin()
    admin = db.one("SELECT id FROM users WHERE user_name='admin'")
    return int(admin["id"])


def add_worldbook(uid, eid, payload):
    db.execute(
        "INSERT INTO worldbook_entries(user_id, entry_id, character_id, custom,"
        " payload, position, updated_at) VALUES(?,?,?,?,?,?,?)",
        (uid, eid, CID, 0, json.dumps(payload, ensure_ascii=False), 0, db.now_iso()))


def test_worldbook(uid):
    print("[1] 世界书按用户隔离")
    add_worldbook(0, "sys1", {"content": "系统设定", "constant": True})
    add_worldbook(uid, "mine1", {"content": "我的设定", "constant": True})
    add_worldbook(uid + 1, "other1", {"content": "别人的设定", "constant": True})
    got = server.Handler._chat_worldbook(CID, uid)
    ids = {p["worldbookID"] for p in got}
    check("系统 + 自己的条目可见", ids == {"sys1", "mine1"}, ids)
    check("其他用户条目不可见", "other1" not in ids)
    from roleplay_core.prompt import match_worldbook
    matched = match_worldbook(got, "随便聊聊")
    text = "\n".join(e["content"] for e in matched["before"] + matched["after"])
    check("匹配器只注入可见条目", "别人的设定" not in text and "我的设定" in text, text)


def _seed_legacy_memory(uid, cid, sid, content, round_no=1):
    """旧表 chat_memories 只读兼容：测试用直接写行，不再经 v1 写函数。"""
    db.execute("INSERT INTO chat_memories(user_id, character_id, session_id,"
               " content, round_no, created_at) VALUES(?,?,?,?,?,?)",
               (uid, cid, sid, content, round_no, db.now_iso()))


def test_memory(uid):
    print("[2] 旧记忆表按会话只读 + 删除清理")
    _seed_legacy_memory(uid, CID, "sidA", "A-1", 1)
    _seed_legacy_memory(uid, CID, "sidA", "A-2", 2)
    _seed_legacy_memory(uid, CID, "sidB", "B-1", 1)
    a = db.chat_memories(uid, CID, session_id="sidA")
    b = db.chat_memories(uid, CID, session_id="sidB")
    check("会话 A 只读自己的记忆", len(a) == 2 and all("A-" in m["content"] for m in a), a)
    check("会话 B 不串味", len(b) == 1 and b[0]["content"] == "B-1", b)
    db.delete_chat_memory_state(uid, session_id="sidA")
    check("删会话记忆后 A 清空、B 保留",
          not db.chat_memories(uid, CID, session_id="sidA")
          and len(db.chat_memories(uid, CID, session_id="sidB")) == 1)


def test_status(uid):
    print("[3] 状态面板落库合并")
    view = db.ensure_chat_session(uid, CID, initial_stats={"hp": 100, "mp": 50})
    sid = view["sessionId"]
    db.update_thread_stats(uid, sid, {"hp": 90})
    st = db.thread_stats(uid, sid)
    check("本轮更新覆盖字段", st.get("hp") == 90, st)
    check("缺字段沿用上一轮（不回落到初值之外的默认）", st.get("mp") == 50, st)
    db.update_thread_stats(uid, sid, {"mp": 40})
    st = db.thread_stats(uid, sid)
    check("二次更新继续合并", st.get("hp") == 90 and st.get("mp") == 40, st)


def test_prompt_sections():
    print("[4] 分区统计 + 历史预算")
    from roleplay_core.models import ChatContext, Message
    from roleplay_core.prompt import build_prompt

    history = [Message(role="user" if i % 2 == 0 else "assistant",
                       content=f"第{i}句" + "剧情" * 30) for i in range(20)]
    ctx = ChatContext(
        session_id="sections",
        character_data={"name": "测试角色", "hasPayload": True,
                        "character": {"name": "测试角色", "description": "角色设定",
                                      "personality": "温柔"},
                        "promptData": {"authorNote": "作者注"}, "cg": None},
        history=history,
        user_input="本轮用户输入",
        current_status={"values": {"好感度": 30}})
    msgs = build_prompt(
        ctx,
        worldbook_entries=[{"worldbookID": "wb1", "content": "世界书内容",
                            "constant": True}],
        config={"contextWindow": 1200})
    check("首条仍是固定 system 前缀", msgs[0]["role"] == "system", msgs[0]["role"])
    check("最后一条是本轮 user", msgs[-1]["role"] == "user"
          and "本轮用户输入" in msgs[-1]["content"])
    check("世界书/状态面板各成一段 system",
          any("### 世界书" in m["content"] for m in msgs)
          and any("### 当前状态面板" in m["content"] for m in msgs))
    secs = prompt_builder.sectionize(msgs)
    ids = [s["id"] for s in secs]
    check("section 覆盖 persona/worldbook/status/current",
          {"persona", "worldbook", "status", "current"}.issubset(set(ids)), ids)
    total = sum(s["tokens"] for s in secs)
    check("section token 统计为正值", total > 0, total)
    hist_msgs = [s for s in secs if s["id"].startswith("history.")]
    check("历史被预算裁剪", len(hist_msgs) < len(history), len(hist_msgs))
    check("固定前缀 hash 稳定",
          prompt_builder.prompt_prefix_hash(msgs)
          == prompt_builder.prompt_prefix_hash(
              build_prompt(ctx, config={"contextWindow": 1200})),
          prompt_builder.prompt_prefix_hash(msgs))


def test_prompt_log(uid):
    print("[5] prompt 快照扩展字段")
    _seed_legacy_memory(uid, CID, "sid-log", "记忆", 1)
    db.record_chat_prompt(
        uid, "sid-log", CID, "test-model",
        [{"url": "http://x/v1/chat/completions", "stream": True,
          "payload": {"model": "test-model",
                      "messages": [{"role": "system", "content": "前缀"},
                                   {"role": "user", "content": "你好"}]}}],
        turn_id="turn-1", finish_reason="stop",
        sections=[{"id": "persona", "tokens": 2}], prompt_hash="abc123")
    row = db.one("SELECT * FROM chat_prompt_log WHERE session_id='sid-log'"
                 " ORDER BY id DESC LIMIT 1")
    check("prompt 日志写入 turn_id", row and row["turn_id"] == "turn-1", row)
    check("prompt 日志写入 finish_reason", row and row["finish_reason"] == "stop")
    check("prompt 日志写入 sections", row and "persona" in row["sections_json"])
    check("prompt 日志写入前缀 hash", row and row["prompt_hash"] == "abc123")


def test_delete_orphan(uid):
    print("[6] 失败路径删除孤立 user 消息")
    view = db.ensure_chat_session(uid, CID, initial_stats={"hp": 100})
    sid = view["sessionId"]
    mid = db.record_chat_message(uid, sid, "user", "失败前的一句话")
    tid = db.one("SELECT id FROM chat_threads WHERE user_id IS ? AND session_id=?",
                 (uid, sid))["id"]
    ok = db.delete_chat_message(uid, mid)
    left = db.one("SELECT COUNT(*) AS c FROM chat_messages WHERE thread_id=?"
                  " AND id=?", (tid, mid))
    check("删除成功", ok)
    check("消息确实被清掉", left["c"] == 0, left["c"])
    check("会话计数同步", db.thread_stats(uid, sid) is not None)


def test_cancel():
    print("[7] 取消：停止读上游 / 跳过补格式 / 空回复不落库")
    from roleplay_core.chat_router import (ChatPorts, ChatRequest, ChatRouter,
                                            StreamDecision)
    from roleplay_core.models import ChatContext

    saved = []
    ensured = []

    def post(text, ctx):
        return {"display_text": text, "tagged_text": text,
                "status": [], "options": [], "has_image": False}

    def make_ports(flag, pieces):
        def stream(_msgs):
            for i, p in enumerate(pieces):
                if i == 1:
                    flag["cancelled"] = True
                yield p

        return ChatPorts(
            load_context=lambda req: ChatContext(session_id="s1"),
            stream_llm=stream,
            on_stream_error=lambda exc, raw, req: StreamDecision(fatal=True, error=str(exc)),
            post_process=post,
            save_assistant=lambda ctx, d, t: (saved.append(d) or 1),
            emit=lambda ev: True,
            local_reply=lambda req: "本地兜底",
            build_messages=lambda ctx: [{"role": "user", "content": "hi"}],
            ensure_format=lambda ctx, msgs, text: (ensured.append(text) or text),
            cancelled=lambda: flag["cancelled"],
        )

    req = ChatRequest(session_id="s1", character_id="c1",
                      character_name="角色", user_input="hi", user_message_id=1,
                      fail_on_empty=True)
    turn = ChatRouter(make_ports({"cancelled": False}, ["你", "好", "呀"])).chat_stream(req)
    check("取消后保留已有 partial", turn.ok and turn.aborted
          and turn.display_text == "你好", (turn.ok, turn.aborted, turn.display_text))
    check("取消后跳过补格式", not ensured, ensured)
    check("partial 已落库", saved == ["你好"], saved)

    saved.clear()
    ensured.clear()
    turn = ChatRouter(make_ports({"cancelled": True}, [])).chat_stream(req)
    check("空回复取消不落 assistant", turn.ok and turn.aborted and not saved,
          (turn.ok, turn.aborted, saved))


def test_prompt_engine(uid):
    print("[9] 组装层：唯一引擎 / 世界书 / 角色卡 / firstMes")
    from roleplay_core.models import ChatContext, Message
    from roleplay_core.prompt import build_prompt, match_worldbook

    # 旧开关行即使残留也不再影响拼装（prompt.py 是唯一引擎）
    # 注意：prompt_engine_v2 / promptEngine:{uid}:{cid} 是已持久化的配置键，
    # 改了会让老数据失效，所以保留 v2 字样，仅作兼容读取。
    db.set_config("prompt_engine_v2", {"enabled": False, "users": []})
    db.set_config(f"promptEngine:{uid}:{CID}", {"engine": "v1"})
    legacy_ctx = ChatContext(
        session_id="legacy-switch-sid",
        character_data={"id": CID, "name": "测试", "hasPayload": True,
                        "cg": None, "character": {}, "promptData": {}},
        user_input="hi")
    got_legacy = server.Handler._prompt_messages(uid, "legacy-switch-sid", CID,
                                             legacy_ctx)
    check("旧 engine 配置被忽略，仍走当前引擎", bool(got_legacy) and bool(got_legacy[0]),
          got_legacy)
    db.set_config(f"promptEngine:{uid}:{CID}", {"engine": ""})

    # contextWindow：默认 8192，可按模型覆盖
    check("contextWindow 默认 8192",
          server.Handler._prompt_config(uid, CID, "0")["contextWindow"] == 8192)
    db.set_config(f"modelConfig:{uid}:7", {"contextWindow": 16384})
    check("按模型覆盖 contextWindow",
          server.Handler._prompt_config(uid, CID, "7")["contextWindow"] == 16384)

    # 世界书：order / group / 递归 1 步 / position / depth
    entries = [
        {"id": "a1", "content": "KEYA 内容", "keys": ["k1"], "order": 1},
        {"id": "a2", "content": "KEYB 内容", "keys": ["k2"], "order": 2},
        {"id": "g1", "content": "组内低", "keys": ["k1"], "order": 1, "group": "G"},
        {"id": "g2", "content": "组内高", "keys": ["k1"], "order": 5, "group": "G"},
        {"id": "r1", "content": "递归命中", "keys": ["keya"], "order": 4},
        {"id": "d1", "content": "深度设定", "keys": ["k1"],
         "position": "at_depth", "depth": 1},
        {"id": "p1", "content": "后置设定", "keys": ["k1"],
         "position": "after_char", "order": 3},
    ]
    got = match_worldbook(entries, "k1 与 k2")
    before_ids = [e["id"] for e in got["before"]]
    check("group 同组只留 order 最高", "g1" not in before_ids and "g2" in before_ids,
          before_ids)
    check("order 升序", before_ids == ["a1", "a2", "r1", "g2"], before_ids)
    check("递归扩 1 步命中", "r1" in before_ids, before_ids)
    check("后置条目单独分桶", [e["id"] for e in got["after"]] == ["p1"], got["after"])
    check("depth 条目单独分桶", [e["id"] for e in got["depth"]] == ["d1"], got["depth"])

    ctx = ChatContext(
        session_id="prompt-sid",
        character_data={
            "id": CID, "name": "测试角色", "hasPayload": True, "cg": None,
            "character": {"description": "角色描述", "personality": "外冷内热",
                          "scenario": "雨夜便利店", "mesExample": "示例对话一句"},
            "promptData": {"systemPrompt": "核心设定", "postPrompt": "继续推进"},
        },
        history=[Message(role="assistant", content="旧回复")],
        user_input="k1 与 k2",
        current_status={"values": {"好感度": 40}},
    )
    msgs = build_prompt(ctx, worldbook_entries=entries,
                           memories=[{"content": "记忆A"}],
                           config={"contextWindow": 8192})
    texts = [m["content"] for m in msgs]
    check("人格/场景进入固定前缀", "外冷内热" in texts[0] and "雨夜便利店" in texts[0],
          texts[0][:80])
    check("角色卡 mesExample 成独立 system 段",
          any("示例对话一句" in t for t in texts))
    check("记忆注入且只出现一次", sum("记忆A" in t for t in texts) == 1)
    check("世界书前置/后置/深度分开注入",
          any("### 世界书（前置）" in t for t in texts)
          and any("### 世界书（后置）" in t for t in texts)
          and any("### 世界书（深度注入）" in t for t in texts))
    check("末尾是本轮 user + 格式提醒",
          msgs[-1]["role"] == "user" and "k1 与 k2" in msgs[-1]["content"]
          and "next_prompt" in msgs[-1]["content"], msgs[-1]["content"][:80])

    # firstMes 只写一次
    sid = db.ensure_chat_session(uid, "firstmes-char")["sessionId"]
    db.record_first_message_once(uid, sid, "开场白")
    db.record_first_message_once(uid, sid, "开场白")
    cnt = db.one(
        "SELECT COUNT(*) AS c FROM chat_messages m"
        " JOIN chat_threads t ON t.id=m.thread_id"
        " WHERE t.user_id IS ? AND t.session_id=?", (uid, sid))["c"]
    check("firstMes 只写一次", cnt == 1, cnt)


def test_memory_slots(uid):
    print("[10] 记忆四槽 + 滚动摘要 + 深度注入")
    from roleplay_core.models import ChatContext, Message
    from roleplay_core.prompt import build_prompt

    sid = "slot-sid"
    db.set_memory_slot(uid, CID, sid, "promise", "答应周末陪用户看展", 10)
    db.set_memory_slot(uid, CID, sid, "user_info", "用户怕黑", 10)
    db.set_memory_slot(uid, CID, sid, "long_term", "两人已确认搭档关系", 10)
    db.set_memory_slot(uid, CID, sid, "short_term", "刚一起从雨里跑回便利店", 10)
    got = db.memory_slots(uid, sid)
    check("四槽落库并可读回",
          got["promise"]["content"].startswith("答应")
          and got["short_term"]["covered_until"] == 10, got)

    ctx = ChatContext(
        session_id=sid,
        character_data={"id": CID, "name": "测试", "hasPayload": True, "cg": None,
                        "character": {"description": "角色"}, "promptData": {}},
        history=[Message(role="user" if i % 2 == 0 else "assistant",
                         content=f"第{i}句") for i in range(8)],
        user_input="继续",
        current_status={"values": {"好感度": 50}},
    )
    msgs = build_prompt(
        ctx, slots={k: v["content"] for k, v in got.items()},
        config={"contextWindow": 8192,
                "anchor": {"enabled": True, "depth": 2, "frequency": 1,
                           "text": "保持冷峻"}})
    texts = [m["content"] for m in msgs]
    labels = {"promise": "承诺与待办", "user_info": "用户信息",
              "long_term": "长期记忆", "short_term": "近期剧情"}
    idx = {slot: next((i for i, t in enumerate(texts)
                       if "### 记忆 · " in t and label in t), -1)
           for slot, label in labels.items()}
    check("四槽都注入", all(i >= 0 for i in idx.values()), idx)
    check("promise 比 short_term 更靠近生成端",
          idx["promise"] > idx["short_term"], idx)
    same = texts[idx["user_info"]]
    check("同 depth 按 promise→user_info→long_term 排序",
          idx["promise"] > idx["user_info"]
          and same.index("用户信息") < same.index("长期记忆"), idx)
    check("作者注按 depth/frequency 注入",
          any("保持冷峻" in t for t in texts))

    # 滚动摘要：只总结被窗口淘汰的区间，写入四槽并推进游标
    view = db.ensure_chat_session(uid, "sum-slot-char")["sessionId"]
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        db.record_chat_message(uid, view, role, f"{role}-{i}")
    payload = {"short_term": "近期剧情", "long_term": "长期事实",
               "user_info": "用户信息", "promise": "答应的事"}
    real = server.llm.chat_stream
    real_params = server.Handler._summary_params
    real_endpoint = server.Handler._llm_endpoint
    server.llm.chat_stream = lambda *a, **k: iter(
        [json.dumps(payload, ensure_ascii=False)])
    server.Handler._summary_params = staticmethod(lambda _u, _c: (True, 3))
    server.Handler._llm_endpoint = staticmethod(lambda _r: ("http://fake", "k", "m"))
    try:
        server.Handler._maybe_summarize(uid, view, CID)
    finally:
        server.llm.chat_stream = real
        server.Handler._summary_params = real_params
        server.Handler._llm_endpoint = real_endpoint
    slots = db.memory_slots(uid, view)
    st = db.summary_state(uid, view)
    check("滚动摘要写入四槽",
          all(slots[s]["content"] for s in db.MEMORY_SLOTS), slots)
    check("游标推进且记录滚动摘要",
          st["covered_until"] > 0 and st["rolling_summary"], st)

    # 旧表只读兼容：没有四槽时整段作为 short_term 注入
    old_sid = "legacy-old-sid"
    _seed_legacy_memory(uid, CID, old_sid, "旧摘要内容", 1)
    ctx2 = ChatContext(
        session_id=old_sid,
        character_data={"id": CID, "name": "测试", "hasPayload": True,
                        "cg": None, "character": {}, "promptData": {}},
        user_input="hi")
    msgs = server.Handler._prompt_messages(uid, old_sid, CID, ctx2)
    check("旧表兼容为 short_term",
          bool(msgs) and "旧摘要内容" in (msgs[1]["memory"].get("short_term") or ""),
          msgs[1] if msgs else None)


def test_provider_profile(uid):
    print("[11] provider profile 数据化 + prompt 只读接口")
    import llm
    db.execute(
        "INSERT INTO llm_providers(kind, name, base_url, api_key, models, status,"
        " enabled, is_default, default_model, profile, created_at, updated_at)"
        " VALUES('openai','测试接入','http://x','k','[]','ok',1,1,'m',"
        " '{\"max_tokens_field\":\"max_completion_tokens\",\"stop\":[\"END\"],"
        " \"extra\":{\"top_k\":40}}', ?, ?)", (db.now_iso(), db.now_iso()))
    prof = db.llm_profile(None)
    check("接入 profile 可读取", prof.get("max_tokens_field") == "max_completion_tokens",
          prof)
    payload = llm._chat_payload("m", [{"role": "user", "content": "hi"}], True,
                                0.7, 8192, {}, prof)
    check("max_tokens 字段按 profile 切换",
          "max_completion_tokens" in payload and "max_tokens" not in payload, payload)
    check("stop / 额外参数并入 payload",
          payload.get("stop") == ["END"] and payload.get("top_k") == 40, payload)

    db.record_chat_prompt(uid, "prompt-read-sid", CID, "m",
                          [{"url": "u", "stream": True,
                            "payload": {"model": "m", "messages": [
                                {"role": "user", "content": "hi"}]}}],
                          turn_id="t11", finish_reason="stop",
                          sections=[{"section": "history", "tokens": 10}],
                          prompt_hash="h11", worldbook=[{"id": "w1"}],
                          memory={"short_term": "x"}, diff=[{"section": "history"}])
    rows = db.recent_chat_prompts(uid, "prompt-read-sid", 5)
    check("prompt 只读接口可回放分段与 diff",
          bool(rows) and rows[0]["sections_json"][0]["tokens"] == 10
          and rows[0]["diff_json"][0]["section"] == "history"
          and rows[0]["memory_json"]["short_term"] == "x", rows[:1])


def test_algorithm_memory(uid):
    print("[12] 算法记忆：嵌入 / BM25 / 衰减 / 混合检索 / 去重 / 聚类 / 归档 / 召回日志")
    from roleplay_core import memory_engine as me
    from roleplay_core.models import ChatContext, Message

    sid = "alg-mem-sid"
    db._purge_memory_items("user_id IS ? AND session_id=?", (uid, sid))

    # 表示层：确定性 + 余弦
    v1 = me.embed("我喜欢下雨天")
    e2 = me.embed("我喜欢下雨天")
    check("特征哈希嵌入确定可复现", v1 == e2)
    check("同文本余弦≈1", me.cosine(v1, e2) > 0.99, me.cosine(v1, e2))
    check("不同文本余弦更低",
          me.cosine(v1, me.embed("今天去爬山")) < 0.7,
          me.cosine(v1, me.embed("今天去爬山")))

    # 时近性：半衰期 30 天 → 30 天前约 0.5
    check("新记忆时近性≈1", me.recency(db.now_iso()) > 0.99)
    old30 = "2026-10-08T00:00:00+08:00"
    check("30 天前约半衰", 0.45 < me.recency(old30, 30.0) < 0.55,
          me.recency(old30, 30.0))

    # 词法 BM25（纯 Python 兜底路径）
    docs = [(1, "用户喜欢下雨天"), (2, "今天去爬山")]
    scores = me.bm25("下雨天", docs)
    check("BM25 命中项得分更高",
          scores.get(1, 0) > scores.get(2, 0), scores)

    # 写入层：规则兜底抽取
    rules = me.extract_rules("我喜欢下雨天。我答应周末去看海。", "")
    kinds = {r["kind"] for r in rules}
    check("规则抽取识别偏好/承诺", kinds == {"preference", "promise"}, kinds)

    # 去重与冲突合并
    act, tgt = me.classify_duplicate(
        [{"id": 99, "kind": "fact", "content": "用户住在北京", "subject": "住址",
          "status": "active", "vec": me.embed("用户住在北京")}],
        {"kind": "fact", "content": "用户住在上海", "subject": "住址"}, 384)
    check("同主体不同值 → 冲突替换", (act, tgt) == ("replace", 99), (act, tgt))
    act2, _ = me.classify_duplicate(
        [{"id": 98, "kind": "preference", "content": "用户喜欢下雨天",
          "status": "active", "vec": me.embed("用户喜欢下雨天")}],
        {"kind": "preference", "content": "用户喜欢下雨天"}, 384)
    check("语义近重复 → 合并", act2 == "merge", act2)

    # 聚类归并
    same = [{"id": 1, "kind": "fact", "content": "用户喜欢下雨天",
             "importance": 0.6},
            {"id": 2, "kind": "fact", "content": "用户喜欢下雨天",
             "importance": 0.7}]
    groups = me.cluster_items(same, 0.85)
    check("同主题聚类成组", len(groups) == 1 and len(groups[0]) == 2, groups)
    check("组内归并文本可用", "下雨天" in me.union_content(groups[0]))

    # 写入整轮对话（关掉 LLM 抽取，走规则兜底 → 不联网）
    db.set_config(f"promptEngine:{uid}:{CID}",
                  {"memory": {"llmExtract": False}})
    server.Handler._write_memories(uid, sid, CID, "t-alg-1",
                                   "我喜欢下雨天，今天去了海边。", "好的，我记住了。")
    active = db.memory_items(uid, CID, sid)
    check("规则兜底写入记忆条目", len(active) >= 1, active)
    server.Handler._write_memories(uid, sid, CID, "t-alg-2",
                                   "我答应你周末去看海。", "")
    after_add = db.memory_count(uid, CID, sid)
    server.Handler._write_memories(uid, sid, CID, "t-alg-3",
                                   "我喜欢下雨天，今天去了海边。", "")
    check("近似去重不新增行", db.memory_count(uid, CID, sid) == after_add,
          (after_add, db.memory_count(uid, CID, sid)))

    # FTS5 全文索引（中文 trigram）
    fts = db.search_memory_fts(uid, CID, sid, "喜欢下雨天")
    check("FTS5 中文命中", bool(fts) and max(fts.values()) > 0, fts)

    # 混合检索：相关性 + 重要性 + 时近性
    cfg = server.Handler._memory_cfg(uid, CID)
    check("memory 配置可覆盖", cfg["llmExtract"] is False, cfg)
    retrieved = me.retrieve(uid, CID, sid, "下雨天 海边 承诺", cfg)
    check("混合检索可召回", bool(retrieved), retrieved)
    check("召回带分项得分",
          bool(retrieved) and {"relevance", "bm25", "cosine", "importance",
                               "recency", "total"} <= set(retrieved[0]["scores"]),
          retrieved[:1])

    # 用户隔离
    other = db.add_memory_item(uid + 1, CID, sid, "fact", "别人的秘密下雨天",
                               importance=1.0, embedding=me.embed("别人的秘密下雨天"))
    leak = [x for x in me.retrieve(uid, CID, sid, "下雨天", cfg)
            if int(x["id"]) == other]
    check("检索按用户隔离", not leak, leak)

    # 记忆注入 + 可观测层
    ctx = ChatContext(
        session_id=sid,
        character_data={"id": CID, "name": "测试角色", "hasPayload": True,
                        "cg": None,
                        "character": {"description": "角色描述",
                                      "personality": "外冷内热"},
                        "promptData": {"systemPrompt": "核心设定"}},
        history=[Message(role="assistant", content="海边")],
        user_input="下雨天还记得吗",
    )
    msgs = server.Handler._prompt_messages(uid, sid, CID, ctx, None)
    check("开启记忆检索后返回检索记忆",
          bool(msgs) and bool(msgs[1]["retrieved"]), msgs[1]["retrieved"][:1] if msgs else None)
    texts = [m["content"] for m in msgs[0]] if msgs else []
    check("检索块注入到 prompt",
          any("### 相关记忆" in t for t in texts), texts[:2])
    check("注入位置记录为配置深度",
          bool(msgs) and msgs[1]["retrieved"][0]["injected_at"] == 4,
          msgs[1]["retrieved"][:1] if msgs else None)
    server.Handler._record_recalls(uid, sid, "t-alg-4", msgs[1] if msgs else None)
    logs = db.memory_recalls(uid, sid, limit=5)
    check("召回日志落库（含分项得分与注入位置）",
          bool(logs) and logs[0]["injected_at"] == 4
          and bool(logs[0]["scores"]), logs[:1])
    bumped = db.one("SELECT recall_count FROM chat_memory_items WHERE id=?",
                    (int(logs[0]["memory_id"]),))
    check("召回次数累加", bool(bumped) and int(bumped["recall_count"]) >= 1, bumped)

    # 遗忘层：低重要度 + 长期未召回 → 归档（行保留）
    mid = int(db.memory_items(uid, CID, sid)[-1]["id"])
    db.execute("UPDATE chat_memory_items SET importance=0.1, created_at=?,"
               " last_recalled_at=NULL WHERE id=?",
               ("2026-01-01T00:00:00+08:00", mid))
    archived = db.archive_stale_memories(uid, CID, sid, days=30, max_importance=0.5)
    row = db.one("SELECT status FROM chat_memory_items WHERE id=?", (mid,))
    check("低分老记忆归档", archived >= 1 and row["status"] == "archived",
          (archived, row))
    check("归档不物理删除", row is not None)
    check("归档条目不再参与检索",
          all(int(x["id"]) != mid for x in
              me.retrieve(uid, CID, sid, "下雨天 海边 承诺", cfg)))

def test_memory_embedding(uid):
    print("[13] embedding 调用链：批量/归一化/模型绑定/缓存复用/MMR/降级")
    import llm
    from roleplay_core import memory_engine as me

    sid = "emb-mem-sid"
    db._purge_memory_items("user_id IS ? AND session_id=?", (uid, sid))
    db.set_config(f"promptEngine:{uid}:{CID}",
                  {"memory": {"llmExtract": False}})

    calls = {"n": 0}
    real = llm.embeddings

    def fake(base, key, model, texts, **kw):
        calls["n"] += 1
        return [[float((i + 1) * (j + 1)) for j in range(8)]
                for i, _ in enumerate(texts)]

    llm.embeddings = fake
    try:
        cfg = server.Handler._memory_cfg(uid, CID)
        cfg["embedding"] = {"enabled": True, "base_url": "http://fake/v1",
                            "api_key": "k", "model": "fake-embed",
                            "dimensions": 8, "batch_size": 3,
                            "timeout": 2.0, "max_retries": 1}
        model_id, vecs, degraded = me.provider_embeddings(
            ["a", "b", "c", "d"], cfg)
        check("批量 embedding 返回同序向量",
              len(vecs) == 4 and not degraded, (len(vecs), degraded))
        check("模型标识带 openai 前缀",
              model_id == "openai:fake-embed@8", model_id)
        check("向量 L2 归一化",
              abs(sum(x * x for x in vecs[0]) - 1.0) < 1e-6,
              sum(x * x for x in vecs[0]))
        check("查询向量复用模型空间", me.query_vector("a", cfg)[0] == model_id)

        # 写入 + 补算：先哈希基线，再补 OpenAI 向量
        server.Handler._write_memories(uid, sid, CID, "t-emb-1",
                                       "我喜欢在海边看雨。", "")
        items = db.memory_items(uid, CID, sid)
        mid = int(items[0]["id"])
        stored = server.Handler._ensure_vectors(uid, CID, sid, cfg, limit=8)
        got = db.memory_items(uid, CID, sid)
        check("补算写入 OpenAI 向量", stored >= 1 and model_id in got[0]["vecs"],
              (stored, list(got[0]["vecs"].keys())))
        check("哈希基线向量保留", "hash" in got[0]["vecs"],
              list(got[0]["vecs"].keys()))
        check("已补算不重复请求",
              server.Handler._ensure_vectors(uid, CID, sid, cfg, limit=8) == 0)

        # 内容 hash 缓存：同内容新条目直接复用，不再请求
        before = calls["n"]
        dup = db.add_memory_item(uid, CID, sid, "preference", "我喜欢在海边看雨",
                                 embedding=me.embed("我喜欢在海边看雨"))
        server.Handler._ensure_vectors(uid, CID, sid, cfg, limit=8)
        check("同内容 hash 复用向量不请求",
              calls["n"] == before and model_id in
              db.memory_items(uid, CID, sid, limit=300)[0]["vecs"], calls["n"])

        check("补算入口可跑", server.Handler._backfill_embeddings(uid, CID, sid) >= 0)

        # 模型绑定：换模型互不混用
        db.set_memory_vector(mid, [1.0, 0.0], "openai:other")
        bound = next(x for x in db.memory_items(uid, CID, sid)
                     if int(x["id"]) == mid)
        check("换模型不覆盖旧模型向量",
              "openai:other" in bound["vecs"] and model_id in bound["vecs"],
              list(bound["vecs"].keys()))

        # MMR 多样性
        duo = [
            {"id": 1, "score": 0.9, "vec": me.embed("用户喜欢下雨天")},
            {"id": 2, "score": 0.88, "vec": me.embed("用户喜欢下雨天")},
            {"id": 3, "score": 0.7, "vec": me.embed("今天去爬山")},
        ]
        picked = me.mmr_rerank(duo, 0.3, 2)
        check("MMR 抑制同义记忆", {x["id"] for x in picked} == {1, 3},
              [x["id"] for x in picked])

        # 降级：provider 报错 → 哈希，且标记 degraded
        def boom(*a, **k):
            raise llm.LLMError("HTTP 429 rate limited")

        llm.embeddings = boom
        mid2, vecs2, deg = me.provider_embeddings(["x"], cfg)
        check("接口报错回落哈希", mid2 == "hash" and deg and bool(vecs2), (mid2, deg))

        # 查询侧超时/报错也回落，不阻塞主流程
        model_id2, qv, deg2 = me.query_vector("全新查询", cfg)
        check("查询向量报错回落哈希", model_id2 == "hash" and deg2 and bool(qv),
              (model_id2, deg2))
    finally:
        llm.embeddings = real


def test_stream_rollback():
    print("[8] <2000 字流式回滚：清屏后整段重来一次")
    from llm import LLMError
    from roleplay_core.chat_router import (ChatPorts, ChatRequest, ChatRouter,
                                            StreamDecision)
    from roleplay_core.models import ChatContext

    def run(pieces_before_error):
        events, calls = [], {"n": 0}

        def stream(_msgs):
            calls["n"] += 1
            if calls["n"] == 1:
                yield pieces_before_error
                raise LLMError("boom", status=500)
            yield "重来一次"

        ports = ChatPorts(
            load_context=lambda req: ChatContext(session_id="s1"),
            stream_llm=stream,
            on_stream_error=lambda exc, raw, req: StreamDecision(
                src="chat-llm-partial"),
            post_process=lambda t, ctx: {"display_text": t, "tagged_text": t,
                                         "status": [], "options": [],
                                         "has_image": False},
            save_assistant=lambda ctx, d, t: 1,
            emit=lambda ev: (events.append(ev) or True),
            local_reply=lambda req: "本地兜底",
            build_messages=lambda ctx: [{"role": "user", "content": "hi"}],
        )
        turn = ChatRouter(ports).chat_stream(ChatRequest(
            session_id="s1", character_id="c1", character_name="角色",
            user_input="hi", user_message_id=1))
        return turn, events, calls["n"]

    turn, events, n = run("甲" * 40 + "\n")
    check("不足 2000 字 → 清屏重来一次",
          n == 2 and any(e.get("type") == "clear_content" for e in events)
          and turn.display_text == "重来一次", (n, events, turn.display_text))
    turn, events, n = run("乙" * 2100 + "\n")
    check("超过 2000 字 → 保留半截不清屏",
          n == 1 and not any(e.get("type") == "clear_content" for e in events)
          and turn.display_text.startswith("乙" * 2100), (n, turn.display_text[:8]))


def main():
    tmp = tempfile.mkdtemp(prefix="missai-regression-")
    try:
        uid = setup_db(tmp)
        test_worldbook(uid)
        test_memory(uid)
        test_status(uid)
        test_prompt_sections()
        test_prompt_log(uid)
        test_delete_orphan(uid)
        test_cancel()
        test_stream_rollback()
        test_prompt_engine(uid)
        test_memory_slots(uid)
        test_provider_profile(uid)
        test_algorithm_memory(uid)
        test_memory_embedding(uid)
    finally:
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            db._local.conn = None
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 -> {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
