# MissAI 本地镜像（自建前端 + Python + SQLite）

把 <https://missai01.com> 搬到本地运行，并接上本地 SQLite：

- **前端**：唯一前端是**自建原生页** `/app/*`（`html/app/`，10 个页面 + 共享 css/js）；
  原站镜像页面已下线，旧地址统一 **302** 到新页（见「路由架构」），未抓到的资源与接口仍反代线上
- **后端**：Python，标准库为主（第三方依赖只有 `roleplay_core` 的 **pydantic** 与传输层的 **httpx**）—— 九段路由分发 + 本地接管 + 反代兜底
- **数据库**：
  - `data/missai.db` —— 本地账号会话、访问与操作日志、收藏、对话、上传记录，
    以及**内容库**（`works` 卡片文字 / `work_tags` 标签 / `tag_catalog` 标签字典 / `content_api` 离线载荷）
  - `data/image.db` —— **图片专用库**：页面里每张图（动图/静态图）的请求地址 → 相对项目根的路径

## 目录结构

```
missai_local/
├── html/                      前端全部内容
│   ├── console.html           本地控制台页面（/local/ 使用，深色主题）
│   ├── app/                   唯一前端：自建原生页（/app/*）
│   │   ├── *.html             10 个页面：explore / chat / plaza / draw / creation /
│   │   │                      worldbook / works / account / console / creation-guide
│   │   ├── css/               app.css（全站共享）+ guide.css（教程页）
│   │   └── js/
│   │       ├── core/          api / guard / sse / store / ui.js
│   │       └── pages/         每页一个脚本（explore / chat / draw / creation …）
│   ├── js/                    注入脚本（hide-ads / no-feed-cache / fault-tolerance /
│   │                          empty-cleaner / publish-jump / console-entry）+ console.js 等
│   ├── css/                   控制台样式 console.css + 站点样式
│   ├── image/                 所有图片落盘目录（上传图、CDN 图、AI 生成图都存这里）
│   └── assets/                站点资源：图标、字体（fonts/）、原始图片
├── server.py                  本地服务器（九段路由 + 反代 + 本地接管）
├── captcha.py                 数字加减法验证码（出题 / PNG 题图 / 判分）
├── db.py                      主库数据层（missai.db）
├── llm.py                     OpenAI 协议传输层（httpx）/models 检测 + /chat/completions 对话（三段超时 + 错误分类重试 + SSE 分帧）
├── imagegen.py                绘图协议对接（/models 检测 + /images/generations 文生图 + 本地占位图）
├── mediagen.py                绘图配方引擎（内置模板目录 data/json/ + 提示词构建）
├── imagedb.py                 图片库数据层（image.db：图片 → 相对路径，绝对路径运行时拼接）
├── tplimport.py               第三方模板 JSON 通用适配（行业格式 → MediaGen 配方）
├── prompt_builder.py          Token 估算与分段工具（粗估 + EMA 校准 / sectionize / 前缀 hash），供 prompt.py 与 server.py 调用
├── roleplay_core/             角色扮演分层核心：聊天生成管线（模型层 + 数值规则 + 清洗 + 输出后处理）
│   ├── __init__.py            导出 ChatContext / Message
│   ├── models.py              强类型数据模型（Message / ChatContext，基于 pydantic）
│   ├── prompt.py              唯一提示词拼装引擎（分区预算 / 世界书 / 角色卡 / 记忆槽 / 三层压缩）
│   ├── memory_engine.py       算法记忆（特征哈希向量 + FTS5/BM25 + 余弦 / MMR 重排 / 规则抽取）
│   ├── chat_router.py         单次对话请求编排（上下文 → 拼装 → LLM 流式 → 清洗 → 展示态 → 落库）
│   ├── emotion_mapper.py      数值规则匹配器（把好感度 / SAN 等数值翻译成情绪 / 行为约束）
│   ├── sanitizer.py           文本清洗层（<say> 归一、标签剥离、脚本过滤；纯字符串无依赖）
│   └── post_processor.py      响应后处理（拆 <image> CG、<book_status> 状态栏、<next_prompt> 选项）
├── selftest.py                冒烟测试（227 项：离线兜底、对话设置、模型接入、AI 绘图、流式聊天 …）
├── tools/
│   └── mirror.py              镜像脚本（重建静态镜像 / 手动镜像落库 / 创意广场同步）
├── tests/                     专项测试（stub_llm.py 模型桩 + test_cg_* / test_chat_local / test_chat_regression）
├── data/
│   ├── missai.db              主库（首次启动自动创建）
│   ├── image.db               图片库（首次启动自动创建）
│   ├── json/                  内置数据 JSON：ai-draw-*.json 绘图模板 + 其他导入数据
│   ├── rules/                 平台数值规则：affection_rules.json（好感度 -100~100）、
│   │                          san_rules.json（SAN 值 0~100）
│   └── .current_db            控制台切换数据库后记录当前库路径
├── logs/                      运行期日志（如 tests/stub_llm 的捕获日志）
├── requirements.txt           第三方依赖（httpx）
├── start.bat                  Windows 一键启动（缺 httpx 时先装依赖，装不上直接退出）
└── README.md
```

`roleplay_core/` 是**聊天生成管线**：`server.py` 只做 HTTP 编排，
把一次对话交给 `roleplay_core/chat_router.py`（`ChatPorts` 注入 db / 模型 / SSE），按
**上下文 → prompt.py 拼 messages → LLM 流式 → sanitizer 清洗 → post_processor 拆展示态 → 落库**
走完。提示词组装只有一个引擎 `roleplay_core/prompt.py`（`prompt_builder.py` 只提供 token 估算与分段工具）。
好感度 / SAN 数值规则落在 `data/rules/*.json`，由 `emotion_mapper` 匹配档位后注入提示词。
第三方依赖是 **pydantic**（`roleplay_core/models.py`）与 **httpx**（`llm.py` 传输层）。

## 运行

```bash
python server.py          # 默认 4173 端口
python server.py 8080     # 指定端口
# Windows：双击 start.bat
```

| 地址 | 说明 |
|---|---|
| <http://localhost:4173/> | 根路径 → **302** 到 `/app/explore` |
| <http://localhost:4173/app/explore> | 新首页（探索：热力橱窗最新 / 24H / 周榜 / 月榜） |
| <http://localhost:4173/app/chat> | 聊天（会话、历史、游玩历史） |
| <http://localhost:4173/app/creation>、`/app/worldbook` | 创作页、世界书 |
| <http://localhost:4173/app/plaza>、`/app/draw` | 创意广场、AI 绘图 |
| <http://localhost:4173/app/works>、`/app/account` | 我的作品、账户 |
| <http://localhost:4173/app/creation-guide> | 创作教程（原 `/creation-guide/*`） |
| <http://localhost:4173/local/> | 本地控制台（**仅管理员**）：数据库 / 访问日志 / 账号 / 积分管理 / 消耗管理 / 规则管理 / 收藏 / 对话 / 图片库 / 模型接入 / 绘图模型 / 内容库 / 创作数据 / 创意广场 |
| <http://localhost:4173/app/console> | 控制台的自建页版本（同套 `/js/console.js`，同样**仅管理员**） |
| <http://localhost:4173/login> | 线上原站登录页（反代，备用；推荐用页面右上角登录弹窗，见「怎么登录」） |

**控制台只让管理员看**（页面 + 接口 + 侧栏入口，三处同时把关）：

| 未登录 / 普通用户 | 管理员（`role=admin`） |
|---|---|
| `/local/` → 控制台登录页（`/local/api/login` 校验账号密码，非管理员回 401） | `/local/` → 控制台页 |
| `/app/console` → 同样的管理员登录页 | `/app/console` → 控制台页 |
| `/local/api/*` → 401 `{"code":401,"msg":"请先用管理员账号登录"}` | 正常返回数据 |
| 站点侧栏**没有**「控制台」按钮（注入前先查会话角色） | 侧栏末尾出现「控制台」按钮 |

**控制台入口两条路**：

- **原站代理页**（`/login` 等反代回来的 HTML）：服务端**回包时**在 `</body>` 前注入
  `<script src="/js/console-entry.js">`，脚本克隆侧栏原有条目、换成 `href=/local/`，
  并用 MutationObserver 在 React 重渲染后补回。**只有管理员的请求才注入**；
  磁盘上的 HTML **完全不动**，重跑镜像也不会丢（`selftest.py` 会检查）
- **自建页**（`/app/*`）：顶栏导航自带「控制台」项（`html/app/js/core/ui.js`），
  指向 `/app/console`，服务端按管理员会话把关

### 路由架构（九段 dispatcher）

所有请求进 `server.py` 的 `route()`，按**固定顺序**依次尝试九个分段方法，
任一段处理完即返回；全部未命中 → 兜底反代 `missai01.com`：

| # | 分段 | 负责 |
|---|---|---|
| 1 | `_route_console` | `/local` 页面、`/local/login` 302、`/local/api/login` |
| 2 | `_route_chat_api` | 聊天页用户级接口：`/api/chat/models`、`/api/chat/memories`（GET/DELETE） |
| 3 | `_route_admin_api` | `/local/api/*` —— 一律先过管理员会话，未过 401 |
| 4 | `_route_app_pages` | `/app/*` 页面与共享资源（css/js/img/assets）、`/creation-guide*` 302 |
| 5 | `_route_legacy_redirects` | 旧镜像路径 → 302 到新页（见下表） |
| 6 | `_route_static` | `/js` `/css` `/html/assets` `/fonts` 与根目录固定文件 |
| 7 | `_route_media` | 上传 / `/image/` 图片库出口 / `/cdn*` 抓图 / `/cdn-cgi` 信标 204 |
| 8 | `_route_auth` | 会话、登录注册、数字验证码、邮件短信类本地回执 |
| 9 | `_route_business_api` | 画廊 / 探索 / 创意广场 / CG / 创作聊天本地读写 / 噪音桩 |
| — | 反代兜底 | 其余 `/api/*`、没抓到的 chunk → `ORIGIN` |

**顺序有依赖，不能随意调换**：管理员接口（3）必须在静态资源（6）之前，
否则 `/local/api/*` 会被当静态文件挡掉；账号会话（8）必须在业务 API（9）之前，
因为登录路径 `/api/gva/user/*` 与本地数据接口的 `user/` 尾缀重叠。

**旧路径 302 跳转表**（`_route_legacy_redirects`，query 串原样保留）：

