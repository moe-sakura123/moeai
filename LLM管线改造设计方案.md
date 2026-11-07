# LLM 管线提示词与记忆改造设计方案

> 输入：`LLM后端诊断报告.md`（本项目现状与问题）+ `AI角色扮演项目_LLM管线调研报告.md`（SillyTavern / RisuAI / Agnai / CCv2 / LingChat 等实践）。
> 状态：**设计稿，未动代码**。本方案覆盖阶段 2 及之后的提示词/记忆重构；阶段 0/1 的止血项作为地基一并纳入。
> 原则：保留现有三项优势——固定前缀 prefix cache、数值档位映射、流式 hold-back + 选项回填 + 格式修复 + CG 白名单。

---

## 0. 需要先拍板的 7 个取舍

| # | 取舍 | 建议 | 备选/影响 |
|---|---|---|---|
| D1 | `context_window` 从哪来 | 模型配置里新增 `contextWindow`，默认 8192；未配置时按 8192 安全水位 | 若做 provider 能力表自动识别，需要维护模型清单，首期不做 |
| D2 | 记忆槽位是否直接替换旧 `chat_memories` | 新建 `chat_memory_slots` + `chat_summary_state`，旧表只读兼容 | 直接改旧表会丢历史摘要，且无法滚动合并 |
| D3 | `firstMes` 是否自动开场 | 新会话历史为空时，把 `firstMes` 作为 assistant 开场写入会话（只写一次） | 前端 `chat.js:1101-1108` 只在 0 消息时渲染，不落库；自动写入不会重复，还能在首轮后保留开场上下文 |
| D4 | 世界书 `probability` 默认值 | 未填写按 100（恒触发）；显式 0 才跳过 | 若默认 0，会让大量旧卡条目失效 |
| D5 | `group` 语义 | 同组只取 `insertionOrder` 最高的一条；不做权重抽签 | 权重抽签不可复现，难定位 OOC |
| D6 | 递归触发 | 2A 只做 1 步；2C 放开到 2 步并带环检测 | 一步已能覆盖 80% 场景，两步会放大预算压力 |
| D7 | v2 提示词引擎上线方式 | 配置开关 `promptEngine: v1/v2`，按用户+角色灰度；回归通过后默认 v2 | 直接替换回滚成本高 |

---

## 1. 目标与不变量

### 1.1 目标分层（与调研报告第六节对齐）

```
L0 固定前缀（stable，永不裁剪，保 prefix cache）
   ├─ 角色核心设定：systemPrompt / personality / scenario / description（去重合并）
   ├─ 身份与任务：aiIdentityStylePrompt
   ├─ 世界观：worldViewPrompt
   ├─ 写作风格：writingStylePrompt
   ├─ CG 白名单目录
   ├─ 破甲词 breakArmorWord
   └─ 输出格式规范（say / book_status / next_prompt / image）

L1 Few-shot：mesExample 解析出的 2~4 组 user/assistant（可截断，位于 system 之后、历史之前）

L2 历史：token 窗口，从新到旧装载；超长单条“头尾截断”而不是整条丢弃

L3 深度注入（插在历史倒数第 N 条之前）
   ├─ 世界书（order + position/depth + budget + 递归）
   ├─ 记忆槽位（promises / user_info / long_term / short_term）
   └─ Persona anchor / Author's Note（depth + frequency）

L4 状态面板（depth 0，紧贴本轮输入；数值 + 档位行为 + 规则摘要）

L5 本轮用户消息 + 格式提醒（FORMAT_REMINDER）
```

### 1.2 必须保持的不变量

1. **固定前缀稳定**：L0 与 L1 在本会话内不随轮次变化；动态内容（记忆/世界书/状态/作者注）一律不进 `messages[0]`。
2. **数值档位映射不变**：`EmotionMapper` 的区间→情绪/行为仍然生效；状态面板仍包含“当前值 + 当前档位表现”。
3. **流式 hold-back 不变**：`<book_status>`、`<next_prompt>`、选项行仍然不在正文中提前下发。
4. **选项回填不变**：`wrap_next_prompts` 仍负责把缺失/错位的选项块补到末尾。
5. **格式修复不变**：`_ensure_format` 仍在缺块时补一次，但补全请求必须带完整 schema。
6. **CG 白名单不变**：仍只允许角色 `cgList` 内、且未被 `triggerOnce` 消费的图片；非法 ID 丢弃。
7. **流式已发文本不整段重画**：后续阶段用增量解析器替代 `clear_content`；本方案不改变对外 SSE 事件语义。

