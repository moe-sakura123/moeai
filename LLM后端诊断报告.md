# LLM 后端处理逻辑深度诊断报告

> 范围：`server.py` 聊天/LLM 路由与编排、`llm.py`、`prompt_builder.py`、`roleplay_core/`、`db.py` 对话存储、`data/rules/`、聊天相关测试。
> 方式：静态精读 + 只读数据库/提示词快照抽样。**本轮未修改任何代码。**
> 结论口径：所有判断均给出文件与行号；无法从代码确证的地方标注为「推断」。

---

## 0. 结论摘要

1. **聊天不是一条管线，而是四条**：流式（`_local_chat_stream`）、非流式（`_local_chat_sync` → `_ai_reply`）、访客（guest 分支）、以及消息历史回放各自独立处理，行为已明显分叉（非流式路径不做 schema 数值钳制、不生成记忆摘要）。
2. **token 预算只覆盖历史消息，且只算正文**（`prompt_builder.py:19` / `119-136`）；system、世界书、记忆摘要、状态面板都没有总预算，实际 prompt 中 system 占比远大于历史。
3. **「长期记忆」不是压缩，而是每 4 条 assistant 消息异步追加一条摘要**；摘要游标用「该角色全部摘要条数 × 4」推算（`server.py:3691`），而摘要按角色、不按会话存储（`db.py:226-235`、`5642`），导致新会话长期不生成摘要、旧会话记忆串入新会话。**多轮后失忆与跑偏有确定性成因，不是随机现象。**
4. **数值状态没有持久化**：`chat_threads.stats_json` 只在建会话时写入初值（`db.py:5688-5705`），之后从不更新；每轮从最近消息重新解析，缺字段即回落到「初值」，不是「上一次值」（`server.py:2636-2657`）。
5. **流式内容会被整段重画**：CG 图被 hoist 到开头（`post_processor.py:151-159`）且 `say` 被归一化，最终 `display` 与已下发的原文前缀必然不同，触发 `clear_content`（`chat_router.py:167-176`）。带图的回复基本都会闪一次。
6. **`stop`/`cancel` 是假停止**（`server.py:4236-4237`）：没有取消令牌，上游流仍继续读、继续消耗 token。
7. **补全请求（`_ensure_format`）拿不到真正的状态定义**，只从 `msgs[0]` 里筛含「状态栏」的行（`server.py:3081-3084`），模型只能编属性名/数值；失败还会静默降级（`3103-3108`）。
8. **世界书跨用户泄漏**：`_chat_worldbook(cid)` 只按 `character_id` 查询、不带 `user_id`、不过滤 `custom`（`server.py:2811-2813`），任何用户为某角色建的私有世界书条目都会进入所有用户的 prompt。
9. **模型兼容与容错基本缺失**：不读 `finish_reason`、不处理 `reasoning_content`、不发送 `max_tokens`、只对 404 重试一次、超时是单次 socket 超时而非总时限。
10. **测试只覆盖到 HTTP 路由**，stub 永远不返回任何标签（`tests/stub_llm.py:46-57`），所以 `ensure_format`、状态解析、CG 后处理、记忆摘要这些最容易出错的路径实际上没有被断言。

---

## 一、真实管线与调用顺序

### 1.1 入口分发

- `handle_local_data_api` 按 URL 前缀进入本地实现（`server.py:4507`）。
- `/chat/*` 交给 `_local_chat`（`server.py:4000`），再按子命令分发：
  - `chatStream` → `_local_chat_stream`（`4233-4234`）
  - `chatSync` → `_local_chat_sync`（`3922`，由 `4000` 附近分发）
  - `guestChat` → `_local_chat_stream(..., guest=True)`（`3768-3785` 内部分支）
  - `chatStream/cancel` / `stop` → 直接返回 `{"stopped": True}`（`4236-4237`）

### 1.2 流式路径时序（主路径）

```
HTTP chatStream
 └─ _local_chat_stream(server.py:3729)
     ├─ ensure_chat_session + _initial_stats           3740-3746
     ├─ record_chat_message(user)                       3748-3749   ← 先落库
     ├─ chat_quote 余额校验                             3755-3766   ← 后校验
     ├─ 解析 endpoint / 回退开关 / failOnEmpty           3787-3802
     ├─ SSE start                                       3794
     ├─ ChatRouter.chat_stream(roleplay_core/chat_router.py:131)
     │   ├─ load_context → _chat_context(server.py:2603)
     │   │   ├─ find_work(cid)（只查 my_works）           2606
     │   │   ├─ 该会话全部消息（无 LIMIT）→ rows[-24:]    2614-2621
     │   │   ├─ _stats_schema / _initial_stats / 规则表   2631-2635
     │   │   ├─ values = thread_stats 初值               2636
     │   │   ├─ 从最近一条有状态的 assistant 覆盖 values   2647-2657
     │   │   └─ EmotionMapper.clamp + 关键词规则          2658-2664
     │   ├─ build_messages(chat_router.py:98)
     │   │   └─ PromptBuilder.build(prompt_builder.py:253)
     │   │       ├─ _trim_history（2000 token，仅正文）   119-136
     │   │       ├─ 弹出最后一条 user 作为 tail           262-267
     │   │       ├─ _worldbook_match                     211-216
     │   │       └─ [system] + history + [worldbook system] + [status system] + [tail]
     │   ├─ prepare → _inject_custom(server.py:3609)
     │   │       ├─ customSettings 按 position 合并       3618-3639
     │   │       ├─ 读 chat_memories[-N:]                3640-3645
     │   │       └─ 统一追加到 msgs[0] / 最后一条 user    3651-3663
     │   ├─ _pump → llm.chat_stream → SSE content 帧     190-219 / llm.py:275-304
     │   ├─ normalize_say_blocks → wrap_next_prompts     148-156
     │   ├─ ensure_format（可选第二次 LLM 调用）          157-159 / 3069-3123
     │   ├─ post_process → process_output                178 / post_processor.py:181
     │   │       ├─ 数值钳制（schema 或内置范围）         202-215
     │   │       ├─ 解析 first book_status               202-211
     │   │       ├─ CG 映射 + 兜底补图                    122-148 / 202-210
     │   │       └─ hoist_images（图挪到开头）            151-159
     │   ├─ 终帧：续传剩余文本，或 clear_content 全量重画  161-176
     │   └─ save_assistant → 后台 _maybe_summarize 线程    178 / 3824-3831
     ├─ 补充 _match_cg（与 post 内部重复匹配）            3872-3893
     └─ charge_chat / usage / done / [DONE]               3890-3893
```