| 旧地址 | 新地址 |
|---|---|
| `/`、`/home`、`/index.html`、`/favorites`、`/hot-chart` | `/app/explore` |
| `/profile`、`/history` | `/app/chat` |
| `/works` | `/app/works` |
| `/creators`、`/creative-plaza` | `/app/plaza` |
| `/ai-creator`、`/ai-gallery`、`/ai-history` | `/app/draw` |
| 前缀 `/creation-guide*` | `/app/creation-guide` |
| 前缀 `/creation/worldbook*` | `/app/worldbook` |
| 前缀 `/creation*` | `/app/creation` |
| 前缀 `/creators*` | `/app/plaza` |
| 前缀 `/profile*`、`/history*` | `/app/chat` |
| 前缀 `/works*` | `/app/works` |
| `/app` | `/app/explore` |

自测：

```bash
python selftest.py 4173   # 227 项，全绿输出「通过 227，失败 0」
```

> 已知的失败项：依赖线上内容的用例（榜单入库、聊天回复等）在**上游额度耗尽 / 不通**时失败，
> 属环境问题，与本地代码无关。

## SQLite 存了什么

| 表 | 用途 |
|---|---|
| `users` | 本地账号（PBKDF2-SHA256 加盐哈希，12 万轮；`role` = `admin` / `user`，默认管理员 `admin`；个人资料：`nick_name` / `header_img` / `email` / `signature` / `sex`） |
| `sessions` | 登录会话，Cookie `local_session`（HttpOnly，30 天） |
| `checkin_records` | **每日签到**：`user_id + date` 唯一，记当天拿到的积分（重复签到不重复加分） |
| `request_log` | 每次请求的方法/路径/状态/耗时/用户/IP/体积/备注 |
| `favorites` | 本地收藏、点赞、关注（嗅探 `favorite/like/follow` 类请求） |
| `chat_threads` / `chat_messages` | 本地对话记录（用户消息入库，AI 流式回复解析后入库；`content_llm` 存清洗后纯文本供下一轮拼 prompt） |
| `chat_memories` | 旧记忆摘要表，**只读兼容**（新流程不再写新行） |
| `chat_summary_state` | 滚动摘要游标：`covered_until` / `covered_count` / `rolling_summary` / `turns_since_update`，按会话隔离 |
| `chat_memory_slots` | 记忆四槽：`promise`（承诺与待办）/ `user_info` / `long_term` / `short_term`，`session_id + slot` 唯一 |
| `chat_memory_items` | 算法记忆条目：类型、重要度、置信度、来源消息、去重 hash、合并/聚类状态 |
| `chat_memory_vectors` | 记忆向量，按 `memory_id + model` 存；`hash` 基线向量始终有，`openai` 向量可补算 |
| `chat_memory_recall_log` | 每轮召回日志：命中的记忆、排名、注入位置、打分明细 |
| `chat_prompt_log` | 每次聊天前的完整 prompt 快照：messages / 实际请求 / `turn_id` / `finish_reason` / 分段 token / 前缀 hash / 世界书 / 记忆槽 / 与上一轮的分段 diff |
| `uploads` | 上传记录：本地路径、SHA256、字节数（图片本体的登记在 `image.db`） |
| `works` | **内容库**：卡片文字 —— 名称、简介、作者、评分、封面、标签 JSON、原始条目 |
| `work_tags` | 作品 ↔ 标签（一行一个标签，控制台的标签统计、离线标签筛选都读它） |
| `tag_catalog` | 标签字典（`/api/gva/creativePlaza/tags` 的 200 条标签） |
| `content_api` | 每个内容接口的**形状**（list / `dict:list` / `dict1:character`）与**原始文字载荷**，离线还原回包用 |
| `creative_items` | **创意广场**：商品（标题/简介/价格/标签/销量/赞踩/热度/状态/版本）、开源源码 `prompt_content` / `prompt_data` / `paired_css_prompt`、原始条目 `raw` |
| `creative_user_items` | 我的创意：购买 / 试用记录（`acquisition_type`、试用轮次、启用状态） |
| `creative_comments` | 创意评论（线上抓取 + 本地发布，`item_id + remote_id` 去重） |
| `creative_votes` | 点赞 / 点踩（`user_id + item_id` 唯一，改票会回滚旧票） |
| `creative_purchases` | 购买流水（价格、购买后余额、幂等键唯一） |
| `creative_bindings` | 装备绑定（会话 / 角色维度） |
| `creative_defaults` | 全局默认装备（每种类型一条） |
| `config_cache` | 配置种子（筛选项 / 标签）与**用户设置**：对话设置的模型参数、记忆配置，按 `键:用户:对象` 存；积分余额 `points:{用户}` 也在这里 |
| `llm_providers` | **模型接入**：OpenAI 协议的 `base_url` + `api_key`（就这两样手填），检测到的模型缓存与默认模型，详见「模型接入」一章 |
| `image_providers` | **绘图模型接入**：`base_url` + `api_key` + 绘图模型缓存 + 可选尺寸 + 默认模型，详见「AI 绘图」一章 |
| `image_works` | **绘图作品**：提示词 / 反向提示词 / 模型 / 尺寸 / 图片路径（`/image/…`）/ 点赞 / 作者，绘图广场直接读它 |
| `db_op_log` | **控制台操作日志**：`op` / 表名 / JSON 参数 / 用户 / 时间，只保留最近 2000 条 |
| `meta` | schema 版本 |

> **普通读接口的缓存放浏览器**（HTTP 缓存），旧库里的 `api_cache` 表会在启动时自动 `DROP`。
> 进库的只有：`works` 文字与标签、创意广场商品 / 源码 / 购买 / 评论、`image.db` 的图片、
> `llm_providers` 的模型接入配置，以及账号 / 个人资料 / 签到积分这类**用户设置**。
> 控制台「数据库」页能看到每张表的列与行数。

数据库位置：`data/missai.db`（WAL 模式，多线程安全）。控制台页面直接读 `/local/api/*`。

## 数据库控制台（/local/ 与 /app/console）

控制台是深色主题的单页应用（与站点统一：深底 + 紫粉 + 少量金），共用
`html/console.html`（`/local/`，带原站侧栏外壳）与 `html/app/console.html`（`/app/console`，
自建顶栏版）两份外壳、同一套 `js/console.js` + `css/console.css`。顶部导航：
数据库、访问日志、账号、积分管理、消耗管理、规则管理、收藏、对话、图片库、模型接入、
绘图模型、内容库、创作数据、创意广场。默认打开「数据库」视图，直接对本地 SQLite 做增删改查。

### 概览

- 连接状态卡：应用数据连接正常、`sqlite` 标签、当前数据库、主机 / 端口、连接池（QueuePool）、数据库大小、完整路径。
- 三张统计卡：数据表数量、数据库记录总数、状态栏字段（列）总数。
- 右上按钮：刷新、扫描 `data/json/`、清空所有数据、重新创建数据表、删除所有数据表。

### 数据库连接

表单支持数据库类型（SQLite / PostgreSQL / MySQL）、主机、端口、数据库名 / SQLite 路径、用户名、密码，
点「连接并切换数据库」生效：

- **SQLite**：填 `.db` 文件路径即可切换，相对路径按项目根目录解析，文件不存在会自动创建并建好全部表；
  切换成功后路径写入 `data/.current_db`，重启服务仍连这个库，同时为管理员重发会话 Cookie。
- **PostgreSQL / MySQL**：本地后端只内置 `sqlite3`，会返回明确提示；需要自行安装驱动后再扩展。

### 表浏览与行编辑

- 左栏是数据表列表（表名 + `N 行 · M 列`），可搜索、选中高亮；右栏是表数据。
- 表头显示列名、类型与 `PK` 标记；长文本单行省略，鼠标悬停看全文，宽表在面板内横向滚动。
- 每行「编辑 / 删除」；顶部「+ 新增记录」；分页支持每页 20 / 50 / 100 / 200 条与上一页 / 下一页，
  底部显示「显示 X - Y / Z 条」。
- 「+ 新建数据表」可自定义表名与字段（类型 / 主键 / 非空 / 默认值），「清空数据」删掉整张表的记录但保留表结构。

### 维护操作与安全保留表

破坏性操作都会先弹确认框，并要求服务端参数 `confirm: "yes"`：

| 操作 | 行为 | 保留 |
|---|---|---|
| 清空所有数据 | 清空业务表记录，表结构保留 | `meta` / `db_op_log` / `users` / `sessions` / `llm_providers` |
| 重新创建数据表 | 删除业务表后按 schema 重建空表 | 同上 |
| 删除所有数据表 | 删除所有业务表 | `meta` / `db_op_log` / `users` / `sessions` |

`users` / `sessions` / `meta` / `db_op_log` 不能被单表删除，避免把控制台锁死。

### 扫描 data/json/ 与操作终端

- 「扫描 json/」只读 **`data/json/`** 下的 `*.json`：
  JSON 数组按文件名建同名表；`{"list": [...]}` 取列表键名建表；对象数组的字段自动推断类型并写入。
  绘图内置模板（`data/json/ai-draw-*.json`，顶层是 `categories[].recipes[]`）不是数据表，会被跳过
  并计到「跳过 N 个模板文件」里。
- 「Python 数据库操作终端」显示最近的操作日志：`READ_STATE` / `QUERY` / `SAVE_ROW` / `DELETE_ROW` /
  `CREATE_TABLE` / `DROP_TABLE` / `CLEAR_ALL` / `RECREATE` / `DROP_ALL` / `SCAN_JSON` / `CONNECT_DB`，
  含表名、时间与 JSON 参数，可点「刷新日志」。日志写入 `db_op_log`，只保留最近 2000 条。

### 接口

