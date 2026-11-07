# 本项目 LLM 后端改进方案（合并版）

> 输入：`Operit_LLM后端调研与移植方案.md`（下称 **O**）、`RisuAI_LLM后端对标分析.md`（下称 **R**）
> 目标：d:\ai_manager_workspace\missai_local（Python 标准库 + SQLite，OpenAI 兼容协议，SSE 流式，v2 唯一拼装引擎）
> 本轮只出方案，不改代码、不提交。

---

## 0. 取证边界（影响可执行性，先声明）

- **O** 的 `OpenAIProvider.kt`（145 KB）只读到前 378 行，**SSE 逐帧代码未取证**。本文不复制其 SSE 实现，只按 SSE 规范重写。
- **R** 中 `maxContext=4000`、`maxResponse=500`、`requestRetrys=2`、`loreBookToken=8000`、`depth` 索引换算等标注"未逐字复核"的默认值，本文引用时会写"约"，落地前需以实际仓库为准。
- 两份文档都只到"方案"层级，本文所有参数是**待落地默认值**，不是既有事实。

---

## 1. 关键取舍（先给结论，后文不再并列罗列）

| 议题 | 结论 | 理由 |
|---|---|---|
| 记忆融合：RRF(k=60) vs 现有 BM25+余弦线性加权 | **保留线性加权，不上 RRF** | ① 我们是"一个检索器 + 两个特征"（BM25 0.6 / 余弦 0.4，再乘 relevance 0.6、importance 0.25、recency 0.15），候选集本来就同源；RRF 用来融合**两路独立排名**，同源打分做 RRF 没有增量。② 我们有 `minScore=0.25` 相关性门槛和槽位预算，**RRF 的分值只有名次语义、不可阈值化**，引入后会破坏"低分不注入"的既有行为。③ 召回规模是单角色/单会话 `capacity=300`，topK=8，小候选集上线性融合更稳、可复现。**只有等接入真 embedding、并出现"向量 chunk 索引 + 词法索引"两路独立召回时，才启用 RRF k=60**（见 §4）。 |
| 四段贪心的"随机 20%" | **不保留** | ① 我们的记忆是四槽位（承诺 / 用户信息 / 长期 / 近期）语义，随机段会破坏槽位语义；② 随机注入不可复现，直接破坏 `tests/test_chat_regression.py` 的确定性；③ 该段本质是"预算没用完时填满"，用**低分兜底**替代更安全：剩余预算只给 `short_term`，按时间倒序，且仍要求 score ≥ `minScore`。 |
| 世界书"按优先级装满、再按 order 输出"的分离 | **照搬** | 我们 `prompt.match_worldbook` 目前用 `(order, id)` 排序后按预算截断，**"谁先占预算"和"插在哪"被同一个字段绑死**。拆开后：`priority` 决定淘汰顺序（默认等于 order，老数据行为不变），`order` 只决定注入位置。改动小、向后兼容、收益明确（高优先级短条目不会被低优先级长条目挤掉）。 |
| Token 估算：1.5×CJK+0.25×其他 vs 真分词器 | **走粗估 + 动态校准，不引入分词器** | ① 硬约束是零依赖（标准库），`tiktoken` 是 Rust wheel、`tokenizers` 是 HF 二进制，都会破坏部署形态；② 我们的预算用途是**分区比例分配**（`SECTION_SPECS` 的 ratio），不是精确计费，误差 20% 可由 `0.30/0.10/...` 的余量吸收；③ 可校准：用上游 `prompt_tokens` 回归出每个 provider 的系数（见 §3.1），精度可追到 ±10% 内，且零依赖不变。RisuAI 的真分词器方案标记为"等想破零依赖再说"。 |
| 重试：O 的 5 次指数（1→16s） vs R 的 3 次固定 1s | **取 O 的退避曲线，取 R 的状态码分类，次数折中为 4** | 交互场景首字延迟敏感，16s 末档太长；3 次又扛不住 429 抖动。定为 4 次总尝试、`1/2/4s` + 抖动，并**尊重 `Retry-After`**（上限 30s）。R 的"401/400 也重试 3 次"是明确缺陷，不采纳。 |
| SSE 分帧：O（未取证） vs R（累积缓冲全量重解析，O(n²)） | **都不照搬，按 SSE 规范重写** | R 的 Chat Completions 路径只认 `"data: "`、无空行分帧、无多行 data、无 flush，是其自列缺陷；O 的关键代码未取证。我们现 `_stream_once` 也是"按行 `data:` 单行解析"，同样缺多行与空行语义。统一重写为"行增量 + 空行定帧 + 多行 data 拼接 + EOF flush"。 |
| 输出预算：R 算了不回传（缺陷） vs O 下发 max_tokens | **按 O，并补"预算回传"** | 我们已在 `_model_params` 下发 `max_tokens`（默认 8192）。R 的 `outputTokens` 只用于展示，是明确缺陷。改为 `max_tokens = min(用户值/8192, contextWindow - est_input - 64)`。 |

