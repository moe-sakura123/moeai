# Operit LLM 后端调研 → 本项目移植方案

> 对象：https://github.com/AAswordman/Operit（main，最近提交 2026-11-04）
> 目标项目：d:\ai_manager_workspace\missai_local（Python 标准库 HTTP + SQLite + OpenAI 兼容协议）
> 本轮只出方案，不改代码、不提交。

---

## 0. 取证范围与边界（先说清楚，避免把推测当结论）

仓库为 Android/Kotlin 多模块工程（`app` / `llm` / `avator` / `web-chat` / `terminal` 等）。LLM 后端集中在：

```
app/src/main/java/com/ai/assistance/operit/api/chat/llmprovider/
app/src/main/java/com/ai/assistance/operit/api/chat/EnhancedAIService.kt
app/src/main/java/com/ai/assistance/operit/core/chat/AIMessageManager.kt
app/src/main/java/com/ai/assistance/operit/util/{ChatUtils,TokenCacheManager,StreamingJsonXmlConverter,StructuredAssistantContentParser}.kt
app/src/main/java/com/ai/assistance/operit/util/stream/  (自研 Stream 框架)
app/src/main/java/com/ai/assistance/operit/util/vector/  (HNSW 向量索引)
```

**已完整读取**（文件 < 抓取上限）：`AIService.kt`、`EndpointCompleter.kt`、`LlmRetryPolicy.kt`、`SlidingWindowRateLimiter.kt`、`HttpStatusCodeException.kt`、`TokenCacheManager.kt`、`ChatUtils.kt`、`StreamingJsonXmlConverter.kt`、`StructuredAssistantContentParser.kt`、`VectorIndexManager.kt`、`ModelListFetcher.kt`（前 283 行）。

**部分读取 / 截断**：
- `OpenAIProvider.kt`（145 KB，只拿到 1–378 行：类签名、Token 计数、错误对象解析、取消逻辑、日志脱敏）。**SSE 逐帧解析、payload 主体构造、重试主循环都在后半段，未取证。**
- `AIServiceFactory.kt`（29 KB，拿到共享 OkHttpClient 与 createService 分支前半段）。
- `StructuredToolCallBridge.kt`（15.7 KB，基本读全，尾部落到 16 KB 截断）。

**补充证据**：最近提交 `3ca9a8a`（*keep streamed revisions aligned across retries and consumers*）的 `.patch` 提供了流式重试的最关键设计（SAVEPOINT/ROLLBACK 的坐标语义），已取证。

**结论**：凡标"【未取证】"处，请不要在排期里当作已确认事实；落地前需本地 clone 复核对应行。

---

## 1. 它怎么做的

### 1.1 Provider / 模型注册与切换

**接口契约**（`AIService.kt`）：

```kotlin
interface AIService {
    val inputTokenCount: Long          // 新增输入
    val cachedInputTokenCount: Long    // 命中缓存
    val outputTokenCount: Long
    val providerModel: String          // "PROVIDER:model"
    fun cancelStreaming()
    suspend fun getModelsList(context): Result<List<ModelOption>>
    suspend fun sendMessage(..., stream: Boolean = true, availableTools: List<ToolPrompt>?, ...): Stream<String>
    suspend fun testConnection(context): Result<String>
    suspend fun calculateInputTokens(chatHistory, availableTools): Long
}
```

两个刻意的设计点，值得抄：

1. **`stream` 只是请求参数，返回值永远是 `Stream<String>`**。非流式时上游整包返回，Provider 内部把整包切成一个元素再发出去。上层编排只有一套代码。
2. **统计通道与 UI 通道分离**：`onTokensUpdated`（可能带估算值，UI 用）与 `onUsageReported(snapshot, attempt)`（只上报真实 usage，账本用）分开，且带 `attempt` 序号，允许同一次尝试内多次上报、后者覆盖前者。`onUsageFinalized(attempt)` 只在请求正常完成时回调，避免把失败尝试的 usage 算成成功。
3. `calculateInputTokens(history, tools)` 是**只读预估**，不推进缓存状态（`TokenCacheManager.calculateInputTokens(updateState=false)`）。

