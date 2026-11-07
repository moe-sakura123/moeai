# RisuAI LLM 后端对标分析

> 分析对象：<https://github.com/kwaroran/RisuAI>（SvelteKit + TypeScript 前端应用）
> 分析范围：`src/ts` 下的模型元数据、请求分发、OpenAI 兼容层、SSE 解析、token 预算、错误重试、记忆算法、本地/远程分支。
> 分析方式：GitHub 主干浅克隆（`--depth 1`）后逐文件阅读源码。**具体 commit 未记录，未核实**。
> 本文只写阅读到并有代码位置支撑的结论；未读到或与仓库结构不一致的部分明确标注「未核实」。

---

## 0. 结论速览

| 主题 | 核心结论 | 可移植性总判 |
|---|---|---|
| Provider 抽象 | 没有厂商级注册表，只有「静态模型目录 + LLMFormat 分发 + 布尔 flag 描述能力」 | 需改造适配 |
| 请求构建 | 固定桶（main/jailbreak/chats/lorebook/authorNote/lastChat/...）+ 可配置顺序；支持模板卡片流 | 可直接移植（桶与顺序） |
| OpenAI 兼容 | 单文件集中处理 base_url、鉴权、请求体、厂商怪癖；怪癖靠 `aiModel` 字符串硬编码分支 | 需改造适配 |
| SSE 解析 | Chat Completions 路径用「累积缓冲全量重解析」，无空行分帧、无 usage、无 finish_reason；Responses API 路径分帧正确 | Chat 路径不适用，需重写；Responses 分帧可移植 |
| Token 预算 | 全局 `maxContext` / `maxResponse`，单预算池；两阶段裁剪；真实 tokenizer 库而非字符估算 | 裁剪顺序可移植；预算模型需改造 |
| 错误重试 | 模型链回退 + 每模型固定次数；仅 1 秒固定 sleep，无状态码分类、无指数退避 | 模型链可移植；退避与分类不适用 |
| 记忆 | HypaV3 默认：预算比例 + 重要/近期/相似/随机四段选择 + 加权余弦 + RRF(k=60) | 需改造适配 |
| 本地/远程 | 本地模型绕过 token 预算与 usage，流式返回全量快照；远程走 OpenAI 兼容层 | 不适用（除非自托管） |

---

## 1. 核实范围说明

**本轮重新逐段阅读并确认的文件：**