### 1.3 非流式路径

`_local_chat_sync`（`server.py:3922`）不复用 `ChatRouter`，而是：

```
chatSync → 余额校验(3936) → record user(3941-3943) → _ai_reply(3023)
        → _llm_messages(2686) → _chat_context + PromptBuilder.build + _inject_custom
        → llm.chat（非流式）
        → _ensure_format
        → process_output（未传 schema）(3965-3966)
        → record assistant(3970-3971)
```

与流式路径的差异（都是实质性差异，不是风格差异）：

| 能力 | 流式路径 | 非流式路径 |
|---|---|---|
| 数值 schema 钳制 | 有（`ctx.stats_schema`） | 无（`schema=None`，`server.py:3966`） |
| 生成记忆摘要 | 有（`3829-3830`） | 无（只 `record_chat_message`） |
| 空回复策略 | `failOnEmpty` | 直接抛错/回退 |
| 积分扣费 | `3890-3893` | `3986` 附近 |
| 前端增量渲染 | 有 | 无 |

访客分支（`3768-3785`）更进一步：完全非流式、不走 `post_process`、原始 `<say>/<image>` 标签直接落库并下发。

### 1.4 数据在各层的流转

| 层 | 读入 | 写出 | 关键事实 |
|---|---|---|---|
| DB 消息 | `chat_messages.content` / `content_llm` | — | `content_llm` 是去标签后的版本，供后续 prompt 使用；`content` 保留 `<say>` 供回放 |
| ChatContext | 24 条消息、初值、角色卡、规则、CG | `ChatContext`（`roleplay_core/models.py:34-88`） | 状态只在内存推导，**不回写 DB** |
| PromptBuilder | `Message[]` | `list[dict]`（OpenAI messages） | 历史被二次裁剪；system 无预算 |
| LLM 网关 | messages | 纯文本 chunk | 不做标签解析 |
| 后处理 | 原始文本 | `(display, tagged, status, options, cg)` | 先流式后加工，导致「已发送不可改」 |
| 持久化 | tagged | `content`=tagged，`content_llm`=clean_for_storage(tagged) | 状态块被剥离，下一轮只能靠重新解析 |

---

## 二、提示词拼装与 token 预算

### 2.1 实际拼装顺序

`PromptBuilder.build`（`prompt_builder.py:253-286`）：

1. `_history_text(ctx.llm_history())` → `_trim_history`（`119-136`）
2. 若历史最后一条是 user，弹出为 `tail`
3. `system = _build_system_prompt(ctx)`（`165-209`）
4. `messages = [{"role":"system", content: system}]`
5. `+ trimmed history`
6. `+ {"role":"system", 世界书}`（`211-216`，有命中才加）
7. `+ {"role":"system", 状态面板}`（`218-251`，始终加）
8. `+ {"role":"user", tail + FORMAT_REMINDER}`（`284`）

随后 `_inject_custom` 再把自定义设定、记忆摘要**字符串拼接进 `msgs[0]`**（`server.py:3651-3657`），`afterUser` 拼到最后一条 user。

问题：

- **分层语义被破坏**：`PromptBuilder` 好不容易把「稳定前缀 / 历史 / 世界书 / 状态 / 当前输入」分开放置，`_inject_custom` 又把记忆和设定塞回第一个 system。记忆是每轮都可能变化的内容，放在前缀位等于每轮让 prefix cache 从第一个 token 起失效。
- **无「不可信内容」隔离**：角色卡 `description`、世界书 `content`、用户自定义设定都是自由文本，直接拼进 system，没有分隔符、没有优先级声明。角色卡里一句「忽略以上所有设定」即可越权（提示注入）。
- **系统提示词有两条格式契约**：`FORMAT_RULES`（`prompt_builder.py:39-53`）和 `_ensure_format` 的补全指令（`server.py:3109-3123`）各写一套，属性名/边界描述不一致。

### 2.2 预算的真实行为

| 段落 | 预算 | 证据 |
|---|---|---|
| 历史 | 2000 token | `prompt_builder.py:19` |
| system（角色卡 + 格式规则 + CG 目录 + postPrompt） | **无上限** | `165-209` |
| 世界书 | **无上限**，命中即全量拼接 | `2826-2857` |
| 状态面板 + 情绪档位 + 规则表 | 面板无上限；规则表最多 1800 字符 | `218-251` / `server.py:2791-2805` |
| 记忆摘要 | 最多 `memKeep × 1500` 字符，无 token 约束 | `server.py:3645`、`3722` |
| 自定义设定 | 无上限 | `3618-3639` |
| 输出 | **默认不下发 `max_tokens`** | `server.py:2991-3006`、`llm.py:154-156` |

实测：`my_works` 中角色 `description` 为 7686 字符，而最近一次 prompt 快照的 `system` 为 2236 + 662 字符、user 234。也就是说**历史预算（2000 token ≈ 1000-1800 中文字）比系统提示词本身还小**，历史被系统提示词挤成了配角。

更关键的是 `max_tokens` 不下发：`_model_params` 在用户未保存模型参数时返回 `{}`（`2991-3006`），`llm._chat_payload` 只在 `max_tokens` 为真值时写入（`135-144`），`llm.chat` 默认 `max_tokens=None`（`154-156`）。前端展示的默认 8192（`server.py:4545-4550`）只是 UI 文案，不保证进入请求体。结果是输出长度交给服务商默认值，长回复在状态栏中间被截断，随后又触发一次补全调用。

### 2.3 截断错位清单

