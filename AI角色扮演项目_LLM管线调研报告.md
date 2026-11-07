# AI 角色扮演开源项目 LLM 处理逻辑调研

> 调研日期：2026-11-07
> 范围：GitHub / Gitee / GitCode 上可核实存在的 AI roleplay、角色卡、AI 伴侣项目
> 方法：只读本地代码 + 官方文档、官方仓库与代码索引；**未修改任何代码**
> 目标项目：`d:\ai_manager_workspace\missai_local`（Python 标准库后端 + `roleplay_core` 管线 + `prompt_builder.py`）

---

## 一、结论先行

1. **成熟项目的核心竞争力不是“写更长的提示词”，而是把 prompt 当成分层预算系统**：固定人设层、动态世界层、深度注入层、历史层、本轮指令层各自有 token 配额、插入位置和降级顺序。SillyTavern 的 World Info 预算、RisuAI 的 `formatingOrder`、Agnai 的 memory priority/weight 都是这个思路。
2. **世界书不是“关键词字典”，而是“触发器 + 预算 + 排序 + 位置 + 递归”**。missai_local 目前只实现了 `constant` 和整串子串命中，`secondary_keys / order / probability / depth / regex / group` 等字段基本没用上，且历史被硬截到 24 条，扫描深度也随之被锁死。
3. **记忆的最佳实践是“结构化槽位 + 滚动摘要 + 可选检索”，并且注入到靠近生成端的位置**。SillyTavern 的 summary 以 depth 注入；LingChat 把记忆压成 `short_term / long_term / user_info / promises` 四类；missai_local 是“每 4 轮生成一条扁平摘要，塞进第一个 system”，位置最远、信息最散、没有滚动合并。
4. **角色一致性靠三件套：角色卡 few-shot 示例、persona 锚点、输出约束**。SillyTavern / Agnai / LingChat 都有示例对话或 sample chat；missai_local 已存 `mesExample` 却没有注入，`personality/scenario` 也没进 prompt，只靠 `systemPrompt` 兜底。
5. **missai_local 有三处设计优于多数开源项目，应保留而不是推翻**：固定前缀命中 prefix cache；数值档位（EmotionMapper）把数字翻译成行为指令；流式回填 + 格式修复 + CG 白名单校验。改造重点应放在“组装层”和“记忆层”，而不是重写整条管线。

---

## 二、逐项目分析

### 2.1 SillyTavern（GitHub: `SillyTavern/SillyTavern`）

**定位**：最主流的自托管 LLM 角色扮演前端，Node 服务 + 浏览器 UI，AGPL-3.0。

**LLM 管线**：Prompt Manager / Advanced Formatting 负责把「主提示词、角色描述、personality、scenario、用户 persona、世界书、示例对话、聊天历史、post-history instructions」按可配置顺序拼成一次请求；Chat Completion 与 Text Completion 是两套组装策略，后者还要做 instruct 模板转换。

**关键机制**

- **提示词分层与预算**：每个 prompt 区块可单独启用/禁用/调顺序；`max_context` 减去 `amount_gen` 得到可用上下文；历史从最旧开始丢。Tokenizer 按模型自动选择（GPT-2/OpenAI/Llama/Claude/Llama3/Gemma/Qwen2/Command-R/Nemo/DeepSeek），并有 token 缓存。
- **世界书**：主关键字 + 次要关键字 + `selective`（AND 语义）；`constant` 恒注入；`order` 决定插入顺序；`position` 决定角色定义前/后；`depth` 决定插入到聊天历史的第几条之前；`probability` 概率触发；支持大小写、整词匹配、正则；`scan depth` 控制扫描最近多少条消息；`token budget`（Context % 或绝对 Budget）耗尽即停止；`recursion` 允许条目内容再触发其他条目，直接命中优先于递归命中。
- **作者注记（Author's Note）**：可放在 Scenario 后，或 in-chat 的指定 depth；`frequency` 控制每 N 轮插入一次。文档明确“越靠近底部对下一次回复影响越大”。
- **摘要记忆**：Summarize 扩展支持 raw / classic 两种摘要模式；摘要以 `{{summary}}` 模板在指定 depth 注入；更新周期按消息数或词数；raw 模式会把“上一版摘要 + 新增消息”一起送给模型，形成滚动摘要。
- **一致性 / OOC**：角色卡 `mes_example`、`system_prompt`、`post_history_instructions` 都是标准字段；user persona 单独成层；World Info 可临时充当 character bias。
- **后处理**：宏替换、regex 脚本、swipe/regenerate、stop sequence、logit bias、reasoning 配置（按 Claude/Gemini/OpenAI 分别适配）。

**可迁移设计**

1. 世界书条目做成“可排序、可定位、可预算”的数据结构，而不是一段拼好的字符串。
2. 摘要/作者注记用 **depth + frequency** 注入，而不是固定塞在 system 头部。
3. Token 预算按模型算，且为输出预留空间。
4. Prompt 区块顺序数据化（类似 RisuAI 的 `formatingOrder`），便于调试和 A/B。