---

## 2. 与现有诊断问题的对应关系

| 本方案模块 | 解决的诊断问题 |
|---|---|
| 统一 `PromptPlan` / 单一准备路径 | P0-5、P0-10、P1-7、P2-1~P2-5 |
| 分区预算 + 模型上下文 | P0-5、P0-6、P1-4、P1-5 |
| 世界书触发器 + 用户隔离 | P0-1、P1-14、世界书字段缺失 |
| 记忆槽位 + 会话游标 + 深度注入 | P0-2、P0-3、P1-9、P1-10、P1-8 |
| 状态持久化 + 数值规则执行 | P0-4、P0-8、P0-12、P1-11 |
| 角色卡字段消费 | 人设跑偏、`mesExample` 未用 |
| provider profile + finish_reason | P1-1~P1-5、P3 兼容 |
| prompt 分段 diff | P1-13、P2-6、OOC 定位 |

> 阶段 1 先落地的 6 项止血（世界书用户隔离、记忆会话化、状态落库、真实取消、max_tokens/finish_reason、孤立消息清理、路径对齐）是本方案的“数据正确性前提”。阶段 2 只在其上做组装层重构，不重复实现。

---

## 3. 数据模型改动

### 3.1 世界书：从 JSON 字符串升级为“触发器条目”

现状：`worldbook_entries` 只有 `payload` 与 `position`（`db.py:454-465`），匹配时只读 `content/keys/constant`（`server.py:2826-2857`），样例卡的 `key/value/secondary_keys/order/and/group` 没有归一。

**方案**：保留 `payload` 作为原始数据，新增可查询/可排序列（写入时同时回填，读取时兼容旧行）：

```sql
ALTER TABLE worldbook_entries ADD COLUMN keys TEXT DEFAULT '';            -- JSON 数组
ALTER TABLE worldbook_entries ADD COLUMN secondary_keys TEXT DEFAULT '';  -- JSON 数组
ALTER TABLE worldbook_entries ADD COLUMN constant INTEGER DEFAULT 0;
ALTER TABLE worldbook_entries ADD COLUMN selective INTEGER DEFAULT 0;
ALTER TABLE worldbook_entries ADD COLUMN selective_logic INTEGER DEFAULT 0; -- 0 AND,1 NOT,2 OR
ALTER TABLE worldbook_entries ADD COLUMN use_regex INTEGER DEFAULT 0;
ALTER TABLE worldbook_entries ADD COLUMN insertion_order INTEGER DEFAULT 0;
ALTER TABLE worldbook_entries ADD COLUMN ext_position TEXT DEFAULT 'after_char'; -- before_char/after_char/at_depth
ALTER TABLE worldbook_entries ADD COLUMN ext_depth INTEGER DEFAULT 0;
ALTER TABLE worldbook_entries ADD COLUMN probability REAL DEFAULT 100;
ALTER TABLE worldbook_entries ADD COLUMN group_id TEXT DEFAULT '';
ALTER TABLE worldbook_entries ADD COLUMN sticky INTEGER DEFAULT 0;
ALTER TABLE worldbook_entries ADD COLUMN cooldown INTEGER DEFAULT 0;
ALTER TABLE worldbook_entries ADD COLUMN recursive INTEGER DEFAULT 0;
ALTER TABLE worldbook_entries ADD COLUMN enabled INTEGER DEFAULT 1;
ALTER TABLE worldbook_entries ADD COLUMN token_budget INTEGER DEFAULT 0;  -- 0=跟随分区预算
```

**入库归一**（`save_worldbook_entry` 内做一次）：

| 卡字段 | 归一字段 |
|---|---|
| `key` / `keywords` (逗号串) | `keys[]`，按逗号/中文逗号拆分、去空 |
| `value` | `content`（保留在 payload） |
| `secondary_keys` | `secondaryKeys[]` |
| `order` | `insertionOrder` |
| `position` | `extPosition`（数字 0/1 → before/after；`at_depth` 保留） |
| `depth` | `extDepth` |
| `probability` | `probability`（0~100，默认 100） |
| `and` | `selectiveLogic` |
| `group` | `groupId` |
| `useRegex` | `useRegex` |
| `constant` | `constant` |

**向后兼容**：旧行没有新列时，读取走 `payload` 的旧字段（`key/value/order/position`）；启动时一次幂等回填，失败只记日志不影响启动。

### 3.2 记忆：新表 + 滚动摘要状态