1. **只算正文、不算 role/结构开销**：`_trim_history` 对 `content` 调 `count_tokens`（`prompt_builder.py:112-117`、`128-134`），一条消息至少漏算 role 字段与分隔符；在 2000 token 预算下误差可达 5%-10%。
2. **超预算的第一条永远保留**：`if kept and used + tokens > budget: break`，最新一条无论多长都会进入（`131-136`）。单条 5000 字的长回复可把窗口压到只剩它自己。
3. **窗口可能以 assistant 开头**：裁剪只按「从新到旧」截断，可能丢掉与保留 assistant 配对的 user，模型看到「assistant 先说话」的开场；部分服务商会直接 400。
4. **两层窗口叠加且互不感知**：先 `rows[-24:]`（`server.py:2621`），再 2000 token；前者按条数、后者按正文 token，谁先截断不可预测，且都不记录「丢了什么」。
5. **历史中连续两条 user 是合法输入**：`_prev_status` 与 `_chat_context` 都假设 user/assistant 交替，但失败回合会留下孤立的 user 消息（见 `server.py:3748-3766`）。实测 `chat_prompt_log` 第 3 条快照就是 `... assistant, user(31), user(134)`。
6. **状态块被落库前剥离，但 prompt 仍靠「重新解析」**：`clean_for_storage` 去掉 `<book_status>`（`sanitizer.py:178-192`），下一轮却从 `content` 里解析状态（`server.py:2647-2657`）。一旦某轮没写状态，就会回退到更早的一条或初值，而不是「上一轮的值」。
7. **世界书命中不排序、不去重、不截断**：`_worldbook_match` 把所有命中条目拼接（`2826-2857`），关键词命中数越多，prompt 越可能超出模型上下文。

---

## 三、长期记忆与上下文压缩

### 3.1 现状

- **没有真正的上下文压缩**。只有：
  - 硬窗口：24 条消息（`server.py:2621`）+ 2000 token（`prompt_builder.py:19`）。
  - 记忆摘要：每 4 条 assistant 消息生成一条，注入最近 `memKeep` 条（`server.py:3666`、`3824-3831`、`3640-3645`）。
- 摘要生成是后台线程（`3829-3830`），模型用**默认端点**而不是用户当前选择的模型（`3695`），15 秒单次流式，超过 2000 字符直接 `break`（`3715-3720`），最终截到 1500 字符（`3722`）。
- 摘要是「按角色」存储，`session_id` 字段写了但查询不用（`db.py:226-235`、`5642`）。

### 3.2 已确证的缺陷

**缺陷 A：摘要游标跨会话错位 → 新会话长期不生成摘要**

`done = len(db.chat_memories(uid, cid)) * 4`（`server.py:3691`）。`chat_memories` 返回的是该角色**所有会话**的摘要。实测同一角色有 5 条摘要，其中 2 条属于已删除的 session 1、3 条属于已删除的 session 7。此时新开会话 13，`done=20`，而新会话只有 3 条 assistant 消息，`turns[20:]` 为空，`return`（`3692-3694`）——**新会话必须积累 20 条 assistant 消息后才可能第一次生成摘要**，而这 20 条早已被 24 条/2000 token 窗口挤掉了。

**缺陷 B：跨会话记忆串味**

`db.chat_memories(uid, cid)` 不带 `session_id` 过滤（`db.py:5642`），`_inject_custom` 直接取 `[-mem_keep:]`（`3645`）。实测 session 13 会拿到 session 7 的 3 条摘要（内容是雨夜书店、路人、毛巾大麦茶——属于另一个开局）。角色会把另一个存档的剧情当成当前发生的事。

**缺陷 C：摘要覆盖的不是「被窗口丢弃的那一段」**

摘要是「从第 done×2 条开始固定取 8 条」（`3706`），不是「取即将被裁剪的历史」。所以：
- 窗口裁剪在第 10 条附近发生时，摘要可能还在总结第 2-8 条；
- 长对话中最需要被压缩的中段（第 12-30 条）既不在窗口里，也没被摘要覆盖；
- 因为 `seg` 用「最后一条是 user 才追加 assistant」构造（`3701-3705`），遇到孤立 user 消息会整体错位一位，后续每一条摘要都偏一段。

**缺陷 D：删除会话不删记忆**

`delete_chat_history` 只删 `chat_threads`（`db.py:5888-5893`），`chat_messages` 因外键级联删除，但 `chat_memories` 没有外键、也没有显式清理（`226-235`）。实测删除 session 1/7 后，5 条记忆仍在，并继续注入。用户主动「删除历史」并不能清除角色对旧剧情的记忆。

**缺陷 E：摘要线程无并发控制**

每次 assistant 落库后都 `Thread(...).start()`（`3829-3830`），没有「该会话正在摘要」的锁或幂等键。连续两轮间隔 < 摘要耗时时，两个线程会用同一个 `done` 生成内容相近的两条摘要，进一步加剧 A/C。

### 3.3 多轮之后会不会失忆/跑偏？

**会，而且是可预测的**：

1. 10-20 轮后，早期剧情同时掉出 24 条窗口和 2000 token 窗口；
2. 此时摘要大概率还没覆盖到那一段（缺陷 C），或者新会话根本没摘要（缺陷 A）；
3. 数值状态在某轮缺状态块时回到初值（`server.py:2636-2657`），好感度/SAN 直接「归零」；
4. 角色卡若不在 `my_works`（只有 `works` 内容库或镜像角色），`has_payload=False`（`prompt_builder.py:204-206`），system 退化成一句通用人设，角色直接失去细节；
5. 世界书还会把别的用户的私有设定注入进来（见下），进一步污染人设。

---

## 四、情绪与数值如何进入 prompt，是否真的影响生成

### 4.1 进入路径

1. 上一轮 assistant 的 `content` 被 `parse_status` 解析（只取**第一个** `<book_status>`，`post_processor.py:65-83`）；
2. `values` 先取 `thread_stats` 初值，再用最近一条有状态的 assistant 覆盖（`server.py:2636-2657`）；
3. `EmotionMapper.clamp_stats` 按平台 meta 钳制（`2658`）；
4. `_status_rules` 根据当前消息 + 最近 4 条历史的关键词，决定是否把**整张档位表**注入（`2763-2805`）；
5. `_build_status_panel` 把「属性当前值 + 当前档位情绪/行为」写成一段 system（`prompt_builder.py:218-251`）；
6. `FORMAT_REMINDER` 要求回复结尾必须带状态栏（`56-61`）。

### 4.2 实际效果：提示层生效，执行层不受控