---

## 2. P0：现在就该做（直接影响对话正确性与稳定性）

判断标准：不修就会"答错、截断、卡死、丢内容、误重试"。

### P0-1 SSE 解析重写（`llm.py::_stream_once`）

**做法（参数写死，可直接实现）**

1. 统一换行：`\r\n → \n`，`\r → \n`。
2. 以**空行**（`\n\n`）为一个事件边界，而不是"一行一帧"。
3. 事件内允许**多行 `data:`**，按到达顺序用 `"\n"` 拼接后再 `json.loads`。
4. 忽略 `event:` / `id:` / `retry:` / 以 `:` 开头的注释行（`:` 开头为 SSE 注释）。
5. `data` 值为 `[DONE]` → 正常结束标记。
6. 流结束（EOF）时若缓冲区还有未定帧内容 → **flush 一次**，不丢最后一帧（对应 R 缺陷 #3）。
7. JSON 解析失败：计入 `bad_frames`，跳过该帧但**记录前 3 条坏帧原文到日志**；`bad_frames / total_frames > 5%` 且 `total_frames ≥ 20` → 抛 `LLMError("上游流式帧损坏")`（避免"静默吞掉"导致半截回复当成功）。
8. 每帧按需取：`choices[0].delta.content`、`delta.reasoning_content`/`delta.reasoning`、`delta.tool_calls`、`finish_reason`、`usage`。

**落到哪**：`llm.py:_stream_once`（现有 L275 起）；`llm.py:chat_stream`（L406）继续负责"未输出前可重试"。

**验收**：mock 上游给 ①多行 data ②`\r\n` ③半截 JSON ④EOF 无 `[DONE]` ⑤空响应，五种都能正确收尾；`tests/test_chat_regression.py` 全绿。

### P0-2 错误分类 + 重试（`llm.py`、`server.py`）

**分类（决定重不重试）**

| 类别 | 状态/异常 | 动作 |
|---|---|---|
| 可重试 | 408、409、425、429、5xx、`TimeoutError`、`URLError`、连接重置 | 退避重试 |
| 不可重试 | 400、401、403、404（`/v1` 兜底那次除外）、422 | 直接报错（401/403 提示换密钥） |
| 上下文超限 | 400 且错误体含 `context_length_exceeded` / `maximum context` / `too many tokens` | 不重试同一 payload，走 P0-3 降级 |

**退避参数**：总尝试 4 次；第 n 次失败后等待 `delay(n) = 2^(n-1)` 秒（1、2、4）+ `random(0, 0.25*delay)` 抖动；若响应头有 `Retry-After`（秒或 HTTP 日期），取 `min(Retry-After, 30)` 覆盖本次 delay。

**落到哪**：`llm.py` 新增 `_classify_error(exc) -> (retryable, code, retry_after)`、`_request_with_retry()`；`chat_with_meta`（L182）、`embeddings`（L246）改走它；`chat_stream` 仅在**尚未 yield 任何片段**时重试（已下发就不重连，交给 P1-4 的回滚方案）。
**server.py**：`_decide_stream_error`（L3797）按类别返回"重试 / 降级 / 报错"三态，不再一律本地兜底。