现状：`chat_memories` 是扁平文本、按角色存储、每 4 条 assistant 生成一条（`db.py:225-235`、`server.py:3668-3727`）。不满足“会话隔离 + 固定槽位 + 滚动 + 覆盖淘汰区间”。

**新表**：

```sql
CREATE TABLE IF NOT EXISTS chat_memory_slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER,
    character_id TEXT,
    session_id   TEXT NOT NULL,
    slot         TEXT NOT NULL,      -- short_term | long_term | user_info | promises
    content      TEXT NOT NULL,
    importance   INTEGER DEFAULT 1,  -- 1~5
    source_from  INTEGER DEFAULT 0,  -- 来源消息 id 起
    source_to    INTEGER DEFAULT 0,  -- 来源消息 id 止
    fact_hash    TEXT DEFAULT '',    -- 归一化事实去重
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_slot_hash
    ON chat_memory_slots(user_id, character_id, session_id, slot, fact_hash);

CREATE TABLE IF NOT EXISTS chat_summary_state (
    session_id   TEXT PRIMARY KEY,
    user_id      INTEGER,
    character_id TEXT,
    covered_until INTEGER NOT NULL DEFAULT 0, -- 已覆盖到的消息 id
    rolling_summary TEXT NOT NULL DEFAULT '',  -- 上一版总摘要（滚动输入）
    turns_since_update INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
```

**滚动策略**：

- 触发：`turns_since_update >= 4` 或“待淘汰消息数 >= 8”时触发；
- 输入：`rolling_summary` + 从 `covered_until` 到“当前窗口起点之前”的全部消息（**只总结被窗口淘汰的区间**），重叠窗口保留最近 15 条仅作为上下文，不重复计入 `covered_until`；
- 输出：一次结构化 JSON，四槽分别写入，而不是一段自由文本：
```json
{
  "short_term": "最近 2~4 轮的剧情推进（≤300 token）",
  "long_term": "稳定事实/关系变化/世界观确认（≤300 token）",
  "user_info": "用户偏好/身份/禁忌（≤150 token）",
  "promises": "约定、待办、承诺（≤150 token）"
}
```
- 失败降级：JSON 解析失败 → 整段写入 `short_term`；模型失败 → 不推进 `covered_until`，下轮重试；
- 去重：`fact_hash = sha1(slot + 归一化事实)`，命中则更新 `updated_at` 与 `content`，不新增；
- 删除会话：显式删除 `chat_memory_slots` + `chat_summary_state`；删除角色历史：按 `user_id + character_id` 全删。

**旧表兼容**：`chat_memories` 保留只读。启动迁移时按 `session_id` 归到 `short_term`，`covered_until` 用该会话最后一条 assistant 消息 id 回填；迁移失败则继续只读旧表（v1 行为），v2 忽略旧表。

### 3.3 提示词配置与观测

新增配置（`config_cache`，沿用现有 `get_config/set_config`）：

```
promptConfig:{uid}:{cid} = {
  "engine": "v2",
  "contextWindow": 8192,
  "maxTokens": 8192,
  "sectionBudget": { "persona": 0.30, "fewshot": 0.10, "worldbook": 0.15,
                     "memory": 0.10, "history": 0.30, "tail": 0.05 },
  "historyMinMessages": 8,
  "fewshotMaxTokens": 800,
  "worldbookMaxTokens": 1200,
  "memoryMaxTokens": 800,
  "anchor": { "enabled": true, "depth": 2, "frequency": 4, "text": "..." }
}
```

`chat_prompt_log` 扩展：

```sql
ALTER TABLE chat_prompt_log ADD COLUMN turn_id TEXT DEFAULT '';
ALTER TABLE chat_prompt_log ADD COLUMN finish_reason TEXT DEFAULT '';
ALTER TABLE chat_prompt_log ADD COLUMN sections_json TEXT DEFAULT '{}';
ALTER TABLE chat_prompt_log ADD COLUMN worldbook_json TEXT DEFAULT '[]';
ALTER TABLE chat_prompt_log ADD COLUMN memory_json TEXT DEFAULT '{}';
ALTER TABLE chat_prompt_log ADD COLUMN prompt_hash TEXT DEFAULT '';
```

`sections_json` 每段记录：`id / layer / position / depth / tokens / hash / trimmed`；`prompt_hash` 覆盖 L0+L1，用于验证 prefix cache 稳定性；`worldbook_json` 记录命中条目 id、触发原因、被预算裁掉的条目；`memory_json` 记录四槽 token 与注入 depth。