- **只有文案约束，没有服务端 enforcement**。`DEFAULT_STATUS_RULE` 写「单次变化幅度不超过 5」（`prompt_builder.py:34`），但代码里没有任何增量校验；`_clamp_status_text` 只钳上下界（`post_processor.py:86-102`）。模型一次 +50，服务端照单全收。
- **规则表是按关键词门控的**（`server.py:2780-2789`）。当前消息与最近 4 条 `content_llm` 里没有命中词时，整张档位表不会注入，只剩「当前档位」一句话；模型看不到「达到下一档需要什么」，好感度增长就靠猜。
- **情绪只来自数值映射**，不来自剧情语义。`EmotionMapper` 用区间匹配（`emotion_mapper.py:108-138`），同一档位下无论刚被救还是刚被背叛，注入的「情绪/行为」完全一样。
- **状态面板与补全契约冲突**：面板说「属性名与数值按角色设定填写」，补全请求却把完整属性表/当前值截断了（`server.py:3109-3123` 只带 base 文本和极简 spec），模型只能编。实测流程里 `_ensure_format` 的 `spec` 只筛到 `msgs[0]` 中含「状态栏」的规则行（`3081-3084`），属性名一个都没带。
- **数值不持久化**（`db.py:5688-5705` 只在建会话时 seed）。`stats_json` 永远是 `{"好感度":0,"SAN值":70}`（实测 thread 13），前端若读该字段会与消息里的真实状态不一致。

结论：情绪/数值**会进入 prompt，也会影响文风**（尤其当规则表命中时），但**不会稳定约束行为**；数值是「模型自报 + 事后钳制」，既没有增量规则校验，也没有跨轮状态机，长对话里必然漂移。

---

## 五、流式输出与后处理的鲁棒性

### 5.1 流式阶段

- `stream_hold_start` 只保留「最后一行」和两类行（状态块、选项行）（`sanitizer.py:118-145`）。对单行长文本，超过 60 字符后只保留尾部 20 字符（`130-135`），会**把一行之内未闭合的 `<say>` / `<image>` 标签切开发送**，前端可能瞬时渲染出裸标签。
- 状态块只在「行首」时被 hold（`126-127`）；若模型把 `<book_status>` 写在句子中间，`_parse_status` 能解析（`post_processor.py:65`），但流式阶段会先把整块当正文发出去，最终只能靠 `clear_content` 重画。
- `_maybe_emit_status` 每次 flush 都对**累计全文**重跑 `parse_status`（`chat_router.py:212-219`）。每条回复的解析成本是 O(n²)，长回复 + 高帧率时 CPU 明显。

### 5.2 收尾阶段：先发后修，已发不可逆

`ChatRouter.chat_stream`（`chat_router.py:161-176`）：

```python
if display != sent_prefix:
    emit clear_content then full redraw
```

而 `post_process` 几乎必然让 `display` 变化：

- `hoist_images` 把 `<image>` 从行内挪到最前（`post_processor.py:151-159`）；
- `normalize_say_blocks` 把 `<say>` 里括号外的对话移出去、重排空白（`sanitizer.py:21-34`）；
- `wrap_next_prompts` 收集并重排状态块/选项块（`58-115`）；
- 兜底 CG 会**新增**一张图到开头（`post_processor.py:202-210`）。

后果：**只要回复带图或模型没严格按标签格式输出，前端就会闪一次「清空 + 整段重画」**。这是当前流式体验最明显的结构性问题。

### 5.3 异常输出会发生什么

| 异常输出 | 实际行为 | 证据 |
|---|---|---|
| 没有 `<book_status>` | 触发一次 `_ensure_format` 二次调用；失败则静默返回原文 | `server.py:3103-3108` |
| 没有 `<next_prompt>` | 同上；若模型返回的选项不足 2 条，前端无按钮 | `post_processor.py:105-111` |
| 状态块未闭合 / 拼写错误 | `parse_status` 不匹配，原始标签残留在 display 里 | `post_processor.py:65`、`sanitizer.py:10-14` |
| 同一回复多个 `<book_status>` | 只取第一个做数值；`wrap_next_prompts` 却把所有状态块挪到末尾 | `post_processor.py:65-83` vs `sanitizer.py:58-115` |
| 多个 `<next_prompt>` | 同上，取第一个块；其余被原样保留 | `post_processor.py:105-111` |
| `<image>` 的 cgID 不存在 | 静默丢弃，无任何提示/日志 | `post_processor.py:129-138` |
| 数值非数字（如「很高」） | `status_dict` 原样返回字符串，前端可能 `NaN` | `post_processor.py:46-49`、`73-78` |
| 模型只返回 reasoning/tool_calls | `content` 为空 → 视为空回复，可能 fatal | `llm.py:147-151`、`227-265` |
| 输出超长 | `record_chat_message` 静默截到 20000 字符；display 与落库不一致 | `db.py:1115-1122` 附近 |
| `<say>` 永远不闭合 | 正则兜底到结尾，文本能保留，但动作/对白边界可能错 | `sanitizer.py:10`、`21-34` |
| 全角/半角括号混用 | 只识别 `（…）` 动作，半角 `(…)` 会被当成对白移进 `<say>` | `sanitizer.py:25-27` |

另外：`<image>` 的 `cgID` 只校验「属于该角色」，不校验 URL 域名（`post_processor.py:140-148` 仅补全相对 URL），若上游返回 `javascript:`/`data:` 需要前端自行防护。

### 5.4 「假停止」

`4236-4237` 直接返回 `stopped: True`。流式循环里唯一的「客户端还在不在」检测是 `self._emit` 的写失败（`chat_router.py:224-239`），且只有在需要 flush 时才检测。用户点停止后：上游连接不会断、token 继续产出、直到下一次写 SSE 失败才退出；若回复被 hold 住（无换行），可能持续很久。

---

## 六、超时、重试、模型不兼容时的降级

### 6.1 超时

- `llm._request` 非流式 `timeout=timeout`，默认 90s（`llm.py:54-94`）。
- 流式 `urlopen(timeout=timeout)` 的语义是**单次 socket 操作超时**，不是整段生成时限：只要上游每隔 `<timeout` 发一个 heartbeat，就能无限拖长。
- 服务端选择的流式超时非常短：`timeout = 10 if fb else 60`（`server.py:3579-3592`）。开启回退时 10 秒无数据就判失败；正常模式 60 秒内没新块就失败。**注意这不是总时长**，慢模型可能在前 10 秒没吐第一个 token 就被判定超时。
- 没有整体 deadline、没有 `requests` 的 read/connect 分离、没有取消传播。