**工厂与装配**（`AIServiceFactory.buildService`）：

```
1) ToolPkgAiProviderRegistry.get(providerTypeId) 命中 → ToolPkgJsAiProviderService（插件自定义 provider 优先）
2) ApiProviderType.fromProviderTypeId(...)
3) useMultipleApiKeys ? MultiApiKeyProvider : SingleApiKeyProvider
4) supportsVision = config.enableDirectImageProcessing
   supportsAudio = config.enableDirectAudioProcessing
   supportsVideo = config.enableDirectVideoProcessing
   supportsFiles / enableToolCall 同源配置
5) 按 providerType 分支 → OpenAIProvider / XaiProvider / ClaudeProvider / GeminiProvider / DeepseekProvider ...
6) 统一包一层 TokenTrackingAIService(delegate) 做计费边界
```

**模型能力不是"猜"，是配置开关**：图片/音频/视频/文件/工具调用全部来自 `ModelConfigData` 的布尔项，Provider 构造参数接收。这是它比"按模型名硬编码"更好的地方。

**共享 HTTP 客户端**（`SharedHttpClient`）：

| 参数 | 值 |
|---|---|
| connectTimeout | 60 s |
| readTimeout | 1000 s |
| writeTimeout | 1000 s |
| ConnectionPool | 10 个空闲连接，5 分钟 |
| protocols | HTTP/2 优先，回落 HTTP/1.1 |
| eventListener | 每次请求带 `LlmRequestTraceContext(requestId, provider, model, stream, attempt, endpointLabel)` 打点 |

模型列表用的是**独立客户端**：connect/read/write 各 30 s。

### 1.2 端点补全（`EndpointCompleter`）

纯字符串/URL 规则，零依赖：

- 结尾是 `#` → 去掉 `#`，**不做补全**（用户强制原样）。
- 路径为空（如 `https://api.example.com`）→ 追加 `/v1/chat/completions`。
- 路径以 `/v1` 结尾（如 `https://proxy/custom/v1`）→ 追加 `/chat/completions`。
- 其他情况原样返回（说明用户已写了完整路径）。
- Responses API 变体 → `/v1/responses`；Anthropic → `/v1/messages`（路径以 `/anthropic` 结尾也补 `/v1/messages`）。

**注意它没有"404 再补 /v1 重试"**——补全在请求前完成。这一点我们比它多一层兜底（`_with_v1_fallback`），保留我们的。

### 1.3 OpenAI 兼容请求构造

内部统一消息模型是 `PromptTurn(kind, content, metadata)`，`kind ∈ {SYSTEM, USER, ASSISTANT, TOOL_CALL, TOOL_RESULT, SUMMARY}`。转 OpenAI 消息时先做**历史归并**（`compileHistoryForProvider`）：

- `ASSISTANT` 与 `TOOL_CALL` 归并为同一块（同一角色连续内容用 `\n` 拼接）；
- 连续 `TOOL_RESULT` 归并为一块；连续 `USER` / `SUMMARY` 归并为一块；
- `SYSTEM` 不合并，逐条独立；
- 角色切换才 flush，空内容丢弃。

工具格式是**内部 XML ↔ OpenAI tool_calls 双向转换**：

```xml
<tool name="xxx"><param name="yyy">value</param></tool>
```
```json
{"tool_calls":[{"id":"call_0","type":"function","function":{"name":"xxx","arguments":"{\"yyy\":\"value\"}"}}]}
```

`id` 由自增序号生成（`generatedToolCallId(nextToolCallOrdinal++)`），不依赖上游。`package_proxy` / `proxy` 两个代理壳会被解包，取出真实 `tool_name`，用于结果回填时按**真实工具名**匹配。

**协议合法性修补**（`StructuredToolCallBridge`）：遇到 system/user/assistant 边界时，仍未拿到结果的 open tool call 一律补一条占位 `role:"tool"` 消息，文案形如：

> `工具结果缺失：{tool} {原因}。这不是用户取消。`

原因码覆盖 `system_boundary / user_boundary / assistant_boundary / assistant_tool_call_before_result / typed_* / history_end` 等。**目的**：不让上游因为"assistant 有 tool_calls 却没有对应 tool 消息"而 400。