均需管理员会话：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/local/api/db/overview` | 概览（库名、路径、大小、表数、行数、列数） |
| GET | `/local/api/db/tables` | 表列表（行数 / 列数） |
| GET | `/local/api/db/table?name=&limit=&offset=` | 表数据 + 列信息（含 `pk`） |
| GET | `/local/api/db/logs?limit=&offset=` | 操作日志 |
| GET | `/local/api/prompt-logs?userId=&sessionId=&limit=` | 最近的 prompt 快照（messages / 实际请求 / 分段 token / 前缀 hash / 与上一轮 diff），只读 |
| POST | `/local/api/db/row/save` | 新增 / 更新记录（`table`、`values`、可选 `pk`） |
| POST | `/local/api/db/row/delete` | 删除记录（`table`、`pk`） |
| POST | `/local/api/db/table/create` | 建表（`name`、`columns`） |
| POST | `/local/api/db/table/drop` | 删表（`name`、`confirm: "yes"`） |
| POST | `/local/api/db/table/clear` | 清空表记录、保留结构（`name`、`confirm: "yes"`） |
| POST | `/local/api/db/maintenance` | `action` = `clear` / `recreate` / `drop_all`，需 `confirm: "yes"` |
| POST | `/local/api/db/scan-json` | 扫描并导入 `data/json/` |
| POST | `/local/api/db/connect` | 切换数据库（`type`、`database`） |

## 创作数据（控制台：作品 + 世界书）

控制台顶部的「创作数据」把创作页保存的东西单独抽成一栏，数据全部来自本地库的两张表：

| 表 | 存什么 | 主键 |
|---|---|---|
| `my_works` | 作品整包（标题 / 简介 / 封面 / 状态 / `payload` 提示词 JSON） | `(user_id, character_id)` |
| `worldbook_entries` | 世界书条目（`custom=1` 用户自定义，`0` 内置） | `(user_id, entry_id)` |

### 列表

- 五张统计卡：作品、已发布、草稿 / 定时、世界书条目、作者。
- 工具栏：标题 / characterID / 简介搜索，状态与作者筛选，支持分页（每页 10 / 20 / 50 / 100）。
- 表格列：封面、作品（标题 + characterID）、作者、状态、世界书（总数 + 自定义数）、更新时间、操作。
- 操作：查看（整包弹窗）、导出、仅世界书、删除（作品 + 其全部世界书，已发布会同时从创意广场下线）。

> 删除是级联的：`my_works` 那一行、它的全部 `worldbook_entries`（含 `custom=1`）、
> 以及发布后留在 `works` 里的 `path='local/published'` 投影一起清掉。
> 创作页（`DELETE /api/gva/work`）和控制台（`POST /local/api/creation/delete`）走的是同一套
> `db._purge_work()`，不会出现「作品删了、世界书还在」的孤儿条目。

「查看」弹窗里是作品键值（characterID、作者、状态、标题、简介、开场白、描述、人设、场景、标签、时间）、
世界书表格（ID / 自定义或内置 / keys / 内容 / 位置）和可展开的 `payload` 原文，
右上按钮可直接导出整包、仅导出世界书、复制完整 JSON。

### 导出格式（`missai.creation/v1`）

```json
{
  "schema": "missai.creation/v1",
  "exportedAt": "2026-01-01T00:00:00+08:00",
  "scope": "full",
  "counts": {"works": 1, "entries": 2},
  "works": [{
    "user": "admin", "userId": 11,
    "characterID": "char-xxx", "status": "draft",
    "title": "作品名", "summary": "简介", "coverURL": "",
    "scheduledAt": null, "createdAt": "...", "updatedAt": "...",
    "payload": {"name": "作品名", "firstMes": "...", "description": "..."},
    "refs": null,
    "worldbook": [{"entryId": "wb_1", "custom": true, "position": "0",
                   "updatedAt": "...", "payload": {"keys": ["关键词"], "content": "设定"}}]
  }]
}
```

- `scope=full` 带 `payload` / `refs`；`scope=worldbook` 只留作品身份字段 + `worldbook`，用于「只同步世界书」。
- 导出走附件下载：`creation-works.json` / `creation-worldbook.json`，同时写一条 `EXPORT` 操作日志。

### 导入

支持三种写法，服务端统一归一化后逐件写入：

1. 上面那种整包（`{works:[…]}`，或裸数组、`{characterID, worldbook:[…]}`、只含世界书的条目数组）；
2. 创作页导出的单个作品对象（`characterID` + `name` / `firstMes` / `description` 等作品字段）；
3. 只有世界书的包——作品正文不会被改（对比导入前后 `payload` 一致）。

两种模式：

| 模式 | 作品 | 世界书 |
|---|---|---|
| `merge`（默认） | 同 `characterID` 合并字段（保留 `created_at`） | 按 `entryId` 逐条覆盖 / 新增 |
| `replace` | 整包覆盖 `payload` | 先清掉该作品旧世界书再写 |

- 归属用户优先级：面板选的「归属用户」> 文件里的 `userId` > 当前管理员会话 > 第一个管理员。
- 提交前可以「解析预览」：显示会写入几个作品、几条世界书，逐件标注「作品整包 / 仅世界书」并给出告警。
- 导入结果提示形如「导入完成：新增 N 个作品、覆盖 M 个，世界书 K 条」。

### 接口

均需管理员会话：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/local/api/creation/overview` | 统计 + 用户列表 + `schema` |
| GET | `/local/api/creation/works?userId=&q=&status=&page=&pageSize=` | 作品分页列表 |
| GET | `/local/api/creation/work?userId=&characterID=` | 单件作品整包 + 世界书 |
| GET | `/local/api/creation/export?userId=&characterID=&scope=` | 导出（`scope` = `full` / `worldbook`，附件下载） |
| POST | `/local/api/creation/preview` | `{json}` → 解析预览 |
| POST | `/local/api/creation/import` | `{json, mode, targetUserId}` |
| POST | `/local/api/creation/delete` | `{userId, characterID, confirm: "yes"}`，连世界书与公开库投影一起删 |

## 图片库（data/image.db）

页面里出现的**每一张图**——动图（gif/apng/动效 webp）、静态图（png/jpg/svg）、上传图、CDN 图——
都会在这里登记一条记录。数据库里**只存相对项目根的路径**，绝对路径由 Python 后端
按当前项目目录（`imagedb.ROOT`，即 `imagedb.py` 所在目录）在读取时自动拼接，
**整个项目搬到别的目录也不用改库**：

| 字段 | 含义 |
|---|---|
| `url_key` | 页面里请求的地址，如 `/cdn/uploads/xx.gif`、`/image/a.png` |
| `local_path` | 相对项目根的路径，如 `html/image/3f9a….gif`（**唯一落库的路径**） |
| `abs_path` | 不落库；接口读取时由 `ROOT / local_path` 实时计算后返回 |
| `kind` / `animated` | 类型；`animated=1` 表示动图（webp/apng 按文件头 `ANIM`/`acTL` 判定） |
| `bytes` / `sha256` | 文件大小与内容指纹 |
| `source` | 来源：`upload` 上传 / `cdn` 线上图 / `page` 页面静态图 / `local` 本地已有 |

三条落库路径：

1. **上传**：`POST /api/uploads/file/*` → 写 `html/image/` → 登记（返回 `path` + `absPath`）
2. **CDN 图**：`/cdn/*`、`/cdn2/*` 第一次抓取落 `html/image/<sha1>.<ext>` → 登记；
   之后同一张图直接读本地，不再回源（自测里有「第二次请求走本地」）
3. **页面静态图**：`send_file` 吐出图片时顺手登记，`/image/*` 入口先查库再读文件

控制台左栏「图片库」页可看：预览、请求地址、**本地相对路径**（悬停显示后端拼出的完整绝对路径）、
类型（动图标紫）、字节、来源、时间；概览页也有「图片库 / 动图」两张卡片。
接口：`GET /local/api/images`（仅管理员，返回里带运行时计算的 `abs_path`）。

## 内容库（文字与标签）

页面卡片上的**文字**（角色名、简介、作者、评分）和**标签**（大世界 / 剧情向 / 火影忍者…）
来自内容读接口，回包时服务端顺手拆进本地库：

| 落库目标 | 内容 | 触发接口 |
|---|---|---|
| `works` | 名称 / 简介 / 作者 / 评分 / 封面 / 标签 JSON / 原始条目 | `narratium/showcase/list`、`narratium/ranking/*`、`narratium/discovery/*`、`narratium/personalizedRecommendation/*`、`narratiumCharacter/find*` |
| `work_tags` | 作品 ↔ 标签（含顺序、更新时间） | 同上（条目里的 `tags`） |
| `tag_catalog` | 标签字典 200 条（名称 / 分类 / 使用次数） | `creativePlaza/tags` |
| `content_api` | 每个接口的形状 + 原始文字载荷（`data` 部分，上限 1MB、留最近 600 条） | 同上（仅这些 GET，账号/会话接口绝不进） |

**离线兜底**：线上连不上、返回 5xx，或用 `MISSAI_OFFLINE=1` 启动时，内容接口直接用上面的数据拼一个
结构一致的回包（响应头 `X-Local: db-fallback`）：

1. 先按「路径 + 查询串」取 `content_api.payload` —— 原样还原（榜单的 `{list,total,page,pageSize}` 等形状不丢）
2. 没有精确载荷 → 按该接口记下的形状用 `works` 表重建；角色详情按 `?characterID=` 精确取条目，
   并把 `name` / `characterName` / `summary` / `coverURL` 两套叫法补齐
3. 连内容库都没有 → 仍然回 `502` + 明确错误（不猜数据）

控制台左栏「内容库」页：作品 / 标签 / 内容接口三张卡片，
文字内容表（封面、名称、简介、标签、作者、评分、来源接口、更新时间，可横向滚动）、
标签统计、内容接口（形状 / 条目 / 载荷大小 / 最近抓取）。
接口（均仅管理员）：`GET /local/api/works?limit=&offset=&q=`、`/local/api/tags`、`/local/api/content_apis`。

## 创意广场（全量本地化）

旧页面地址 `/creative-plaza`、`/creators*` 等现在 **302 到 `/app/plaza`**，
背后的 **所有 `/api/gva/creativePlaza/*` 请求都由本地处理**，不再依赖线上：