---

## 4. 预算模型与优先级

### 4.1 总预算

```
available = contextWindow - maxTokens - safety(200)
```

- `contextWindow` 未配置时默认 8192；`maxTokens` 取用户配置，未配置默认 8192（与现有 UI 默认一致）。
- 当 `available < 2000`（小模型/大输出）时，按“固定前缀 → 状态 → 当前用户”优先，历史至少保留 1 条，并在 `sections_json` 标记 `overflow=true`。
- token 计数：优先 `tiktoken cl100k_base`；不可用时中文按 **1.6 字/token**，英文按 4 字符/token，替代当前“1 字=1 token”的 1:1 估算（`prompt_builder.py:112-117`）。

### 4.2 分区与降级

| 分区 | 默认上限 | 最小保留 | 降级顺序 |
|---|---|---|---|
| L0 固定前缀 | 30% | 不裁 | 永不裁 |
| L1 Few-shot | 10% 或 800 token | 2 组中保留 1 组 | 4→3→2 组 |
| L2 历史 | 30% 或剩余额度 | 至少 1 轮（2 条） | 从最旧开始整条丢；单条超长先头尾截断 |
| L3 世界书 | 15% 或 1200 token | constant 条目 | 先丢低 order、再丢低 probability |
| L3 记忆 | 10% 或 800 token | promises 至少 1 条 | promises > user_info > long_term > short_term |
| L4 状态 + L5 尾部 | 5% 或 400 token | 不裁 | 规则表超限时只留当前档位 |

**实现要求**：预算分配按 section 粒度执行，不允许在消息中间截断；历史消息“整条保留或整条丢弃”，超长单条单独做头尾截断（保留开头 60% + 结尾 40%）。

### 4.3 去掉双重上限

- 删除 `server.py:2621` 的 `rows[-24:]`；
- 删除固定 `HISTORY_TOKEN_BUDGET=2000`（`prompt_builder.py:19`），改为 `promptConfig.sectionBudget.history × available`；
- 数据库读取改为按 cursor 分段加载（先取最近 40 条 ID，再按预算需要继续向前取），避免长会话全表读取；
- `scanDepth`（世界书扫描最近多少条）独立配置，默认 4 条，不受历史预算影响。

---

## 5. 世界书：触发器、排序、位置、预算、递归

### 5.1 匹配语义

1. `enabled=0` 直接跳过；
2. `constant=1` → 恒命中；
3. 否则扫描最近 `scanDepth` 条消息（默认 4），主键 `keys` 命中任一即可（`useRegex` 时按正则）；
4. `selective=1` 时要求 `secondaryKeys` 至少一个命中；`selectiveLogic`：
   - `0 AND`：主键 + 次键都命中；
   - `1 NOT`：主键命中且次键不命中；
   - `2 OR`：主键或次键任一命中。
5. `probability`：用 `sha1(entry_id + turn_no + last_user_message_id)` 取模 100，`< probability` 命中；**同一轮重试结果稳定**，不使用 `random()`。
6. `sticky`：命中后保持 N 轮，会话级 `sticky_until_turn` 记录；`cooldown`：命中后 N 轮内不再触发。
7. `group`：同组只保留 `insertionOrder` 最高的一条；不做权重抽签（D5）。
8. `recursive`：被注入内容再次触发其他条目；2A 最多 1 步，2C 最多 2 步；用 `visited` 集合防环。

### 5.2 组装与位置

- 按 `insertion_order` 升序处理；`constant` 条目优先占预算。
- `extPosition`：
  - `before_char`：L0 之前（角色设定前）；
  - `after_char`：L1 之后、L2 之前（默认）；
  - `at_depth`：历史倒数第 `extDepth` 条之前（0 = 当前用户消息前，1 = 最后一条历史前）。
- `token_budget`：条目自身上限；分区总上限 `worldbookMaxTokens`；超限时停止注入并记录 `dropped`。
- 输出结构：不再拼成一整段字符串，而是 `WorldbookHit {entry_id, keys, order, position, depth, tokens, reason}` 列表；由渲染器按位置插入。

### 5.3 用户隔离

`_chat_worldbook(cid, uid)` 查询：

```sql
WHERE character_id=? AND (user_id=0 OR user_id=?)
```

- `user_id=0` 为系统镜像条目（只读）；
- 当前用户自建条目可见；
- 其他用户条目永不可见。
- `_status_rules` 同样按此口径读取，避免规则表跨用户泄漏。