参数下发（`ModelParameter`）按值类型分派 `INT / FLOAT / STRING / BOOLEAN / OBJECT`；`OBJECT` 先尝试 `{`→JSONObject、`[`→JSONArray，解析失败则按原文字符串下发。

DeepSeek 专属（`DeepseekProvider.createRequestBody`）：

- assistant 历史里的 `<think>` / `<thinking>` 内容抽出来放进 `reasoning_content` 字段；
- **关闭思考也要显式下发** `thinking.type = "disabled"`；
- 流式时固定加 `stream_options: {"include_usage": true}`；
- 只有 `enableToolCall && availableTools 非空` 才加 `tools` + `tool_choice:"auto"`。

### 1.4 SSE 与流式

**已取证部分**：

- 取消：`activeResponse?.close()` + `activeCall?.cancel()` + `isManuallyCancelled` 标志三件套（对应我们 llm.py 的 `_ACTIVE_RESP` + `close_stream`，我们已经有了）。
- 流是**自研 `Stream<String>`**（Flow 风格），文本分片与"修订事件"走两条通道：`TextStreamEventCarrier.eventChannel`。
- **流式重试是"可回滚"的**（提交 `3ca9a8a`），这是最有价值的部分：

```
Producer 侧保存：
  savepointLengths[id]    = receivedContent.length      // 当前尝试缓冲区内长度
  savepointRawOffsets[id] = receivedContent.length + discardedTotal
  discardedTotal          // 历史回滚累计丢弃的字符数

emitSavepoint(id) → 事件 contentLength = rawOffset
emitRollback(id)  → discardedLength = (receivedContent.length - retainedLength)
                    receivedContent.setLength(retainedLength)
                    discardedTotal += discardedLength
                    savepointRawOffsets[id] = rawOffset + discardedLength
                    事件 contentLength = rawOffset, discardedLength = discardedLength
```

关键点：**事件坐标是"原始分片序列"坐标，不是可见文本坐标**。因为 producer 回滚会截断自己的缓冲区，用缓冲区长度去定位会跳过更早尝试已经丢弃的文本。

Consumer 侧（`RevisionAppliedText.kt`）：先把整条流 `collect` 成 raw，再用 `applyRevisionEvents(raw, eventChannel.replayCache)` **重放完整事件历史**，从 raw 里删掉 `[contentLength, contentLength + discardedLength)` 区间。它明确拒绝"边收边应用"——因为文本块和事件在两条协程通道里，事件可能早于/晚于对应分片到达，边收边改会截错位置。

跨请求（一轮里多次请求拼成一条流）用 `shiftedBy(offset)` 把后一次请求的坐标搬到拼接流坐标系。

**【未取证】**：`data:` 前缀切分、`[DONE]` 判定、多行 `data` 拼接、`\r\n` 处理、EOF/读超时如何区分"正常结束/断流"，都在 `OpenAIProvider.kt` 1528 行之后，我只拿到 1528–1600 附近的补丁片段。**这一段不要按本文推测实现，落地前必须看原文件。**

### 1.5 错误与重试

**退避策略**（`LlmRetryPolicy`，完整取证）：

```
MAX_RETRY_ATTEMPTS = 5
nextDelayMs(attempt) = min(16000, 1000 * 2^(attempt-1))   // attempt 取 1..5，指数上限 4
                      = 1s, 2s, 4s, 8s, 16s（无 jitter）
```

**状态码异常**：`HttpStatusException(message, statusCode)` 实现 `HttpStatusCodeException` 接口。`shouldSuppressKeyPoolRateLimitNotice()`：当 429 且 `candidateKeyCount > 1` 时，**不弹中间提示**，直接换下一个 key 继续（多 key 轮询场景）。

**错误体解析**（`buildOpenAiErrorDetail`）：取 `error.message`（空则 fallback），有 `type`/`code` 时拼成 `message [type=..., code=...]`。

**模型列表**有独立重试：`maxRetries = 2`（最多 3 次请求），失败读响应体截断返回。