**不要照搬**：40+ 个细粒度开关对本地小项目过重；先实现 6 个核心字段即可。

---

### 2.2 RisuAI（GitHub: `kwaroran/RisuAI`）

**定位**：Svelte + Tauri 的跨平台角色扮演客户端，GPL-3.0，主打多 provider、移动端、正则脚本。

**LLM 管线**

- `formatingOrder`：一个显式的数组，决定 system / persona / char description / lorebook / memory / example / chat / post 各段在 prompt 中的顺序。
- `stringlization`：内部统一用 OpenAI Chat 数组，发请求前再按目标模型“字符串化”成 ChatML / Kobold / NovelAI 等格式；不同 provider 有独立请求适配层。
- 输出侧有 regex script、翻译层、prompt diff 弹窗（可看本轮实际发送内容）。

**记忆系统（三代同堂，是很好的“演进样本”）**

- **SupaMemory**（2023）：摘要式。旧消息累积到阈值后让 LLM 总结成 memory 块，注入 `memory` 槽位；问题是摘要丢失细节、可能幻觉。
- **HypaV2**（2024）：向量检索式。把历史做 embedding，按当前输入召回 top-k 相关片段；问题是需要 embedding 端点、成本/延迟、召回不相关。
- **HypaV3**（2025）：LLM 记忆处理 + 结构化/重排；试图同时解决“丢失细节”和“召回不准”。代价是复杂度高（单文件约 2000 行，主路径与实验路径近乎重复）。
- **Hanurai Memory**：另一套向量检索实现。
- 独立分析（Agent Memory Atlas）指出：三代并存是“可读的演进史”，但不是应照抄的工程实践；真正可复用的是“记忆是独立槽位”和“摘要要增量滚动”。

**一致性 / 情绪 / 后处理**

- 示例对话独立成段；persona、jailbreak、post-history 都是可排序槽位。
- 情绪主要靠 prompt 指令 + 前端表现，不是独立分类器；正则脚本承担标签清洗和 UI 定制。
- Provider 适配是显式的：OpenAI Chat / Responses、Claude、NovelAI、Kobold 等。

**可迁移设计**

1. 把 prompt 顺序做成数据（`formatingOrder`），而不是写死在 builder 里。
2. 记忆独立成槽位，且摘要按“上一版摘要 + 新消息”增量更新。
3. 保留 prompt diff / 实际发送快照，便于定位“为什么这一轮 OOC”。

**不要照搬**：三套记忆系统并存、2000 行单文件；本地项目只需一套“结构化摘要 + 可选 FTS 召回”。

---

### 2.3 Agnai（GitHub: `agnaistic/agnai`）

**定位**：多用户、多角色、多 provider 的角色扮演平台，SolidJS + Node，可选 Python pipeline。

**LLM 管线**

- Prompt 由 preset 决定 persona schema：W++ / Square bracket / Boostyle / Plain text；scenario、persona、sample chat 都是独立块。
- Memory Book：每条记忆 = `keywords` + `entry`，可选 `priority` 与 `weight`。
  - `priority` 高者优先进入记忆预算；
  - `weight` 高者排在记忆段更靠后（越靠后越影响生成）；
  - `memory depth` 限制扫描最近多少条消息；
  - `contextLimit` 限制记忆段可用的 token。
- 历史预算按上下文上限裁剪；浏览器端最多先加载 100 条消息，所以 depth 设太大不会生效。
- 可选 `--pipeline` 服务提供长期记忆、Wikipedia/PDF embedding。

**可迁移设计**

1. **priority（是否入选）+ weight（放在哪里）两个正交维度**，比单一“相关度”更可控。
2. 记忆扫描深度与上下文预算分离配置。
3. 多 provider 的 end token / stop 配置集中管理。

**局限**：记忆触发仍是关键词，depth 受前端加载限制；对本地单机项目，这套“多用户 + 外部 pipeline”过重。

---

### 2.4 Character Card V2 / V3（规范生态）

**仓库**：`malfoyslastname/character-card-spec-v2`、`kwaroran/character-card-spec-v3`。

**V2 核心字段**：`name`、`description`、`personality`、`scenario`、`first_mes`、`mes_example`、`creator_notes`、`system_prompt`、`post_history_instructions`、`alternate_greetings`、`character_book`、`tags`、`creator`、`character_version`、`extensions`。

**`character_book` 条目字段**：`keys`、`secondary_keys`、`selective`、`constant`、`insertion_order`、`position`（before_char / after_char）、`depth`、`probability`、`case_sensitive`、`name`、`comment`、`enabled`、`use_regex`。

**V3** 在 V2 基础上扩展 assets、group_only_greetings、创建/修改时间等，仍是同一套“卡即 prompt 契约”的思路。

**对 missai_local 的意义**：本地样例卡 JSON 里已经出现 `key`、`value`、`secondary_keys`、`and`、`order`、`constant`、`useRegex`、`group` 等字段；`db.work_as_character()` 也保留了 `mesExample`、`firstMes`、`alternateGreetings`。也就是说**数据层已经具备角色卡生态的字段，缺的是组装层消费**。

