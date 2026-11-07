# AI 角色扮演产品对话接口协议核查报告

- 核查日期：2026-11-07
- 目标：AI风月、SoulAI、MissAI（missai01.com）、sch.tkoqk.com（追加）
- 问题：对话接口用的是 OpenAI `/v1/responses`、`/v1/chat/completions`，还是自建协议？
- 边界：仅访问公开页面与公开 JS bundle；未登录、未绕过鉴权、未调用需要授权的接口、未采集用户数据。

## 结论速览

| 产品 | 结论 | 可信度 | 主要依据 |
|---|---|---|---|
| MissAI（missai01.com / missai.me） | **自建协议**（自有 `/api/gva/narratium/...` + 自定义 SSE 帧） | **已证实** | 公开 Next.js bundle 中的请求路径与 SSE 解析器；本地镜像 README/服务端代码佐证 |
| AI风月（风月AI） | **未核实**（未找到公开的对话客户端代码或接口文档） | **未核实** | 公开站点均为下载/SEO 落地页，聊天气泡为静态演示；仅“自定义 API 接入”页显示模型层兼容 OpenAI 格式 |
| SoulAI | **未核实**（未找到公开的对话客户端代码或接口文档） | **未核实** | 公开站点为落地页/FAQ，无可用网页版入口；未发现可下载的前端 bundle |
| sch.tkoqk.com（页面标题 SoulAI） | **自建协议**（自有 `/chat/chat/chat` + 自定义纯文本 SSE） | **已证实（客户端侧）** | 入口壳 iframe 指向 chatclient.cphtq.com；公开 Nuxt bundle 中的发送函数与 SSE 解析器 |

**总体判断**：目前只有 MissAI 与 sch.tkoqk.com 能用公开证据下定论——两者都是自建对话协议，不是 OpenAI `/v1/responses`，也不是 `/v1/chat/completions`。AI风月与 SoulAI 缺乏可核验的公开客户端或文档，不能据此判断；现有信息只能说明它们宣称支持多模型/OpenAI 兼容的模型接入，无法证明其产品自身的对话接口形态。

---

## 一、MissAI（missai01.com）

### 1.1 结论

MissAI 的客户端与自家网关通信，路径为：

- 登录态：`POST /api/gva/narratium/chatSession/chatStream`
- 游客态：`POST /api/gva/narratium/guestChat/chatStream`

响应为 `text/event-stream`，每帧是 `data: {JSON}`，JSON 内部用 `type` / `content` 等自定义字段；结束标记为 `data: [DONE]`。这套格式既不是 `choices[0].delta`，也不是 `response.output_text.delta`，属于**自建应用层协议**。

可信度：**已证实**。

### 1.2 直接证据（公开 JS bundle）

公开页面 `https://missai01.com/` 正常可访问（HTTP 200）。首页与 `/history`、`/profile` 页面引用的 Next.js chunk 均公开可下载。关键 chunk：

1. `https://missai01.com/_next/static/chunks/6906-164e18e235830e62.js`
   - 登录态请求：`` `${u}/narratium/chatSession/chatStream` ``
   - 请求体字段：`sessionId`、`message`、`modelRateId`、`modelName`、`llmConfigId`、`clientRequestId`，可选 `presetKey`、`metaInstruction`
   - 按响应头 `Content-Type` 分流：`text/event-stream` → `handleStreamResponse`；`application/json` → 读 `{code:0, data:{assistantMessageId,userMessageId,...}}`
   - SSE 解析器 `processStreamChunk` / 断线续传 `parseSSEBuffer`，以 `data:` 行 + `[DONE]` 为边界

2. `https://missai01.com/_next/static/chunks/8390-23381583f7850610.js`
   - 游客请求：`fetch("/api/gva/narratium/guestChat/chatStream", ...)`
   - 额外请求头：`X-Device-Fingerprint`、`X-Guest-Request-ID`、签名头 `X-Sign-Version` / `X-Sign-Timestamp` / `X-Sign-Nonce` / `X-Signature`
   - 游客前置流程：`/api/gva/narratium/guestChat/signingSalt`、`/api/gva/narratium/guestChat/powChallenge`
   - 响应头 `X-Guest-Stream-Protocol: keepalive-v1`；心跳是 SSE 注释行 `: guest-heartbeat`
   - 帧类型：`{"type":"message","content":"..."}`、`{"type":"error",...}`、`{"type":"guest_info"|"guest_info_updated", maxChats, remainingChats, totalChats}`、`data: [DONE]`