**【未取证】**：上下文超限（`context_length_exceeded`）的自动降级、读流中途断开的判定与重试入口，在 `OpenAIProvider.kt` 后半段。

### 1.6 Token 预算与上下文

**估算公式**（`ChatUtils.estimateTokenCount`，完整取证）：

```kotlin
chineseCharCount = count { it.code in 0x4E00..0x9FFF }
otherCharCount   = text.length - chineseCharCount
tokens = (chineseCharCount * 1.5 + otherCharCount * 0.25).toLong()
```

即：CJK 每字 1.5 token，其余每 4 字符 1 token。**粗估、偏保守，但零依赖、稳定可复现。**

**前缀缓存计数**（`TokenCacheManager.calculateInputTokens`，完整取证）：

1. 把 `toolsJson` 拼到**第一条 system 消息前面**（没有 system 就插一条）。目的是让工具定义处在历史最前缀，前缀命中率最高。
2. 与 `previousChatHistory` 逐条比较求**公共前缀长度**（逐元素 `==`，遇不同即停）。
3. 完全等于上次 → 直接复用 `previousHistoryTokenCount`。
4. 部分相同 → 只对公共前缀重新计数 + 对 `drop(commonPrefix)` 的增量计数。
5. 无公共前缀 → 全量计数。
6. 上游返回真实 usage 时用 `setOutputTokens` / `updateActualTokens(actualInput, cachedInput)` **覆盖估算值**。

**没有取证到的**：历史裁剪/摘要分层策略（`AIMessageManager.kt` 67 KB 只读了一小段）。它更偏"Agent 工具循环 + 长上下文"，与我们的 v2 分区预算不是同一套思路——**这一块不建议照抄**。

### 1.7 工具调用编排

`StructuredToolCallBridge.consumeMatchingToolCalls` 的核心是**按名字匹配而不是按顺序配对**：

```kotlin
resultToolNames.forEachIndexed { resultIndex, name ->
    val callIndex = openToolCalls.indexOfFirst { it.matchingName == name.trim() }
    if (callIndex >= 0) matched.add(MatchedToolCall(resultIndex, openToolCalls.removeAt(callIndex)))
}
```

注释给出的理由很实际：被拒绝的调用会被前移到结果列表头部，按位置配对会把结果挂到错误的 call id 上。未匹配的结果留给调用方继续消费。

**流式 tool call 参数 → XML**（`StreamingJsonXmlConverter`，10 状态机，完整取证）：

```
WAIT_BRACE → WAIT_KEY_QUOTE → READ_KEY → WAIT_COLON → WAIT_VALUE
           → READ_STRING | READ_PRIMITIVE → WAIT_COMMA
READ_STRING 内：ESCAPE → UNICODE_ESCAPE（累计 4 位 hex）
READ_PRIMITIVE：数组/对象值用 nestingDepth + inString + escape 三态跟踪，
                只有 depth==0 且不在字符串/转义中才允许收尾
```

对外两个保护：`hasUnfinishedParam()`（还有未闭合 `<param>` 时，调用方不许补 `</tool>`）；`flush()` 只在 `READ_PRIMITIVE` 且 `canFinalizePrimitiveOnFlush()` 为真时才 emit。这解决"流被截断在参数中间"导致的假 XML。

### 1.8 记忆 / 知识库

`VectorIndexManager`：hnswlib 的 `HnswIndex`，`DistanceFunctions.FLOAT_COSINE_DISTANCE`，`withRemoveEnabled()`，`findNearest(query, k)`，容量不够时 `resize`，落盘用 Java `ObjectOutputStream` 序列化整个索引；加载失败则删文件重建。

`ChatUtils.removeThinkingContent`：正则去掉 `<think|thinking>...</think|thinking>` 和 `<search ...>...</search>`，含未闭合情况（`(\z)` 兜底）。

---

## 2. 我们能移植什么（三分类）

### A. 可直接移植（纯逻辑/参数，改写成 Python 即可）