### P0-3 上下文超限降级（`server.py`）

**降级顺序（严格按此顺序，每步后重算估算）**

1. 丢弃 `worldbook_after`、`worldbook_before` 中 `priority` 最低的条目（逐条，一次丢到预算内）。
2. 记忆 `short_term` → `long_term` → `user_info`（`promise` 最后，不丢）。
3. 历史从**最旧**开始丢，但保留 `SUMMARY_WINDOW = 24` 作为摘要窗口——**注意：24 是 v2 摘要窗口，不是硬截条数，别当 v1 逻辑删掉**。
4. 仍超限 → 触发一次即时摘要（复用 `_maybe_summarize`），再重试一次原请求。
5. 再失败 → 返回明确错误（不静默截断）。

**落到哪**：`server.py:_decide_stream_error`（L3797）、`_maybe_summarize`（L3858）、`_summarize_slots`（L3889）；裁剪顺序在 `roleplay_core/prompt.py` 新增 `shrink_for_overflow()`，复用现有 `trim_tokens`。

### P0-4 超时三段（`llm.py`）

现在只有 `urlopen(timeout=...)` 一个总超时（流式 300s），无法区分"首字节不来"和"中途卡住"。

| 阶段 | 阈值 | 语义 |
|---|---|---|
| 连接 | 10 s | TCP/TLS |
| 首字节 | 非流式 30 s / 流式 60 s | 超过即视为网关问题，可重试 |
| 空闲 | 120 s | 两个 SSE 帧之间无数据 |
| 总时长 | 300 s | 兜底 |

**落到哪**：`llm.py:_request`（L92）与 `_stream_once`；用线程计时器或 `socket.settimeout` 分段设置，不引入 asyncio。

### P0-5 `finish_reason` 与空响应（`llm.py`、`chat_router.py`）

- 已读 `finish_reason` 但未使用：`length` → 在 `ChatTurn` 标记 `truncated=True`，前端提示"回复被截断"；不自动续写（避免死循环）。
- `stop` 且内容为空 → 视为失败（可重试一次），而不是当成功落库。
- `content_filter` → 直接报错。

**落到哪**：`llm.py:chat_with_meta`（已有 meta）、`_stream_once`；`roleplay_core/chat_router.py:ChatTurn` 增加字段，`_pump` 透传。

---

## 3. P1：质量与成本

### P1-1 Token 估算与动态校准（`llm.py` 新增 `token_estimate.py` 或放 `prompt_builder`）

**基础公式（O）**

```
tokens = ceil(1.5 * CJK + 0.25 * (len - CJK)) + 4 * 消息数
CJK = [0x4E00, 0x9FFF]
```

- 每条消息 +4（role/分隔开销，R 的 3/5 取中间值）。
- 图片（若本轮带图）：低清固定 87；高清按 `ceil(w/512) * ceil(h/512) * 2 + 85`，先等比缩到最长边 ≤768（R 公式）。
- 工具定义（未来）：`tools_json` 字符串同样过公式。

**校准（补 O 没有的部分）**

```
ratio = actual_prompt_tokens / estimated
α_new = 0.7 * α_old + 0.3 * ratio        # EMA
仅在 est >= 200 且 0.6 <= ratio <= 1.8 时更新
α 夹在 [0.75, 1.6]，初值 1.0；按 (provider_id, model) 分键存 db.config
最终估算 = clamp(round(α * base), 1, ∞)
```

**落到哪**：`prompt_builder.count_tokens`（被 `prompt._tok`/`trim_tokens` 调用）改为调新函数；`server.py:_prompt_config`（L3044）读出 α 注入。

**为什么不是 tiktoken**：见 §1；另注意 R 的"只按全局 aiModel 选分词器"本身就是缺陷，我们按 (provider, model) 校准天然避开。

