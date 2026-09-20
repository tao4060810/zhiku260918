# 掌柜智库

> 基于 LangGraph 的多用户 RAG 知识问答系统 · FastAPI + Milvus + MongoDB + MinIO

面向产品说明书、使用方法、参数与故障排查场景的检索增强问答应用。用户导入 PDF / Markdown 建立私有知识库，通过聊天页面提问，系统从知识库与网络检索资料，交给大模型生成**带引用、可溯源**的答案。

项目从课程案例出发，进一步完成了**多用户体系、私有知识库隔离、对象存储安全改造、协作式任务取消、SSE 事件重放**等工程化建设。

![知识问答](docs/images/chat-answer-with-image.png)

## ✨ 功能亮点

**多用户与私有知识库**
注册、登录、改密、退出完整闭环；每个账号一个默认私有库，用户 / 文档 / 任务 / 历史 / 资产全维度隔离。聊天、任务、SSE、产品匹配与检索都限定在用户与知识库范围内，检索结果送入模型前复核本地来源归属。**管理员没有跨用户读取内容的特权。**

**认证安全**
密码 Argon2 摘要存储；登录凭证经 HttpOnly / SameSite Cookie 传递，数据库只保存凭证摘要；写请求校验 `Origin` 与 CSRF 令牌；改密或禁用账号即时令旧登录失效；SSE 每 2 秒复核身份，认证超时停止输出。

**对象存储安全**
课程原版将 MinIO 桶设为匿名公开读，本项目改为**私有桶**并按库 / 文档 / 任务分层。客户端每次操作校验桶策略、不创建匿名权限；页面统一走 `/assets/{id}`，由后端认证并核对元数据归属后代理，不暴露对象存储直链。

**文档版本安全发布**
同名文档按 `(库, 标题)` 识别为同一文档，新版本**完整成功后才切换**，失败保留旧的可检索版本；不同用户的同名文件互不影响；检索只读取已发布版本。

**协作式任务取消**
覆盖接收前 / 接收后 / 排队 / 备份 / 发布前全阶段。外部调用返回后停止后续节点、清理本次产物、释放配额预留，已完成文件不回退；取消与新版本发布共用任务锁。

**流式答案与事件重放**
后台始终执行完整工作流，流式只改变推送方式。事件带递增编号，重连经 `Last-Event-ID` 重放；`delta` 与 `final` 分离，仅校验后的 `final` 为可信结果；前端另以轮询兜底恢复。

**引用与配图可靠性**
答案正文以 `[cite:编号]` 关联来源并逐条校验编号有效；配图输出前核验权限与所属文档，前端在生成图片 HTML 前核对最终白名单，**不执行模型输出的原始 HTML**；流式期间只显示文字，校验完成后再原位插图。

## 🖼 界面预览

| 回答含配图与引用 | 正文内配图 |
| --- | --- |
| ![回答含配图](docs/images/chat-answer-with-image.png) | ![正文内配图](docs/images/chat-answer-inline-image.png) |

![纯文本回答](docs/images/chat-answer-text-only.png)

## 🧱 技术栈

| 层次 | 技术 |
| --- | --- |
| 接口与页面 | FastAPI · Uvicorn · Jinja2 · 原生 HTML / CSS / JS |
| 工作流编排 | LangGraph |
| 模型接入 | OpenAI 兼容接口 · LangChain · DashScope |
| 文档解析 | MinerU（PDF → Markdown）· 视觉语言模型（图片摘要） |
| 向量检索 | BGE-M3（稠密 + 稀疏）· BGE Reranker · Milvus |
| 数据存储 | MongoDB（账号 / 会话 / 文档元数据）· MinIO（私有对象存储） |
| 网络检索 | 智谱 MCP `webSearchPro` |
| 前端依赖 | marked · DOMPurify · Lucide（均已本地化） |

## 🏗 系统架构

```mermaid
flowchart TD
    A["浏览器页面"] --> B["Cookie 认证 / CSRF 校验"]
    B --> C["FastAPI 路由 · 用户与知识库归属检查"]
    C --> D["创建内存任务 · 提交线程池"]
    D --> E["工作流适配层"]
    E --> F1["导入工作流 · LangGraph"]
    E --> F2["检索问答工作流 · LangGraph"]
    F1 --> G1["MinerU / VLM 解析"]
    F1 --> G2["BGE-M3 / Milvus 向量"]
    F1 --> G3["MinIO 私有桶"]
    F2 --> H1["多路召回 · 向量 / HyDE / 网络"]
    F2 --> H2["RRF 融合 · Reranker 重排"]
    F2 --> H3["大模型生成带引用答案"]
    G2 --> I["任务状态与结果"]
    H3 --> I
    I --> J["SSE 事件推送 / 轮询恢复"]
    J --> A
```