| # | 内容 | 具体做法与参数 |
|---|---|---|
| A1 | 端点补全规则 | `#` 结尾禁用补全；空 path → `/v1/chat/completions`；path 以 `/v1` 结尾 → `/chat/completions`；否则原样 |
| A2 | 退避重试 | 最多 5 次；`min(16000, 1000*2^(n-1))` → 1/2/4/8/16 s |
| A3 | 错误体拼装 | `error.message` + ` [type=…, code=…]` |
| A4 | Token 估算 | `1.5 * CJK + 0.25 * 其他字符` |
| A5 | 前缀缓存计数 | tools 前置到首条 system；逐条比较求公共前缀；全同复用、部分只算增量 |
| A6 | 模型列表派生 | `extractBaseUrl` 用 `/v\d+` 正则截到版本路径前，再按 provider 拼 `/v1/models`(或 `/v3` `/v4` `/maas/v1` `/llm/v1`) |
| A7 | 认证头差异 | Gemini `?key=`；Anthropic `x-api-key` + `anthropic-version: 2023-06-01`；OpenRouter/NousPortal 加 `HTTP-Referer`+`X-Title`；MiMo `Authorization`+`api-key` 双头 |
| A8 | 模型列表重试 | 3 次（`maxRetries=2`），30 s 超时 |
| A9 | 消息归并规则 | 连续 ASSISTANT/TOOL_CALL 合并、TOOL_RESULT 合并、USER/SUMMARY 合并、SYSTEM 逐条 |
| A10 | tool 协议修补 | 未匹配 call 补 `role:"tool"` 占位消息，文案固定"这不是用户取消" |
| A11 | 滑动窗口限流 | `deque<timestamp>`，`< 60s` 的弹出；满了返回 `windowMs-(now-oldest)`，否则入队返回 0 |
| A12 | JSON→XML 状态机 | 10 状态；unicode 转义 4 位；复合值用 `depth/inString/escape` 三态；`flush()` 只在安全点收尾 |
| A13 | 结构化内容解析 | 先剥 ```` ```json ```` 包裹，再取首个 `{` 到末个 `}`（数组同理） |
| A14 | 日志脱敏 | base64 `data:` 值替换为 `[image base64 omitted, length=N]`；URL 里 key/api_key/token 等 query 参数打码 |

### B. 需改造适配（思路可用，形态必须重写）

| # | 内容 | 为什么不能照搬 | 改造方向 |
|---|---|---|---|
| B1 | SAVEPOINT/ROLLBACK 流式重试 | 它是 Kotlin 协程双通道 + 重放缓存；我们是单线程生成器 + 边发边 flush | 在 `llm.py::_stream_once` 里维护 `raw_offset / discarded_total`，重试时先回滚到 savepoint 再继续；前端用"清屏重画"（已有 `clear_content` 帧）承载回滚 |
| B2 | 统一 `Stream<String>` 契约 | 我们没有 Stream 类型 | 保持生成器；把 `stream` 只当参数（我们已经是这样） |
| B3 | 多 Key 轮询 + 429 抑制 | 我们 `llm_providers` 是单 key | 若要做：`llm_providers` 增 `api_keys` JSON 数组 + 轮询游标（DB 迁移，风险中） |
| B4 | 能力标志（vision/audio/video/files/toolcall） | 它走 `ModelConfigData`；我们是 `llm_profile` dict | 直接挂到 `llm_profile`，`_chat_payload` 按标志决定是否发 `tools`、是否走多模态 content 数组 |
| B5 | 前缀缓存计数 | 我们每次请求重建 messages，且 v2 会在中间插入记忆/世界书，前缀不稳定 | 只对**稳定前缀**（persona/identity/examples 段）做缓存；把 v2 `SECTION_SPECS` 里 `stable=True` 的段拼成一个整体做 key |
| B6 | 上游 usage 回填估算 | 我们流式只统计 `reasoning_chars`，不读 usage | `_stream_once` 采集每个 chunk 的 `usage`（含 `stream_options.include_usage`），最后 `on_finish` 带回 input/output/cached |
| B7 | 流式 tool call | 我们完全没有 tools 通路 | 若要做：`_chat_payload` 加 `tools`/`tool_choice`；流式累计 `delta.tool_calls[index].function.arguments` 字符串，用 A12 状态机增量转 XML 交给现有 `chat_router` |
| B8 | token 估算校准 | 公式会低估英文长文、高估纯中文 | 用上游 usage 做滑动校准系数（如最近 5 次 `实际/估算` 中位数），仅在无 usage 时使用 |

### C. 不适用（Android 特性或依赖其运行时，明确排除）

| 内容 | 排除原因 |
|---|---|
| OkHttp / EventListener / ConnectionPool / HTTP2 协商 | Python 标准库无对应物；我们用 urllib，只借"超时数值"（connect 60s / read 1000s） |
| 自研 `Stream` / `SharedStream` / `StateStream` / 协程 `eventChannel` | Kotlin 协程语义，Python 里是生成器 + 线程，重写成本远大于收益 |
| `Context` / `Uri` / `Environment.getExternalStoragePublicDirectory` / `Base64` 图片写盘 | Android 文件系统与图片落盘逻辑 |
| hnswlib + Java 对象序列化 | 需 JNI 原生库；我们 `memory_engine.py` 已有余弦 + BM25 + MMR + 聚类，功能更强 |
| ToolPkg / QuickJS / MCP / A2A / Shower | 依赖 Android 运行时与 JS 沙箱，不是本轮范围 |
| MNN / llama.cpp 本地推理 Provider | 需要 native 编译与模型文件 |
| ObjectBox / Room 数据库 | 我们是 SQLite |

---

## 3. 落到哪个文件 / 函数

| 能力 | 文件 | 函数/位置 | 具体动作 |
|---|---|---|---|
| 端点补全对齐 | `llm.py` | `_endpoint`（L55）、`_with_v1_fallback`（L117） | 加 `#` 禁用语义；把"路径以 /v1 结尾"特判补进 `_endpoint`，减少一次 404 往返 |
| 通用重试 | `llm.py` | 新增 `_retry_delay(attempt)`、`_request_with_retry()` | 5 次上限、1→16 s 指数退避；只对 429/5xx/超时/连接失败重试 |
| 非流式重试 | `llm.py` | `chat_with_meta`（L182） | 走 `_request_with_retry` |
| 流式重试（未开始输出前） | `llm.py` | `chat_stream`（L406） | 现有"404 才换 URL"扩展为"未 yield 任何 piece 时可按 A2 重试" |
| 流式重试（已输出后回滚） | `llm.py` | `_stream_once`（L275） | 增加 `savepoint`/`discarded_total`；重试成功后把已发内容回滚，由“清屏重画”兜底（见 B1） |
| SSE 解析加固 | `llm.py` | `_stream_once`（L338-378 的 for 循环） | 多行 `data:` 拼接（空行才交付一个事件）；支持 `\r\n`；忽略 `event:/id:/retry:`；JSON 解析失败计数并记录首条坏帧 |
| usage 采集 | `llm.py` | `_stream_once` + `chat_stream` | payload 加 `stream_options:{include_usage:true}`（可开关）；`on_finish` 带回 usage |
| 错误分类 | `llm.py` | `LLMError`、`_request` 的 `HTTPError` 分支 | 新增 `status`/`retryable`/`code` 字段；识别 `context_length_exceeded` |
| 降级决策 | `server.py` | `_decide_stream_error`（L3797） | 依据错误类别决定：可重试 → 重试；上下文超限 → 触发摘要/裁剪后重试；否则维持现有本地兜底/报错 |
| 超时参数 | `server.py` | `_stream_pieces`（L3776） | `10.0 if fb else 60.0` 保留；另加"首字节超时"与"分片空闲超时"两个阈值 |
| SSE 输出帧 | `server.py` | `_sse_start`（L3736）、`_sse_frame`（L3748） | 增 `usage` 帧（input/output/cached/估算标记），前端只更新计数不重绘正文 |
| 模型参数 / 能力标志 | `server.py` | `_model_params`（L3022）、`_prompt_config`（L3044） | `llm_profile` 增加 `supports_tools/vision/audio/video`、`max_tokens_field`（已有）、`include_usage` |
| 上下文预算 | `roleplay_core/prompt.py` | `SECTION_SPECS`、`trim_tokens`、`DEFAULT_CONTEXT_WINDOW=8192` | 用实测 usage 校准 `count_tokens` 的系数；`stable=True` 段拼成稳定前缀供 B5 缓存 |
| 摘要窗口 | `server.py` | `_summary_params`（L3193）、`_maybe_summarize`（L3858）、`_summarize_slots`（L3889） | 不改（`SUMMARY_WINDOW=24` 是 v2 摘要窗口，别当 v1 硬截） |
| 流式节流 | `roleplay_core/chat_router.py` | `_pump`（`FLUSH_CHARS=30`、`FLUSH_SECONDS=0.15`） | 不改；只在 `on_stream_error` 处接 B1 的回滚信号 |
| 记忆 | `roleplay_core/memory_engine.py` | `mmr_rerank(lambda_=0.7,k=8)`、`bm25(k1=1.5)`、`recency(30天)`、`cluster_items(0.85)` | 基本不动；Operit 的 HNSW 不优于现有实现 |