### 6.2 重试

- 只有一次「404 → 补 `/v1` 再试」(`llm.py:103-108`)，且仅限首个 chunk 之前（`chat_stream` `291-304`）。
- 没有 429/5xx/网络抖动的重试，没有指数退避，没有 `Retry-After`。
- `_ensure_format` 失败不重试、不告警（`server.py:3103-3108`）。
- 摘要线程失败只 `print` 到 stderr（`3726-3727`），没有指标、没有补跑。

### 6.3 不兼容与降级

- 只支持 OpenAI 兼容 `/chat/completions`，不支持 Anthropic/Gemini 原生协议（`llm.py` 全篇）。
- 不读 `finish_reason`（`llm.py:163-174`、`251-263`）。`finish_reason="length"` 时把半截文本当完整回复落库、计费、再触发补全。
- 不处理 `reasoning_content`（DeepSeek/GLM 等推理模型只在这一字段时会被判「空回复」）。
- 不处理 `content` 为 `null` 的 tool_call 响应。
- 请求体里 `temperature=0.8` 硬编码（`llm.py:154-156`），而前端默认展示 0.7（`server.py:4547`）；`top_p/presence_penalty` 等任意参数被透传（`_chat_payload` `extra`），服务商不支持时直接 400，没有「剔除未知参数再试」的降级。
- 没有能力探测：不同模型对 `max_tokens` / `max_completion_tokens` / `stream_options` 的支持差异全靠上游报错暴露。
- 超时回退开关默认关闭（`server.py:2984-2988`），生产环境更接近「一次失败 = 用户看到错误」。

---

## 七、代码结构上的耦合、重复与难维护点

1. **`server.py` 是 272KB 的上帝模块**，聊天编排、提示词拼装、后处理、CG、积分、SSE、作品/世界观/后台管理全在一个文件里；`make_server()` 内部嵌套函数层层闭包。
2. **四条聊天路径重复实现同一套业务**：`_local_chat_stream` / `_local_chat_sync` / guest / 历史回放。任何一处修复（如 schema 钳制、记忆摘要）都不会自动传播到另外三处。
3. **同一个「状态解析」有至少三套正则**：
   - `post_processor.py:11-17`（`<book_status[^>]*>`）
   - `sanitizer.py:10-14`（`<book_status>` 严格）
   - `server.py:3054-3067`（`_prev_status` 自己再写一套）
   外加 `_ensure_format` 里再拼一段自然语言。改标签格式要改四处。
4. **同名函数遮蔽**：模块级 `_match_cg(text, cg_list, weights)`（`server.py:357`）与 `make_server` 内的 `_match_cg(uid, cid, text, sid)`（`2860`）同名不同签名。
5. **职责边界混乱**：
   - `PromptBuilder` 负责分层，`_inject_custom` 负责把层级打乱；
   - `ChatRouter` 负责编排，但 CG 兜底匹配又由 server 在 router 之外再跑一次（`3872-3893`）；
   - `_local_reply`（`2591`）是产品话术，却写在 HTTP handler 闭包里。
6. **`ChatPorts` 用 8 个回调注入**（`roleplay_core/chat_router.py:266-273`），其中多个是 `None` 可选，`if self._ensure is not None` 这类隐式开关（`159`）让「有没有补全」变成运行时状态，难以单测。
7. **状态在实例上而非回合上**：`ChatRouter` 每次请求新建，`_last_status` 只在单次流内去重；跨回合的状态完全靠重新解析文本，没有 `TurnState` 实体。
8. **无 trace ID**：`print` 到 stderr，prompt 日志与一次 SSE 连接无法关联；`chat_prompt_log` 无清理策略（`db.py:238-250`），长期会膨胀。
9. **测试与实现脱节**：`selftest.py:768-813` 只断言 HTTP 200/字段存在；`selftest.py:815-825` 只测 `wrap_next_prompts`；PromptBuilder、EmotionMapper、`_trim_history`、`process_output` 的状态/CG 分支都没有单测。`tests/stub_llm.py:46-57` 永远返回不含任何标签的 `MOCK`，所以 `_ensure_format` 每次都会发第二次请求，但测试从不检查它是否发生、是否成功。

---

## 八、问题清单（按严重度）

### P0（正确性 / 数据隔离 / 直接导致失忆或跑偏）

| ID | 问题 | 证据 | 影响 |
|---|---|---|---|
| P0-1 | 世界书按 `character_id` 全局读取，跨用户泄漏私有设定 | `server.py:2809-2823`（`2811-2813`） | 他人私有世界书进入你的 prompt；人设污染；隐私泄露 |
| P0-2 | 记忆摘要按角色不按会话，跨存档串味 | `db.py:226-235`、`5642`；`server.py:3645` | 新开局继承旧剧情，角色「记得没发生的事」 |
| P0-3 | 摘要游标用「全部摘要数×4」，新会话长期不摘要 | `server.py:3691-3694` | 新会话前 20 条 assistant 无任何压缩，必然失忆 |
| P0-4 | 数值状态不持久化，缺字段回落到初值而非上一轮 | `db.py:5688-5705`；`server.py:2636-2657` | 好感度/SAN 突然归零，行为档位跳变 |
| P0-5 | 总预算缺失：system/世界书/记忆无上限 | `prompt_builder.py:19`、`165-216`；`server.py:2826-2857`、`3645` | 上下文溢出、成本失控、历史被挤没 |
| P0-6 | `max_tokens` 默认不下发，输出被服务商默认值截断 | `server.py:2991-3006`、`4545-4550`；`llm.py:135-156` | 回复/状态栏半截，触发补全与重画 |
| P0-7 | 补全请求缺少真实属性表与当前值 | `server.py:3081-3108` | 模型编属性、编数值，状态栏与真实状态脱节 |
| P0-8 | 数值增量规则只在提示词里，代码无校验 | `prompt_builder.py:34`；`post_processor.py:86-102` | 单轮暴涨/暴跌，数值系统形同虚设 |
| P0-9 | 流式已发内容被整段重画（CG hoist / say 归一化 / 兜底图） | `post_processor.py:151-159`、`202-210`；`chat_router.py:167-176` | 所有带图回复闪屏；长回复重绘成本高 |
| P0-10 | 非流式/访客路径行为分叉（无 schema 钳制、无记忆、无后处理） | `server.py:3965-3966`、`3970-3971`、`3768-3785` | 同一角色在不同入口数值/记忆/格式不一致 |
| P0-11 | `stop/cancel` 假停止 | `server.py:4236-4237`；`chat_router.py:224-239` | 上游继续生成并计费，用户以为已停止 |
| P0-12 | 用户消息先落库、后校验积分 | `server.py:3748-3749` vs `3755-3766` | 余额不足也留下孤立 user 消息，污染下一轮 prompt（实测 prompt log 连续 user） |