一次请求的调用链：

```text
浏览器页面
  -> Cookie 登录认证 / CSRF 校验
  -> FastAPI 路由 / 用户与知识库归属检查
  -> 创建内存任务并提交线程池
  -> 工作流适配层 -> LangGraph 节点
  -> 模型、数据库或对象存储
  -> 更新任务结果、保存聊天历史
  -> 页面通过 SSE 或轮询展示结果
```

项目分四层：页面交互层（`web/templates/`、`web/static/`）、Web 接口层（`web/app.py`、`web/api/`）、业务工作流层（`processor/`）、基础能力层（`utils/`、`config/`、`tool/`）。完整模块职责与开发导航见 [项目整体梳理](docs/项目整体梳理.md)。

## 🚀 本地启动

Python 3.11+，在项目根目录执行：

```powershell
uv sync --locked
# 首次配置时参考 .env.example 创建 .env；已有 .env 不要覆盖。
uv run uvicorn web.app:app --host 127.0.0.1 --port 8000
```

当前依赖源使用 CUDA 12.6 的 PyTorch。模型默认可在 CPU 运行，使用 GPU 时设置 `BGE_DEVICE` 和 `BGE_RERANKER_DEVICE`，并确认驱动及显存满足要求。

- 登录/注册：<http://127.0.0.1:8000/login.html>
- 问答页面：<http://127.0.0.1:8000/chat.html>
- 文档管理：<http://127.0.0.1:8000/import.html>
- 接口文档：<http://127.0.0.1:8000/docs>

前端使用 Jinja2 模板与原生 HTML/CSS/JS，所有脚本、图标和 Markdown 渲染依赖已保存在本地，不需要运行前端开发服务器。导入和问答共用一个 FastAPI 服务，无需跨域配置。

前端文件按资源类型和页面职责组织；两个页面继承公共模板，静态资源由 FastAPI 统一通过 `/static/` 提供：

```text
web/
├── templates/
│   ├── base.html         # 公共布局、导航和删除确认
│   ├── chat.html         # 聊天页面
│   └── import.html       # 文档管理页面
└── static/
    ├── assets/
    │   └── brand.png
    ├── css/
    │   ├── base.css
    │   ├── chat.css
    │   └── import.css
    ├── js/
    │   ├── common.js     # 公共交互和侧栏会话管理
    │   ├── import-status.js # 跨页面导入进度和界面锁定
    │   ├── chat.js       # 聊天、流式恢复、引用和图片
    │   └── import.js     # 上传、任务列表和筛选
    └── vendor/          # 第三方库及许可证
```

认证页为 `auth_base.html`、`login.html`、`register.html`，使用 `auth.js` 和 `auth.css`，不加载聊天和任务请求。业务接口新增 `auth_service.py`、`auth_dependencies.py`、`knowledge_service.py`、`asset_service.py`。

每个页面只加载公共资源和自己的业务资源。切换页面使用正常链接跳转；返回聊天页时恢复当前会话及未完成任务，返回导入页时重新查询任务列表。页面离开不会取消已提交的后台任务；文件仍在上传时会提示确认离开。

上传、排队、解析和入库期间显示导入进度弹窗，当前账号的界面暂停其他业务操作；点击“终止上传”会取消当前账号仍在处理的导入任务，已完成的文件不会回退，退出登录仍使用右上角账户菜单。其他账号不受这个弹窗影响。所有导入任务完成、失败或终止后自动恢复；刷新页面也会重新读取任务并恢复弹窗。进度查询失败时保留锁定并自动重试。此限制作用于网页交互，后端仍保留会话冲突和任务容量校验。

## ⚙️ 环境配置

参考 `.env.example` 配置模型路径、LLM/MinerU 密钥，以及 Milvus、MongoDB、MinIO 连接。`.env` 已被 Git 忽略。模型可使用现有 `tool/download_bgem3.py`、`tool/download_reranker.py` 下载，下载前检查脚本中的目标路径。

联网搜索使用智谱 MCP，`MCP_ZHIPU_API_KEY` 可以单独配置；未设置时兼容原项目的 `OPENAI_API_KEY`。网络搜索失败会继续使用本地知识库，并在答案中显示提示。登录和业务接口依赖 MongoDB；不可用时返回 503，公开登录页仍可打开。生产设置 `APP_ENV=production`、`AUTH_COOKIE_SECURE=true`，并把 `AUTH_ALLOWED_ORIGINS` 设置为实际 HTTPS 站点源；生产不开放接口文档。

## 📖 使用流程