3. `https://missai01.com/_next/static/chunks/webpack-153fa23f2f03307c.js`
   - Webpack 运行时 chunk 映射表；据此枚举并下载了 49 个懒加载 chunk（1 个超时），覆盖聊天相关模块。

### 1.3 MissAI 自建协议长什么样

登录态 SSE 帧（`type` 取值由 bundle 中的解析分支确认）：

| type | 含义/字段 |
|---|---|
| `content` | 正文增量，字段 `content`；也兼容裸 `{"content":"..."}` |
| `clear_content` | 清空已渲染正文，前端重画 |
| `thinking` | 思考提示，字段 `message` |
| `usage` | 用量/扣点，字段 `totalPointsDeducted` |
| `messageIds` | 返回 `userMessageId` / `assistantMessageId` |
| `stopped` | 用户停止，字段 `stoppedByUser` / `pointsDeducted` |
| `trial_status` | 试用状态，字段 `items[]` |
| `cgTrigger` | CG 触发，字段 `triggerTime` / `messageId` / `triggers[]` |
| `cgTriggerStream` | 流式 CG 触发 |
| `fallback_notice` | 降级提示，字段 `message` |
| `error` | 错误，字段 `message` / `code` |
| `data: [DONE]` | 流结束哨兵 |

游客态 SSE 帧：

| type | 含义/字段 |
|---|---|
| `message` | 正文，字段 `content`（游客前端只认这一种正文类型） |
| `error` | 错误 `code` / `error` / `message` |
| `guest_info` / `guest_info_updated` | 游客额度 `maxChats` / `remainingChats` / `totalChats` |
| `data: [DONE]` | 流结束哨兵 |

非流式回退（当响应是 `application/json` 时）：`{code:0, data:{assistantMessageId, userMessageId, message,...}}`。

### 1.4 OpenAI 协议特征缺失核验

对已下载的 MissAI 全部公开 chunk（首页 33 个 + 懒加载 49 个，含 1 个超时未取）做了全文检索，以下标识**命中 0 次**：

- `choices`
- `delta.content`
- `output_text`
- `response.completed`
- `chat/completions`
- `/v1/`

因此从客户端侧看不到任何 OpenAI Chat Completions 或 Responses API 的请求/解析特征；客户端只认自家 `type/content` 帧。

> 注意：这只证明“客户端 ↔ MissAI 网关”的协议是自建的。MissAI 服务端内部再调用哪家模型、是否用 OpenAI 兼容协议，属于服务端实现，公开证据无法核实。

### 1.5 本地镜像的佐证与局限

本地镜像 `d:\ai_manager_workspace\missai_local` 中：

