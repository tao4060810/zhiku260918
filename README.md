# 掌柜智库

基于 LangGraph 的 RAG 知识库，使用 MinerU 解析 PDF、VLM 生成图片摘要、BGE-M3 混合检索和本地 Reranker 重排。Milvus 保存知识切片，MinIO 保存文件与图片，MongoDB 保存多轮会话。

项目架构、模块职责、业务流程及开发导航见 [项目整体梳理](docs/项目整体梳理.md)。

## 本地启动

Python 3.11+，在项目根目录执行：

```powershell
uv sync --locked
# 首次配置时参考 .env.example 创建 .env；已有 .env 不要覆盖。
uv run uvicorn web.app:app --host 127.0.0.1 --port 8000
```

当前依赖源使用 CUDA 12.6 的 PyTorch。模型默认可在 CPU 运行，使用 GPU 时设置 `BGE_DEVICE` 和 `BGE_RERANKER_DEVICE`，并确认驱动及显存满足要求。

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

每个页面只加载公共资源和自己的业务资源。切换页面使用正常链接跳转；返回聊天页时恢复当前会话及未完成任务，返回导入页时重新查询任务列表。页面离开不会取消已提交的后台任务；文件仍在上传时会提示确认离开。

上传、排队、解析和入库期间显示不可关闭的导入进度弹窗，整个界面暂停其他操作。所有导入任务完成或失败后自动恢复；刷新页面也会重新读取任务并恢复弹窗。进度查询失败时保留锁定并自动重试。此限制作用于网页交互，后端仍保留会话冲突和任务容量校验。

## 环境配置

参考 `.env.example` 配置模型路径、LLM/MinerU 密钥，以及 Milvus、MongoDB、MinIO 连接。`.env` 已被 Git 忽略。模型可使用现有 `tool/download_bgem3.py`、`tool/download_reranker.py` 下载，下载前检查脚本中的目标路径。

联网搜索使用智谱 MCP，`MCP_ZHIPU_API_KEY` 可以单独配置；未设置时兼容原项目的 `OPENAI_API_KEY`。网络搜索失败会继续使用本地知识库，并在答案中显示提示。应用启动和页面访问不依赖数据库在线，业务接口会返回可见的失败状态。

## 使用流程

1. 在文档管理中上传 PDF 或 UTF-8 Markdown。每批最多 10 个文件，单文件最多 50 MB。
2. 页面显示文件备份、解析、切片、向量化及入库进度。Markdown 直接读取正文，PDF 通过 MinerU 解析并提取图片。单独上传 Markdown 不会携带其本地 `images` 文件夹。
3. 在知识问答中提问。支持流式/非流式回答、多轮会话、历史恢复、引用资料、图片预览、复制答案和删除当前会话。

原项目按文件标题更新知识切片；再次导入同名文档会替换该文档的旧切片。请使用不同文件名区分不同版本。

回答中的小号引用可打开原文摘录，底部“查看依据”默认收起；手机端以底部面板显示摘录。只展示本轮答案实际引用、且已提供给模型的资料。图片由回答按需选择，校验其来自已引用的本地资料后最多展示三张，前端过滤过小图片。旧对话在读取时兼容原有引用格式，不改写 MongoDB 中的历史原文。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/upload` | Multipart 批量上传，字段为 `files`，返回任务 ID |
| GET | `/status/{task_id}` | 任务状态、完成/运行节点、错误与结果 |
| GET | `/tasks` | 当前进程保留的导入记录 |
| POST | `/query` | `{query, session_id?, is_stream?}`，默认流式 |
| GET | `/stream/{session_id}?task_id=...` | SSE：progress / delta / final / error |
| GET | `/sessions` | 最近会话列表 |
| GET | `/history/{session_id}` | 历史记录，支持 `limit`（1–500） |
| DELETE | `/history/{session_id}` | 删除会话记录；进行中的会话返回 409 |

每次提问分配独立 `task_id`，同一会话的重叠请求返回 409。SSE 支持 `Last-Event-ID` 重放，前端另外轮询任务状态以恢复最终答案。后台始终实际执行完整工作流，流式模式只改变答案推送方式。

答案正文使用 `[cite:编号]` 关联 `sources[].source_id`；编号只在单条消息内有效，前端按首次引用顺序显示编号。流式 `delta` 是模型原始增量，页面暂时隐藏引用和图片区块；`final` 返回校验后的 `answer`、实际引用的 `sources` 和选中的 `image_urls`。第三方客户端应以 `final` 为准。编号校验保证资料映射有效，不替代对事实是否被资料支持的核验。

## 运行边界

当前是本机单用户版本，没有账号鉴权，默认仅监听 `127.0.0.1`。请勿直接暴露到公网。任务和 SSE 事件保存在当前进程内，最多保留 500 个任务，并在创建任务时清理超过一天的已结束记录；每任务保留最近 4096 个事件。聊天历史持久化在 MongoDB，原始文件保存在本地和 MinIO。

**请使用单个 Uvicorn worker。** 后台一次处理一条完整流水线，最多排队 20 个任务，避免同名文档并发覆盖和模型重复加载。重启会清空任务进度；优雅关闭会等待队列处理完毕。多用户/多进程部署需要增加鉴权和共享任务队列。

## 验证

```powershell
uv run python -m unittest discover -s test -p "test_*.py" -v
node test/test_answer_view.cjs
node test/test_session_delete.cjs
node test/test_page_navigation.cjs
node test/test_import_status.cjs
```

自动化测试覆盖上传验证、批次回滚、路径安全、问答错误、SSE 重放与会话隔离、历史读写错误、Markdown 入口以及多路检索汇合。外部模型、数据库边界使用替身，运行测试不会提交真实导入或产生模型调用费用。

引用测试覆盖历史兼容、不改写原记录、上下文预算、无效编号、图片白名单及流式/非流式保存一致性；前端检查覆盖逐字符流式边界，避免内部标记和图片地址闪现。

前端第三方库版本与许可证见 `web/static/vendor/README.md`。