---

## 6. 记忆：固定槽位 + 滚动摘要 + 深度注入

### 6.1 槽位定义

| 槽位 | 内容 | 上限 | 优先级 | 注入位置 |
|---|---|---|---|---|
| `promises` | 约定、待办、承诺 | 150 token | 1 | depth 2 |
| `user_info` | 用户身份/偏好/禁忌 | 150 token | 2 | depth 3 |
| `long_term` | 稳定事实、关系变化 | 300 token | 3 | depth 3 |
| `short_term` | 最近 2~4 轮剧情 | 300 token | 4 | depth 4 |

总预算 800 token；超出按优先级反向裁剪（先砍 `short_term`，最后砍 `promises`）。

### 6.2 更新与覆盖

- `covered_until` 存在 `chat_summary_state`（按 `session_id` 唯一），只增不减；
- 每次触发只处理 `covered_until` 之后、且已被历史窗口淘汰的连续消息；
- **滚动**：输入旧 `rolling_summary` + 新淘汰段 + 15 条重叠上下文；输出新总摘要与四槽；
- 同一会话摘要任务串行：`chat_summary_state` 增加 `summarizing INTEGER`，用 `UPDATE ... WHERE summarizing=0` 抢占；抢不到直接跳过；
- 后台线程保留，不阻塞主回复；摘要端点独立配置（可复用主模型，也可指定轻量模型）。

### 6.3 注入

- 不再拼进 `messages[0]`；
- 按 depth 插入到历史中：`promises` depth 2、`user_info`/`long_term` depth 3、`short_term` depth 4；
- 若 provider 不支持历史中插入 system，则渲染为 `user` 消息并以 `【记忆】` 前缀包裹（profile 控制）；
- 状态面板仍在 depth 0；作者注/PERSONA anchor 在 depth 2，与记忆同位置时按 `promises → anchor → long_term → short_term` 排列。

---

## 7. 角色卡字段消费

| 字段 | 现状 | 目标 |
|---|---|---|
| `systemPrompt` | 已用 | L0 主提示词（作者定义优先） |
| `description` | 仅 `work_as_character` 透传 | `systemPrompt` 缺失或过短时使用；与 `personality/scenario` 去重 |
| `personality` | 未用 | L0「性格」小节 |
| `scenario` | 未用 | L0「场景」小节 |
| `worldViewPrompt` | 已用 | L0 世界观 |
| `aiIdentityStylePrompt` | 已用 | L0 身份与任务 |
| `writingStylePrompt` | 已用 | L0 写作风格 |
| `postPrompt` | 已用 | L1 之后、历史之前（或 L4 尾部，按 profile） |
| `post_history_instructions` (CCv2) | 未用 | 导入时映射到 `postPrompt`（尾部指令） |
| `system_prompt` (CCv2) | 未用 | 导入时映射到 `systemPrompt` |
| `breakArmorWord` | 已用 | L0 末尾（保持现有位置） |
| `mesExample` | 未用 | L1 few-shot：解析 2~4 组 user/assistant，按 800 token 截断 |
| `firstMes` | 未用 | 新会话历史为空时，作为开场 assistant 写入（D3） |
| `alternateGreetings` | 未用 | 会话创建时随机/可选一条，与 `firstMes` 二选一 |

**mesExample 解析规则**：

- 支持 `{{user}}:` / `{{char}}:` / `User:` / `Character:` 前缀；
- 支持 `<START>` 分隔；
- 连续多行合并为同一条消息；
- 超过 4 组时保留最近 4 组；超过预算时从最早的一组开始丢；
- 解析失败不阻塞，回退为纯文本放入 L1（最多 400 token）。

---

## 8. 统一准备路径与渲染

### 8.1 单一准备阶段

当前：流式走 `_prepare`，非流式/访客走 `_llm_messages`，导致自定义设定与记忆缺失（诊断 P0-10）。

目标：

```text
ChatTurnRunner.prepare(ctx, mode)
  ├─ context.loader   → ContextSnapshot
  ├─ prompt.planner   → PromptPlan（section 列表 + 预算）
  ├─ worldbook.assemble → 命中条目 + 位置
  ├─ memory.assemble   → 四槽 + 位置
  ├─ custom.assemble   → 作者注/破甲/afterUser（depth/frequency）
  └─ renderer.render   → messages（按 provider profile）
```