| 接口 | 本地行为 |
|---|---|
| `GET /items`、`/items/byWork/:id` | 从 `creative_items` 分页 / 筛选（`itemType`、`keyword`、`tags`、`boundWorkId`、`sortBy`） |
| `GET /items/:id`、`/prompt`、`/export` | 详情 / 源码 / 导出；开源源码直接给，非开源要购买或作者本人 |
| `GET /catalog`、`/tags`、`/editorChoices`、`/listSpotlight` | 分类目录、标签、编辑精选、聚光灯（本地库） |
| `POST /items/:id/purchase` | **扣本地积分** → 写 `creative_purchases` + `creative_user_items`（幂等键去重，余额不足直接拒绝） |
| `POST /items/:id/trial` | 试用 3 轮，写 `creative_user_items`（`acquisition_type='trial'`） |
| `POST /items/:id/vote` | 购买/试用/作者才能投票；赞踩数、好评率实时写回商品 |
| `GET/POST /items/:id/comment`、`/comments` | 本地发布评论；线上评论第一次访问时抓一次落库，之后不再回源 |
| `GET /userItems`、`/myItems` | 我的选购 / 我的商品 |
| `POST /userItems/:id/enable|disable|trialConvert|trialAbandon` | 启用、停用、试用转购买、放弃试用 |
| `POST /equip`、`GET /session|character/:id/bindings`、`/session/:id/shareConfig` | 装备到会话 / 角色、分享配置 |
| `GET/POST /defaults`、`/defaults/set`、`DELETE /defaults/:type` | 全局默认装备 |
| `POST /items`、`PUT /items/:id`、`/delist`、`/relist` | 本地发布 / 修改 / 上下架（本地商品 id 从 900001 起） |
| `GET /seller/dashboard`、`/salesTrend`、`/itemPerformance` | 卖家看板（按本地销量与价格算收入） |
| `/tip`、`/report`、`/cliffhanger/unlock`、`/contextTrigger` | 本地成功桩（积分/举报等暂不外发） |

**源码说明**：开源商品同步时会把 `promptContent` 一起落库，控制台可查看 / 复制 / 下载；
非开源商品的正文线上本来就不返回（购买后由 `/prompt` 给出），本地同样按权限判断。

**从线上同步**：控制台「创意广场」页 →「从线上同步」，或
`POST /local/api/creative/sync {"limit":60}`：抓分类、标签、商品列表（默认 60 件）、
详情 / 开源源码，全部 upsert 进本地库。评论不放进批量同步（接口限流很紧，会把一次同步拖到几分钟），
而是**打开商品详情时按需抓一次**并落库。
列表读接口只在「本地一件都没有」或带 `refresh=true` 时才回源。

**控制台（仅管理员）**：

| 接口 | 内容 |
|---|---|
| `GET /local/api/creative?q=&open=1&limit=&offset=` | 商品列表 + 统计（商品 / 开源 / 有源码 / 评论 / 购买 / 我的创意） |
| `GET /local/api/creative/item?id=` | 单个商品完整字段（含源码） |
| `GET /local/api/creative/source?id=` | 源码正文（`text/plain`，可直接下载） |
| `GET /local/api/creative/export?scope=open|all` | 导出 JSON（开源或全部） |
| `POST /local/api/creative/sync` | 从线上同步 |

控制台页面：统计卡片、搜索、只看开源、同步、导出、商品表、源码面板（复制 / 下载）。

**离线**：`MISSAI_OFFLINE=1` 启动时完全不出网，列表 / 详情 / 评论 / 购买 / 投票全部读本地库。

## 账户设置与每日签到（本地）

设置页原 `/settings` 的签到日历曾经让整页变成「页面出了点问题」，根因不在账号接口，
而在**签到日历**：

- 页面按月调 `GET /api/gva/checkin/calendar?month=YYYY-MM`，前端把每个月回包的
  `data.days` 摊平成一个数组（`Promise.all(...).flatMap(e => e.data.days)`）
- 本地桩当时回 `{code:0,data:{}}` → `days` 是 `undefined` → 日历数组变成 `[undefined]`
- 渲染时 `s.map(e => [e.date, e])` 读 `undefined.date` → `TypeError`，被错误边界换成错误页

（设置页现由自建页 `/app/account` 承接，同一套接口。）签到与个人资料都由 `server.py` + `db.py` 本地处理：

| 接口 | 本地行为 |
|---|---|
| `GET /api/gva/checkin/reward-config` | 基础 100 分、周末 +50、连签 3/7/14/30 天奖励 500/1000/2000/9999 |
| `GET /api/gva/checkin/status` | `hasCheckedIn` / `currentConsecutiveDays` / `totalCheckinDays` / `lastCheckinDate`（连签只在「今天或昨天签过」时延续） |
| `GET /api/gva/checkin/calendar?month=YYYY-MM` | `{month, days:[{date,hasCheckin,points}]}`，**当月每一天都有一条**，不会再让设置页崩 |
| `POST /api/gva/checkin/sign` | 写 `checkin_records`，按基础 + 周末 + 连签奖励加积分；同一天重复请求幂等（`alreadyCheckedIn:true`，不重复加分） |
| `PUT /api/gva/user/setSelfInfo` | 昵称 / 头像写 `users.nick_name`、`users.header_img`（刷新后 bootstrap 直接读库） |
| `GET /api/gva/user/getUserInfo` | 回 `{userInfo: public_user}`（含昵称、头像、邮箱、签名、性别） |
| `POST /api/gva/user/changeMyPassword` | 校验旧密码 → 重设加盐哈希，回 `data.token`（当前会话仍有效，不用重登） |
| `GET /api/gva/oauth/*`、`email/getVerificationStatus` | 未绑 Google；邮箱状态由 `users.email` 推导（有邮箱即 `emailVerified:true`） |

头像上传走 `POST /api/uploads/file/*`，回包除原来的 `url/path/absPath` 外补了
**`data.file.url`**（设置页读的就是这个字段），再调 `setSelfInfo({headerImg})` 落到 `users.header_img`。

> 手机号和性别在设置页是**纯前端**逻辑（只写 `localStorage.userPhone` / `userGender`），
> 线上也没有对应接口，所以本地保持原样；昵称、头像、密码、邮箱状态才是走数据库的。

## 请求怎么走

**1. 静态部分（纯本地，不联网）**

| 页面里的引用 | 实际文件 |
|---|---|
| `/app/*` 页面 | `html/app/*.html`（10 页白名单，未知页 404；`/app/console` 要管理员） |
| `/app/{css,js,img,assets}/*` | `html/app/` 下同名路径 |
| `/js/*`、`/css/*` | `html/js/*`、`html/css/*`（css 会先查 `mirror_assets` 表） |
| `/fonts/*` | `html/assets/fonts/*`（本地缺该字体 → 快速 404 走回退字体，不回源挂 90s） |
| `/logo-goddess1.webp`、`/favicon.svg`、`/res.js` … | `html/assets/*`、`html/js/*`（`ROOT_FILES` 固定表） |
| `/image/*`、`/api/uploads/file/*` | **先查 `image.db`** 拿到相对路径并按项目根拼接，再读 `html/image/*` |
| `/cdn/*`、`/cdn2/*` | 首次抓取 → 落 `html/image/` + 写 `image.db`；之后全走本地 |
| `/_next/*`（线上页面残留引用） | **已无本地映射**，走反代兜底回源 |

**2. 本地接管（写 SQLite，不外发）**

| 接口 | 行为 |
|---|---|
| `POST /api/gva/base/*login`、`/api/gva/user/register_*` | 本地注册/登录，下发 `local_session` Cookie；请求带 `captchaId` 才校验数字验证码 |
| `GET /api/auth/session/bootstrap` | 读本地会话；**未登录 401**（对齐线上），已登录 200 `authenticated:true` |
| `GET /api/auth/session` | 同上但**未登录回 200** `{authenticated:false}`（前端 `store.js` 直接读字段） |
| `POST /api/auth/session/establish` | 用 `x-token` 换会话 |
| `POST /api/gva/base/captchaV3`、`/base/captcha` | **本地出题**：100 以内加减法，PNG 题图（`captchaMode:"digit"`、`captchaLength:3`） |
| `POST /api/gva/base/captchaV3/verify` | 本地判分（忽略前导零），不消费题目，可重复校验 |
| 邮件/短信类（`sendCode`、`resetPasswordByEmail` …） | 本地直接放行，不真的外发短信/邮件 |
| `POST/GET /api/uploads/file/*` | 图片写入 `html/image/`，同时登记 `image.db`（返回 `localPath` / `absPath`）与 `uploads` 表 |
| `/image/*` | 图片统一出口：查 `image.db` 拿相对路径 → 按项目根拼绝对路径 → 读文件；文件在但没登记会自动补登记 |
| `/api/gva/pointsAcc/*`、`pointsTrans/*`、`spiritStone/*`、`invitation/*`、`authorRebate/*`、`rechargeOrder/*`、`email/getVerificationStatus`、`narratium/content-preferences` | 已登录时本地回桩（`X-Local: stub`），结构与线上一致；积分余额读 `points:{用户}` |
| `/api/gva/checkin/status|calendar|sign|reward-config` | **每日签到**全部本地：状态 / 当月日历 / 签到加分 / 奖励配置（见「账户设置与签到」） |
| `/api/gva/user/getUserInfo`、`user/setSelfInfo`、`user/changeMyPassword` | **账户设置**：个人资料读写、改密码，全部落 `users` 表（见「账户设置与签到」） |
| `/api/gva/oauth/status`、`oauth/bindings`、`oauth/bind/factors`、`email/getVerificationStatus`、`email/getRewardConfig` | 账户页的绑定/邮箱状态由本地账号推导（未绑 Google → `google:false`，邮箱状态读 `users.email`） |
| `/local/`、`/local/api/*` | 控制台（`db` / `stats` / `schema` / `logs` / `users` / `sessions` / `favorites` / `chat` / `images` / `works` / `tags` / `content_apis` / `llm` / `image-models` / `templates` …）——**仅管理员**，`POST /local/api/login` 登录 |
| `POST/PUT/DELETE /api/gva/work*`、`works/my`、`work/creativeRefs`、`workFilter/config`、`llmModelRate/*`、`aiMedia/creator/catalog`、`fileUploadAndDownload/upload` | **创作页**整页读写本地库（见下节） |
| `narratiumWorldbookEntry/*` | 世界书条目本地增删改查（`data` 必须是数组、`ID` 必须是整数） |
| `narratium/chatSession/*`、`narratium/guestChat/chatStream` | **聊天界面**：会话、历史、消息、流式回复全部本地（见下节） |
| `GET narratiumCharacter/findNarratiumCharacterByCharacterID` | 本地新建的作品在内容库里没有 → 按 `my_works` 拼出 `data.character`（游客也能打开自己的作品） |

> 兜底：任何 `/api/*` 若线上返回 **401/403** 且本地会话有效，账户类降级成本地桩，
> 避免前端因「线上不认识本地会话」而连锁登出；
> 个性化发现流 `/api/gva/narratium/discovery/*` 则返回 `reasonCode: SURFACE_NOT_ENROLLED`，
> 让前端走它自带的热门榜兜底（回 `code:0,data:{}` 会让前端读 `items.length` 直接白屏）。