### P1（可靠性 / 成本 / 可维护性）

| ID | 问题 | 证据 | 影响 |
|---|---|---|---|
| P1-1 | 不读 `finish_reason`，截断当成功 | `llm.py:163-174`、`251-263` | 半截回复落库+计费；后续补全浪费 |
| P1-2 | 不处理 `reasoning_content` / `content:null` | `llm.py:147-151`、`227-265` | 推理模型被判空回复 |
| P1-3 | 只对 404 重试一次，无 429/5xx/退避 | `llm.py:103-108`、`291-304` | 网络抖动=用户可见失败 |
| P1-4 | 超时是单 socket 超时，非总时限；10s 回退阈值过激 | `llm.py:275-276`；`server.py:3579-3592` | 慢模型首 token 前被误杀；长流可无限拖 |
| P1-5 | 参数/能力不兼容无降级（400 即死） | `llm.py:135-144` | 换模型/换中转站即不可用 |
| P1-6 | 流式处理 O(n²)：每帧全文解析 | `chat_router.py:212-219`；`sanitizer.py:130-135` | 长回复 CPU 高、延迟抖动 |
| P1-7 | 历史读取全表 + 每轮多次 DB 查询 | `server.py:2614-2621`；`2936-2953`、`3820`、`3872` | 长会话变慢，移动端明显 |
| P1-8 | 记忆注入破坏 prefix cache | `server.py:3651-3657` | 每轮全量重算缓存，成本上升 |
| P1-9 | 摘要线程无锁/无幂等，且用默认模型 | `server.py:3695`、`3824-3830` | 重复摘要、失败静默、模型不一致 |
| P1-10 | 删除历史不删记忆 | `db.py:5888-5893` | 「删除」后角色仍记得旧剧情 |
| P1-11 | CG 匹配跑两遍，`triggerOnce` 有副作用 | `server.py:3820`、`3872-3893` | 重复查询；一次性 CG 被提前消费 |
| P1-12 | 访问控制：`find_work` 不校验 owner/status | `db.py:3497-3501`；`server.py:2606` | 任意用户可按 cid 聊他人草稿 |
| P1-13 | prompt 日志保存完整人设/世界书，无脱敏/清理 | `db.py:238-250`；`server.py:5780` 附近 | 隐私与磁盘膨胀 |
| P1-14 | 不可信文本直接拼 system | `prompt_builder.py:165-209`；`server.py:3618-3657` | 提示注入可覆盖人设与规则 |
| P1-15 | 计费按估算 token，忽略上游 usage | `db.py:1125-1157`；`server.py:3890-3893` | 与真实成本偏离；争议难解释 |
| P1-16 | 超长落库静默截断 20000 字符 | `db.py:1115-1122` 附近 | display 与历史不一致，下轮 prompt 失真 |
| P1-17 | 测试未覆盖核心逻辑，stub 不产出标签 | `tests/stub_llm.py:46-57`；`selftest.py:768-825` | 回归无保护，补全/解析问题长期潜伏 |

### P2（结构与体验）

| ID | 问题 | 证据 |
|---|---|---|
| P2-1 | `server.py` 272KB 上帝模块 | 全文件 |
| P2-2 | 同一聊天业务四套实现 | `3729`、`3922`、`3768`、`5844` |
| P2-3 | 状态/选项/标签正则三处重复 | `post_processor.py:11-17`、`sanitizer.py:10-14`、`server.py:3054-3067` |
| P2-4 | 同名 `_match_cg` 两个签名 | `server.py:357`、`2860` |
| P2-5 | `ChatPorts` 8 回调 + None 隐式开关 | `roleplay_core/chat_router.py:266-273` |
| P2-6 | 无 turn/trace ID，日志 stdout | `server.py:3726` 等 |
| P2-7 | 角色未知时静默退化通用人设，无提示 | `prompt_builder.py:204-206` |

---

## 九、目标架构

### 9.1 分层与模块

```
api/chat_http.py          # 只做 HTTP/SSE 编解码、断连检测、取消令牌
app/chat_turn.py          # 唯一编排入口：一次 turn 一个用例，流式/非流式共用
context/loader.py         # 会话、角色卡、世界书（按 user 作用域）、状态快照
context/state.py          # 数值状态机：持久化 + 增量校验 + 档位映射
prompt/planner.py         # 总预算分配（按 section priority）
prompt/sections.py        # 每段的渲染器，输出 (text, tokens, priority)
prompt/renderer.py        # 组装 messages，保证稳定前缀
llm/gateway.py            # deadline、重试、退避、能力矩阵、fallback 链
llm/providers/*.py        # openai_compat / anthropic / gemini 适配器
output/parser.py          # 增量状态机：text/say/image/status/options
output/repair.py          # 截断续写、缺块补全（带完整 schema）
output/formatter.py       # 生成 display / tagged / 事件
memory/store.py           # 会话摘要 + 游标 + 长期事实
store/repos.py            # chat_threads/messages/summaries/facts
```

### 9.2 核心契约

```text
TurnInput      : uid, sessionId, characterId, text, options, modelId, customSettings
ContextSnapshot: character, worldbook(user-scoped), history, status, memories
PromptPlan     : sections[{id, priority, text, tokens, trimPolicy}], totalBudget
ModelRequest   : baseUrl, model, messages, params(capability-filtered), deadline
StreamEvent    : text_delta | tag_start/tag_end | status | image | option | finish(reason)
TurnResult     : display, tagged, statusPatch, images, usage, diagnostics{repairs, truncation}
```