- 所有入口（stream / sync / guest）调用同一个 `prepare`；
- 差异只在 `writeAllows`：guest 不写记忆、不更新状态、不扣费；
- 非流式与流式共用 `post_process`、`_ensure_format`、状态更新、记忆触发；
- `ChatRouter` 的 `prepare` 从“可选 `None`”改为必选阶段（保留测试注入点，但默认实现必须存在）。

### 8.2 渲染与 provider 格式数据化

新增 `prompt_profiles.json`（或 `config_cache` 内置默认）：

```json
{
  "openai_compat": {
    "system_role": "system",
    "supports_mid_system": true,
    "merge_system": "all",
    "max_tokens_field": "max_tokens",
    "supports_stream_options": false,
    "tag_style": "xml"
  },
  "deepseek": { "system_role": "system", "supports_mid_system": true, "merge_system": "all" },
  "anthropic": { "system_role": "user", "supports_mid_system": false, "merge_system": "first" },
  "gemini": { "system_role": "user", "supports_mid_system": false, "merge_system": "first" }
}
```

渲染器负责：

- `merge_system`：`all` 保留多个 system；`first` 把非首个 system 降级为带前缀的 user；
- `supports_mid_system=false` 时，depth 注入转为 user 消息；
- `max_tokens_field` 选择 `max_tokens` / `max_completion_tokens`；
- `tag_style` 目前只支持 `xml`，为后续 Claude 风格预留。

### 8.3 分段 diff

- 每个 section 计算 `sha1(layer + position + depth + content)`；
- 与上一轮 `chat_prompt_log.sections_json` 对比，输出：
  - 新增/删除/修改的 section；
  - 每段 token 变化；
  - 世界书命中变化；
  - 记忆槽位变化。
- 前端“查看本轮 prompt”只读该 JSON，不重新拼装；
- 日志保留最近 50 轮/会话，超出按更新时间清理（避免 `chat_prompt_log` 无限膨胀）。

---

## 9. 世界书 group / probability 的明确取舍

| 能力 | 2A | 2B | 说明 |
|---|---|---|---|
| `constant` | ✅ | ✅ | 恒注入，但受预算约束 |
| `keys` / 逗号拆分 | ✅ | ✅ | 主键 OR |
| `secondaryKeys` + `selective/and` | ✅ | ✅ | AND/NOT/OR |
| `order` / `position` / `depth` | ✅ | ✅ | 位置化注入 |
| `token_budget` | ✅ | ✅ | 条目 + 分区双限 |
| `useRegex` | ✅ | ✅ | 编译失败按字面量 |
| `sticky` / `cooldown` | ❌ | ✅ | 需要会话级触发缓存 |
| `probability` | ❌ | ✅ | 稳定哈希，默认 100 |
| `group` | ✅（最高 order 一条） | ✅（同组单条） | 不做权重抽签 |
| `recursive` | ✅（1 步） | ✅（2 步 + 环检测） | 递归结果也受预算 |
| `scanDepth` 独立配置 | ✅ | ✅ | 不再等于历史窗口 |

**为什么 probability 放 2B**：它需要“轮次/消息 id”稳定输入与命中日志，否则会与 `_ensure_format` 重试、流式重试产生不可复现的差异；2A 先用“恒定命中 + 预算”把可预测性建立起来。

**为什么 group 不做抽签**：抽签会让同一上下文在重试时得到不同设定，直接破坏 OOC 定位与回归样本；同组只保留最高 `insertionOrder`，需要多角色并行时用不同 group + 不同位置表达。

---

## 10. 迁移与向后兼容

### 10.1 数据库迁移（幂等，`meta.schema_version` 驱动）

1. `worldbook_entries` 加列（见 3.1），按 `payload` 回填；旧行 `enabled=1`、`probability=100`、`extPosition=after_char`。
2. 新建 `chat_memory_slots` / `chat_summary_state`；旧 `chat_memories` 按会话迁到 `short_term`，保留原表只读。
3. `chat_prompt_log` 加列（见 3.3）；旧记录 `turn_id=''`。
4. `chat_threads.stats_json` 保持兼容；新增 `state_version` 可选。
5. 所有迁移失败只记录日志，不阻塞启动；读取层对缺失列/缺失表做降级。

### 10.2 行为兼容

- `promptEngine=v1` 时走现有 `PromptBuilder` 与原 `_inject_custom`；`v2` 走新 `PromptPlan`。同一请求可通过配置切换，便于灰度与回滚。
- 旧 `customSettings` 字段映射：
  - `beforeMain` → L0 之前（depth=-1，`insertionOrder=-100`）；
  - `afterMain` → L1 之后（depth=0，`order=0`）；
  - `afterUser` → L5 之前（depth=1）；
  - 未配置 `depth/frequency` 时默认 `depth=1`、`frequency=1`，行为与现状一致。