**3. 反向代理到线上（内容实时，回包顺手落内容库）**

- `/api/*` 剩余业务接口（榜单、角色详情、作品、聊天等）
- **内容接口**（`narratium/*`、`narratiumCharacter/*`、`creativePlaza/tags` 的 GET）
  回包时把文字与标签拆进 `works` / `work_tags` / `tag_catalog`，并记下形状与载荷 → 见「内容库」
- `/cdn/*` → `store.story6688.com`，`/cdn2/*` → `images.comfyai.site`
- 没有本地映射的页面与 chunk 自动回源（`/login`、`/_next/*` 等），响应里的跨域地址改写成本地路径
- 线上下发的 Cookie 去掉 `Domain`，会话能落在 `localhost`

**4. 缓存：交给浏览器，不进数据库**

- 可缓存前缀：`/api/gva/narratium/*`、`/api/app/download-config`、`/api/gva/creativePlaza` 等 GET
- 服务端只下发 `Cache-Control: private, max-age=300, stale-while-revalidate=600`，
  响应体留在浏览器 HTTP 缓存里，5 分钟内不回源
- 静态文件：HTML/JS/CSS `no-cache`（本地改脚本立刻生效），字体 7 天、图片 1 天
- 上游超时 / 5xx：**内容接口**先用内容库还原（`X-Local: db-fallback`），其余仍回 `502` + JSON 说明
- 本地回桩 → `X-Local: stub`；**走本地分派的接口一律 `no-store`**（不套 5 分钟规则，
  否则浏览器会拿 5 分钟前的线上回包盖住本地新数据）

## 创作页与聊天界面（读写本地库）

创作页 `/app/creation` 和聊天界面 `/app/chat` 的接口**全部在本地分派**
（`server.py` 里 `handle_local_data_api()`，命中就直接回包，不命中才继续反代）：

**创作页**

| 接口 | 行为 |
|---|---|
| `GET work/my?characterID=` | 查本地作品；**没有必须回 `data:null`**（`{}` 会被前端当成存在） |
| `POST /work`（新建）、`PUT work/content`（更新） | 整包写进 `my_works.payload`，回包必须带 `data.updatedAt`（字符串），前端靠它对账 |
| `POST work/publish`、`unpublish`、`schedule`、`schedule/cancel` | 改 `my_works.status`（`published` / `draft` / `scheduled`） |
| `GET works/my?page=&pageSize=&status=&keyword=` | 我的作品列表（`data.list` + `records` + 整数 `total`） |
| `GET/PUT work/creativeRefs` | 创意广场引用；没存过回 `data:null` |
| `GET workFilter/config`、`aiMedia/creator/catalog` | 配置（`config_cache` 表种子） |
| `llmModelRate/getModelRateList`、`getModelStatusList` | 模型列表 —— **数据来自「模型接入」**，见下一章 |
| `POST fileUploadAndDownload/upload` | multipart 封面 → 落 `html/image/` + 登记 `image.db`，回 `data.file.url` |
| 世界书 `narratiumWorldbookEntry/*` | `getEntriesByCharacterID` 回**数组**；create/update 回 `data` 里带整数 `ID`（前端记作 `primaryKeyId`）；普通条目以 `worldbookID` 为主键、自定义条目以 `id` 为主键 |

保存是**前端先查后写**：`GET work/my` 判存在 → 不存在 `POST /work`，存在 `PUT work/content`。
封面是必填（前端 `validateSaveFields` 第一条就是 `coverImageURL || coverImageFile`），
所以浏览器里测保存要先选封面并「确认裁剪」，否则只会弹「保存失败 / 请上传封面图片」，一个请求都不会发。

**聊天界面**（`narratium/chatSession/*`，除 `resolve` / `state` / `partialContent` 是 GET）

| 接口 | 行为 |
|---|---|
| `list` / `getList` | 会话列表（`getList` 走 query 参数）；**空列表时自动建一个本地会话**，否则前端卡在「正在打开对话…」 |
| `messagesOptimized` / `messages` | 会话消息（`data.messages` + `hasMore`），按 `sessionId` 读 `chat_messages`；**过期 sessionId 兜底**：会话行不存在时回该用户最近一个会话的消息（`X-Local: chat-messages-fallback`），避免前端拿到 0 条消息切到「角色正文」全屏沙箱（聊天区变白色大块 + 左上角「正文」） |
| `update` / `updateTitle` | 改会话标题（历史对话里重命名），返回 `_thread_view` 形状 |
| `deleteMessage` / `editMessage` | 删一条 / 改一条消息，作用域 `thread_id IN (SELECT id FROM chat_threads WHERE user_id IS ?)`；删除后重算 `message_count` |
| `delete` | 删整个会话（连带 `chat_messages`） |
| `summaries` / `summary` | 记忆摘要**不在前端同步**：恒回空列表 / `summary:""`；滚动摘要由生成管线内部维护在 `chat_summary_state`，每轮按槽位注入，不再单独下发给页面 |
| `stopStatus` / `resumeStream` | 本地没有在跑的流：恒 `status:"idle"`、`synced:true` |
| `chatSync` | **输入栏切到「非流式」走这里**：真调模型，回 `{"assistantMessageId":…,"userMessageId":…,"message":…,"pointsConsumed":0,"stoppedByUser":false}`（原来和续传一起当 idle 桩，前端直接显示 `[ERROR]非流式聊天失败，请稍后重试。`） |
| `chatStream` | **真流式** `text/event-stream`：`{"type":"content"}` 逐段 → `{"type":"usage"}` → `{"type":"done", assistantMessageId, userMessageId, upstreamUsage}` → `data: [DONE]`；`assistantMessageId` 是数字，用户这条与完整回复都落库 |
| `guestChat/chatStream` | 试玩流走另一套协议：单帧 `{"type":"message","content":完整回复}` + `guest_info_updated` + `done`（前端只认 `message` 这个类型） |
| `claimGuest` | 回 `status:"ready"`、`sessionId` 是**会话行 id 的十进制字符串**、`sourceSessionId` / `identityScope` 是 UUID 形状（`identityScope ≥ 16` 字符） |
| `userHistory` | 游玩历史（`data.list` + 整数 `data.total`） |
| `resolve` / `state` / `partialContent` | 已解析 / 永远 `idle` / 空内容（处理中才需要续传，本地恒空闲） |
| `modelConfig/getByModelRate`、`saveByModelRate` | **对话设置的模型参数**（温度 / Top_P / 惩罚 / 最大长度 / 上下文窗口）；按 `modelConfig:{用户}:{modelRateId}` 存 `config_cache`，默认 `0.7 / 0.8 / 0 / 0 / 8192 / 8192` |
| `promptConfig/getByCharacter`、`saveByCharacter` | **提示词与记忆参数**：锚点（enabled / depth / frequency / text）+ 记忆权重与检索配置；按 `promptEngine:{用户}:{characterID}` 存 `config_cache` |
| `embeddingConfig/get`、`save`、`backfill` | **检索向量配置**：OpenAI 兼容 `base_url` / `api_key` / `model` / `dimensions` / 批量与超时；用户级优先、空则回落全局 `embeddingConfig:0`，`backfill` 后台补齐向量 |
| `summaryConfig/getByCharacter`、`saveByCharacter` | **摘要参数**：开关 / 注入方式 / 保留轮数；按 `summaryConfig:{用户}:{characterID}` 存 `config_cache` |

> **模型参数面板只管「正文那一轮」**：面板保存的 `temperature / topP / 惩罚 / maxTokens`
> 只作用于用户对话的正文请求。后台三次调用仍是写死的参数，不受面板影响——
> 格式补全（temperature 0.3 / max_tokens 300）、滚动摘要（max_tokens 900）、
> 结构化记忆抽取（temperature 0.2 / max_tokens 600）。
>
> **下发规则**：未保存过配置时也按面板默认值真正下发（0.7 / 0.8 / 0 / 0），
> 不再把 top_p / 惩罚交给上游默认值。`maxTokens` 的实际下发值为
> `min(设定值, 上下文窗口 − 输入估算 − 64)`：输出预算最多占窗口 3/4（输入至少留 1/4，
> 且不低于 512 token），所以「最大回复长度」调到窗口上限也不会挤掉输入、更不会关闭预裁剪。
> 面板会显示上一轮真实下发值；被压低时后端日志另打一行 `[model] max_tokens 被上下文压低`。
>
> **保存校验**：数字越界钳制（温度 / Top-P / 惩罚 0–1，maxTokens 256–8192，
> contextWindow 1024–32768）；非数字 / NaN / 无穷直接拒绝（`code:400`），GET 回填也钳制。
>
> **参数被上游拒绝（400）**：按顺序摘参重试——先摘 `presence_penalty / frequency_penalty`，
> 再摘 `temperature / top_p`，最后 `max_tokens` 改名 `max_completion_tokens`；每组只摘一次，
> 全部失败按原错误返回（正常触发退款）。

> 这两组 GET 原本会被反代到线上 → 线上不认本地会话回 401 → 降级成 `data:{}`，
> 前端拿到 `{}` 后直接 `value.toFixed()`，**「对话设置」一打开就报
> `Cannot read properties of undefined (reading 'toFixed')`**。现在由 `Handler._local_prefs()`
> 本地回完整字段（前端读哪些字段一个都不能少），保存也真的落库，刷新页面设置还在。

- `sessionId` 一律用 `chat_threads.id` 的十进制字符串，前端 `parseInt(sessionId)` 才能过；
- 所有聊天查询用 `user_id IS ?`（NULL 安全），**游客没登录也能聊**；
- 角色名/头像先查内容库 `works`，查不到再查 `my_works`（本地刚建的作品也能在历史里显示真名）；
- AI 回复走**角色扮演管线** `roleplay_core`（见下「聊天生成管线」）：优先按所选/默认模型
  走 OpenAI 协议（见下一章「模型接入」），没配模型或调用失败才落到本地生成，
  刷新页面依然能读回。

**聊天生成管线（`roleplay_core/`）**

`server.py` 只做 HTTP 编排：把一次对话封装成 `ChatRequest`（session / character / user_input /
AICG 开关 / 来源 `src` / 是否允许本地兜底）后交给 `ChatRouter(ports).chat_stream()`；
数据库、模型客户端、SSE 通道、本地兜底全部通过 `ChatPorts` 端口注入，核心层**不 import
server**（避免循环依赖）。一次对话按固定顺序走：