- [README.md](file:///d:/ai_manager_workspace/missai_local/README.md#L507) 记录了原站聊天接口：`narratium/chatSession/*`、`narratium/guestChat/chatStream`。
- [README.md](file:///d:/ai_manager_workspace/missai_local/README.md#L557-L575) 描述了流式帧：`{"type":"content"}` → `{"type":"usage"}` → `{"type":"done"}` → `data: [DONE]`，以及游客流“只认 `message` 类型”。
- [server.py](file:///d:/ai_manager_workspace/missai_local/server.py#L4859-L4860) 将 `chatSession/chatStream` 转到本地实现；[server.py](file:///d:/ai_manager_workspace/missai_local/server.py#L4632) 处理 `guestChat/chatStream`。
- [server.py](file:///d:/ai_manager_workspace/missai_local/server.py#L4277) 注明游客流客户端只认 `{"type":"message","content":…}`。

局限：`html/app/*` 是本项目**自建前端**，不是原站 bundle；`html/js/*.js` 是注入脚本；`mirror_assets` 表 0 条记录，仓库内也没有原站 `_next` chunk。因此本地镜像只能作为旁证，本报告的 MissAI 主证据来自线上公开 bundle。

---

## 二、AI风月（风月AI）

### 2.1 结论

**未核实**。没有找到可公开访问的 Web 对话客户端、开发者 API 文档或可下载的前端 bundle；因此无法判断其对话接口是 `/v1/responses`、`/v1/chat/completions` 还是自建协议。

### 2.2 核查过程与证据

检索到的域名包括 `aiifengyue.cn`、`aifyue.cn`、`fengyueba.cn`、`fengyue-ai.cn`、`fyai-app.com.cn`、`fyueai.com`、`fyaipro.cn` 等，均表现为下载/SEO 落地页：

- `https://fengyueba.cn/`：页面有“网页版”文案，但 `#play` 区域只是静态手机聊天气泡演示，无 `iframe`、无真实请求脚本。
- `https://www.aiifengyue.cn/`：仅 1 个 `static/main.js?v=1012`（3.5 KB），无 API 请求代码；无 `.apk` / App Store 链接。
- `https://www.aifyue.cn/`：仅 `desk-bbcbe6.js`（9 KB），无 API 请求代码。
- `https://www.fyaipro.cn/`：导航站式落地页，无聊天客户端。

唯一与“接口格式”相关的公开信息：

- `https://www.fyueai.com/model/` 的“自定义 API 接入”章节写明：用户可在“设置 → API 管理”填写 API 密钥与 `Endpoint`、模型名称，并称“可接入兼容 OpenAI 格式 API 的模型或微调模型”。

这条只能说明：**AI风月的模型接入层接受 OpenAI 兼容格式的第三方模型服务**。它不能证明 AI风月自身客户端与自家服务端的对话接口形态，页面也没有给出 `/v1/chat/completions` 或 `/v1/responses` 的具体路径。

### 2.3 可信度判定

- “模型接入层宣称兼容 OpenAI 格式”：**有迹象**（来自营销页文案，非技术文档）。
- “产品自身对话接口协议”：**未核实**。

---

## 三、SoulAI

### 3.1 结论

**未核实**。未找到官方开发者文档、公开 Web 客户端或可分析的 JS bundle，无法判断其对话接口协议。

### 3.2 核查过程与证据

- `https://soul-ai.cc/`：Cloudflare 落地页，脚本只有 `script.js`（页面滚动/FAQ/动效，约 7.5 KB），无任何聊天请求逻辑；页面宣称支持 H5/PWA/原生 App，但“立即体验”类入口指向联盟链接 `https://aiaff.ink/...`，没有可用的网页版地址。
- `https://soulaipro.cn/`：静态落地页，无脚本、无可执行对话入口；只描述“支持导入 PNG/JSON 角色卡、多模型拟真互动”。
- `https://www.soulaiapp.cn/`：访问时 TLS 证书已过期，无法完成公开页面核验。
- `https://soulai.me/`：标题为“SoulAI官网 - 从角色设定到故事共创 | 中文AI对话指南”，内容为指南/导航页，不是产品 Web 客户端。
- 另检索到 `soul.ai`、`soulai.com` 等英文同名产品，与中文 AI 陪伴产品不是同一对象，未纳入判断。

由于没有可下载的前端代码，无法进行 `choices` / `delta` / `output_text.delta` / `response.completed` 的模式匹配。按任务要求，不用推测填坑。

### 3.3 可信度判定

- “支持多模型、可导入角色卡”：**有迹象**（落地页文案）。
- “对话接口协议”：**未核实**。

---

## 四、追加核查：sch.tkoqk.com（页面标题 SoulAI）

### 4.1 结论

**自建协议**：客户端把消息 POST 到自有路径 `/chat/chat/chat`，响应是 `text/event-stream`，但帧内容不是 JSON，而是**纯文本增量 + `&$*` 控制标记**（心跳 `&$*heartbeat`、完成 `&$*...`、错误码 `504/505` 等）。既不是 OpenAI Chat Completions 的 `data: {"choices":[{"delta":...}]}`，也不是 Responses 的 `response.output_text.delta`。

可信度：**已证实（客户端侧）**；服务端再向哪家模型转发属于服务端实现，**未核实**。

### 4.2 入口结构

- `https://sch.tkoqk.com/` 本身只是一个 2160 字节的入口壳，加载 `/entry-shell.js`。
- `/domainconfig.json` 返回候选域名：

```json
{
  "domesticDomains": [
    "https://chatclient.cphtq.com",
    "https://chatclient.vhe7g.com",
    "https://chatclient.ugegn.com"
  ]
}
```

- `entry-shell.js` 依次探测候选域名，把第一个可达域名放进 `iframe.src`（并保留 query/hash），同时通过 `postMessage` 处理 `SAVE_TOKEN` / `RESTORE_TOKEN`。本次实际加载的是 `https://chatclient.cphtq.com/`。
- 该页面标题为 `SoulAI｜AI 角色聊天与创作`，构建版本 `1.0.211+9f75f937`，Nuxt buildId `c317ae1a-ca92-472c-9698-30136ede8a0e`；运行时配置 `siteUrl:"https://sch.tkoqk.com"`、`apiBase:"https://chatclient.prjub.com"`、`androidDownloadUrl:".../download/soulai.apk"`。
- 注意：仅凭标题与 `soulai.apk` 不能证明它与本报告第三节的 SoulAI 是同一品牌，未做账号/主体层面的同一性验证，因此不合并结论。

### 4.3 实际发请求的 JS 与请求体

聊天核心模块是 `https://chatclient.cphtq.com/_nuxt/BFtOppuJ.js`（6.9 KB），路由链为：

`/session` → `B7t3CEy4.js` → 移动端 `B_zktkgh.js` / 桌面端 `BXR7mJQf.js` → `BMfzSOpz.js`（调用 `sendChat`） → `BFtOppuJ.js`（SSE 发送/解析）

发送函数（`BFtOppuJ.js` 末尾）：

```js
sendChat:(i,u)=>v("/chat/chat/chat",i,u),
regenerate:(i,u)=>{ ... return v("/chat/chat/regenerateResponse",t,u) }
```

请求体（`BFtOppuJ.js` 内构造）：

```js
JSON.stringify({...u, analyticsLogicalId:void 0, analyticsAttemptId:void 0, stream:!0})
```

调用侧（`BMfzSOpz.js`）传入的业务字段：

```js
sendChat({
  analyticsLogicalId, chatId, roleId, chatType:1,
  message, goodSettingId, selfChatCount:0,
  history, greetingIndex
},{ onChunk, onFinish, onInterrupted, onRetry, onError })
```

请求头包含 `Content-Type: application/json`、`Cache-Control: no-cache`、`X-Requested-With: XMLHttpRequest`、`platId`，有登录态时带 `token`；另有 60 秒无数据超时（`St=6e4`）与 `AbortController` 取消。

### 4.4 响应格式（关键证据）

`BFtOppuJ.js` 中的 `class tt` 是 SSE 行解析器：

```js
p.startsWith("data:") ? this.data.push(p.slice(5).replace(/^ /,"")) : ...
```

它只剥离 `data:` 前缀，**不做 JSON.parse**；空行把多行 data 合并后交给 `pt()` 判定：

```js
function pt(r,a){
  const s=r.trim();
  return s==="&$*heartbeat"||s==="数据正在准备中" ? {kind:"heartbeat"}
    : s.startsWith("&$*") ? {kind:"complete",parts:s.slice(3).split("&$*")}
    : s==="504" ? (a?{kind:"error",code:504}:{kind:"retry",code:504})
    : s==="505" ? {kind:"error",code:505}
    : !a&&/^\d+$/.test(s)&&gt.has(Number(s)) ? {kind:"error",code:Number(s)}
    : {kind:"text",text:r};
}
```

其中错误码集合 `gt = new Set([102,104,2052,2023,2024,3101,3106,3107,3109,9999])`。其余内容直接作为文本增量传给 `onChunk`，前端把文本拼到 `rawContent`。

也就是说，响应是“**纯文本 SSE**”，不是 OpenAI 的 JSON 事件流：

- 没有 `choices`
- 没有 `delta.content`
- 没有 `response.output_text.delta` / `response.completed`
- 没有 `[DONE]` 哨兵；完成由业务标记 `&$*...` 表达
- 心跳是 `&$*heartbeat` 或中文 `数据正在准备中`

非流式/错误回退：若响应 `content-type` 为 `application/json`，读取 `{code,msg}` 作为错误（`code` 缺省按 500），不走 OpenAI 错误结构。

### 4.5 全量特征检索

枚举 `_nuxt` 路由 chunk 与懒加载依赖后，共下载并检索 **83 个 JS 文件、约 3.03 MB**（未压缩）。以下字符串命中均为 **0**：

`chat/completions`、`/v1/`、`v1/chat`、`v1/responses`、`response.output_text`、`output_text`、`response.completed`、`choices`、`delta.content`、`"delta"`、`[DONE]`、`openai`、`OpenAI`。

唯一出现 `text/event-stream` 的位置是 ofetch/Nuxt 框架的通用响应类型分派（`CZMCnAtL.js` / `B-qJ6Oef-legacy.js`），不是聊天业务代码。聊天业务只认 `data:` + 纯文本 + `&$*` 控制标记。

### 4.6 判定与边界

- **判定**：自有后端转发 + 自定义纯文本 SSE。不是 OpenAI Chat Completions，也不是 OpenAI Responses；后端是否把请求再转成 OpenAI 协议发给模型，公开客户端代码看不出来，**未核实**。
- **核验到哪一步**：静态 bundle 已完整核验到“客户端如何发请求、如何解析响应”。真实对话请求需要登录 token（`token` 头），本次未调用、未登录，因此没有抓取真实响应报文；结论基于公开代码，可信度为“已证实（客户端侧）”。
- **未做**：未调用任何需要授权的聊天接口，未验证服务端上游模型供应商。

---

## 五、总体判断

1. **MissAI 与 sch.tkoqk.com 可以下定论**：两者都是自建协议。MissAI 是“自有路径 + 自定义 JSON SSE 帧（`type/content` + `[DONE]`）”；sch.tkoqk.com 是“自有 `/chat/chat/chat` + 纯文本 SSE + `&$*` 控制标记”。两者的公开 chunk 中都不存在 OpenAI Chat Completions / Responses 的特征字符串。
2. **AI风月、以及未确认品牌同一性的 SoulAI 不能下定论**：目前只有营销落地页，没有公开客户端代码、接口文档或可用网页版；任何“它们用 chat completions / responses”的说法都缺乏证据。sch.tkoqk.com 标题虽为 SoulAI，但未验证主体同一性，不并入该结论。
3. 若后续需要补齐 AI风月，可行路径只有：获取其公开可下载的 App 安装包并分析网络层（本次未做，属于超出“公开页面/本地镜像”的取证范围），或等待其公开开发者文档；在此之前应维持“未核实”。

---

## 附：证据清单

| # | 证据 | 类型 | 可信度 |
|---|---|---|---|
| 1 | `https://missai01.com/_next/static/chunks/6906-164e18e235830e62.js`：`/narratium/chatSession/chatStream` + 自定义 SSE 解析 | 公开 JS bundle | 已证实 |
| 2 | `https://missai01.com/_next/static/chunks/8390-23381583f7850610.js`：`/narratium/guestChat/chatStream`、`type:"message"`、`: guest-heartbeat`、`[DONE]` | 公开 JS bundle | 已证实 |
| 3 | `https://missai01.com/_next/static/chunks/webpack-153fa23f2f03307c.js`：chunk 映射，支撑全量扫描 | 公开 JS bundle | 已证实 |
| 4 | MissAI 全部已下载 chunk 中 `choices` / `delta.content` / `output_text` / `response.completed` / `chat/completions` / `/v1/` 命中 0 | 全量检索结果 | 已证实（客户端侧） |
| 5 | [README.md](file:///d:/ai_manager_workspace/missai_local/README.md#L507-L575)、[server.py](file:///d:/ai_manager_workspace/missai_local/server.py#L4277-L4860) | 本地镜像旁证 | 有迹象/旁证 |
| 6 | `https://www.fyueai.com/model/`：“可接入兼容 OpenAI 格式 API 的模型” | 营销页文案 | 有迹象（仅模型层） |
| 7 | `https://fengyueba.cn/`、`https://www.aiifengyue.cn/`、`https://www.aifyue.cn/`、`https://www.fyaipro.cn/` | 落地页/静态演示 | 未核实 |
| 8 | `https://soul-ai.cc/`、`https://soulaipro.cn/`、`https://www.soulaiapp.cn/`（证书过期）、`https://soulai.me/` | 落地页/导航页 | 未核实 |
| 9 | `https://sch.tkoqk.com/domainconfig.json` + `https://sch.tkoqk.com/entry-shell.js`：iframe 候选域名 `chatclient.cphtq.com` | 公开静态资源 | 已证实 |
| 10 | `https://chatclient.cphtq.com/_nuxt/BFtOppuJ.js`：`sendChat:...v("/chat/chat/chat",...)`、`stream:!0`、纯文本 SSE 解析器与 `&$*` 控制标记 | 公开 JS bundle | 已证实（客户端侧） |
| 11 | `https://chatclient.cphtq.com/_nuxt/BMfzSOpz.js`：调用 `sendChat` 的业务字段（`chatId`/`roleId`/`message`/`history`/`goodSettingId`） | 公开 JS bundle | 已证实（客户端侧） |
| 12 | sch 全部 83 个 JS、约 3.03 MB 中 `chat/completions` / `/v1/` / `choices` / `delta.content` / `output_text` / `response.completed` / `[DONE]` 命中 0 | 全量检索结果 | 已证实（客户端侧） |