### 9.3 关键设计原则

1. **单一编排路径**：`chat_turn.run(TurnInput)` 同时服务 stream 与 sync，模式只影响传输层，不影响业务（钳制、记忆、后处理必须在同一条代码路径）。
2. **预算即一等公民**：`budget = model.context_window - max_tokens - safety`；section 有优先级，按优先级分配；历史拿剩余额度；**绝不在消息中间截断**，要么整条丢，要么整条留；日志记录每段 token。
3. **稳定前缀**：persona/format contract 放最前且跨轮不变；状态、记忆、世界书放 system 尾部或用户消息前；每轮只让尾部变化，保护 prefix cache。
4. **数值状态机**：每轮从 `statusPatch` 合并进已持久化的 `TurnState`，校验增量（如 ±5 或按规则表），越界拒绝并回写诊断；`stats_json` 成为唯一真相源。
5. **记忆分区**：`chat_summaries(session_id, covered_until_message_id, content)`，按会话隔离；另建 `character_facts(user_id, character_id, fact)` 承载跨会话的「设定级事实」，需显式开关。摘要只覆盖**被窗口淘汰的那段**，游标持久化，DB 唯一约束防重复。
6. **增量解析器**：流式阶段用状态机逐字符/逐行解析，标签未闭合就不输出；`<image>` 作为独立事件发出，而不是事后把文本重排。**已发送的文本永不重画**；需要修正时发 `patch` 事件，由前端就地替换，而不是 `clear_content`。
7. **错误分类**：
   - Retryable（429/5xx/连接重置/首 token 前超时）→ 退避重试 1-2 次；
   - Capability（400 参数不支持）→ 剔参数重试一次；
   - Truncation（`finish_reason=length`）→ 自动续写一次，合并后重新解析；
   - Empty → 修复请求一次，带完整 schema；
   - Fatal → 明确错误事件 + 回滚本轮 user 消息（或标记 pending）。
8. **取消令牌贯穿**：HTTP 断连 → `CancellationToken.cancel()` → 网关关闭响应流 → 上游停止 → 未完成 turn 标记 aborted，不落 assistant、不计费。
9. **可观测性**：turn_id 贯穿 prompt/memory/model/后处理日志；prompt_log 存 section 级 token、hash、finish_reason、repair 次数、上游 usage。

---

## 十、分阶段改造方案（本轮不改代码）

### 阶段 0：建立基线（无行为变更）

- 给每次 turn 加 `turn_id`，在 `_sse_frame`、prompt 日志、记忆日志、异常日志里串起来。
- prompt 日志增加 section 级统计：各段字符/token、总预算、裁剪条数。
- 建回归样本：10 组固定历史 + 固定模型输出（含缺状态、双状态块、半截标签、空回复、带图），作为后续阶段的验收集。

**退出标准**：能从一条 turn_id 还原「发了什么、花了多少、截了什么、修了几次」。

### 阶段 1：正确性止血（P0 中不与架构冲突的部分）

1. 世界书按 `user_id + character_id` 查询，区分 `custom` 与全局条目（`server.py:2811`）。
2. 记忆摘要按 `session_id` 存储/查询，游标改为「该会话已覆盖的 message_id」，不再用 `摘要数×4`（`3691`）；删除会话时清理该会话摘要或明确保留策略。
3. 状态持久化：每轮把校验后的 `statusPatch` 写回 `chat_threads.stats_json`；缺字段继承上一轮，不再回初值。
4. `max_tokens` 真正下发（取用户配置，否则按模型上限留出余量）；解析 `finish_reason`，`length` 单独处理。
5. 用户消息在「余额校验 + 端点解析」通过后再落库；fatal 时回滚或标记 pending，避免孤立 user。
6. 非流式路径接入 `stats_schema` 钳制与记忆摘要；或直接删除该路径，统一走 `chat_turn`。
7. 实现真实取消令牌。

**退出标准**：新会话能正常摘要；删档后记忆清空；状态跨轮连续；余额不足不再留下孤儿消息；stop 后上游 1 秒内断开。

### 阶段 2：预算与提示词重构

1. 引入 `PromptPlan`：persona / format contract / worldbook / status / memory / history 各有优先级与 token 上限。
2. 历史预算 = 总预算 − 其他段占用；按整条裁剪，记录被淘汰的消息 id。
3. 稳定前缀：persona 与格式契约固定在 `msgs[0]`；状态/记忆/世界书放尾部；禁止把每轮变化的内容拼进前缀。
4. 世界书按相关性排序、去重、按预算截断（按条目整条丢弃）。
5. 统一格式契约：只保留一份 schema 描述，`_ensure_format` 使用完整属性表 + 当前值 + 允许变化范围。
6. 引入数值增量校验（规则表 → 代码可执行规则）。

**退出标准**：prompt 总 token ≤ 模型窗口的安全水位；同一会话连续 20 轮，历史裁剪与摘要覆盖无空洞；补全后的状态栏 100% 含合法属性且数值在界内。

### 阶段 3：流式与后处理重写

1. 用增量解析器替换「先发后正则」：`text / say / image / status / next_prompt` 各自状态，标签闭合才提交文本。
2. `<image>` 改为独立 SSE 事件，不再 hoist 文本；前端负责占位/插图。
3. 取消 `clear_content` 全量重画；需要修正时发 `patch` 事件。
4. `_ensure_format` 只补缺失块，且把补全内容作为独立事件合并，不重排已发正文。
5. 解析器覆盖：未闭合标签、多状态块、半角括号、未知 cgID、非数字数值，并输出 diagnostics。

**退出标准**：带图回复 0 次全量重画；标签不完整时前端不出现裸标签；所有异常输出都有可追踪诊断码。

### 阶段 4：记忆系统闭环

1. `chat_summaries` 游标化：只总结「被窗口淘汰的连续区间」，同一区间只生成一次。
2. 会话隔离 + 跨会话 `character_facts`（显式开关、可查看/可删除）。
3. 摘要生成使用当前会话模型；失败重试一次并记录；长对话分段摘要（map-reduce），不做 6000 字符硬截。
4. 记忆注入做预算与相关性排序，不再全量 `[-N:]`。