- `summaryReserveRounds` 继续作为“记忆注入条数/预算档位”的 UI 映射；v2 下映射到 `memoryMaxTokens`，不再直接取最近 N 条。
- `summaryInjectMode: inline` 保留，v2 实际按 depth 注入；前端字段不变。
- 模型参数：`modelConfig` 新增 `contextWindow`，旧配置缺省 8192；`maxTokens` 继续沿用 8192 默认。

### 10.3 回滚

- 数据迁移只增列/增表，不删旧表；
- `promptEngine` 回 v1 后，旧 `chat_memories` 仍可读；
- 新表写入失败不影响主回复（降级为 v1 注入或跳过记忆）；
- 世界书新列读取失败时回退 `payload` 旧字段。

---

## 11. 代码结构与接口（设计级）

```
roleplay_core/
  context_snapshot.py     # ContextSnapshot：角色卡、历史、状态、世界书输入
  prompt_section.py      # PromptSection / PromptPlan / SectionBudget
  prompt_planner.py      # 预算分配、优先级、降级
  prompt_renderer.py     # provider profile → messages（含 depth 插入）
  worldbook_engine.py    # 归一、触发、排序、预算、递归、sticky
  memory_engine.py       # 槽位、滚动摘要、去重、深度注入
  output_pipeline.py     # 增量解析 + 修复 + 后处理（3 阶段共用）
  chat_turn.py           # 唯一编排：prepare → generate → finalize
server.py                # 只保留 HTTP/SSE/计费/存储适配
```

核心结构：

```python
@dataclass
class PromptSection:
    id: str                 # persona.identity / worldbook.entry:xxx / memory.promises
    layer: str              # L0..L5
    priority: int           # 越小越先保留
    position: str           # before_char | after_char | at_depth
    depth: int              # at_depth 时的倒数位置
    stable: bool            # 是否进 prefix cache
    text: str
    tokens: int
    trim: str               # keep | drop_oldest | head_tail | compress

@dataclass
class PromptPlan:
    sections: list[PromptSection]
    budget: dict            # 各分区上限
    total_tokens: int
    overflow: bool
    diagnostics: list[str]
```

`ChatTurnRunner.run(turn_input, mode)` 的 `mode` 只控制传输与写库，不控制注入内容；guest 通过 `write_allows=False` 关闭记忆/状态/计费。

---

## 12. 验收与回归样本

### 12.1 固定回归样本（阶段 0 建立，阶段 2 扩展）

| 样本 | 输入 | 断言 |
|---|---|---|
| P-01 空历史 | 无消息，只有角色卡 | L0 完整；L1 有 mesExample；L4 在尾部；总 token ≤ 30% 上下文 |
| P-02 普通 6 轮 | 标准 user/assistant 交替 | L2 按 token 从新到旧；L0/L1 hash 跨轮不变 |
| P-03 超长单条 | 2000 字 user 消息 | 单条头尾截断，历史不为空，标记 `head_tail` |
| P-04 世界书主键 | 命中 `key` | 只注入命中条目，`order` 升序 |
| P-05 主次键 AND | 只命中主键 | `selective` 未满足，不注入 |
| P-06 概率 0/100 | `probability=0/100` | 0 不注入，100 恒注入，重试结果一致 |
| P-07 递归 | A 命中后触发 B | 只递归 1 步，无环，总预算不超 |
| P-08 记忆四槽 | 20 轮对话 | promises 能回答“上次答应什么”；总记忆 ≤ 800 token |
| P-09 淘汰覆盖 | 30 轮 | `covered_until` 无空洞；被淘汰区间全部进入摘要 |
| P-10 会话隔离 | 会话 A 产生记忆后新建 B | B 的 prompt 不含 A 记忆 |
| P-11 prefix cache | 连续 5 轮同一角色 | L0+L1 hash 不变 |
| P-12 路径一致 | 同一历史 stream/sync/guest | messages 结构一致（guest 仅写库差异） |
| P-13 流式保真 | 带图、状态、选项 | 不触发整段重画；hold-back 正常 |
| P-14 异常输出 | 缺状态、双状态、半截标签、空回复 | 1 次修复或安全降级，均有 diagnostics |

### 12.2 指标