---

## 4. 实施顺序与风险

### 阶段 P0（1 天量级、风险低、收益直接）

1. **SSE 分帧加固**（`_stream_once`）：多行 data、`\r\n`、坏帧计数。
2. **非流式 + 未输出前的流式重试**（A2/A3）。
3. **错误分类字段**（status/retryable/code）。

- 风险：现有 `data:` 单行假设被改，可能改变"坏帧被静默跳过"的既有行为 → 保留 `continue` 语义，只加计数与日志。
- 验证：`tests/test_chat_regression.py` + 手工 mock 一个"多行 data"与"半截 JSON"上游。

### 阶段 P1（2–3 天）

4. **usage 采集与回填**（B6）+ `_sse_frame` 新帧。
5. **token 估算与预算校准**（A4/A5 的稳定前缀部分）。

- 风险：`stream_options` 不被部分中转站支持 → 必须可开关（`llm_profile.include_usage`，默认关，探测成功后开）。
- 风险：新增 `usage` 帧可能让老前端报未知帧 → 前端需容错（现有前端对未知 type 是忽略，需确认）。

### 阶段 P2（3–5 天）

6. **能力标志入 `llm_profile`**（B4）。
7. **多 Key 轮询 + 429 抑制**（B3，需 DB 迁移）。

- 风险：DB 迁移；老记录无 `api_keys` 字段需默认 `[]` 并回落单 key。
- 风险：429 换 key 会改变现有"直接报错给用户"的体验，属产品决策，需确认。