- [tokenizer.ts](file:///d:/tmp_risuai_analysis/src/ts/tokenizer.ts)：`encode()`（L80）、`ChatTokenizer`（L409）、`tokenizeChat()`（L418）、`tokenizeMultiModal()`（L445）。
- [index.svelte.ts](file:///d:/tmp_risuai_analysis/src/ts/process/index.svelte.ts)：`sendChat` 的 token 初始化（L287-293、L341-345、L614-618）、`unformated` 桶（L349-360）、预裁剪（L1143-1152）、重算与二次裁剪（L1501-1529）、`generationInfo`（L1533-1545）。
- [requests.ts](file:///d:/tmp_risuai_analysis/src/ts/process/request/openAI/requests.ts)：请求体构造（L380-485）、URL 与鉴权（L512-564）、多生成（L565+）、SSE（L972-1136）。
- [request.ts](file:///d:/tmp_risuai_analysis/src/ts/process/request/request.ts)：`requestChatData` 重试循环（L205-346）、`reformater`（L348-432）、`maxTokens` 赋值（L457）。
- [shared.ts](file:///d:/tmp_risuai_analysis/src/ts/process/request/shared.ts)：`applyParameters`（L154-330）。
- [lorebook.svelte.ts](file:///d:/tmp_risuai_analysis/src/ts/process/lorebook.svelte.ts)：`searchMatch`（L100-235）、激活与预算（L237-625）、注入合并（L643-669）。
- [hypav3.ts](file:///d:/tmp_risuai_analysis/src/ts/process/memory/hypav3.ts)：预算（L237-254）、摘要触发（L256-300）、四段选择（L505-857）、查询权重（L680-699）、`simpleCC`/`childToParentRRF`/`normalizeScores`（L1836-1920）、默认设置（L1798-1815）。
- [hypav2.ts](file:///d:/tmp_risuai_analysis/src/ts/process/memory/hypav2.ts)：预算与摘要（L372-560）、检索打分（L590-626）。
- [supaMemory.ts](file:///d:/tmp_risuai_analysis/src/ts/process/memory/supaMemory.ts)：分块收缩（L286-340）、注入（L172-182）。

**未在本轮逐字复核、仅依据前序阅读记录的部分（文中均再次标注）：**

- `model/types.ts` 的 `LLMFormat` 全量枚举、`LLMModel` 全部 flag。
- `database.svelte.ts` 的默认值清单（`formatingOrder`、`maxContext=4000`、`maxResponse=500`、`requestRetrys=2`、`loreBookToken=8000` 等）。
- `hanuraiMemory.ts`、`hypamemory.ts`、`taskRateLimiter.ts`、`webllm.ts`、`transformers.ts` 的具体行号。
- `openAI/responses.ts` 的具体行号。
- `globalApi.svelte.ts` 的超时与 `fetchNative` 细节。

---

## 2. Provider 抽象与注册

### 2.1 它怎么做的

**没有「Provider 注册表」。** 结构上分三层：

1. **静态模型目录**：`src/ts/model/providers/openai.ts`、`anthropic.ts`、`google.ts`、`nanogpt.ts`。每个模型是一条 `LLMModel`，包含 `id`、`internalID`、`provider`、`endpoint`、`keyIdentifier`、`parameters`（参数白名单）、`flags`（能力布尔量）、`tokenizer`（分词器枚举）。
2. **模型查找/动态注册**：`src/ts/model/modellist.ts` 的 `getModelInfo()` 负责按 id 找模型并合并内置/自定义/动态模型；`registerModelDynamic()` 用于运行时注册（如 OpenRouter 动态拉取的模型）。自定义模型走 `db.customModels` 字段列表。
3. **请求分发**：`request.ts` 的 `requestChatDataMain()` 按 `LLMFormat` 分支到具体请求函数，而不是按厂商维护独立 adapter 类。

支持的「大量厂商」实际是两类：

- 真厂商适配：Claude / Google / VertexAI / Mistral / Cohere / NovelAI / Ooba / Kobold / Ollama / Transformers / Horde / Echo（`LLMFormat` 枚举成员，**本轮未逐字复核全量**）。
- OpenAI 兼容厂商：统一走 `requestOpenAI`，靠 `aiModel` 字符串（`openrouter`、`nanogpt`、`reverse_proxy`、`xcustom:::`、`gpt` 等）在函数内做差异化分支。

### 2.2 能力描述：具体字段与缺口

`LLMModel.flags` 是**布尔能力位**，不是能力值对象。已确认存在的 flag（全部为 `boolean`）：

| 类别 | flag | 作用 |
|---|---|---|
| 视觉 | `hasImageInput` | 是否接受图片输入 |
| 预填充 | `hasPrefill` | 是否支持 assistant 预填充 |
| 系统消息 | `hasFullSystemPrompt` | 是否原生支持 system 角色 |
| 系统消息 | `hasFirstSystemPrompt` | 仅支持「首条 system」 |
| 系统消息 | `requiresAlternateRole` | 必须 user/assistant 交替 |
| 系统消息 | `DeveloperRole` | system 需改写为 developer |
| 推理 | `hasReasoning` / `hasThinking` | 是否输出思维链 |
| 推理 | `deepSeekThinkingInput` / `deepSeekThinkingOutput` / `deepSeekThinkingToggle` / `deepSeekPrefix` | DeepSeek 专属思考与前缀 |
| 工具 | `hasToolCalls` / `noToolCalling` | 工具调用能力（仅布尔开关，无 schema 约束描述） |
| 结构化输出 | `noStructuredOutput` | 是否禁用 `response_format` |
| 计费字段 | `OAICompletionTokens` | `max_tokens` 需改名 `max_completion_tokens` |
| 其他 | `hasStreaming`（**代码中未见实际使用，未核实**） | — |

**两个关键缺口（已确认）：**

- **没有上下文字段。** `context_length` 只出现在模型选择器的元数据里（`modelGrid.ts`、`openrouter.ts`、`ollama.ts` 这些列表/展示层），**不参与请求预算计算**。预算只看全局 `db.maxContext`。
- **没有工具能力字段。** 请求侧只要 `arg.tools` 非空就附加 `tools`（[requests.ts L474-485](file:///d:/tmp_risuai_analysis/src/ts/process/request/openAI/requests.ts#L474-L485)），没有「该模型最多几个工具 / 是否支持并行工具」这类描述。

### 2.3 参数白名单机制

每个模型条目带 `parameters: string[]`，`applyParameters` 只写入白名单内的字段（[shared.ts L197-330](file:///d:/tmp_risuai_analysis/src/ts/process/request/shared.ts#L197-L330)）。默认值：

- `temperature = 1`、`top_p = 1`、`top_k = 0`、`frequency_penalty = 0`、`presence_penalty = 0`。
- UI 中的 `temperature`、`frequency_penalty`、`presence_penalty` 以 `×100` 存储，写入前除以 100。
- `top_k = 0` 且 `ignoreTopKIfZero` 时跳过 `top_k`。
- `-1000` 作为「未设置」哨兵值在比较中保留（**其最终是否被剔除，未逐字复核**）。

### 2.4 可移植性判断

| 项 | 判断 | 理由 |
|---|---|---|
| 布尔 flag 能力表 | **可直接移植** | 结构简单，适合作为「能力描述」的第一版；但需要补 `contextWindow`、`maxOutput`、`toolSchemaVersion` 等数值/枚举字段 |
| LLMFormat 分发 | **需改造适配** | 本项目若只对接 OpenAI 协议，应保留接口而裁剪枚举；`aiModel` 字符串分支应改为配置驱动的 `vendorQuirk` 集合 |
| 每厂商静态模型目录 | **不适用** | 人工维护成本高、易过期；更适合服务端下发或配置中心 |
| 缺上下文长度字段 | **需改造适配** | 必须在模型元数据中新增 `contextLength` 与 `maxOutputTokens`，否则预算无法按模型计算 |
| 缺工具能力字段 | **需改造适配** | 需要新增 `supportsTools`、`maxTools`、`parallelToolCalls`，否则只能「无条件附带」 |

---

## 3. 请求构建与 Prompt 顺序

### 3.1 它怎么做的

入口是 [index.svelte.ts](file:///d:/tmp_risuai_analysis/src/ts/process/index.svelte.ts) 的 `sendChat()`。

**（1）先建固定桶。** 在 L349-360 建立 `unformated` 对象，键为：

`main`、`jailbreak`、`chats`、`lorebook`、`globalNote`、`authorNote`、`lastChat`、`description`、`postEverything`、`personaPrompt`。

每个桶是 `OpenAIChat[]`。角色统一经 `convertPromptRole` 映射：`system → system`、`user → user`、`bot → assistant`（L648-652）。

**（2）决定顺序。** 两种模式：

- **兼容模式（无 `db.promptTemplate`）**：按 `db.formatingOrder` 数组顺序拼接桶。默认值（据 `database.svelte.ts`，**本轮未逐字复核**）为：

  ```
  ['main','description','personaPrompt','chats','lastChat','jailbreak','lorebook','globalNote','authorNote']
  ```

  即：主提示 → 角色描述 → 人设 → 聊天历史 → 最后一条聊天 → 越狱提示 → 世界书 → 全局注记 → 作者注记。

- **模板模式（有 `db.promptTemplate`）**：按卡片列表顺序渲染，卡片类型为 `main | description | personality | scenario | postEverything | chats | lastChat | jailbreak | lorebook | memory`；每张卡片可指定 `role2`（覆盖角色）和 `innerFormat`（用 `{{slot}}` 包裹原内容）。`postEverything` 卡片会被强制移到 `lastChat` 之前、其余内容之后（L362-370 的 `hasPostEverything` 检测）。

**（3）世界书/作者注记的插入点。** 世界书不是单独一段，而是按条目的 `position` / `depth` 分散插入：

- `position = 'depth'`：按 `depth` 值插到聊天历史倒数第 N 条附近；
- `position = 'before_char' / 'after_char'` 等：进入对应桶；
- 注记（`authorNote` / `globalNote`）按 `position` 与 `depth` 同样处理；
- 最终以 `unformated.chats` 中带 `memo` 的方式保留可裁剪标记。

**（4）记忆插入点。** 记忆不是独立卡片（模板模式下有 `memory` 卡片可选），而是**在 `chats` 数组头部 unshift 一条 `role: 'system'` 的消息**，并打 `memo: 'supaMemory' | 'hypaMemory'` 标记：

- SupaMemory：[supaMemory.ts L172-177](file:///d:/tmp_risuai_analysis/src/ts/process/memory/supaMemory.ts#L172-L177)。
- HypaV2 注入 `<Past Events Summary>` + `<Past Events Details>`：[hypav2.ts L626-640](file:///d:/tmp_risuai_analysis/src/ts/process/memory/hypav2.ts#L626-L640)。
- HypaV3 注入位置未在本轮落到具体行号，**未核实**（其返回值同样通过 `chats` 回传，`index.svelte.ts` L1104-1116 接收后赋值 `currentTokens`）。

**（5）格式化后统一修正。** `formated` 交给 `reformater()`（[request.ts L348-432](file:///d:/tmp_risuai_analysis/src/ts/process/request/request.ts#L348-L432)）：

- 模型不支持完整 system（无 `hasFullSystemPrompt`）：
  - 若支持首条 system（`hasFirstSystemPrompt`）：把开头连续的 system 合并成一条，最后 `unshift` 回去；
  - 其余 system 改写为 `user`，内容前缀 `"system: "`，或用 `db.systemContentReplacement`（`{{slot}}` 替换）。
- `requiresAlternateRole`：相邻同角色消息用 `'\n'` 合并（同时合并 `multimodals` / `thoughts` / `cachePoint`）。
- `mustStartWithUserInput`：首条不是 user 时，插入 `{role:'user', content:' '}`（单空格）。

### 3.2 算法与参数细节

- **顺序不是树，而是线性数组。** 所有桶按 `formatingOrder` 线性展开；同一桶内按条目 `insertorder` 排序（世界书部分见 §7.1）。
- **`postEverything` 的语义**：后置内容（通常是「回复格式要求」）被移到 `lastChat` 之前，确保它出现在用户最后一句之后、但仍属于「聊天桶」尾部。
- **`depth` 的语义**：`depth = 0` 表示插到最后一条消息之后；数值越大越往前。`reverse_depth` 则从前往后计数。**具体索引换算公式未逐字复核，未核实**。
- **模板卡片的 `innerFormat`**：对桶内每条消息用 `risuChatParser` 解析后 `.replace('{{slot}}', 原内容)`，即「模板包裹」而不是「替换」。
- **计数时机**：在构建过程中每加入一条消息就累加 token（模板模式 L679-684；兼容模式 L826-893），`currentTokens` 初值为 `db.maxResponse + 50`（L614-618）。

### 3.3 可移植性判断

| 项 | 判断 | 说明 |
|---|---|---|
| 「桶 + 顺序数组」模型 | **可直接移植** | 与厂商无关，且便于让用户自定义顺序 |
| `postEverything` 后置 | **可直接移植** | 纯数组操作，无外部依赖 |
| 模板卡片系统 | **需改造适配** | 若产品不需要「卡片式 prompt 编辑器」，可直接删；若要保留，需要重写 UI 与渲染 |
| `depth` 位置插入 | **需改造适配** | 需要明确「倒数第 N 条」的换算公式；不同协议对相邻同角色消息容忍度不同 |
| `reformater` 的 system 降级 | **可直接移植** | 规则清晰（前缀 `system: `、合并首条、单空格占位），但建议改为按 provider 配置而非 flag 组合 |
| 记忆作为首条 system | **需改造适配** | 把记忆塞进 `chats` 桶会与「历史裁剪」耦合；更稳妥是独立 `memory` 插槽并显式排序 |

### 3.4 世界书（Lorebook）的激活与预算算法

代码：[lorebook.svelte.ts](file:///d:/tmp_risuai_analysis/src/ts/process/lorebook.svelte.ts)。

**扫描参数**（L84-88）：

```
scanDepth   = char.loreSettings?.scanDepth   ?? db.loreBookDepth   // 默认 5（未逐字复核）
tokenBudget = char.loreSettings?.tokenBudget ?? db.loreBookToken   // 迁移后默认 8000（未逐字复核）
fullWordMatching = char.loreSettings?.fullWordMatching ?? false
recursiveScanning = char.loreSettings?.recursiveScanning ?? true
```

**匹配规则（`searchMatch`，L100-235）：**

1. 取最近 `scanDepth` 条消息：`messages.slice(messages.length - 5, messages.length)`。
2. 非正则模式：正文与 key 都 `toLocaleLowerCase()`，先去掉 `{{//...}}` 与 `{{comment:...}}`，再**删除所有空格**后做子串 `includes`（L179-228）。也就是说「New York」会匹配「NewYork」。
3. 全词模式（`fullWordMatching`）：按单空格 `split(' ')` 后做数组 `includes`，不删除标点（L192-209）。
4. 正则模式：key 必须是 `/pattern/flags` 形式，`new RegExp(pattern, flags)` 后逐条 `test`（L145-176）。
5. `all` 模式：所有 key 都要命中才激活。

**逐条装饰器语义（L307-519）：**

| 装饰器 | 行为 |
|---|---|
| `@probability(n)` | `Math.random() * 100 > n` 时**取消激活**（L493-497），即概率门 |
| `@priority(n)` | 覆盖条目 `priority`（L499-501） |
| `@ignore_on_max_context` | `priority = -1000`（L440-441），预算不足时最先被淘汰 |
| `@depth(n)` / `@reverse_depth(n)` | 覆盖插入深度 |
| `@activate_only_after(n)` | `currentChat.length < n` 时不激活 |
| `@activate_only_every(n)` | `currentChat.length % n !== 0` 时不激活 |
| `@recursive` / `@unrecursive` | 是否参与递归扫描 |
| `@no_recursive_search` | 不把已激活内容加入递归搜索文本 |
| `@dont_activate` / `@keep_activate_after_match` | 强制关闭 / 保持激活 |

**递归激活（L237-611）：**

- 外层 `while (matching)`：只要本轮有新条目激活就继续。
- 已激活条目（`activatedIndexes`）不重复扫描。
- 激活条目的内容会被加入 `recursivePrompt`，参与下一轮 `searchMatch`（可被其他条目的 key 命中）。

**排序与预算截断（L613-629）：**

```ts
// 1) 按 priority 降序
actives.sort((a, b) => b.priority - a.priority)

// 2) 贪心装预算
for (act of actives) {
  if (usedTokens + act.tokens <= loreToken) { usedTokens += act.tokens; keep(act) }
}

// 3) 按 order 降序，再 reverse → 最终按 order 升序输出
activesResorted.sort((a, b) => b.order - a.order)
return { actives: activesResorted.reverse() }
```

注意：**先按优先级筛，再按 order 排**。优先级只决定「谁先占用预算」，不决定最终在 prompt 中的位置；位置由 `order` 决定。`priority` 与 `order` 默认都取 `insertorder`（L276-277）。

**注入（L643-664）：** 带 `inject.lore` 的条目不再是独立消息，而是按 `inject.operation`（`append` / `prepend` / `replace`）合并到 `inject.location` 对应的已有条目中。

---

## 4. OpenAI 兼容适配

### 4.1 它怎么做的

核心函数：`requestOpenAI()`（[requests.ts L35](file:///d:/tmp_risuai_analysis/src/ts/process/request/openAI/requests.ts)）。

**base_url 解析优先级**（L512-541）：

1. `aiModel === 'nanogpt'` → `https://nano-gpt.com/api/v1/chat/completions`（订阅模式则 `.../subscription/v1/chat/completions`）。
2. `aiModel === 'openrouter'` → `https://openrouter.ai/api/v1/chat/completions`。
3. `arg.customURL`（反向代理/自定义）。
4. 兜底 `https://api.openai.com/v1/chat/completions`。
5. `modelInfo.endpoint` 存在则覆盖。
6. URL 以 `risu::` 开头：去掉前缀，并加 `X-Proxy-Risu: RisuAI` 头。

**反向代理 URL 自动补全**（`aiModel === 'reverse_proxy' && db.autofillRequestUrl`）：

- 以 `v1` 结尾 → 追加 `/chat/completions`；
- 以 `v1/` 结尾 → 追加 `chat/completions`；
- 不以 `completions` / `completions/` 结尾 → 追加 `/v1/chat/completions`（末尾已有 `/` 时补 `v1/chat/completions`）。

**鉴权与请求头**（L547-564）：

- `Authorization: Bearer <key>`；key 优先级：`arg.key` > `db.nanogptKey` / `db.proxyKey` / `db.openrouterKey` / `db.openAIKey`。
- 模型带 `keyIdentifier` 时，改用 `db.OaiCompAPIKeys[keyIdentifier]`（多 key 池）。
- OpenRouter 额外：`X-Title: RisuAI`、`HTTP-Referer: https://risuai.xyz`。
- NanoGPT 额外：`X-Provider`（`db.nanogptProvider`）。

**请求体字段**（L380-485）：

```jsonc
{
  "model": "<modelInfo.internalID | requestModel | gpt-3.5-turbo>",
  "messages": [...],
  "max_tokens": <arg.maxTokens>,
  "logit_bias": {...},       // 为空则删除
  "stream": false            // 之后按 useStreaming 改 true
}
```

之后按条件追加/改写：

| 条件 | 处理 |
|---|---|
| `LLMFlags.OAICompletionTokens` | `max_tokens` → `max_completion_tokens`，删除 `max_tokens` |
| `db.generationSeed > 0` | `body.seed = db.generationSeed` |
| 有 schema 且非 `noStructuredOutput` | `response_format: {type:'json_schema', json_schema: ...}` |
| `db.OAIPrediction` | `prediction: {type:'content', content: ...}` |
| `aiModel === 'openrouter'` | `route='fallback'`（若开启）、`transforms=['middle-out']`（若开启）、`provider.order/only/ignore`；`useInstructPrompt` 时删除 `messages`、改写 `prompt` |
| `deepSeekThinkingToggle` | `thinking: {type:'enabled', reasoning_effort: db.deepseekReasoningEffort ?? 'high'}`，并删除 `temperature/top_p/frequency_penalty/presence_penalty`；否则 `thinking: {type:'disabled'}` |
| `arg.tools` 非空 | 映射为 `{type:'function', function:{name, description, parameters: simplifySchema(inputSchema)}}` |
| 反向代理 Ooba 模式 | 把 `db.reverseProxyOobaArgs` 的非空键整体覆盖进 body |
| 支持内联图片且非 gpt 系 | 删除 `logit_bias` |
| `openAIFlexProcessing` 且命中判定 | `service_tier = 'flex'` |
| `multiGen` 且有 tools | 直接失败（不支持多生成 + 工具） |
| `multiGen` | `body.n = db.genTime`（**是否仅限 gpt 系，未逐字复核**） |

**流式开关**（L579-588）：`!arg.useStreaming → body.stream = false`；`arg.useStreaming → body.stream = true`。`arg.multiGen` 时走 `getTranStream`。

**`max_tokens` 的实际值**：`requestChatDataMain` 在 [request.ts L457](file:///d:/tmp_risuai_analysis/src/ts/process/request/request.ts#L457) 设置 `targ.maxTokens = arg.maxTokens ?? db.maxResponse`。而 `sendChat` 调用 `requestChatData` 时**没有传 `maxTokens`**（L1554-1567），所以**始终等于 `db.maxResponse`**。`generationInfo.outputTokens` 虽然按剩余预算算出来了（L1526-1529），但**没有传回请求**。

### 4.2 兼容层处理的厂商怪癖（清单）

1. **URL 自动补全**：反向代理只填域名也能用。
2. **OpenRouter**：`transforms: ['middle-out']` 让超长请求由服务端截断；`route: 'fallback'`；`provider.order/only/ignore`；`X-Title` / `HTTP-Referer` 统计头；`useInstructPrompt` 走 `/v1/completions` 风格 prompt。
3. **NanoGPT**：订阅端点切换 + `X-Provider` 指定上游。
4. **DeepSeek**：`thinking` 开关 + 开启时删除采样参数（避免 API 报错）。
5. **OpenAI 新模型**：`max_completion_tokens` 改名（`OAICompletionTokens`）。
6. **OpenAI Flex**：`service_tier: 'flex'` 降本。
7. **内联图片模型**：删除 `logit_bias`（部分兼容服务不接受该字段）。
8. **反向代理 Ooba 模式**：允许整段自定义 body 覆盖。
9. **本地网络限制**：`localNetwork.ts` 判定 localhost / `0.0.0.0` / 单标签主机名；浏览器安全策略下禁用并提示 `webLocalNetworkBlockedMessage`。**存在笔误**：`urlHost.includes("172.0.0.1")`（应为 `127.0.0.1`），本轮未逐字复核该行。

### 4.3 可移植性判断

| 项 | 判断 | 说明 |
|---|---|---|
| URL 拼装 + `/v1/chat/completions` 自动补全 | **可直接移植** | 纯字符串规则，建议改为 URL 规范化函数 |
| Bearer + 多 key 池 | **可直接移植** | 与 OpenAI 协议一致 |
| OpenRouter / NanoGPT 专用字段 | **需改造适配** | 应做成 `vendorQuirk` 配置（`transforms`、`provider`、`route`、`X-Provider`），不要硬编码 `aiModel` 字符串 |
| DeepSeek thinking 参数 | **需改造适配** | 属于特定厂商扩展，建议按模型元数据声明 |
| `max_completion_tokens` 改名 | **可直接移植** | 一个 flag 即可 |
| `logit_bias` 删除、Ooba body 覆盖 | **需改造适配** | 属于兼容兜底，建议放进「兼容开关」而非核心路径 |
| `service_tier: 'flex'` | **需改造适配** | 与计费策略绑定，需产品确认 |

---

## 5. SSE 流式解析

### 5.1 Chat Completions 路径（`getTranStream`）

代码：[requests.ts L972-1136](file:///d:/tmp_risuai_analysis/src/ts/process/request/openAI/requests.ts#L972-L1136)。

**分帧方式（关键缺陷）：**

1. 维护一个**累积缓冲区** `dataUint`，每个 `chunk` 到达就 `concat` 进缓冲区（L990-993）。
2. 每次都对**整个缓冲区** `toString().split('\n')` 重新遍历（L997）。
3. 只处理以 **`"data: "`（带一个空格）** 开头的行（L1000）。
4. 每行去掉 `"data: "` 后 `JSON.parse`（L1002、L1036）。
5. 遇到 `"[DONE]"` 时把当前累积结果整体 `enqueue` 一次并 `return`（L1003-1034）。

**因此：**

- **O(n²) 复杂度**：流越长，每帧重复解析的文本越多。
- **不支持空行分帧**（SSE 标准用 `\n\n` 分隔事件）、**不支持多行 `data:` 拼接**、**不支持 `event:` 字段**、**不支持无空格的 `data:`**。
- **没有 flush / close 处理**：`TransformStream` 没有 `flush()`，流异常结束时不会补发最后一帧。
- **没有 usage**：不读取 `usage`、`prompt_tokens`、`completion_tokens`。
- **没有 `finish_reason`**：不处理结束原因。
- 异常被 `catch (error) {}` 静默吞掉（L1097、L1131-1133）。

**增量语义（全量快照）：**

`appendStreamingFragment(current, incoming)`（L978-986）：

```ts
if (incoming.length > current.length && incoming.startsWith(current)) return incoming
return current + incoming
```

即：如果新片段是「当前结果的前缀扩展」，则**替换**；否则**追加**。这同时兼容「delta 增量」与「全量快照」两种服务端实现，但一旦服务端先发全量、再发增量，就会重复拼接。最终**每一帧 `enqueue` 的都是完整文本快照**（L1128-1130），由下游 `wrapToolStream` 再做增量截取（`prefix` 对比，L1151+）。

**多候选**：

- `arg.multiGen`：按 `choice.index` 分键 `readed[index]`；
- 非 `multiGen`：所有 `choices` 的内容都合并进 `readed["0"]`（L1047-1052）。

**工具调用**：

- `choice.delta.tool_calls` 按 `index` 累积，`function.arguments` 用同一 `appendStreamingFragment` 拼接（L1055-1090），最终序列化到 `readed["__tool_calls"]`。

**推理内容**：

- `delta.reasoning_content` 或 `delta.reasoning` 追加到 `reasoningContent`，并置 `reasoningFromStructured = true`（L1091-1095）。
- `[DONE]` 时若模型 flag 为 `deepSeekThinkingOutput` 且没有结构化推理，则用正则 `/(.*)<\/think>/gms` 从正文中剥离 `...`，包成 `<Thoughts>...</Thoughts>` 前缀（L1004-1030）。

**已知运行时风险（已确认）：**

- 若服务端返回空内容（没有 `delta.content`），`[DONE]` 分支里 `readed["0"].replace(...)`（L1005）会抛 `TypeError: Cannot read properties of undefined`，被外层 `catch` 吞掉，**最后一帧不会发出**。
- 解析错误全部静默，无法区分「服务端返回非 JSON」与「网络截断」。

### 5.2 Responses API 路径

代码：`src/ts/process/request/openAI/responses.ts`（`requestOpenAIResponseAPI`）。**本轮未逐字复核行号**，以下为前序阅读结论：

- 分帧**按空行**（`\n\n`），多行 `data:` 用换行拼接，CRLF 先剥离。
- 处理事件：`response.output_text.delta`、`response.reasoning_summary_text.delta`、`response.completed`、`error`、`[DONE]`。
- 同样是「每个事件携带全量快照」的语义。
- **不读取 usage**。
- 忽略 `event:` 行，仅按 `data:` JSON 的 `type` 字段分派。

### 5.3 超时与断流

- `buildTimeoutSignal` 只对**本地网络流式**请求设置 600 000 ms 超时（`getLocalNetworkRequestOptions`，`openAI/shared.ts`）；**远程请求与非流式请求没有超时**（前序阅读结论，本轮未逐字复核）。
- 用户主动中断通过 `AbortSignal` 传入 `fetchNative`；`requestChatData` 在每次尝试前后检查 `abortSignal?.aborted`（L232-237、L274-279）。
- 没有「流中断后自动重连/续传」逻辑。

### 5.4 可移植性判断

| 项 | 判断 | 说明 |
|---|---|---|
| Responses API 的「空行分帧 + 多行 data 拼接」 | **可直接移植** | 符合 SSE 规范，代码量小 |
| Chat Completions 的「累积缓冲全量重解析」 | **不适用** | O(n²)、无空行分帧、无 flush，必须重写为「按行增量 + 事件缓冲」 |
| `appendStreamingFragment` 前缀判断 | **需改造适配** | 思路可借鉴（兼容 delta/snapshot），但应在协议层明确模式，而不是靠启发式 |
| 工具调用增量累积 | **可直接移植** | 按 `index` 聚合 `arguments` 是标准做法 |
| 推理内容剥离 `<think>` | **需改造适配** | 依赖模型 flag 与正则，建议改为「按字段优先、正则兜底」 |
| usage / finish_reason 统计 | **不适用** | 当前完全缺失，需要新增 `stream_options: {include_usage:true}` 并解析最后一帧 |

---

## 6. Token 计数与预算

### 6.1 它怎么做的

**分词器**：[tokenizer.ts](file:///d:/tmp_risuai_analysis/src/ts/tokenizer.ts) 的 `encode()`（L80-207）**不是字符估算**，而是真正加载分词器库：

- `tikJS()`：`@dqbd/tiktoken`，`cl100k_base`（默认）或 `o200k_base`。
- `tokenizeWebTokenizers()`：`@mlc-ai/web-tokenizers`，从 `/token/...` 拉取 SentencePiece / `tokenizer.json`，覆盖 claude、llama、llama3、mistral、novelai、novellist、cohere、deepseek、deepseek-v4、glm4、glm5。
- `gemmaTokenize()`：`@huggingface/transformers` 的 `GemmaTokenizer`。
- `tokenizeGoogleCloud()`：调用 Google `countTokens` API；失败回退 gemma。
- 缓存：`LRUMap`，上限 **1500** 条，key 由 `text::aiModel::customTokenizer::pluginProvider::googleClaudeTokenizing::modelInfo.tokenizer::pluginTokenizer` 拼接（L13-28）。

**关键：`encode()` 只按全局 `db.aiModel` 选分词器，不按单条消息或单个请求的模型选。** 因此同一会话中「主模型 / 子模型 / 记忆模型」混用时，token 估算会失真。

**每条消息的附加 token**：[index.svelte.ts L287-293](file:///d:/tmp_risuai_analysis/src/ts/process/index.svelte.ts#L287-L293)：

```ts
let caculatedChatTokens = 0
if (DBState.db.aiModel.startsWith('gpt')) caculatedChatTokens += 5
else caculatedChatTokens += 3
```

`ChatTokenizer`（L409-436）用该常量，并在消息有 `name` 时额外加 `encode(name).length + 1`；gpt 系模型用 `'noName'` 模式（不计 name）。图片按 §6.2 公式计。

**预算模型**：

- `maxContextTokens = db.maxContext`（默认 **4000**，据 `database.svelte.ts`，本轮未逐字复核）。
- `currentTokens` 初值 = `db.maxResponse`（默认 **500**）+ **50**（注释「for unexpected error」）（L614-618）。
- 只有一个池：**输入 + 输出共用 `maxContext`**。

**两阶段裁剪：**

1. **预裁剪（历史桶）**：在记忆处理之前，若 `currentTokens > maxContextTokens`，从 `chats[0]` 开始逐条 `splice(0,1)` 并扣减 token；当 `chats.length <= 1` 仍超限则报 `toomuchtoken` 错误（L1143-1152）。
2. **二次裁剪（格式化后）**：对最终 `formated` 重新逐条计数（L1501-1505）；若超过，按 `pointer` 从 0 开始遍历，**只清空带 `removable` 标记的消息**（`content = ''`），然后过滤掉空内容（保留有 `multimodals` 的），指针越界则报错（L1507-1523）。

**输出预算**（L1526-1529）：

```ts
let outputTokens = db.maxResponse
if (inputTokens + outputTokens > maxContextTokens) {
  outputTokens = maxContextTokens - inputTokens
}
```

结果写入 `generationInfo`（L1533-1545），但**请求侧仍用 `db.maxResponse`**（见 §4.1），所以这个计算目前只影响展示，不影响请求。

### 6.2 具体公式

**图片 token（`tokenizeMultiModal`，L445-482）：**

- 低清晰度：固定 **87** tokens。
- 高清晰度：
  1. 等比缩放到最长边 ≤ **768**；
  2. `chunk = ceil(width / 512) * ceil(height / 512)`；
  3. `tokens = chatAdditionalTokens + chunk * 2 + 85`。

**每条消息：**

```
tokens(msg) = encode(content).length
            + chatAdditionalTokens(5 或 3)
            + (name ? encode(name).length + 1 : 0)
            + Σ imageTokens
            + (countThoughts ? Σ (encode(thought).length + 1) : 0)
```

**裁剪顺序总结：**

1. 先扣掉输出预留（`maxResponse`）与 50 token 缓冲；
2. 从**最旧**的聊天开始丢；
3. 格式化后再从**前部**清空 `removable` 消息（通常是 prompt 卡、世界书、注记等）；
4. 仍超限则报错终止，不自动降低 `maxResponse`（`outputTokens` 的调整未回传给请求）。

### 6.3 可移植性判断

| 项 | 判断 | 说明 |
|---|---|---|
| 真实分词器库（tiktoken / HF tokenizers） | **可直接移植** | 服务端用 `tiktoken`/`tokenizers` 即可；注意浏览器版依赖 `/token/...` 静态资源，Node 环境需改为本地文件或包内置 |
| 按全局 `aiModel` 选分词器 | **需改造适配** | 必须改为「按请求模型」选择，否则主/子模型混用时统计错误 |
| 消息附加 token 常量（5/3） | **可直接移植** | 作为近似值足够；不同厂商可配置 |
| 图片 token 公式 | **可直接移植** | 与 OpenAI vision 计费口径一致；其他厂商需单独公式 |
| 单池 `maxContext` 预算 | **需改造适配** | 应改为「模型上下文 + 输出预留 + 安全边际」三段式，并按模型读取 `contextLength` |
| 两阶段裁剪顺序（先旧历史、后 removable） | **可直接移植** | 顺序合理；建议把「不可裁剪」标记显式化 |
| 二次裁剪只清空 `removable` | **可直接移植** | 避免误删系统提示；但需注意「清空后仍可能超限」的报错路径 |
| 输出预算未回传请求 | **不适用（缺陷）** | 移植时应让 `max_tokens = min(maxResponse, maxContext - inputTokens)` |

---

## 7. 错误、重试与回退

### 7.1 它怎么做的

入口：`requestChatData()`（[request.ts L205-346](file:///d:/tmp_risuai_analysis/src/ts/process/request/request.ts#L205-L346)）。

**模型链**：

```
fallBackModels = clone(db.fallbackModels[model] ?? []) + ['']
```

`''` 表示「使用当前选中的主模型」。循环 `fallbackIndex` 从 0 到链尾；第 0 个是主模型，之后是回退模型。

**单模型重试**：

```ts
let trys = 0
while (true) {
  ...
  if (da.type === 'success' && db.banCharacterset ...) { trys += 1; continue }
  if (da.type !== 'fail' || da.noRetry) return da
  if (da.failByServerError) {
    await sleep(1000)
    if (db.antiServerOverloads) trys -= 0.5
  }
  trys += 1
  if (trys > db.requestRetrys) {
    if (last model || plugin model) return da
    break   // 切下一个模型
  }
}
```

**参数与行为：**

- `db.requestRetrys` 默认 **2**（据 `database.svelte.ts`，未逐字复核）→ 每模型实际尝试 **3 次**（`trys = 1,2,3`，`>2` 时退出）。
- `failByServerError`：仅 Anthropic / Google 的适配层会置位（前序阅读结论）；**OpenAI 路径不置位**。
- `antiServerOverloads`：开启时 `trys -= 0.5`，等效把尝试次数翻倍（约 5 次）。
- 固定 `sleep(1000)`，**没有指数退避、没有 jitter、没有 `Retry-After` 解析**。
- **空响应回退**：若 `type === 'success'`、不是最后一个模型、且 `db.fallbackWhenBlankResponse`，结果 `trim()` 为空则 `break` 切下一个模型（L309-313）。
- **字符集封禁**：`db.banCharacterset` 命中则计一次重试并重试当前模型（L291-307）。
- **用户中断**：每次尝试前后检查 `abortSignal.aborted`，返回 `{type:'fail', result:'Aborted'}`。
- **插件模型**：`da.model === 'custom' || startsWith('pluginmodel:::')` 时不回退（L332-334）。

**错误分类：无。** 未见按 HTTP 状态码（429 / 5xx / 401 / 400）分类处理；`requestOpenAI` 失败分支返回 `type:'fail'` 并把响应体文本作为 `result`（具体构造未逐字复核）。也就是说：**401 密钥错误也会按普通失败重试 3 次**。

### 7.2 算法与参数细节

| 参数 | 值 | 来源 |
|---|---|---|
| 每模型尝试次数 | `db.requestRetrys + 1`，默认 3 | `request.ts L331` |
| 服务端错误 sleep | 固定 1000 ms | `request.ts L324` |
| 过载补偿 | `trys -= 0.5` | `request.ts L326` |
| 回退模型数 | `db.fallbackModels[model]` 长度 + 1 | `request.ts L207-209` |
| 空响应回退 | `fallbackWhenBlankResponse` 开关 | `request.ts L309-313` |
| 超时 | 仅本地流式 600 000 ms | `openAI/shared.ts`（未逐字复核） |
| 远程/非流式超时 | **无** | 同上 |
| 退避 | **固定 1 s，无指数、无 jitter** | `request.ts L324` |

### 7.3 可移植性判断

| 项 | 判断 | 说明 |
|---|---|---|
| 模型链 + 逐模型回退 | **可直接移植** | 结构清晰：主模型 → 备用列表 → 空字符串回落 |
| 空响应视为失败并切换模型 | **可直接移植** | 建议保留，阈值可配置 |
| 固定 1 s sleep | **不适用** | 应替换为指数退避（如 0.5s/1s/2s，上限 8s）+ 随机 jitter |
| 无状态码分类 | **不适用** | 必须新增：401/403 不重试、400 不重试、429 读 `Retry-After`、5xx 重试、网络错误重试 |
| 无远程超时 | **不适用** | 必须给所有请求加 `AbortSignal.timeout` 或 `Promise.race` |
| 字符集封禁重试 | **需改造适配** | 属于内容层过滤，建议放到后处理而非重试主循环 |

---

## 8. 记忆与摘要机制

RisuAI 有 4 套记忆实现：**HypaV3（默认）**、HypaV2、SupaMemory、Hanurai。核心思路一致：**按 token 预算摘要旧聊天，检索相关摘要后作为 system 消息注入**。

### 8.1 HypaV3（默认实现）

代码：[hypav3.ts](file:///d:/tmp_risuai_analysis/src/ts/process/memory/hypav3.ts)。

**默认参数**（`createHypaV3Preset`，L1798-1815）：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `memoryTokensRatio` | **0.2** | 记忆预算占 `maxContext` 比例 |
| `extraSummarizationRatio` | **0** | 额外摘要触发阈值 |
| `maxChatsPerSummary` | **6** | 每次摘要最多吞几条消息 |
| `recentMemoryRatio` | **0.4** | 近期摘要预算占比 |
| `similarMemoryRatio` | **0.4** | 相似摘要预算占比 |
| `queryChatCount` | **3** | 检索用最近几条聊天 |
| `summarizationModel` | **`subModel`** | 用子模型做摘要 |
| `summaryChunkSeparator` | **`"\\n\\n"`** | 摘要分块分隔符（运行时是字面 `\n\n` 还是换行，**未核实**） |
| `summarizationRequestsPerMinute` | **20** | 摘要速率限制 |
| `summarizationMaxConcurrent` | **1** | 摘要并发 |
| `embeddingRequestsPerMinute` | **100** | 向量化速率限制 |
| `embeddingMaxConcurrent` | **1** | 向量化并发 |
| `enableSimilarityCorrection` | false | 是否用摘要再生成查询 |
| `preserveOrphanedMemory` | false | 是否保留孤立记忆 |
| `doNotSummarizeUserMessage` | false | 是否跳过 user 消息 |
| `useExperimentalImpl` | false | 是否用实验实现 |

**预算公式**（L237-254）：

```
memoryTokens = floor(maxContext * memoryTokensRatio)      // 默认 0.2
空记忆消息 token = tokenizeChat({role:'system', content: wrapWithXml(memoryPromptTag, '')})
availableMemoryTokens = memoryTokens - 空记忆消息 token   // 仅当已有摘要或已超限时启用
currentTokens += memoryTokens                              // 预留
```

**摘要触发**（L256-300）：

```
触发条件：currentTokens > maxContext
目标值：targetTokens = maxContext * (1 - extraSummarizationRatio)
循环：当 currentTokens > targetTokens 时
  每批最多 maxChatsPerSummary(6) 条
  且不能动最后 queryChatCount(3) 条
  若剩余可摘要条数 <= 3：若已不超 maxContext 则停止，否则报错
  摘要完成后 currentTokens -= toSummarizeTokens，并从 chats 中移除
```

**四段选择（按顺序消耗预算）**：

1. **重要摘要**（L511-527）：按 `data.summaries` 原顺序，`isImportant === true` 的逐条加入，直到单条放不下 `availableMemoryTokens` 就 `break`。
2. **近期摘要**（L544-589）：`reservedRecent = floor(available * 0.4)`；从未选中列表**从末尾往前**逐条加入，直到超预算。
3. **相似摘要**（L592-776）：
   - `reservedSimilar = floor(available * 0.4)`；
   - 若 `randomMemoryRatio <= 0`（即 0.4 + 0.4 = 0.8 < 1 时不会成立，默认不触发），把近期未用完的预算划给相似；
   - 把未选摘要按分隔符切成 chunk；
   - 用最近 `queryChatCount` 条聊天做查询，向量检索；
   - 按分数取摘要，直到预算耗尽。
4. **随机摘要**（L817-857）：
   - `randomMemoryRatio = 1 - 0.4 - 0.4 = 0.2`；
   - `reservedRandom = available * 0.2 + 近期未用完 + 相似未用完`；
   - 未选摘要用 `sort(() => Math.random() - 0.5)` 打乱后逐条加入；
   - 放不下的**跳过继续**（`continue`，不是 `break`）。

**查询与打分公式**（L680-699、L1836-1896）：

```ts
// 最近 n = queryChatCount 条非空聊天，每条再按 "\n\n" 切成 subQueries
weight = (index + 1) / ((n * (n + 1)) / 2) / subQueries.length
```

即：越新的消息权重越高，总权重按三角数归一化，再按子查询数量均分。

**融合：**

```ts
// simpleCC：加权求和
score(item) = Σ_list score_list(item) * weight_list

// childToParentRRF：把 chunk 分数映射回摘要
rrf(parent) = Σ_child 1 / (60 + rank_child)
```

`k = 60` 是 RRF 默认值。`normalizeScores()`（L1899-1920）做 min-max 归一化（分子为 0 时全 0，全相同时全 1），**是否在 V3 主流程中被调用，未核实**。

**摘要调用**：

- 走 `requestChatData(..., 'memory')`，**非流式**。
- 默认摘要 prompt（据前序阅读）：`[Summarize the ongoing role story, It must also remove redundancy and unnecessary text and content from the output.]`
- 摘要结果剥离 `<Thoughts>` 等思考标签。
- 本地摘要走 `chatCompletion`：`max_tokens = 8192`、`temperature = 0`、`enable_thinking = false`，并剥离 `<think>`。
- 速率限制：`taskRateLimiter.ts` 按类别（摘要 / 向量化）做 60 秒窗口限流 + 并发队列。

### 8.2 HypaV2

代码：[hypav2.ts](file:///d:/tmp_risuai_analysis/src/ts/process/memory/hypav2.ts)。

**参数：**

- `db.hypaAllocatedTokens` 默认 **3000**：虚拟预算，先 `currentTokens += 3000` 再判断（L372-374）。
- `db.hypaChunkSize` 默认 **3000**：每批摘要的最大 token。
- 保留最后 **4** 条消息不摘要（`idx < chats.length - 4`，L417）。

**摘要路径（遗留实现）：**

- 直接调 `https://api.openai.com/v1/completions`（L66）。
- 模型 `text-davinci-003`（可配置），`max_tokens = 600`，温度 0（L78-80）。
- prompt 手工拼 `<|im_start|>system ...`。

**检索打分（L589-600）：**

```ts
for (i = 0; i < 3; i++) {          // 最近 3 条聊天
  query = chats[chats.length - i - 1]
  results = similaritySearch("search_query: " + query)
  for (result of results) {
    scoredResults[result.text] += result.score / (i + 1)
  }
}
```

**预算分配：**

- 摘要正文最多用 `allocatedTokens / 2`（L570）；
- 剩余预算给相似 chunk 详情（L607-624）；
- 最终拼成 `<Past Events Summary>...</Past Events Summary>\n<Past Events Details>...</Past Events Details>`（L626）。

**裁剪：** 摘要完成后只保留 `chats.slice(idx)`（未摘要部分），注入 system 记忆消息（L634-640）。

### 8.3 SupaMemory

代码：[supaMemory.ts](file:///d:/tmp_risuai_analysis/src/ts/process/memory/supaMemory.ts)。

- **摘要循环**：当 `currentTokens > maxContext` 时，从 `chats` 头部开始攒够 `chunkSize` 就摘要（L286-340）。
- **分块上限**：

  ```
  maxChunkSize = min(floor(maxContext / 3), db.maxSupaChunkSize)  // maxSupaChunkSize 默认 1200
  ```

  若一次攒不够，`maxChunkSize *= 0.7` 递归收缩；小于 **500** 时返回 `"Not Enough Tokens to summarize"`（L304-311）。
- **`asHyper` 扩展**：对摘要 chunk 做向量检索，查询 = 过滤后 `chats.slice(0, 4)` 的字符串化，取 **top 3**，前缀 `"past events: "`，额外 +10 token（L157-168）。
- **注入**：`chats.unshift({role:'system', content: supaMemory + '\n\n' + hypaResult, memo:'supaMemory'})`（L172-177）。
- 默认摘要 prompt（L202-204）：`[Summarize the ongoing role story, It must also remove redundancy and unnecessary text and content from the output to reduce tokens for gpt3 and other sublanguage models]`。

### 8.4 Hanurai

代码：`src/ts/process/memory/hanuraiMemory.ts`（**本轮未逐字复核行号**）。前序阅读结论：

- `db.hanuraiTokens` 默认 **1000**，`db.hanuraiSplit` 默认 false。
- 查询最后 4 条聊天（索引 1..3），前缀 `search_document:` / `search_query:`。
- 分数权重 `1/i`，累加去重。
- 按预算从最旧聊天开始丢弃以腾出空间。
- 注入 `memo: 'supaMemory'` 的 system 消息。

### 8.5 向量层（HypaProcesser）

代码：`src/ts/process/memory/hypamemory.ts`（**本轮未逐字复核行号**）。前序阅读结论：

- **本地向量**：`transformers.js` `feature-extraction`，mean pooling + L2 normalize。
- **远程向量**：OpenAI 兼容 embeddings，key = `db.supaMemoryKey`，model = `db.hypaModel`。
- **批大小 50**。
- **缓存**：localforage（IndexedDB），key = `原文本 + "|" + model`（**不是内容哈希**，长文本会撑大存储）。
- **相似度**：`similarity()` 用**点积**（向量已归一化时等价余弦）；`cosineSimilarity` 存在但未被使用。
- **`similaritySearch` 排序比较器有缺陷**：`(a,b) => a.similarity > b.similarity ? -1 : 0`，不满足严格弱序；HypaV3 的 `similaritySearchScoredEx` 使用了正确排序（前序结论）。

### 8.6 可移植性判断

| 项 | 判断 | 说明 |
|---|---|---|
| HypaV3「四段预算选择」 | **需改造适配** | 结构可复用（重要→近期→相似→随机），但 0.2/0.4/0.4 的比例需按产品调；`random` 段用偏置洗牌应改为 Fisher–Yates |
| 加权余弦 + RRF(k=60) | **可直接移植** | 公式明确，适合作为混合检索融合层 |
| 摘要触发（超限后回退到 `maxContext*(1-extra)`） | **可直接移植** | 阈值语义清晰 |
| 摘要批次 6 条、保留最近 3 条 | **可直接移植** | 参数可配置 |
| HypaV2 的 `text-davinci-003` / `/v1/completions` | **不适用** | 模型已下线，接口也非 Chat Completions |
| SupaMemory 分块 0.7 收缩、下限 500 | **可直接移植** | 递归收缩策略简单有效 |
| 向量缓存用原文做 key | **需改造适配** | 应改为内容哈希 + 模型名，避免重复与膨胀 |
| 点积相似度 | **可直接移植** | 前提是向量已归一化；需在代码中显式 normalize |
| 缺陷比较器 / 偏置洗牌 | **不适用（缺陷）** | 不要照搬 |
| 速率限制（20/min、100/min、并发 1） | **可直接移植** | 适合前端或单机后端；服务端可换成队列/令牌桶 |

---

## 9. 本地模型 vs 远程模型

### 9.1 分支判定

- **Ollama**：`requestOllama()` 走 `ollama` SDK 的 `/api/chat`，不经过 OpenAI 兼容层。
- **浏览器内 Transformers / WebLLM**：模型 id 带 `:::` 后缀时走 `runTransformers()`，用 `applyChatTemplate()` 套 ChatML / Llama3 模板。
- **本地网络判定**：`network/localNetwork.ts` 的 `isLocalNetworkUrl()`（localhost、`0.0.0.0`、单标签主机名）；`db.localNetworkMode` 开启时走 `getLocalNetworkRequestOptions()`（含 600 000 ms 超时）。
- **远程**：走 `requestOpenAI` 等标准适配层。

### 9.2 行为差异

| 维度 | 远程（OpenAI 兼容） | 本地（Ollama / Transformers） |
|---|---|---|
| token 预算 | 有（`maxContext` 两阶段裁剪） | **不执行**（前序结论；Ollama 路径未见预算逻辑） |
| usage | 不解析（OpenAI 路径） | 无 |
| 流式 | SSE 全量快照 | 全量快照（Transformers 甚至非流式） |
| 采样参数 | `applyParameters` 白名单 | `db.ooba` 的 top_k/top_p/repetition/typical |
| 输出上限 | `db.maxResponse` | `max_new_tokens`（部分分支用 `db.maxResponse`，见 `request.ts L670`） |
| 模板 | 由服务端 tokenizer 处理 | `applyChatTemplate` 手工套 ChatML/Llama3 |
| 系统消息 | 按 flag 降级 | 模板决定；本地摘要强制 system 在首位 |

### 9.3 可移植性判断

| 项 | 判断 | 说明 |
|---|---|---|
| 「本地/远程」分支接口 | **需改造适配** | 若产品只做云端，可省略；若保留，应把「预算、流式、usage」做成统一中间层，而不是各分支各写一套 |
| Ollama `/api/chat` | **不适用** | 与 OpenAI 协议不同，除非明确支持 Ollama |
| Transformers 模板体系 | **需改造适配** | ChatML/Llama3 模板可借鉴，但和 OpenAI messages 语义不完全一致 |
| 本地网络超时 600s | **可直接移植** | 数值可配置 |

---

## 10. 缺陷与风险清单（移植时不要照搬）

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| 1 | `requests.ts` `getTranStream` | 累积缓冲每帧全量重解析 | O(n²)，长流卡顿 |
| 2 | 同上 | 只认 `"data: "`，无空行分帧、无多行 data、无 `event:` | 与标准 SSE 服务器不兼容 |
| 3 | 同上 | 无 `flush()` | 断流丢最后一帧 |
| 4 | 同上 | 不解析 `usage` / `finish_reason` | 无法计费/统计/判断截断 |
| 5 | 同上 | `[DONE]` 分支 `readed["0"].replace` 可能 undefined | 空响应时 TypeError 被吞，最终帧丢失 |
| 6 | 同上 | `catch {}` 静默 | 无法区分协议错误与网络错误 |
| 7 | `request.ts L457` + `index.svelte.ts L1554` | `max_tokens` 永远是 `db.maxResponse` | 输出预算计算无效，长输入可能超限 |
| 8 | `globalApi` / `shared.ts` | 远程与非流式无超时 | 请求可能永久挂起 |
| 9 | `request.ts` | 无状态码分类、固定 1s sleep | 401/400 也重试，浪费时间；429 不读 `Retry-After` |
| 10 | `localNetwork.ts` | `172.0.0.1` 笔误（应为 `127.0.0.1`） | 本机地址可能漏判 |
| 11 | `hypamemory.ts` | `similaritySearch` 比较器非严格弱序 | 排序结果不稳定 |
| 12 | `hypav3.ts L839` | `sort(() => Math.random() - 0.5)` | 随机分布偏置 |
| 13 | `tokenizer.ts` `encode()` | 只看全局 `db.aiModel` | 主/子模型混用时估算失真 |
| 14 | `model/types.ts` | `hasStreaming` flag 未使用（未核实其他位置） | 元数据误导 |
| 15 | `requests.ts` L565+ | `multiGen` 与 tools 互斥直接失败 | 功能限制，需产品确认 |

---

## 11. 可移植性总表

### 11.1 可直接移植

| 能力 | 代码位置 | 备注 |
|---|---|---|
| 桶 + 顺序数组的 prompt 组装 | `index.svelte.ts` `sendChat` L349-360、L676+ | 需替换默认顺序 |
| `postEverything` 后置 | 同上 L362-370 | 纯数组操作 |
| `reformater` system 降级规则 | `request.ts` L348-432 | 前缀 `system: `、首条合并、单空格占位 |
| URL 自动补全 + Bearer + 多 key 池 | `requests.ts` L512-564 | 建议函数化 |
| `max_completion_tokens` 改名 | `requests.ts` L396-399 | 一个 flag |
| 工具调用 delta 按 index 聚合 | `requests.ts` L1055-1090 | 标准做法 |
| 分词器库接入（tiktoken / HF） | `tokenizer.ts` L264-387 | Node 侧改用本地文件 |
| 消息附加 token 常量、图片 token 公式 | `tokenizer.ts` L445-482 | 数值可配置 |
| 两阶段裁剪顺序 | `index.svelte.ts` L1143-1152、L1507-1523 | 先旧历史、后 removable |
| 模型链 + 空响应回退 | `request.ts` L205-346 | 建议加状态码分类 |
| HypaV3 四段预算选择 | `hypav3.ts` L505-857 | 比例可调 |
| 加权余弦 + RRF(k=60) | `hypav3.ts` L1836-1896 | 公式可直接用 |
| SupaMemory 分块 0.7 收缩 / 下限 500 | `supaMemory.ts` L286-311 | 简单有效 |
| 世界书概率门 / 优先级 / 预算截断 | `lorebook.svelte.ts` L493-501、L613-625 | 见 §3.4 |
| 速率限制器 | `taskRateLimiter.ts` | 前端可用 |

### 11.2 需改造适配

| 能力 | 改造点 |
|---|---|
| Provider/能力描述 | 增加 `contextLength`、`maxOutputTokens`、`supportsTools`、`parallelToolCalls`；flag 改为「能力对象 + 厂商扩展」 |
| 请求分发 | `aiModel` 字符串分支改为 `vendorQuirk` 配置表 |
| Prompt 模板卡片 | 若保留，需要重写渲染与 UI；建议先只保留桶模型 |
| `depth` 插入 | 明确倒数索引换算；同角色合并策略按协议配置 |
| 记忆注入 | 从 `chats` 桶移到独立 `memory` 插槽，避免与裁剪耦合 |
| Token 预算 | 三池：模型上下文 / 输出预留 / 安全边际；按请求模型选 tokenizer |
| 重试策略 | 状态码分类 + 指数退避 + jitter + `Retry-After` |
| SSE 解析 | 改为「按行增量 + 空行分帧 + 多行 data」，并解析 usage/finish_reason |
| 记忆算法 | HypaV3 比例与随机段改造；向量缓存改内容哈希；比较器修正 |
| 本地/远程分支 | 统一预算、流式、usage 中间层 |
| OpenRouter / NanoGPT / DeepSeek 专用字段 | 配置化，不硬编码 |

### 11.3 不适用

| 能力 | 原因 |
|---|---|
| Chat Completions 累积缓冲全量重解析 | O(n²)、不符合 SSE 规范，应重写 |
| 无超时的远程请求 | 生产环境不可接受 |
| 无状态码分类的重试 | 会把 401/400 当临时故障 |
| HypaV2 `text-davinci-003` / `/v1/completions` | 模型与接口均已过时 |
| 每厂商静态模型目录 | 维护成本高、易过期 |
| `sort(() => Math.random() - 0.5)` 随机 | 分布偏置，不应照搬 |
| `similaritySearch` 缺陷比较器 | 排序不稳定 |
| Ollama / Transformers 专属路径 | 除非产品明确支持本地推理 |
| 未使用的 `hasStreaming` flag | 元数据噪音 |

---

## 12. 未核实项汇总

1. `LLMFormat` 全量枚举成员与 `requestChatDataMain` 的完整 switch 分支。
2. `database.svelte.ts` 中 `formatingOrder`、`maxContext=4000`、`maxResponse=500`、`requestRetrys=2`、`loreBookToken=8000` 的默认值（前序阅读，本轮未逐字复核）。
3. `depth` / `reverse_depth` 到消息索引的精确换算。
4. HypaV3 记忆消息的具体注入位置与 `memo` 值。
5. `summaryChunkSeparator` 默认 `"\\n\\n"` 在运行时的实际字符串（字面反斜杠还是换行）。
6. `normalizeScores` 是否在 HypaV3 主流程被调用。
7. `rename max_tokens` 之外，OpenAI 失败分支的具体 `failByServerError` / `noRetry` 构造。
8. `hanuraiMemory.ts`、`hypamemory.ts`、`taskRateLimiter.ts`、`webllm.ts`、`transformers.ts` 的具体行号与实现细节。
9. `openAI/responses.ts` 的具体行号与事件处理细节。
10. `globalApi.svelte.ts` 的 `buildTimeoutSignal` / `fetchNative` 行为。
11. `multiGen` 是否仅限 gpt 系模型。
12. `localNetwork.ts` 的 `172.0.0.1` 笔误是否仍在当前主干。
13. 仓库具体 commit 与版本号。