1. 注册并登录账号，然后在文档管理中上传 PDF 或 UTF-8 Markdown。每批最多 10 个文件，单文件最多 50 MB。
2. 页面显示文件备份、解析、切片、向量化及入库进度。Markdown 直接读取正文，PDF 通过 MinerU 解析并提取图片。单独上传 Markdown 不会携带其本地 `images` 文件夹。
3. 在知识问答中提问。支持流式/非流式回答、多轮会话、历史恢复、引用资料、图片预览、复制答案和删除当前会话。

同一知识库再次导入同名文档，会在新版本完整成功后切换；失败保留旧的可检索版本。不同用户的同名文件独立存在。文档清单和原文件下载持久化，默认每库最多 100 份文档、1 GiB 原文件容量。导入中间文件按库/文档/任务分目录，流程结束后清理；原文件永久保存在私有 MinIO 桶。

回答中的小号引用可打开原文摘录，底部“查看依据”默认收起；手机端以底部面板显示摘录。只展示本轮答案实际引用、且已提供给模型的资料。图片由回答按需选择，验证资产属于当前库及已引用文档后最多展示三张；全部通过需要登录的 `/assets/{id}` 访问，外部图片不直接渲染，前端过滤过小图片。旧对话在读取时兼容原有引用格式，不改写 MongoDB 中的历史原文。

### 文档导入流程

工作流定义见 [import_processor/main_graph.py](processor/import_processor/main_graph.py)。

| 顺序 | 节点 | 处理内容 |
| --- | --- | --- |
| 1 | `node_entry` | 检查输入文件，按扩展名分流 PDF / Markdown |
| 2 | `node_pdf_to_md` | 上传 MinerU、轮询结果、下载解压 Markdown 与图片 |
| 3 | `node_md_img` | 处理正文引用的图片，生成摘要、上传 MinIO、替换引用 |
| 4 | `node_document_split` | 按标题切片，细分长段落，合并短段落 |
| 5 | `node_item_name_recognition` | 识别产品名并写入产品名称向量 |
| 6 | `node_bge_embedding` | 生成稠密与稀疏向量 |
| 7 | `node_import_milvus` | 准备集合、删除旧切片、插入新切片并回填 `chunk_id` |

### 检索问答流程

工作流定义见 [query_processor/main_graph.py](processor/query_processor/main_graph.py)。

1. **产品确认与问题改写**：读取最近历史，提取产品名称并改写问题，经产品名称向量匹配库内产品；高置信度（> 0.85）全部采用，无高置信度时取 ≥ 0.6 的前三个作为候选，未确认时请用户明确型号。
2. **三路并行检索**：普通混合检索（稠密 / 稀疏权重 0.8 / 0.2）、HyDE 假设文档检索、智谱 `webSearchPro` 网络检索；网络失败返回空列表并记录警告，本地结果继续可用。
3. **融合与重排**：`node_rrf` 以 `chunk_id` 去重、按 `weight / (60 + rank)` 融合两路本地结果；`node_rerank` 合并网络资料后交本地 Reranker 打分，重排后最多保留 10 条、候选充足时至少 3 条，并按断崖阈值（绝对差 0.5、相对差 0.25）截断。
4. **答案生成与呈现**：资料与历史共享 12000 字符预算（先资料后历史），生成后由 [answer_presentation.py](utils/answer_presentation.py) 校验引用编号与图片白名单。

## 🔌 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/upload?kb_id=...` | 本人知识库 Multipart 上传，字段 `files` |
| POST | `/tasks/{task_id}/cancel` | 终止本人仍在处理的导入任务 |
| POST | `/uploads/{upload_id}/cancel` | 记录本人上传批次的取消，覆盖文件接收中的请求；上传时附带同一个 UUID `upload_id` |
| GET | `/status/{task_id}` | 任务状态、完成/运行节点、错误与结果 |
| GET | `/tasks` | 当前账号的近期导入任务 |
| GET | `/knowledge-bases` | 当前账号的默认私有库 |
| GET | `/knowledge-bases/{kb_id}/documents` | 持久文档清单及容量 |
| GET | `/assets/{asset_id}` | 校验库归属后提供图片/原文件 |
| POST | `/auth/register`、`/auth/login` | 注册和登录 |
| GET | `/auth/me` | 当前账号、CSRF token 和默认库 ID |
| POST | `/auth/logout`、`/auth/change-password` | 退出和修改密码 |
| POST | `/query` | `{query, kb_id, session_id?, is_stream?}`，默认流式 |
| GET | `/stream/{session_id}?task_id=...` | SSE：progress / delta / final / error |
| GET | `/sessions` | 最近会话列表 |
| GET | `/history/{session_id}` | 历史记录，支持 `limit`（1–500） |
| DELETE | `/history/{session_id}` | 删除会话记录；进行中的会话返回 409 |