### P1-2 usage 采集与回填（`llm.py`、`server.py`）

- 请求：`stream_options: {"include_usage": true}`，**按 `llm_profile.include_usage` 开关，默认关**；首次成功解析到 usage 后自动置 1，连续 3 次解析不到置 0（部分中转站不支持）。
- 解析：末帧 `usage.prompt_tokens / completion_tokens / total_tokens`；OpenAI 新字段 `cached_tokens`（`prompt_tokens_details.cached_tokens`）单独记。
- 回填：`on_finish` 带 `{input, output, cached, estimated: bool}`；失败尝试也上报但标 `attempt`，`_sse_frame` 新增一帧 `{"type":"usage", ...}`。
- 无 usage 时用 P1-1 估算并标 `estimated=true`，**不得**把估算写进真实计费账本。

**落到哪**：`llm.py:_stream_once` / `chat_with_meta`；`server.py:_stream_pieces`（L3776）、`_sse_frame`（L3748）；`roleplay_core/chat_router.py:ChatTurn`。

### P1-3 `max_tokens` 按预算回传

```
max_tokens = min(用户配置 or 8192, contextWindow - est_input - 64)
下限 256；若 < 256 说明上下文已超限 → 走 P0-3 降级
```

**落到哪**：`server.py:_model_params`（L3022，`out.setdefault("max_tokens", 8192)` 处改为计算值）。

### P1-4 流式回滚（已下发后断流）

**取舍**：R 没有续传；O 有 SAVEPOINT/ROLLBACK 的坐标语义。采用 **O 的坐标语义 + 我们现有的"清屏重画"**：

- 生产端记录 `raw_offset / discarded_total`；
- 断流重试成功后，把"已下发但属于失败尝试"的区间作废；
- 由于我们 `chat_router` 已有 `clear_content + content`（`_begin_rewrite`），直接下发一次重画即可，不必做字符级 patch。

**参数**：仅当"已下发字符 > 0 且 < 2000"时允许重试回滚（超过 2000 用户已读了大半，重画体感差，改为直接报错）。

**落到哪**：`llm.py:_stream_once`、`server.py:_decide_stream_error`、`roleplay_core/chat_router.py:_begin_rewrite`（复用）。

### P1-5 世界书 priority / order 分离（`roleplay_core/prompt.py`）

**算法**

```
priority = entry.priority if 有 else entry.order      # 默认兼容老数据
selected = 命中 + 递归 1 步（沿用现有）
group: 同组保留 max(order, id)（沿用现有）
budget 阶段：selected.sort(key=(-priority, -order, id))；贪心 used + tok <= budget
输出阶段：选中项 sort(key=(order, id))；按 position 分 before/after/depth
```

- 预算：沿用 `SECTION_SPECS` 的 `worldbook_before/after` 各 0.15 × contextWindow。
- `@ignore_on_max_context` 语义 → `priority = -1000`（R 的做法），预算不足时最先淘汰。

**落到哪**：`prompt.normalize_entry`（加 `priority` 字段）、`match_worldbook`（排序拆两段）。

**验收**：给"高优先短条目 + 低优先长条目"的用例，断言高优先条目不被挤掉；老数据（无 priority）输出顺序与现在完全一致。

### P1-6 记忆：去掉随机段，改"低分兜底"

**当前参数保留**：`topK=8`、`minScore=0.25`、`halfLifeDays=30`、权重 `relevance 0.6 / importance 0.25 / recency 0.15`、`relevance = bm25 0.6 + cosine 0.4`、`mmrLambda=0.7`、`dedupThreshold=0.92`、`clusterThreshold=0.85`、`maxTokens=600`、槽位预算 `promise 150 / user_info 150 / long_term 300 / short_term 300`、总 800。

**改动**：槽位装完后若剩余预算 > 0，只允许从 `short_term` 按"时间倒序 + score ≥ minScore"补，不引入随机。优先级顺序 `promise → user_info → long_term → short_term` 不变。