**可迁移设计**：把角色卡当成“标准 prompt schema”，建立一张 `card field → prompt slot` 的映射表，而不是只取 `description`。

---

### 2.5 LingChat（GitHub: `SlimeBoyOwO/LingChat`）

**定位**：中文沉浸式 AI Galgame 聊天 / 桌宠，Rust + Vue 桌面端 + FastAPI 后端，AGPL-3.0。

**LLM 管线**

- `MessageProcessor` → 组装上下文 → `LLMProvider` → `stream_producer` → `response_publisher`。
- Prompt 包含：角色 `ai_prompt`、`ai_prompt_example`（few-shot 示例对话）、结构化记忆、用户信息；脚本引擎还会在特定剧情事件里追加事件指令。
- 情绪：LLM 输出情绪标签，另有本地 BERT 分类器（12 类）对回复做分类，结果驱动立绘表情与 TTS 语气；标签在展示前清洗。

**记忆系统（最值得借鉴的中文实现）**

- 从“向量 RAG”演进为 **Structured Memory Bank**：LLM 把对话总结成固定槽位——`short_term`（近期剧情）、`long_term`（长期事实）、`user_info`（用户画像）、`promises`（约定/待办）。
- 每 50 条新消息更新一次记忆，保留最近 15 条作为重叠窗口，避免摘要断裂。
- 记忆按角色/存档隔离；上下文组装时注入这些槽位，而不是拼接一段自由文本。
- 旧的已经总结过的消息会被摘要替换，控制上下文长度。

**可迁移设计**

1. **固定记忆槽位**比“3 条以内自由摘要”更稳定，尤其适合长期关系推进。
2. **增量更新 + 重叠窗口**，避免每次从头总结。
3. **独立情绪分类器**兜底，不完全信任模型自报情绪（本地项目可退化为标签 + 规则）。
4. `ai_prompt_example` 作为独立 few-shot 槽位。

**局限**：BERT 分类器需要额外依赖；本地标准库项目不能直接搬，但“标签 + 规则 + 可选外部分类”可以分层实现。

---

### 2.6 Open-LLM-VTuber（GitHub: `Open-LLM-VTuber/Open-LLM-VTuber`）

**定位**：语音交互 AI 伴侣 / VTuber，Python + FastAPI + Live2D，支持全离线。

**LLM 管线**

- `prompt_loader` 组装 system prompt：角色 persona + Live2D 表情指令 + 工具定义（MCP）。
- `AgentInterface` 可切换：`basic_memory_agent`（内存态历史）、`mem0_agent`（向量库 + 语义检索 + 自动事实抽取 + 跨会话）、`letta_agent`（MemGPT 式分层记忆）、`hume_ai_agent`。
- 回复中的情绪线索被解析为 Live2D 动作/表情；TTS 与文本流并行，支持打断。

**可迁移设计**

1. **Agent 可插拔**：把“记忆策略”做成接口，默认内存态，按需换外部记忆后端。
2. **system prompt 里显式写“表现层指令”**（表情/动作/语气），与 missai_local 的“状态栏 + 档位表现”同源。
3. 情绪线索从回复中解析并驱动表现层，而不是只存文本。

**局限**：Mem0/Letta 都是外部服务；本地项目应保留“无依赖默认实现 + 可选增强”。

---

### 2.7 Index-1.9B Character（Gitee: `Yang-chl/Index-1.9B`）

**定位**：中文小参数角色扮演模型 + 配套 roleplay 框架，Gitee 上少见的“模型 + 角色语料 + 推理 demo”一体项目。

**核心机制**

- 在 SFT/DPO 基础上引入 **RAG 实现 few-shot 角色扮演定制**：每个角色准备一份对话语料 CSV（如 `roleplay/character/三三.csv`）和角色描述；推理时检索相似对话片段作为 few-shot，而不是把全部语料塞进 prompt。
- 提供 `add_character_openai_demo`，可用 OpenAI 兼容接口复现角色。

**可迁移设计**

1. **示例对话不必是静态的 `mes_example`**：可以按当前用户输入，从角色语料库里检索最相似的 2~4 段对话做 few-shot。
2. 角色语料与角色卡分离，便于扩充风格样本。
3. 对本地项目，可用 SQLite FTS5 / TF-IDF 做轻量检索，不引入向量库。

**局限**：这是模型侧方案，应用侧只提供了 demo；不能直接替代 prompt 工程。

---

### 2.8 Gitee / GitCode 生态补充（核实结果）