1. **载入上下文**（`load_context` → `_chat_context`）：组 `ChatContext`（角色整包、历史、
   用户输入、AICG 开关、当前状态数值、平台数值规则 `stat_rules`/`stat_meta`、
   角色卡属性 schema `stats_schema`）。
2. **拼 messages**（`ChatRouter.build_messages` → `roleplay_core/prompt.py`，唯一引擎）：
   - **分区装配**：persona / identity / character_extra / examples / worldbook_before /
     memory / status / worldbook_after / format 各成一段 system，每段有预算比例与优先级；
   - system **前缀每轮固定**（人设 → 身份/世界观/风格 → CG 目录 → 剧情选项 → 破甲词 →
     强制格式规范），同一角色多轮稳定、可命中上游 prefix cache；
   - **token 估算**：`prompt_builder.count_tokens` 粗估（CJK 1.5 / 其他 0.25 / 每条 4），
     按 `host::model` 用上游 `usage` 做 EMA 自校准，预算 = `contextWindow - 输出预算 - 64`；
   - **世界书**：按关键词/常量匹配，`priority` 决定预算淘汰顺序、`order` 只决定注入位置
     （固定前缀 / 历史之后 / 历史内部 depth），`ignore_on_max_context` 直接降到最低优先级；
   - **记忆**：四槽 `promise` / `user_info` / `long_term` / `short_term`（各自预算与深度）+
     `memory_engine` 混合检索（FTS5 优先，失败回落纯 Python BM25；向量用特征哈希 384 维兜底，
     配了 OpenAI embedding 就换到对应模型空间），按重要度 / 时效 / 余弦 / MMR 重排后注入；
   - **状态面板**（当前数值 + 档位表现 + 状态变化规则）与当前用户消息末尾的**格式提醒**
     （离生成最近，防漏状态栏/选项）保持独立分段。
3. **三层压缩**（超预算时逐层退让）：
   - **发送前确定性裁剪**（`compress_for_budget`）：按 世界书前置 → 后置 → depth → few-shot →
     最旧历史 的顺序丢弃，最近 `COMPRESS_KEEP_ROUNDS=6` 轮（12 条）原文永不压，上限 24 轮；
   - **异步压缩**：被裁掉的历史进单线程后台队列（按 `(uid, sid, cid)` 去重），由默认模型并进
     `chat_summary_state.rolling_summary` / 记忆槽与算法记忆，**下一轮生效**；
   - **同步兜底**：上游仍报 `context_overflow` 时只重试一次；摘要成功但没减量就补
     `shrink_for_overflow(level=3, keep=12)`，失败则回退确定性裁剪，均不阻断本轮回复。
   每次压缩都记 `before / after / 区间 / 摘要 id`（`request_log` + `chat_prompt_log`）。
4. **LLM 流式产出**（`stream_llm`）：OpenAI `stream:true`，逐行解析
   `choices[0].delta.content`，前端边收边渲染；上游无视 `stream` 直接回整包也照样能解析。
5. **攒帧下发**：按 **30 字 / 150ms** 攒够再发一帧 `content`（上游常 1~5 字推一条，
   直接转发会让前端每秒重绘几十次）；状态栏一闭合就单独推 `status_update` 帧
   （前端只更新胶囊、不重绘正文）。
6. **收尾清洗**：`stream_hold_start()` 先刷掉攒住的半行 → `wrap_next_prompts()` +
   `normalize_say_blocks()` 统一改写；模型空回复且不允许兜底 → 报错收尾、不落库；
   漏状态栏/选项由 `ensure_format` 再补一次 LLM 调用（用户中断不补）。
7. **后处理成展示态**（`process_output`）：`<image>ID</image>` → 真实 URL（编造 ID 抹掉）、
   没点图按权重兜底补一张、图片统一置顶；`<book_status>` → status 数组、`<next_prompt>`
   → options；`display_text` 去标签（前端用结构化数据重建 DOM），`tagged_text` 保留标签落库。
8. **落库**（`save_assistant`）：`tagged_text` 写 `content` 列（渲染用）、`clean_for_storage()`
   清洗成纯文本写 `content_llm` 列（下一轮拼 prompt 直接读它，不再现算）；有淘汰历史时
   `_enqueue_compress` 投递后台压缩任务。

**流式回滚**：已下发不足 `STREAM_ROLLBACK_CHARS=2000` 字、且错误标了 `retryable` 时，
`ChatRouter` 先发 `clear_content` 清屏、重置缓冲并重开上游一次；超过 2000 字只保留已发部分，
绝不中途换成本地回复。

**收尾改写重画**：若改写越过已下发的半截内容（`display_text` 不以已发部分开头），
`ChatRouter` 先发 `clear_content` 让前端清屏、再按最终文本重画，避免
「正文先吐一遍、按钮再来一遍」。

**自动生成选项（`<next_prompt>`）**：前端只认 `<next_prompt>…</next_prompt>` ——
里面的每一行变成「选择你的回复」按钮，标签内容从正文里隐藏。
模型却经常直接把 `A. / B. / C. / D.` 的 markdown 列表写进正文，
按钮区就只剩默认的「请根据已经发生的故事继续推进剧情」。`wrap_next_prompts()` 在
**落库前**统一改写：

- 只认**结尾**那段连续选项（`A.`/`A、`/`**A. xxx**`/`- **A. xxx**`/`1.`），
  后面跟着的「请做出你的选择：」这类引导语会一起并进标签并从正文删掉；
- 标号必须从 `A` 或 `1` 开始连续（`A,B,C,D` / `1,2,3`），否则当成正文里的普通列表不动；
- 已经有 `<next_prompt>` 的原样返回（不重复包）；
- 状态栏排在选项前/后都先整块摘出、最后按原顺序放回 —— 否则「尾部选项块」会被状态栏
  挡住，永远包不进 `<next_prompt>`。

历史消息不用迁移：前端加载 `messagesOptimized` 时会从 `content` 重新算 `nextPrompts`。
流式下发的边界由 `stream_hold_start()` 控制 —— 最后一行（可能是半个词）和尾部选项块
先攒住，等整段成形再改写补发，所以不会出现「正文先吐一遍选项、按钮再来一遍」。

**刷新后聊天区变白色大块（左上角「正文」）的根因**：
历史检查结束后如果 `messageLength === 0`，前端会渲染完全体沙箱 iframe 显示
`character.description` —— iframe 白底 + 角色正文文本。消息数变 0 只有两条来源：
① `chatSession/clearUserHistory` 把该用户名下**所有**会话删光，
浏览器 sessionStorage `missai_profile_last_session_v1`（10 分钟 TTL）里的 sessionId 失效 →
`chat_messages` 查不到行回空（**已修**：只在会话行不存在时兜底回最近一个会话，
自测里也不再调 `clearUserHistory`，改用 `deleteUserHistory{characterId}`）；
② 未接住的 chatSession 子路由本地分派 `read_body()` 读走请求体，再落回反代二次
`read_body()` 卡 90 秒或 500（**已修**：`read_body()` 结果缓存可重复调用 +
`update`/`updateTitle`/`deleteMessage`/`editMessage`/`delete`/`getList`/`messages`/
`summaries`/`summary`/`stopStatus`/`resumeStream`/`chatSync` 全部本地化）。
> 加新接口时记住：**请求体只能读一次**，本地分派没接住就落到反代，
> 第二次 read 会挂死整条连接（浏览器转圈、后续请求排队）。

## 模型接入（OpenAI 协议：api + key 入库，聊天真调模型）

控制台左侧「图片库」下面是**「模型接入」**栏目（`/local/` → `view-llm`，仅管理员）。
它负责填 **API 地址 + apikey**、存库、连上去检测模型并显示；聊天界面「选择模型」的列表
与聊天回复都由它驱动。

**数据库只存最基本的两项** —— 表 `llm_providers`：

| 列 | 含义 |
|---|---|
| `kind` | 预设键：`openai` / `dashscope` / `deepseek` / `volcengine` / `custom` |
| `name` | 接入名（如「我的中转站」） |
| `base_url`、`api_key` | **就这两样是手填的** |
| `models` | 检测回来的模型列表缓存 `[{"model":…,"rateId":int}]` |
| `status` / `last_error` / `last_check_at` | 检测结果（`ok` / `error`）与最后一次原因 |
| `is_default` / `default_model` | 聊天默认用哪条接入、哪个模型 |
| `enabled` | 关掉就不进模型列表、也不参与聊天 |

**请求怎么走**

- `llm.py`（`httpx` 同步传输）只有两条请求，全按 OpenAI 官方协议：
  `GET {base_url}/models`（检测 + 拉模型）、`POST {base_url}/chat/completions`（对话）。
  地址没写 `/v1` 而上游 404 时自动补一次 `/v1` 重试；连不上/上游报错统一抛 `LLMError`，
  原文直接显示在控制台。
- **三段超时**（流式与非流式同一套语义）：
  | 阶段 | 上限 | 说明 |
  |---|---|---|
  | 连接 | `CONNECT_TIMEOUT=10s` | 含连接池等待（`pool`）与写请求超时 |
  | 首字节 | `FIRST_BYTE_TIMEOUT=30s` / `FIRST_BYTE_TIMEOUT_STREAM=60s` | 从发出请求到收到首个响应体字节/首个 SSE 帧；由 watchdog 关闭响应解除阻塞读 |
  | 空闲 | `IDLE_TIMEOUT=120s` | 首字节之后，任意两个数据块之间 |
- 错误分类保持不变：`LLMError(status, code, retryable, retry_after)`，HTTP 4xx/5xx 读上游
  错误详情并识别 `context_overflow`、解析 `Retry-After`（封顶 30s）；流内 `ReadError` 记
  `stream_broken`，首帧后读超时记 `idle_timeout`。首帧前按状态码/超时重试，总尝试
  `MAX_ATTEMPTS=4`（1/2/4s 退避 + 抖动），已吐内容后不再重试。
- SSE 按空行分帧、多行 `data:` 拼接、忽略注释心跳，`[DONE]` 正常收尾。
- 控制台接口（仅管理员，回包**不带明文密钥**，只有 `apiKeySet` / `apiKeyMasked`，
  表单里密钥留空表示不改）：

  | 接口 | 作用 |
  |---|---|
  | `GET /local/api/llm` | 预设 + 已保存接入 + 当前模型列表 |
  | `POST /local/api/llm` | 只保存 |
  | `POST /local/api/llm/check` | **保存并连上去检测**（成功回「检测到 N 个模型」） |
  | `POST /local/api/llm/select` | 设默认接入 / 默认模型 |
  | `POST /local/api/llm/delete` | 删接入（模型缓存一起清） |