**退出标准**：30 轮后仍能正确引用第 1 轮的关键事实；换会话不串味；删除会话后无残留记忆；摘要条数与覆盖区间一一对应。

### 阶段 5：网关韧性与兼容

1. `ModelGateway`：总 deadline、连接/读取分离超时、429/5xx/网络重试与退避、首 token 前重试。
2. 能力矩阵：`supports(max_tokens|max_completion_tokens|stream_options|penalties)`，400 时自动剔除并重试一次。
3. fallback 链：主模型 → 备用模型 → 本地回退，按配置而非单一布尔开关。
4. 适配 Anthropic/Gemini 原生协议（至少归一化 `reasoning_content` / `content blocks` / `finish_reason`）。
5. 使用上游 `usage` 计费，保留估算作为兜底。

**退出标准**：注入 429/500/断连/400 参数错误的故障测试，全部走预期降级路径；截断回复不再被计为成功。

### 阶段 6：结构收敛

1. 拆 `server.py`：`chat_http` / `chat_app` / `context` / `prompt` / `output` / `memory` / `llm` 七个模块。
2. 删除非流式与访客的重复业务实现，统一走 `chat_turn`。
3. 正则与标签定义收敛到 `output/schema.py` 单一来源。
4. 补单测：`_trim_history` 边界、状态合并、解析器状态机、补全、记忆游标、取消。

**退出标准**：聊天核心逻辑不再散落在 HTTP handler；同一行为只有一处实现；核心模块单测覆盖关键分支。

---

## 十一、测试补强建议（对应现有缺口）

| 现有测试 | 缺口 | 应补的用例 |
|---|---|---|
| `selftest.py:768-813` 只断言 HTTP 字段 | 不校验 prompt 内容、token、标签 | 固定历史 → 断言 section 顺序、总 token、裁剪条数 |
| `selftest.py:815-825` 只测 `wrap_next_prompts` | 无状态解析/裁剪/情绪 | `parse_status` 多块/未闭合/非数字；`_trim_history` 超预算/单条超长/以 assistant 开头 |
| `tests/stub_llm.py:46-57` 永不返回标签 | 补全路径未验证 | stub 按脚本返回：无状态、双状态、半截标签、空 content、reasoning_content、finish_reason=length |
| `tests/test_chat_local.py` 只打印 | 无断言 | 断言 assistant 落库内容、content_llm、数值更新、记忆条数 |
| 无并发测试 | 摘要线程重复 | 连续两轮快速落库 → 断言只生成一条摘要 |
| 无跨会话测试 | 记忆串味 | 会话 A 产生记忆 → 新建会话 B → 断言 B 的 prompt 不含 A 的记忆 |
| 无取消测试 | 假停止 | 客户端断连 → 断言上游请求被关闭、未落 assistant、未计费 |

---

## 十二、实测证据（只读抽样）

数据库 `data/missai.db`（本次未做任何写操作，仅查询）：

- `my_works` 1 行、`works` 1 行，二者 `character_id` 相同（`char-35d9082d-...`）。说明聊天角色卡只来自 `my_works`；仅存在于 `works`/镜像库的角色进入聊天会 `payload=None`，`has_payload=False`，退化为通用人设（`prompt_builder.py:204-206`）。
- `chat_threads` 4 行，其中 2 行 `character_id IS NULL`、`user_id IS NULL`（访客线程）；`stats_json` 只有 thread 13 有初值 `{"好感度":0,"SAN值":70}`，且**从未更新**。
- `chat_messages`：thread 5 只有 1 条 user 消息（失败回合留下的孤立消息）；thread 3 的角色不在 `my_works`、也不在 `works`，仍产生了 301 字符回复。
- `chat_memories` 5 条：2 条属于已删除的 session 1，3 条属于已删除的 session 7；它们仍会被 session 13 的 `_inject_custom` 读取（`3645`）。
- `chat_prompt_log` 第 3 条快照的消息序列为 `system, user, assistant, user, user`，直接印证了「失败回合留下孤立 user」导致连续两条 user 进入 prompt。

---

## 附录：证据索引（核心行号）

- 流式编排：`server.py:3729-3920`
- 非流式编排：`server.py:3922-3998`；`_ai_reply` `3023-3052`
- 上下文构建：`server.py:2603-2683`（历史 2614-2621、状态 2636-2657）
- 世界书：`server.py:2809-2857`
- 状态规则：`server.py:2763-2805`
- 补全：`server.py:3069-3123`
- 记忆注入：`server.py:3609-3663`
- 摘要生成：`server.py:3668-3727`
- 取消：`server.py:4236-4237`
- 提示词：`prompt_builder.py:19`、`39-61`、`112-136`、`165-251`、`253-286`
- 流式路由/重画：`roleplay_core/chat_router.py:131-187`、`190-239`
- 后处理：`roleplay_core/post_processor.py:26-58`、`65-111`、`122-159`、`181-221`
- 清洗：`roleplay_core/sanitizer.py:10-34`、`58-145`、`178-192`
- 情绪：`roleplay_core/emotion_mapper.py:108-138`
- 模型网关：`llm.py:54-108`、`135-174`、`227-304`
- DB：`db.py:205-250`、`1115-1157`、`3497-3501`、`5639-5656`、`5665-5734`、`5737-5766`、`5844-5900`
- 规则：`data/rules/affection_rules.json`、`data/rules/san_rules.json`；DB 合并 `db.py:4817-4930`、`4982-5004`
- 测试：`selftest.py:768-825`；`tests/test_chat_local.py`；`tests/stub_llm.py:46-57`

---

**最终判断**：当前系统的提示词拼装与流式后处理在「单轮、格式规整、无图、短对话」时可用；一旦进入「多轮、带图、跨会话、换模型」场景，失忆、跑偏、重画、假停止、跨用户设定泄漏会同时出现。这些问题不是调参能解决的，根因是**状态未持久化、预算未统一、记忆未分区、流式与后处理顺序颠倒、四条路径各自为政**。建议按第十节阶段 1→6 推进，先把「状态/记忆/取消/预算」四个正确性问题锁死，再重构流式与信封解析。