业务请求使用 HttpOnly Cookie；写请求须携带允许的 `Origin` 和 `/auth/me` 返回的 `X-CSRF-Token`，公开登录/注册同样要求 JSON 和允许的 Origin。`session_id` 由服务端新建；客户端只能续问本人已有会话。

终止采用协作取消：任务先显示“正在终止”，当前解析或模型调用返回后停止后续步骤并清理本次产物，再显示“已终止”。不会强制杀死执行线程，也不会撤销已经完成的文件；取消和新版本发布共用任务锁，取消成功的任务不能再发布。取消批次标记按用户隔离并有容量和过期限制，仅保留在当前进程。

每次提问分配独立 `task_id`，同一会话的重叠请求返回 409。SSE 支持 `Last-Event-ID` 重放，前端另外轮询任务状态以恢复最终答案。后台始终实际执行完整工作流，流式模式只改变答案推送方式。

答案正文使用 `[cite:编号]` 关联 `sources[].source_id`；编号只在单条消息内有效，前端按首次引用顺序显示编号。流式 `delta` 是模型原始增量，页面暂时隐藏引用和图片区块；`final` 返回校验后的 `answer`、实际引用的 `sources` 和选中的 `image_urls`。第三方客户端应以 `final` 为准。编号校验保证资料映射有效，不替代对事实是否被资料支持的核验。

## ⚠️ 运行边界

当前实现个人私有库，不含第二阶段只读分享。管理员没有跨用户读取内容的特权。登录采用 Argon2 密码摘要、MongoDB 服务端会话、HttpOnly/SameSite Cookie；改密、禁用账号令旧登录失效。SSE 每 2 秒复核身份，认证超时停止输出。

**仅支持一个 Uvicorn worker、一个 Web 实例。** 任务、限流器和配额协调锁在本进程内，后台串行执行完整工作流。每用户最多 1 条活动问答、10 个活动导入，全局最多 20 个；任务最多保留 500 条和一天，事件每任务最多 4096 条。重启后任务进度丢失，但登录会话、聊天和文档清单保留；未完成导入释放预留，未发布向量不会被检索。服务中断或清理失败可能留下不可见的旧版本对象/向量，暂未提供自动垃圾回收或文档删除接口。

默认监听本机。实际部署需 HTTPS 反向代理，并限制请求体大小；使用多个 worker 之前必须改为共享任务队列及跨进程配额协调。不要把新集合名称改成旧的无归属集合，也不要给私有 MinIO 桶添加匿名策略。

## 👤 账号管理和旧数据迁移

普通用户可直接注册；管理员通过本机命令创建，密码隐藏输入，不写入命令行或配置：

```powershell
uv run python -m tools.manage_users create --username admin --role admin
uv run python -m tools.manage_users list
uv run python -m tools.manage_users disable --username alice
uv run python -m tools.manage_users reset-password --username alice
```

旧数据默认隔离，不自动归给第一个注册者。按需迁移、预览清单、关闭旧公开入口、幂等恢复及条件回退见 [多用户运维与迁移](docs/多用户运维与迁移.md)。迁移和管理命令未在现有业务数据库上执行。

## ✅ 验证

```powershell
uv run python -m unittest discover -s test -p "test_*.py" -v
node test/test_answer_view.cjs
node test/test_session_delete.cjs
node test/test_page_navigation.cjs
node test/test_import_status.cjs
node test/test_auth_view.cjs
```

自动化测试覆盖上传验证、批次回滚、路径安全、问答错误、SSE 重放与会话隔离、历史读写错误、Markdown 入口以及多路检索汇合。测试环境额外安装 `mongomock`（仅 dev dependency，不参与应用运行）；外部模型、数据库边界使用替身，运行测试不会提交真实导入或产生模型调用费用。

引用测试覆盖历史兼容、不改写原记录、上下文预算、无效编号、图片白名单及流式/非流式保存一致性；前端检查覆盖逐字符流式边界，避免内部标记和图片地址闪现。

前端第三方库版本与许可证见 `web/static/vendor/README.md`。

## 📄 文档

| 文档 | 内容 |
| --- | --- |
| [项目整体梳理](docs/项目整体梳理.md) | 架构分层、目录结构、导入与问答流程、存储设计、接口契约 |
| [多用户登录注册 Spec](docs/多用户登录注册-spec.md) | 账号体系与私有知识库的范围、权限、数据结构、接口与验收标准 |
| [第一阶段实施记录](docs/第一阶段实施记录.md) | 已实现项、验证结果、浏览器验收与剩余部署验证 |
| [多用户运维与迁移](docs/多用户运维与迁移.md) | 账号管理命令、旧数据迁移、回退方案 |

## 📜 License

[MIT](LICENSE)