| 仓库 | 实际情况 | 对本次调研的价值 |
|---|---|---|
| `gitee.com/zhang-gongming/aichat-roles` | Spring Boot 3 + 通义千问/七牛云，用户/角色/历史 CRUD + 语音 | 工程壳，公开代码未见复杂 prompt 分层；可参考其角色/会话建模 |
| `gitee.com/suppermanljr/ai_virtual_mate_web` | 枫云 AI 虚拟伙伴 Web 版，ASR/TTS/Live2D/长期记忆 | 偏多模态集成，LLM 组装逻辑不突出 |
| `gitcode.com/GitHub_Trending/si/SillyTavern` | SillyTavern 的 GitCode 镜像 | 说明 GitCode 主要是 GitHub 镜像，没有独立的中文角色扮演引擎 |
| `github.com/Soulter/AstrBot` + `astrbot_plugin_livingmemory` | 中文 Agent/陪伴框架；记忆插件支持自动总结、`recall/memorize` 工具、关键词/语义/图谱召回、归档/衰减/访问强化 | 可参考“记忆生命周期”设计；但属于插件生态，非核心 prompt 管线 |

**结论**：Gitee/GitCode 上“能直接研究 LLM 组装逻辑”的项目很少，大多是镜像、CRUD 工程壳或多模态集成；真正有参考价值的中文实现是 LingChat、Open-LLM-VTuber 和 Index-1.9B。

---

## 三、横向对比

| 维度 | SillyTavern | RisuAI | Agnai | LingChat | Open-LLM-VTuber | missai_local 现状 |
|---|---|---|---|---|---|---|
| 分层组装 | Prompt Manager + 可排序区块 | `formatingOrder` 数组 | preset + 固定块 | prompt + few-shot + memory + 事件 | persona + 表现指令 + 工具 | 固定前缀 + 世界书 + 状态 + 历史 + 尾部提醒 |
| Token 预算 | 按模型 tokenizer，`max_context - amount_gen` | 按 provider 估算 | contextLimit + memory budget | 按记忆/历史预算 | 按模型 | 固定 2000，tiktoken/字数兜底 |
| 世界书 | 关键词+次要键+顺序+位置+depth+概率+预算+递归 | Lorebook 关键词+概率+顺序 | Memory Book 关键词+priority+weight | 世界观/设定 + 记忆槽 | 角色设定 + 工具 | constant + 子串命中，无预算/顺序/位置/递归 |
| 长期记忆 | Summary（depth 注入，滚动） | Supa/HypaV2/V3/Hanurai | Memory Book + 可选 embedding pipeline | 结构化四槽 + 50 条增量 | basic / Mem0 / Letta | 每 4 轮扁平摘要，最近 3 条塞首个 system |
| 一致性/OOC | 角色卡全字段 + 示例对话 + AN | persona/example/jailbreak 槽 | persona schema + sample chat | few-shot + 固定人设 + 情绪 | persona + 表现指令 | 仅 systemPrompt/description；`mesExample` 未用 |
| 情绪/状态 | 主要靠 prompt，无内建数值 | prompt + 前端 | prompt | BERT 12 类 + 标签 | 回复解析 → Live2D | **数值档位映射 + 状态面板（较强）** |
| 流式/后处理 | regex、stop、swipe、宏 | regex script、翻译、diff | 基础 | 标签清洗、TTS/立绘 | 分句 TTS、打断 | **hold-back + 选项回填 + 格式修复 + CG 校验（较强）** |
| 模型适配 | 多后端 + 多 tokenizer | `stringlization` + provider 层 | 多服务 + end tokens | 多 LLM provider | Ollama/OpenAI/Claude/Gemini/DeepSeek | 仅 OpenAI 兼容；`/v1` 404 兜底；无 stop/reasoning |

---

## 四、missai_local 现状盘点

### 4.1 现有管线

```
ChatRouter.build_messages
  → PromptBuilder（固定 system 前缀 + 历史裁剪 + 世界书 + 状态面板 + 本轮提醒）
  → _inject_custom（自定义设定 / 记忆摘要 / afterUser）
  → llm.chat_stream（OpenAI 兼容 SSE）
  → sanitizer（say 块归一、选项 hold-back）
  → _ensure_format（缺标签时二次修复）
  → post_processor（状态钳制、选项、CG 校验）
  → 落库 + 前端增量渲染
```

### 4.2 现状与差距