**模型列表 = 中转站返回几条就显示几条**

`llmModelRate/getModelRateList`、`getModelStatusList` 一律走 `db.llm_rate_models()`：
有接入模型就逐条列出（`rateId = provider_id*1000+1` 起，重新检测时**已有模型沿用旧 rateId**，
聊天里选过的模型不会漂），一条都没接时才回本地兜底 `local-default` 那一条。
聊天页的模型切换（`/app/chat`）读的也是这份列表（`GET /api/chat/models`，登录即可）。

| 字段 | 组件怎么用 |
|---|---|
| `ID` | 选中后聊天传的 `modelRateId`，必须是正整数 |
| `poolName`、`modelName` | 行标题与「当前模型：」，**都放模型 id** |
| `description` | 行副标题，放接入名 |
| `modelName`（分组推断） | 组件按模型名关键词分组：含 `gpt`→OpenAI、含 `qwen`→Qwen… |

**聊天**（`ChatRouter` 管线，见上一章「聊天生成管线」）

- 传了 `modelRateId`：`db.resolve_rate_id()` 找到接入 → 按该模型调 OpenAI 接口；
  `=1` 或查不到 → 本地兜底回复；
- 没传 → 用默认接入（默认那条还没检测出模型就顺延到下一条可用的）；
- 调用失败由 `_decide_stream_error` 决定：已吐过内容 → 保留已发部分、**绝不中途换本地**；
  允许回退（`fb`）→ 降级成本地兜底，对话不会断；否则 `fatal` 报错收尾、不落库，
  提示可在控制台「模型接入」开启超时回退；
- messages 由 `roleplay_core/prompt.py` 组装：分区 system 前缀（人设/世界观/风格/CG/选项/格式）+
  **预算内历史**（token 粗估 + EMA 校准，最近 6 轮原文不动）+ 世界书 + 记忆四槽 + 状态面板 +
  当前用户消息；
- 日志 `request_log.note` 会记 `chat-llm:模型名` / `chat-llm-failed` / `chat-local`，便于回查。

**日后加平台预设**：`db.LLM_PRESETS` 加一行即可（协议都是 OpenAI 兼容），
`openai` 官方、`dashscope` 阿里百炼、`deepseek`、`volcengine` 火山方舟的地址已占位。

`selftest.py` 的「模型接入」段会**自己起一个标准库 OpenAI 桩**（随机端口，桩代码在
`tests/stub_llm.py`），覆盖：预设齐全、连不上明确报错、检测出 3 个模型、回包无明文密钥、
模型列表条数与字段契约、按选中模型聊天、本地兜底、不指定模型用默认接入、设默认模型、
删完回落、跑前接入原样恢复。

## AI 绘图（/app/draw：绘图页 + 绘图接口，数据全落本地）

绘图页是自建页 `html/app/draw.html` + `js/pages/draw.js`（旧的 `/ai-gallery`、
`/ai-creator`、`/ai-history` 镜像页已删除，统一 302 到这里）：

- **配方卡片**：每个模型一张卡，显示接入名、可选尺寸数，未接模型时显示「接入真实绘图模型」
- **提示词构建器**：画质 / 风格 / 光照 / 镜头四组常用词，点选后**追加**到提示词末尾
- 提示词（带清空）、尺寸、数量、**种子**、反向提示词；右上角显示本地**积分余额**（本地生成免费）
- 作品墙：最新 / 浏览排行 / 我的作品；卡片可**生成同款 / 点赞 / 删除**（生成、点赞、删除需登录）
- 「我的图库」= 我的作品列表

路由分工（均在九段路由里）：

| 路径 | 落地 | 说明 |
|---|---|---|
| `/app/draw` | `html/app/draw.html` | 绘图页（需本地登录才可生成） |
| `/ai-gallery`、`/ai-creator`、`/ai-history` | **302** → `/app/draw` | 旧地址保留可用 |
| `/api/local/gallery/*` | 本地接口（第 9 段） | 作品列表 / 生成 / 点赞 / 删除 |

**两张表**

| 表 | 作用 |
|---|---|
| `image_providers` | 绘图接入：`kind`（预设）、`name`、`base_url`、`api_key`、`models` 缓存、`sizes`、`is_default` / `default_model`、检测状态 |
| `image_works` | 绘图作品：`prompt` / `negative` / `provider` / `model` / `size` / `seed` / `url_key`（`/image/xxx.png`）/ `likes` / `author` / `image_id` |

**协议（`imagegen.py`，纯标准库）**

- 检测：`GET {base_url}/models` —— 拉回全部模型后按关键词挑出绘图类
  （`dall-e` / `gpt-image` / `wanx` / `seedream` / `flux` / `kolors` / `stable-diffusion` …）；
  一个都没匹配上就全部列出，交给用户自己选。
- 生成：`POST {base_url}/images/generations`，`response_format=b64_json`；
  回包兼容 `data[].b64_json` 与 `data[].url`（URL 会自动下载）。地址没写 `/v1` 而上游 404 时自动补一次。
- 图片落 `html/image/`，同时登记进图片库 `image.db`（`source=ai`），作品元数据写 `image_works`。

**没接模型也能跑**：`db.IMAGE_PRESETS` 里的 `local` 预设（`local://placeholder`）不联网，
按「提示词 + 种子」哈希用 zlib 直接写一张深色渐变 PNG，用来把「生成 → 落盘 → 入库 → 广场展示」整条链路先跑通；
同一提示词换个种子就能得到不同配色，适合验证「生成同款」。真实接入若上游不认 `seed`（HTTP 400），
会自动去掉该参数重试一次，不会因为种子把生成打挂。

**接口**

| 接口 | 作用 |
|---|---|
| `GET /api/local/gallery/works?sort=new\|hot\|mine&limit=&offset=` | 作品列表（公开；`mine` 需登录，只回自己的） |
| `GET /api/local/gallery/models` | 绘图模型下拉（公开） |
| `POST /api/local/gallery/generate` | 生成（**需本地登录**）：`prompt` / `negative` / `rateId` / `size` / `n` / `seed` |
| `POST /api/local/gallery/like` | 点赞 +1 |
| `POST /api/local/gallery/delete` | 删除（管理员可删任意，普通用户只删自己的） |

**控制台「绘图模型」**（`/local/` → `view-draw`，仅管理员；与聊天「模型接入」完全分开）

| 接口 | 作用 |
|---|---|
| `GET /local/api/image-models` | 预设 + 已保存接入 + 绘图模型 + 最近作品 |
| `POST /local/api/image-models` | 只保存 |
| `POST /local/api/image-models/check` | **保存并检测**（`GET /models`） |
| `POST /local/api/image-models/select` | 设默认接入 / 默认模型 |
| `POST /local/api/image-models/delete` | 删接入（模型缓存一起清） |
| `POST /local/api/image/works/delete` | 删作品记录（图片文件保留在图片库） |

### 模板接入（行业通用 JSON，`tplimport.py` + `mediagen.py`）

绘图配方目录来自 `GET /api/gva/aiMedia/creator/catalog`，内置配方放在
**`data/json/` 目录**里（`mediagen.TEMPLATE_DIR`，每个分类一份，按文件名排序加载）：

| 文件 | 内容 |
|---|---|
| `data/json/ai-draw-illustration.json` | 插画立绘（国风插画 / 二次元立绘 / 水彩绘本） |
| `data/json/ai-draw-photoreal.json` | 写实影像（人像摄影 / 场景写实） |
| `data/json/ai-draw-design.json` | 设计物料（海报封面 / 头像生成） |

- 目录里的 `*.json` 都算内置模板：**再放一个文件就自动收录**，点「恢复内置」或重启即生效
- 多份文件合并时：同名分类合并，同一个 `recipeKey` 以**文件名排序靠后**的为准；
  分类顺序看分类里的 `sortOrder`（没写就按文件出现顺序）
- 「恢复内置」直接扫这个目录重建，提示里会列出到底读了哪几个文件，
  文件坏掉会单独标出来（不会因为一个文件坏了就整页空）
- 控制台「数据库」页的「扫描 json/」会跳过这些模板文件（它们不是数据表），
  扫描结果里显示为「跳过 N 个模板文件」

控制台「绘图模型」页底部的
**「模板接入 · 行业通用 JSON」** 卡片可以把别人做好的模板直接导进来：
**粘贴 / 选文件 / 给链接**三种入口，合并（按 `recipeKey` 覆盖）或整包覆盖都行，
旁边还有「示例」「预览适配」「导出」可以取样本、试跑、把自己的模板发给别人。

导入前会按 ai-creator 前端 `normalizeCatalog` 的规则先裁一遍（key 正则、select 必须有选项、
分类 ≤ 20 / 配方 ≤ 100、积分 ≥ 0…），并给出报告：识别到的格式、新增 / 覆盖 / 跳过的条目、
以及每一处降级的原因（比如 `select` 没写选项被降级成文本框）。

认得的格式（自动探测）：

- `missai.template/v1` —— 本机原生，`categories[].templates[]`：

```json
{
  "schema": "missai.template/v1",
  "categories": [{
    "key": "portrait",
    "name": "人像写真",
    "templates": [{
      "id": "portrait-idphoto",
      "title": "证件照生成",
      "category": "人像写真",
      "model": "gpt-image-1",
      "params": {"aspectRatio": "2:3"},
      "prompt": {
        "template": "{subject}，{background}背景，{style}，正面免冠，高清细节",
        "negative": "低质量, 模糊, 变形, 多余手指"
      },
      "inputs": [
        {"key": "subject", "label": "人物描述", "type": "text", "required": true},
        {"key": "background", "label": "背景色", "type": "select",
         "options": [{"value": "白色"}, {"value": "蓝色"}, {"value": "红色"}]},
        {"key": "style", "label": "风格", "type": "select",
         "options": [{"value": "影棚布光"}, {"value": "自然光"}]}
      ],
      "price": {"points": 0}
    }]
  }]
}
```

  提示词里的 `{字段key}` 就是占位符，生成时用用户填的值替换（`mediagen.build_prompt`）；
  `select` 取选项文字，其余取原值，没填的占位符连同旁边多余的逗号一起去掉。
  上面这份模板导入后：key=`portrait-idphoto`、分类 key=`portrait`、
  三个字段、尺寸 `1024x1536`，生成出来的提示词是
  「亚洲女性，蓝色背景，影棚布光，正面免冠，高清细节」。