**落到哪**：`roleplay_core/prompt.py:SLOT_ORDER / SLOT_BUDGET` 与记忆拼装段（P1-6 只是补预算逻辑，不动检索）。

### P1-7 摘要参数（`server.py`）

采用 R 的 trigger 语义，映射到我们现有实现：

- 触发：`est_input + reserved_output > contextWindow`；
- 目标：降到 `contextWindow * (1 - 0) = contextWindow` 以下（我们不需要 `extraSummarizationRatio`，先留 0）；
- 每批最多 6 条（R 的 `maxChatsPerSummary`），保留最近 3 条不动（R 的 `queryChatCount`），但我们已有 `SUMMARY_WINDOW=24` 的滚动窗口，**取 max(3, 24 窗口之外才可摘要)**，即摘要窗口外的历史才进摘要批次。

**落到哪**：`server.py:_summary_params`（L3193）、`_maybe_summarize`（L3858）。

---

## 4. P2：长期结构（先设计，不急着写）

1. **能力对象替代布尔散字段**（O 的 `AIService` + R 的 flags 合并）
   `llm_profile` 增加：`contextWindow`、`maxOutputTokens`、`supportsTools`、`supportsVision/Audio/Video`、`max_tokens_field`（已有）、`include_usage`、`vendorQuirk`。
   理由：R 缺 `contextLength` 导致预算无法按模型算，是硬伤；O 的能力标志是构造参数，字段形态更干净。
   落点：`server.py:_model_params`、`_prompt_config`、`llm.py:_chat_payload`。

2. **vendorQuirk 配置化**（R 的核心教训）
   不要把 `openrouter/nanogpt/deepseek` 写进 `if` 分支；用 profile 里的 `{headers, body, delete_fields}` 三张表。
   落点：`llm.py:_chat_payload`（现有 `prof.get("extra")` 已是雏形，扩展为"删字段 + 加头"）。

3. **模型链回退**（R 唯一值得抄的结构）
   `primary → fallback_models[] → 空响应也切换`。参数：链长 ≤3；仅"可重试类错误 + 空响应"触发切换；401/403 不切换（是密钥问题，换模型无用）。
   落点：`server.py` 取模型处 + `llm.py:chat_stream` 外层。

4. **RRF(k=60)** —— **等真 embedding 再做**
   触发条件：出现"向量 chunk 索引"与"词法索引"两路**独立**召回。公式：`rrf(item) = Σ 1/(60 + rank_i)`；chunk→parent 汇总时先按 chunk 召回，再按 parent 累加。当前不做。

5. **工具调用**（O 的 `StructuredToolCallBridge` + `StreamingJsonXmlConverter`）
   只有当产品决定要 tool calling 时再做。若要做的参数：按 `index` 聚合 `arguments`；未匹配 call 补 `role:"tool"` 占位（"这不是用户取消"）；流式 JSON→XML 用 10 状态机，`hasUnfinishedParam()` 未闭合时禁止补 `</tool>`。
   落点：`llm.py:_chat_payload` + 新的 `stream_args.py` + `chat_router` 的 `ensure_format`。

6. **速率限制**
   采用 O 的滑动窗口（60 s deque）+ R 的 20/100 每分钟分档：摘要 20/min、embedding 100/min、并发 1。当前单用户单机不必急，等出现多用户/批量 embedding 再上。
   落点：`roleplay_core/memory_engine.py` 的 embedding 调用、`server.py:_backfill_embeddings`（L4184）。

---

## 5. 明确不做 / 等条件成熟

**现在明确不做**