| 项 | 现状（含代码位置） | 差距 / 风险 |
|---|---|---|
| 历史窗口 | [server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2614-L2629) 只取 `rows[-24:]`；[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L119-L136) 再按 2000 token 裁一次 | 双重上限：24 条可能远小于 2000 token，长上下文模型浪费；也可能因单条超长而挤掉全部历史 |
| 世界书匹配 | [server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2826-L2857) 只认 `content` + `keys` + `constant` | 样例卡使用 `key`/`value`/`secondary_keys`/`order` 等字段；若入库未归一，条目不会触发。`order/position/depth/probability/regex/group/递归` 全部未实现，也没有 token 预算 |
| 世界书隔离 | [server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2809-L2812) 按 `character_id` 查询，无 `user_id` 过滤 | 同一角色的其他用户自建条目可能被注入，既影响人设也可能泄露内容 |
| 角色卡字段 | [prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L165-L209) 只读 `systemPrompt/aiIdentityStylePrompt/worldViewPrompt/writingStylePrompt/postPrompt/breakArmorWord`；[db.py](file:///D:/ai_manager_workspace/missai_local/db.py#L3504-L3536) 保留 `mesExample/firstMes/alternateGreetings` | `mesExample`（few-shot）、`personality`、`scenario`、CCv2 的 `system_prompt`/`post_history_instructions` 没有进入 prompt |
| 记忆 | [server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3668-L3726) 每 4 轮总结一次；[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3640-L3656) 取最近 N 条拼成一段塞进首个 system | 扁平文本、无滚动合并、无去重、无槽位、无重要性/时间衰减；注入位置离生成端最远 |
| 自定义/作者注 | [server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3609-L3663) 支持 `beforeMain/afterMain/afterUser` + 关键词 | 没有 depth、frequency、插入到历史第 N 条之前；没有默认 persona anchor |
| 状态/情绪 | [prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L218-L251) 数值→档位行为；[roleplay_core/emotion_mapper.py](file:///D:/ai_manager_workspace/missai_local/roleplay_core/emotion_mapper.py) 区间映射；[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2991-L3006) 仅常规采样参数 | 有“当前值→行为”，但没有状态转移机、情绪历史、变化原因；数值仍靠模型自报 + 事后钳制 |
| 流式/后处理 | [roleplay_core/sanitizer.py](file:///D:/ai_manager_workspace/missai_local/roleplay_core/sanitizer.py) hold-back/归一；[roleplay_core/post_processor.py](file:///D:/ai_manager_workspace/missai_local/roleplay_core/post_processor.py) 状态/选项/CG；[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3069-L3125) 缺标签二次请求 | 只修“格式缺失”；空回复、复读、出戏、抢话、半截标签没有重试/降级；无 stop sequence |
| 模型适配 | [llm.py](file:///D:/ai_manager_workspace/missai_local/llm.py#L135-L174) OpenAI Chat 协议；[llm.py](file:///D:/ai_manager_workspace/missai_local/llm.py#L97-L108) `/v1` 404 兜底 | 无 provider 能力矩阵、无 `stop`、无 `max_completion_tokens`、无 `reasoning_content`，token 计数按 OpenAI 估算 |
| 路径一致性 | [server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3023-L3061) 非流式 `_ai_reply` 不传 `prepare`；[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3730-L3739) 流式才走 `_prepare` | 用户切“非流式”或游客路径时，自定义设定/记忆摘要不会注入，行为与流式不一致 |

### 4.3 应保留的优势

- 固定 system 前缀稳定，利于上游 prefix cache（[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L165-L170)）。
- 世界书/状态面板放在历史之后、本轮消息之前，天然接近“depth 0/1”注入。
- 数值档位映射比单纯“让模型自己编数值”稳定。
- 流式 hold-back + 选项边界回填 + 格式二次修复 + CG 白名单，是很多开源项目没有的工程细节。

---

## 五、改造建议（按优先级）

### P0：正确性与低风险高收益

**P0-1 统一所有对话路径的 prompt 组装**

- 现状：流式路径走 `_prepare`（自定义设定 + 记忆），非流式和游客路径直接 `_llm_messages`，注入缺失。
- 做法：把 `prepare` 变成 `ChatRouter` 的必选阶段；所有入口（stream / sync / guest）都调用同一套 `prepare`，差异只在“是否允许写库”。
- 落点：[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3023-L3061)、[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3730-L3739)、[roleplay_core/chat_router.py](file:///D:/ai_manager_workspace/missai_local/roleplay_core/chat_router.py)
- 验收：同一角色、同一历史，在流式/非流式/游客下打印的 messages 结构一致（仅 system 文案中的身份差异除外）。

**P0-2 历史窗口改为“token 驱动 + 可配置上限”**

- 现状：先硬截 24 条，再按 2000 token 裁，双重限制且不可配。
- 做法：去掉 `rows[-24:]`，改为按 `history_token_budget` 从最新往旧装；数据库查询可用分页/游标，避免一次拉全量。单条超长消息单独截断（如保留头尾），不要整条丢弃。
- 落点：[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2614-L2629)、[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L119-L136)
- 验收：8k/32k 上下文模型下，历史 token 占比可解释；超长单条不会导致历史为空。

**P0-3 世界书字段归一 + 触发语义**

- 现状：只认 `content/keys/constant`，且 `key` 内逗号串按整串子串匹配。
- 做法（分三步）：
  1. 入库归一：`key→keys`、`value→content`、`secondary_keys→secondaryKeys`、`order→insertionOrder`、`position→extPosition`、`depth→extDepth`、`probability→probability`、`and→selectiveLogic`、`group→group`；逗号串拆成多关键字。
  2. 匹配语义：`constant` 恒注入；否则主键命中；`selective` 时要求次要键也命中（AND/NOT 可选）；`useRegex` 用正则；`probability` 用稳定哈希（同一条目同一轮结果可复现）。
  3. 组装语义：按 `insertionOrder` 排序；`extPosition` 决定角色定义前/后或历史 depth；设置 `budget`（建议 1000~2000 token），预算耗尽停止；`recursion` 最多 1~2 步。
- 落点：[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2826-L2857)、[db.py](file:///D:/ai_manager_workspace/missai_local/db.py#L4684-L4729)、[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L276-L281)
- 验收：用样例卡构造“只命中主键 / 主次键都命中 / 概率 0 / 概率 1 / 递归触发”五组用例，输出条目集合可预测。

**P0-4 世界书按用户隔离**

- 现状：`_chat_worldbook` 仅按 `character_id` 查询，用户自建条目会串。
- 做法：系统镜像条目 + `user_id = 当前用户` 的自建条目；或给系统条目一个 `is_system` 标记。
- 落点：[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2809-L2812)
- 验收：两个用户对同一角色各自新增条目，互相不可见。

**P0-5 角色卡字段接入 prompt**

- 现状：`mesExample`、`personality`、`scenario`、`firstMes`、CCv2 的 `system_prompt`/`post_history_instructions` 未消费。
- 做法：
  - `mesExample` 解析成 2~4 组 user/assistant 示例，按 token 截断，放在 system 之后、历史之前；
  - `personality` + `scenario` 合并进“角色核心设定”，不要重复 `description`；
  - 历史为空时用 `firstMes` 作为开场 assistant 消息；
  - CCv2 导入时把 `system_prompt` 映射到主提示词、`post_history_instructions` 映射到尾部指令。
- 落点：[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L165-L209)、[db.py](file:///D:/ai_manager_workspace/missai_local/db.py#L3504-L3536)、角色卡导入链路
- 验收：导入一张含 `mes_example` 的 V2 卡后，prompt 里能看到示例对话，且风格明显更贴近卡面。

---

### P1：质量提升

**P1-1 记忆结构化 + 深度注入 + 滚动摘要**

- 现状：每 4 轮一条扁平摘要，最近 3 条塞首个 system。
- 建议：
  - 槽位化：`short_term`（最近剧情）、`long_term`（稳定事实）、`user_info`（用户画像/偏好）、`promises`（约定/待办）。最少先做“事实 / 关系 / 约定”三类。
  - 滚动：新摘要 = 上一版摘要 + 新增段落，而不是每次只看新段落；保留最近 15 条消息作为重叠窗口（参考 LingChat 的 50 条/15 条）。
  - 注入：放在历史倒数第 2~4 条之前（depth 2~4），或作为独立 system 放在历史之后；不要塞进首个 system。
  - 预算：记忆段 300~600 token，超出按“承诺 > 用户画像 > 长期事实 > 近期剧情”降级。
  - 去重：同一事实多次出现只保留最新一条。
- 落点：[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3668-L3726)、[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3640-L3656)、[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L253-L285)
- 验收：20 轮后，记忆段仍不超过预算，且能回答“上次答应过什么”。

**P1-2 作者注记：depth + frequency + persona anchor**

- 现状：只有 beforeMain/afterMain/afterUser，没有深度和频率。
- 建议：
  - `customSettings` 增加 `depth`（0=最后一条之前，N=倒数第 N 条之前）和 `frequency`（每 N 轮插入一次）。
  - 默认 anchor（可关）：说话风格、视角、句长、禁止替用户行动、禁止 OOC 元叙述；每 4 轮在 depth 2 插入一次。
  - 与现有“世界书/状态面板在历史之后”的顺序协调：状态面板保持 depth 0，anchor 放 depth 2~4，避免同位置堆叠。
- 落点：[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3609-L3663)、[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L253-L285)
- 验收：长对话第 10 轮后，OOC 元叙述和“替用户说话”显著减少；可通过开关回退。

**P1-3 Token 预算模型化**

- 现状：固定 2000；tiktoken 可用时按 OpenAI 编码，不可用时 1 字=1 token；没有输出预留。
- 建议：
  - 每个模型配置 `context_window`，预算 = `context_window - max_tokens - safety(200)`。
  - 分区预算：固定 system ≤30%，示例 ≤10%，世界书 ≤15%，记忆 ≤10%，历史 ≤35%，尾部 ≤5%。
  - 降级顺序：先丢最旧历史 → 再丢低优先级世界书 → 再压缩记忆 → 最后截断示例；固定人设和格式规则永不裁。
  - 计数：中文按 1.5~1.7 字/token 估算（tiktoken 不可用时），不要把“字数”直接当 token。
- 落点：[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L19)、[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L112-L136)、[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2991-L3006)
- 验收：切换 4k/8k/32k 模型时，不出现 400 context overflow；各分区占比可打印。

**P1-4 OOC 抑制与退化重试**

- 现状：只做格式修复；空回复、复读、出戏、抢话没有兜底。
- 建议：
  - 输出检测：空/超短、连续 n-gram 重复、出现“作为 AI/我是一个语言模型”、连续多句替用户行动、缺少 say 块。
  - 重试策略：首次命中 → 降低 temperature、提高 frequency_penalty，追加一条“只以角色身份回复、不要解释”的 system 提醒，重试一次；仍失败则回退到“短回复 + 状态栏”模板。
  - 不要无限重试；每次重试都记录原因，便于统计。
- 落点：[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L3069-L3125)、[roleplay_core/post_processor.py](file:///D:/ai_manager_workspace/missai_local/roleplay_core/post_processor.py)、[roleplay_core/chat_router.py](file:///D:/ai_manager_workspace/missai_local/roleplay_core/chat_router.py)
- 验收：构造 5 类坏输出，均能在 1 次重试内恢复或安全降级。

**P1-5 世界书 sticky / scan depth / 概率 / 分组**

- 建议：条目触发后保持 N 轮（sticky）；`scan depth` 独立于历史预算；同 `group` 条目只取最高优先级一条；概率用稳定哈希；递归最多 2 步并防止环。
- 落点：世界书匹配器 + 会话级“已触发条目”缓存。
- 验收：同一上下文重复生成，条目集合稳定；不会因递归爆炸。

---

### P2：增强与工程化

**P2-1 SQLite FTS5 关键词/BM25 召回（零新依赖）**

- 场景：长期记忆条目、角色语料、世界书条目越来越多，纯子串匹配召回差。
- 做法：用标准库 `sqlite3` 的 FTS5 虚拟表存“记忆条目/对话片段”，按 `bm25()` + 时间衰减 + 重要性排序，取 top-k 注入；中文可用 `unicode61` + 二元切分，必要时加 `jieba` 作为可选依赖。
- 价值：获得 Agnai/Risu 式“相关召回”，但不引入向量库/外部服务。
- 落点：新增记忆表 + 检索器，`_inject_custom` 消费。

**P2-2 状态机 + 情绪历史**

- 现状：数值由模型自报，事后 ±5 钳制；情绪由数值区间映射。
- 建议：
  - 定义属性转移表：`(attr, min_delta, max_delta, cooldown, triggers)`；模型只提议变化，后处理器裁决。
  - 保存最近 3 轮 `{emotion, values, reason}`，注入“上一轮状态 → 本轮变化”以抑制跳变。
  - 状态可驱动 CG/语气/TTS（本地已有 CG，可扩展）。
- 落点：[roleplay_core/post_processor.py](file:///D:/ai_manager_workspace/missai_local/roleplay_core/post_processor.py)、[roleplay_core/emotion_mapper.py](file:///D:/ai_manager_workspace/missai_local/roleplay_core/emotion_mapper.py)、[prompt_builder.py](file:///D:/ai_manager_workspace/missai_local/prompt_builder.py#L218-L251)
- 验收：连续 10 轮数值曲线平滑，不出现“好感 0→100”跳变。

**P2-3 Prompt 观测与调试**

- 建议：每轮记录分区 token 数、实际发送 messages、世界书命中条目及原因、记忆槽位、是否重试；前端提供“查看本轮 prompt / diff 上一轮”。
- 价值：OOC 定位从“猜”变成“看”；也是 SillyTavern/RisuAI 的成熟做法。
- 落点：现有 prompt 日志 + 请求日志表扩展。

**P2-4 模型适配矩阵**

- 建议：为 provider 建能力表：`supports_stop`、`max_tokens_field`、`supports_reasoning`、`supports_stream_options`、`context_window`；请求层按能力组装。
- 兼容：`max_completion_tokens`（新 OpenAI）、`reasoning_content`（DeepSeek/Claude 网关）、`stop`（`</book_status>`、`</options>` 等）、429/5xx 指数退避、SSE 半包解析。
- 落点：[llm.py](file:///D:/ai_manager_workspace/missai_local/llm.py#L135-L174)、[llm.py](file:///D:/ai_manager_workspace/missai_local/llm.py#L177-L260)、[server.py](file:///D:/ai_manager_workspace/missai_local/server.py#L2991-L3006)

**P2-5 示例对话检索（Index-1.9B 思路）**

- 建议：角色卡 `mesExample` 作为默认样例；若角色有语料库，按当前输入用 FTS5/TF-IDF 检索 2~4 段最相似对话做 few-shot，避免静态示例被模型背下来。
- 落点：few-shot 组装器 + 角色语料表。

---

## 六、建议的目标分层结构

```
L0 固定前缀（永不裁剪，保 prefix cache）
   ├─ 角色核心设定（systemPrompt / personality / scenario / description 去重合并）
   ├─ 身份与任务 / 世界观 / 写作风格
   ├─ CG 白名单目录
   ├─ 剧情选项要求 / 破甲词
   └─ 输出格式规范（say / status / options / image）

L1 Few-shot 示例（角色卡 mesExample 或检索样例，2~4 组，token 截断）

L2 历史（token 窗口，从新到旧；超长单条截断不丢弃）

L3 深度注入（放在历史倒数第 N 条之前）
   ├─ 世界书（按 order 排序、position/depth 定位、budget 限制、可递归）
   ├─ 记忆槽位（promises / user_info / long_term / short_term）
   └─ Persona anchor / Author's Note（depth + frequency）

L4 状态面板（depth 0，紧贴本轮输入；数值 + 档位行为 + 规则）

L5 本轮用户消息 + 格式提醒
```

预算示例（8k 上下文，输出预留 1200）：

| 分区 | 上限 | 降级顺序 |
|---|---|---|
| 固定前缀 | 2400 | 不裁 |
| Few-shot | 800 | 最后裁 |
| 历史 | 2800 | 最先丢最旧 |
| 世界书 | 1200 | 丢低 order / 低概率 |
| 记忆 | 800 | 压成 3 条事实 |
| 状态 + 尾部 | 400 | 不裁 |

---

## 七、风险与不建议照搬的部分

1. **不要引入外部向量库/记忆服务作为默认路径**。RisuAI HypaV3、Agnai pipeline、Mem0/Letta 都依赖额外服务或 embedding API；missai_local 是标准库单机后端，默认应走“结构化摘要 + SQLite FTS5”，向量检索作为可选插件。
2. **不要把 SillyTavern 的全部世界书开关一次搬完**。先做 `keys/secondary/constant/order/position/budget` 六个，概率/递归/分组放 P1/P2。
3. **摘要模型不要阻塞主回复**。现有后台线程 + 静默失败是对的，继续保留；但要注意摘要端点与主模型解耦，避免共享限流。
4. **概率触发要可复现**。用 `hash(entry_id + turn_index)` 而不是 `random()`，否则同一轮重试会得到不同世界书，难以定位问题。
5. **不要让记忆摘要变成“第二套人设”**。摘要只存事实/关系/约定，不写风格指令；风格归 L0 和 anchor。

---

## 八、核实清单与来源

### 仓库核实

| 项目 | 仓库 | 核实方式 |
|---|---|---|
| SillyTavern | https://github.com/SillyTavern/SillyTavern | 官方文档安装页给出 clone 地址；Prompt Manager/World Info/Author's Note/Summarize 官方文档可访问 |
| RisuAI | https://github.com/kwaroran/RisuAI | README、DeepWiki 代码索引（`formatingOrder`/`stringlization`/记忆模块）、Agent Memory Atlas |
| Agnai | https://github.com/agnaistic/agnai | 官方 README 与 Memory 指南 |
| Character Card V2 | https://github.com/malfoyslastname/character-card-spec-v2 | 官方 README/spec |
| Character Card V3 | https://github.com/kwaroran/character-card-spec-v3 | 官方 README |
| LingChat | https://github.com/SlimeBoyOwO/LingChat | 官方 README、DeepWiki（消息管线/记忆/情绪） |
| Open-LLM-VTuber | https://github.com/Open-LLM-VTuber/Open-LLM-VTuber | 官方文档、DeepWiki（Agent/Mem0/Letta、prompt_loader） |
| Index-1.9B | https://gitee.com/Yang-chl/Index-1.9B | Gitee 仓库页与 roleplay 目录 |
| AstrBot / LivingMemory | https://github.com/Soulter/AstrBot 、https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory | 官方站点/插件文档 |
| GitCode SillyTavern 镜像 | https://gitcode.com/GitHub_Trending/si/SillyTavern | 公开镜像页 |

### 参考链接

- SillyTavern World Info：https://docs.sillytavern.app/usage/core-concepts/worldinfo/
- SillyTavern Author's Note：https://docs.sillytavern.app/usage/core-concepts/authors-note/
- SillyTavern Summarize：https://docs.sillytavern.app/extensions/summarize/
- SillyTavern Prompt Manager：https://docs.sillytavern.app/usage/prompts/prompt-manager/
- SillyTavern Token 管理：https://deepwiki.com/SillyTavern/SillyTavern/3.4-tokenization-and-token-management
- RisuAI 记忆系统：https://deepwiki.com/kwaroran/RisuAI/5.1-memory-systems
- RisuAI Prompt Engineering：https://deepwiki.com/kwaroran/Risuai/4.3.3-prompt-engineering
- RisuAI 记忆演进分析：https://neoneye.github.io/agent-memory-atlas/systems/risuai/
- Agnai Memory 指南：https://edge-api.agnai.chat/guides/memory
- LingChat 记忆系统：https://deepwiki.com/SlimeBoyOwO/LingChat/2.5-rag-and-memory-system
- LingChat 情绪分类：https://deepwiki.com/SlimeBoyOwO/LingChat/2.7-emotion-classification-service
- Open-LLM-VTuber Agent 配置：https://deepwiki.com/Open-LLM-VTuber/Open-LLM-VTuber/4.3-agent-and-llm-configuration
- Character Card V2 字段说明：https://blog.mini-tavern.com/blog/sillytavern-character-card-meanings-explained-what-every-field-tag-and-symbol-ac-40454f

---

> 本报告只做调研与分析，未对 `missai_local` 代码做任何修改。