- 其它常见形状：`{"categories": […], "templates|items|recipes|list": […]}`、裸数组、单个模板对象；
- 字段/分类/模板的花式命名（`title`/`caption`、`inputs`/`fields`/`form`、`options`/`enum`/`choices`、
  `1:1` 比例、`width`+`height`、`negative_prompt`…）按别名表映射；
- SD WebUI / A1111 参数片段：提取正/负向提示词与尺寸；
- ComfyUI 工作流：API 格式按 `class_type` 找 `CLIPTextEncode`（`_meta.title` 含「负 / negative」算负向）
  和 `CheckpointLoader` / `UNETLoader` 的模型名；界面格式（`nodes`/`links`）会提示改用 API 格式导出。

| 接口 | 作用 |
|---|---|
| `GET /local/api/templates` | 分类 + 扁平模板列表（含 `origin`：`builtin` 内置 / `imported` 导入）、`templateFile` |
| `POST /local/api/templates` | 导入。体里给 `json` / `text` / `url` / `catalog` 任一，`mode=merge`（默认，按 key 覆盖）或 `replace`（整包替换） |
| `POST /local/api/templates/preview` | 只适配不落库，先看会被裁成什么样 |
| `GET /local/api/templates/export` | 导出当前全部模板（`missai.template/v1`，原样发给别人就能导入） |
| `GET /local/api/templates/sample` | 一份可直接导入的示例 |
| `POST /local/api/templates/delete` | 删单个模板 `{"recipeKey": "…"}`，空分类顺带删掉 |
| `GET /local/api/templates` 返回的 `templateDir` / `templateFiles` | 内置模板目录（`data/json`）与每个文件对应的分类、模板数、读取错误 |

本地做不了的：`outputMediaType=video` 统一按图片处理；`required` 的 `mediaSlot`（垫图）暂不支持，
导入时记一条警告；模板写了积分要求会被置 0（本地生成不扣积分）。

**前端产物补丁（`patch_client_js()`，启动时执行、幂等）**

生成结果的 `assetUrl` 在前端只有「https + 线上白名单域名」算合法，本地产出的 `/image/xxx.png`
会被判成「生成任务数据不完整」，轮询连续失败 3 次后页面报「暂时无法查询生成进度，任务仍在后台处理中。」。
`server.py` 启动时把 `html/js` 里那处严格校验换成同一模块导出的宽松版（放行 `/` 开头的相对路径），
内容没变就不写盘，重启多少次都安全。

**日后加绘图模型**：能跑 `GET /models` + `POST /images/generations` 的服务，
控制台选「自定义 OpenAI 兼容」填地址与密钥即可；常见平台要当预设，
往 `db.IMAGE_PRESETS` 加一行（`label` / `base_url` / `models` / `sizes` / `hint`）。
绘图模型的 `rateId` 从 `IMAGE_RATE_BASE = 500000` 起编号，和聊天模型不撞号；
重新检测时已有模型沿用旧 `rateId`，作品里的模型不会漂。

`selftest.py` 的「AI 绘图」段会自起一个绘图桩（`/v1/models` + `/v1/images/generations`），
覆盖：绘图接口 200、列表公开可读、接口仅管理员、未登录生成 401、本地占位生成入库、
种子随作品入库、点赞与排行、我的作品只回当前用户、图片可访问且登记进图片库、
只保留绘图类模型、真实接口生成、删除后回落本地、跑前接入与作品原样恢复。

## 页面注入的脚本（回包时插，不改磁盘 HTML）

| 脚本 | 注入给谁 | 作用 |
|---|---|---|
| `html/js/no-feed-cache.js` | 所有人 | 关掉原站首页 feed 的 localStorage 快照缓存（不带 defer，在站点 chunk 执行前截断读写），页面直读本地库 |
| `html/js/hide-ads.js` | 所有人 | 隐藏登录后的促销广告轮播条：插一条 CSS（`display:none !important`）+ 按促销文案给节点打 `data-local-ad`，**不删 DOM**，不和 React 抢节点 |
| `html/js/fault-tolerance.js` | 所有人 | 内容接口 502/失败时改写为空成功，页面渲染空态而非无限加载 |
| `html/js/empty-cleaner.js` | 所有人 | 登录态空数据区块（如热力橱窗）骨架占位改为「暂无内容」空态 |
| `html/js/publish-jump.js` | 所有人 | 创作页发布成功后自动跳回作品列表刷新（只在 `/creation` 编辑页触发） |
| `html/js/console-entry.js` | **仅管理员** | 在左侧导航栏加「控制台」入口（`data-local-console-entry`），点开进 `/local/` |

> 早期的 `gallery-link.js`（把侧边栏「AI绘图」改指本地）**已删除、不再注入** ——
> 旧镜像页下线后侧栏入口保持原站链接，由 302 表接管。

`selftest.py` 会检查：未登录/普通用户页面不含 `console-entry.js`、管理员页面含且只含一次、
磁盘上的 HTML 没有被写进这些脚本。

## 怎么登录

**主入口：页面右上角「登录 / 注册」弹窗**（自建页 `html/app/js/core/ui.js` 的 `loginDialog`）：

- 登录 → `POST /api/gva/base/login`（`userName` + `passWord`，**不带验证码也能过** —— 服务端只在请求带 `captchaId` 时才判分）
- 注册 → `POST /api/gva/user/register_v2`（本地直连，不经过线上 `/login` 页）
- 成功下发 `local_session` Cookie（HttpOnly，30 天）

**备用：原站登录页** <http://localhost:4173/login>（反代线上页）：

1. 页面会向 `/api/gva/base/captchaV3` 要验证码 → 本地返回 **100 以内的加减法** PNG 题图，
   例如 `93 - 72 = ?`，答案 `21`
2. 输入答案 → 前端带 `captcha` / `captchaId` 提交 `/api/gva/base/login` → 本地校验通过即登录
3. 题目存在内存里（TTL 10 分钟），`captchaV3/verify` 判分不消耗题目，可重复提交

> 线上登录页的输入框硬编码了「6 位数字验证码」校验，而本地出题答案是 1~3 位 ——
> 早期在本地 4 个 chunk 里放宽过校验，这些 chunk 已随旧镜像删除（现走反代回源）。
> 因此**验证码表单可能过不了前端校验**，日常登录请用右上角弹窗；验证码接口本身
> （出题 / 判分 / 带 `captchaId` 的登录）仍然完全可用，`selftest.py` 就在直接调它们。

> **默认管理员账号**：`admin` / `admin123456`（服务启动时自动写入，`users.role = admin`）。
> - 控制台「账号 / 会话」页的内嵌登录表单用它（`/api/gva/base/email/passwordLogin`，不需验证码）
> - 控制台账号表里带「角色」列，管理员显示为紫色「管理员」
> - **首次启动后改过的密码不会再被覆盖**（`meta.admin_seeded` 打过标记就只保证账号存在）
> - 想恢复默认密码：删掉 `data/missai.db` 里 `meta` 表的 `admin_seeded` 行再重启

或者直接调接口注册/登录：

```bash
curl -X POST http://localhost:4173/api/gva/user/register_and_login \
  -H "Content-Type: application/json" \
  -d '{"userName":"me","passWord":"123456","nickName":"我"}'
```

已有账号：控制台「账号 / 会话」页可查（含内嵌登录表单）；
默认管理员 `admin` / `admin123456`，自测账号 `tester_25551` / `secret123`。

## 重建镜像

```bash
# 1) 静态镜像：抓取首页 HTML + 全部静态资源，覆盖 html/（含 html/js、html/css）
python tools/mirror.py

# 2) 手动镜像落库：按数据面分页抓原站接口，回包拆进内容库（进度写 config_cache）
python tools/mirror.py mirror works 60 [账号] [密码]
python tools/mirror.py sync 60 [账号] [密码]      # 创意广场同步
```

- 控制台里的「镜像」按钮用的就是第 2 种：`server.py` 以**子进程**拉起 `tools/mirror.py`
  （两者完全解耦，删掉 mirror.py 不影响服务本身），进度由它写库、本进程只读库。
- `mirror.py` 需要访问 `missai01.com`；`server.py` 运行时也需要联网（接口反代）。
  断网时静态页面照常可用；**内容接口（文字与标签）用内容库兜底**（`X-Local: db-fallback`），
  其余接口回 `502` + 明确错误（缓存在浏览器里，未过期的副本仍可用）。
- 重新镜像会覆盖 `html/` 下抓到的文件；`/app/*` 自建页与磁盘 HTML 不会被改写
  （注入脚本只发生在回包时，`selftest.py` 会检查）。

## 已知说明

- 未登录时 `/api/auth/session/bootstrap` 返回 401 属正常（对齐线上，线上同样 401）；
  `/api/auth/session` 则回 200 `{authenticated:false}`（前端 `store.js` 读这个字段）。
- `/login` 是线上页反代，其 chunk 带 `?dpl=` 参数走反代回源；线上登录页的 6 位验证码
  输入校验与本地 1~3 位答案不匹配，站内登录请用右上角弹窗（见「怎么登录」）。
- 依赖线上内容/模型的自测用例在上游额度耗尽时会失败。
- 控制台里的 `ReferenceError: __name is not defined` 是**原站自带**的问题，线上也报，不影响使用。
- `/api/gva/narratium/notification/stream`（SSE）在反代下会显示 failed，属已知噪音。
- 快速刷新/关页面时日志里的 **499 `client-abort`** 是浏览器掐断了还没写完的响应，
  不是服务端故障（以前会被记成 500 `ConnectionAbortedError`）；服务端 listen 队列已提到 128，
  并发刷新不会再出现 `ConnectionRefused`。
- `/res.js`、`/legacy-polyfills.js` 目前没有页面引用，保留在 `ROOT_FILES` 里暂未清理。
- 聊天页刷新后若曾出现白色大块 + 左上角「正文」，根因见上一章「刷新后聊天区变白色大块」，
  已修复（过期 sessionId 兜底 + chatSession 子路由全本地化 + `read_body()` 可重复读）。
- 登录/支付等真实交易最终仍由线上后端处理，本地只负责账号、日志、收藏、对话与展示。