| 项 | 来源 | 原因 |
|---|---|---|
| 随机 20% 记忆段 | R | 破坏确定性回归；槽位语义被污染（§1） |
| 累积缓冲全量重解析 SSE | R | O(n²)，长流必卡 |
| 固定 1s sleep 重试 | R | 无退避无 jitter，429 会打得更狠 |
| 401/400 也重试 | R | 明确缺陷，浪费 3 次 |
| 远程请求无超时 | R | 生产不可接受 |
| 静态模型目录（每厂商硬编码） | R | 维护成本高、必过期；我们走 `/models` 动态拉取 |
| tiktoken / HF tokenizers 依赖 | R | 破坏零依赖（§1） |
| HNSW（hnswlib）/ ObjectBox | O | 我们 `memory_engine` 的 BM25+余弦+MMR+聚类已更强 |
| OkHttp / 自研 Stream / 协程通道 | O | Android/Kotlin 运行时 |
| ToolPkg / MCP / QuickJS / A2A / MNN / llama | O | 非本轮范围 |

**等接了真 embedding 再做**

1. RRF(k=60) 与 chunk→parent 汇总；
2. 摘要再生成查询（R 的 `enableSimilarityCorrection`）；
3. 向量缓存改"内容哈希 + 模型名"（R 用原文做 key 是缺陷）；
4. 1500 条 LRU 的 embedding 缓存。

**等产品确认再做**：工具调用、多 key 池、模型链回退、`service_tier: flex` 降本。

---

## 6. 实施顺序与风险

| 顺序 | 内容 | 改动面 | 风险 → 缓解 |
|---|---|---|---|
| 1 | P0-1 SSE 重写 | `llm.py:_stream_once` | 现"坏帧静默跳过"变"超 5% 报错" → 阈值先设宽松，灰度观察一周 |
| 2 | P0-2 分类 + 重试 | `llm.py` 新增两个函数；`_decide_stream_error` | 流式重试会重复计费 → 只允许"未输出前"重试；已输出走 P1-4 |
| 3 | P0-4 超时三段 | `_request` / `_stream_once` | 首字节 60s 对慢模型可能误杀 → 按 provider 可配 |
| 4 | P0-5 finish_reason / 空响应 | `llm.py` + `ChatTurn` | 前端需识别 `truncated` 标记 |
| 5 | P0-3 超限降级 | `prompt.shrink_for_overflow` + `server.py` | 误删世界书会改人设 → 只删 `priority` 最低且非 `constant` |
| 6 | P1-1 token 校准 | `prompt_builder.count_tokens` | α 漂移导致预算失真 → clamp [0.75,1.6]、EMA 0.7/0.3 |
| 7 | P1-2 usage | `_stream_once` + 新 SSE 帧 | 老前端遇未知帧 → 确认前端对未知 type 忽略；`include_usage` 默认关 |
| 8 | P1-3 max_tokens 回传 | `_model_params` | 计算值过小导致截断 → 下限 256 |
| 9 | P1-5 世界书分离 | `prompt` | 老数据顺序变化 → `priority` 缺省=order，断言输出与旧版逐字一致 |
| 10 | P1-6 记忆兜底 | `prompt` | 无（只影响预算未满时的补位） |
| 11 | P1-7 摘要参数 | `server.py` | 摘要触发更频繁 → 设 `maxChatsPerSummary=6`、保留 24 窗口 |
| 12 | P1-4 流式回滚 | `_stream_once` + `_begin_rewrite` | 重画闪烁 → 仅 <2000 字符才回滚 |

**统一验收**：每步跑 `tests/test_chat_regression.py`；P0 阶段额外加 5 个 SSE mock 用例（多行 data / CRLF / 半截 JSON / EOF 无 DONE / 空响应）。

---

## 7. 一句话总结

**P0 修"会不会错"**：SSE 分帧、错误分类与退避、三段超时、超限降级、finish_reason。
**P1 修"准不准、贵不贵"**：粗估+α 校准、usage 回填、max_tokens 预算回传、世界书 priority/order 分离、记忆去掉随机段、摘要批次参数。
**P2 修"能不能长大"**：能力对象、vendorQuirk 配置化、模型链回退；RRF 与工具调用等条件成熟再上。
**四条当前结论**：不上 RRF；不保留随机 20%；照搬世界书"优先级装满 / order 输出"的分离；token 走 Operit 粗估 + 校准，不引真分词器。