- 上下文超限率（400/context overflow）→ 0；
- 长对话（30 轮）记忆召回率（人工抽检 10 条关键事实）≥ 8/10；
- 跨会话记忆污染 = 0；
- 世界书命中集合可复现率 = 100%（同输入同 turn）；
- prefix cache 稳定段命中率（按 L0+L1 hash）≥ 95%；
- 带图回复 `clear_content` 次数 = 0；
- 同一历史三路径 messages diff = 0（结构级）。

---

## 13. 分阶段实施

### 阶段 0（进行中，未落代码）
- turn_id、section token 统计、prompt 日志扩展字段；
- 建立 P-01~P-14 的样本骨架；
- 作为 v1/v2 对照基线。

### 阶段 1（止血，未落代码）
1. 世界书按用户隔离；
2. 记忆按会话 + 游标；
3. 状态落库并跨轮继承；
4. 真实取消；
5. max_tokens + finish_reason；
6. 失败路径清理孤立 user；
7. 非流式/访客对齐（schema 钳制、记忆、后处理）。

### 阶段 2A（组装层地基）
- 数据迁移：世界书列、记忆新表、prompt log 列；
- `PromptSection/PromptPlan` + 预算器（先固定比例，后按模型）；
- L0/L1/L2/L4/L5 渲染；
- 世界书 `constant/keys/secondary/selective/order/position/depth/budget/regex/group`；
- 角色卡 `mesExample/personality/scenario/firstMes`；
- 统一 prepare（stream/sync/guest）；
- v2 开关默认关闭。

**退出标准**：P-01~P-07、P-12 通过；8k 上下文不溢出；L0+L1 hash 稳定。

### 阶段 2B（记忆与深度注入）
- `chat_memory_slots` + `chat_summary_state`；
- 滚动摘要与四槽、去重、会话隔离、删除清理；
- depth 注入 + anchor（depth/frequency）；
- 旧 `chat_memories` 迁移与只读兼容；
- sticky/cooldown/probability/递归 2 步。

**退出标准**：P-08~P-10 通过；30 轮记忆预算达标；跨会话污染为 0。

### 阶段 2C（provider 数据化与可观测）
- profile JSON、`max_completion_tokens`/`reasoning_content`/`stop` 适配；
- prompt 分段 diff 接口与前端只读展示；
- 重试/降级策略（OOC 检测、空回复、截断续写）；
- 日志清理策略。

**退出标准**：P-11、P-13、P-14 通过；切换 4k/8k/32k 模型无溢出；同输入重试 prompt 可复现。

---

## 14. 风险

1. **迁移风险**：旧世界书字段不统一，回填可能有漏；读取必须保留 payload 回退，且 v1 开关可回滚。
2. **prefix cache 被动态段破坏**：任何“按轮变化”的内容都不能进 L0/L1；记忆/世界书/状态必须走 depth。
3. **摘要幻觉**：四槽摘要只允许事实/关系/约定，禁止风格指令；每次摘要输出必须结构校验，失败不推进游标。
4. **预算抖动**：中文 token 估算偏差会导致临界溢出；按 1.6 字/token 预留，并在发送前做一次真实计数校验。
5. **流式改造范围**：本方案不动 hold-back/选项回填/格式修复/CG 白名单；流式解析器重写单列阶段 3，避免与本阶段耦合。
6. **旧摘要与新槽位并存**：v2 只读新表，v1 只读旧表；两者不得同时注入，避免重复记忆。

---

## 15. 待确认清单

1. 是否接受 D1~D7 的默认取舍；
2. `firstMes` 是否由后端自动写入（前端是否已有开场渲染）；
3. 记忆槽位是否按中文键展示（`promises/user_info/long_term/short_term` 是否直接出现在 UI）；
4. v2 是否先按用户灰度，还是全量默认；
5. 世界书 `probability` 默认 100 是否可接受；
6. 旧 `chat_memories` 是自动迁移还是仅只读兼容；
7. `promptConfig` 的默认分区比例（30/10/30/15/10/5）是否调整。

---

**设计结论**：改造的核心不是“换更长的人设”，而是把 prompt 变成 **带预算和位置的 section 图**：稳定段放最前保 cache，动态段按 depth 靠近生成端，世界书变成可排序可触发的条目，记忆变成会话级四槽滚动摘要，所有入口共用同一个 prepare，所有变化可 diff 可回放。阶段 0/1 先保证数据正确，阶段 2 再换组装层，阶段 3 单独处理流式解析，互不耦合。