### 阶段 P3（较大改动，单独排期）

8. **工具调用通路**（B7）：payload `tools` → 流式累计 arguments → A12 状态机转 XML → 复用 `chat_router` 的 `ensure_format`/`post_process`。
9. **SAVEPOINT/ROLLBACK 流式重试**（B1）。

- 风险：tool call 会改变消息结构，`prompt` 的 `format` 段与 `sanitizer` 的 `<say>` 解析可能冲突 → 需先在回归样本里加 tools 场景。
- 风险：回滚需要前端配合 `clear_content + content` 重画；`chat_router._begin_rewrite` 已有该能力，但要确认"回滚区间"与"重画"不会导致闪烁。

### 全局风险

- **不要动 `SUMMARY_WINDOW=24`**：那是 v2 摘要窗口；Operit 那套"硬截历史"不适用于我们。
- **不要引入 OkHttp 语义到 Python**：超时数值可借，连接池/HTTP2 不必。
- **`OpenAIProvider.kt` 后半段未取证**：P0 的 SSE 改动不要声称"对齐 Operit 实现"，只能说"按 SSE 规范加固"。落地前建议本地 clone 复核 1528 行之后。

---

## 5. 一句话结论

Operit 值得抄的是**工程参数与协议修补**（退避曲线、端点规则、token 粗估与前缀缓存、tool 协议合法性兜底、流式参数增量解析状态机、可回滚流式重试的坐标语义）；不值得抄的是**运行时**（Kotlin Stream / OkHttp / HNSW / Android 存储）。本项目在**记忆检索（BM25+余弦+MMR+聚类）与 v2 分区预算**上已经强于对方，本轮改造应集中在 `llm.py` 的流式与重试层，以及 `server.py` 的降级决策与 usage 通道。
