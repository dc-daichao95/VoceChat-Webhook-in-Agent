# AnsweringMachine

VoceChat 自动应答机器人。**接收端(receiver)** 是一个 dumb 的 FastAPI 服务,部署为 fnOS NAS 上的 Docker 容器,只负责「收 webhook → 过滤 → 落盘」;**大脑(brain)** 是本机的 Cursor 会话,经 WebDAV 拉取新消息、基于会话历史生成智能回复,再用 `send.py` 通过 VoceChat bot API 发回。

- 设计规格:`docs/superpowers/specs/2026-07-08-vocechat-answering-machine-design.md`
- 实现计划:`docs/superpowers/plans/2026-07-08-vocechat-answering-machine.md`

## 架构

```
用户 → VoceChat(公网)
        │ ① POST webhook
        ▼
   receiver(FastAPI, Docker @ NAS, dumb)
        │ ② 过滤 + 落盘
        ▼
   NAS 目录(由 fnOS WebDAV 暴露)
     conversations/<conv_id>.jsonl   raw/<ts>_<mid>.json
        │ ③ 本机经 WebDAV(HTTPS, Basic)PROPFIND + 条件 GET
        ▼
   本机大脑(Cursor /loop)
     data/inbound/  data/history/  data/state.json
        │ ④ 生成回复 → send.py 出站 POST /api/bot/...
        ▼
   VoceChat(用户看到回复)
```

- 唯一「跨公网」的入站是 VoceChat → receiver(receiver 在公网可达的 NAS 上,直连)。
- 本机全部为**出站** HTTPS(WebDAV 拉取 + bot API 发送),无需公网 IP、无需内网穿透。
- 传输选型与轮询开销分析见规格 §12/§13;WebDAV 用 Python 直连(`requests`),不依赖 Windows WebClient/Redirector。

## 目录结构

```
AnsweringMachine/
├─ app/                      # receiver(部署到 NAS 的部分)
│  ├─ receiver.py            # FastAPI:GET/POST /, /health;create_app / app_factory
│  ├─ filters.py             # 纯函数:type/scope/@ 过滤、conv_id、记录构建
│  ├─ storage.py             # 落盘:jsonl 追加、seen_mids 去重、raw dump
│  └─ config.py              # 环境变量配置加载 + 校验
├─ brain/                    # 大脑(本机的部分)
│  ├─ pull.py                # WebDAV 客户端 + PROPFIND 列表 + 条件 GET 拉取
│  └─ select.py              # 纯函数:从 inbound+state 选出待处理入站
├─ send.py                   # 出站发送 CLI(本机)
├─ scripts/
│  ├─ package_receiver.ps1   # 打包 receiver 构建上下文为 tar.gz(输出到 build/)
│  ├─ run_receiver.sh        # NAS 上 docker build+run(compose 之外的备选)
│  ├─ webdav_check.py        # WebDAV 连通性 / 往返 / 轮询成本自检
│  ├─ browser_chrome.ps1     # 启动带 CDP 的专用 Chrome(browser-use skill)
│  └─ browse.py              # Cursor 驱动的 CDP 浏览器封装(browser-use skill)
├─ .cursor/skills/           # Cursor Agent Skills(如 browser-use)
├─ skill/                    # 供 agent 使用的大脑操作手册
│  └─ loop_prompt.md         # 供 /loop 使用的大脑每轮操作手册
├─ build/                    # 构建产物(打包 tar.gz;已 gitignore)
├─ Dockerfile               # receiver 镜像(python:3.11-slim)
├─ docker-compose.yml       # NAS 上构建并运行 receiver
├─ requirements.txt
├─ tests/                   # pytest:filters/storage/config/receiver/send/select/pull/factory
└─ docs/superpowers/        # specs/ 规格、plans/ 计划
```

## 前置条件

- **本机(大脑)**:Python 3.8+;`pip install -r requirements.txt`;能出站访问 VoceChat 与 NAS WebDAV(HTTPS)。
- **NAS(receiver)**:fnOS,自带 Docker;一个由 WebDAV 共享暴露的目录(本机据此拉取)。
- **VoceChat**:已建好 bot,拿到 `BOT_ID`、`API Key`、server 地址;bot 的 webhook URL 指向 receiver。

## 配置

真实配置文件(含密钥/密码)**均不进 git**;仓库内提供两个占位模板(**已跟踪**),复制后填入真实值即可。

| 文件 | 模板 | 用途 | 关键项 |
|---|---|---|---|
| `.env` | `.env.example` | 本机 `send.py` + 调度器 + receiver 环境变量参考 | `VOCECHAT_SERVER_URL` / `VOCECHAT_API_KEY`;`SCHEDULER_*`(轮询/SLA/quiet/退避/路径);`BOT_UID` 等 receiver 项 |
| `share.env` | `share.env.example` | 本机 WebDAV 拉取 | `url`(WebDAV 共享地址,以 `/` 结尾)/ `user` / `passwd` |

> 运行时只使用这两个文件。VoceChat 的 bot 凭据(server 地址、API Key、bot uid)直接填进 `.env` 即可。

**配置流程**——从模板复制,再编辑填入真实值:

```powershell
Copy-Item .env.example .env
Copy-Item share.env.example share.env
# 然后用编辑器打开 .env / share.env,替换其中的占位值(<...> / replace-me)
```

- `share.env`:填 NAS 的 WebDAV `url`(以 `/` 结尾)、`user`、`passwd`。填好后用 `python scripts/webdav_check.py --roundtrip` 验证(见下文自检)。
- `.env`:本机 `send.py` 只用到 `VOCECHAT_SERVER_URL` / `VOCECHAT_API_KEY`;文件里的 `BOT_UID`、`SCOPE_*`、`DATA_DIR`、`RAW_DUMP` 是 **receiver 端参考**,实际由 `docker-compose.yml` 的容器环境变量提供(receiver **不需要任何密钥**,只收+落盘)。

## 本机初始化

```powershell
python -m pip install -r requirements.txt
mkdir data\inbound, data\history -Force
'{"conversations": {}, "seen_mids": []}' | Set-Content data\state.json -Encoding UTF8
```

## 连通性自检(WebDAV)

```powershell
python scripts/webdav_check.py --roundtrip     # PUT → GET → DELETE 往返(应全 OK)
python scripts/webdav_check.py --path / --no-write   # 列出 spool 根
python scripts/webdav_check.py --bench 20      # 轮询成本估算
```

## 部署 receiver 到 NAS

1. **打包**构建上下文:

   ```powershell
   powershell -File scripts/package_receiver.ps1
   # 生成 build/receiver-build-context.tar.gz(仅含 Dockerfile / requirements.txt / docker-compose.yml / app/)
   ```

2. **上传**该 tar.gz 到 NAS(fnOS 文件管理、SMB、或 scp 均可),解压到一个目录。

3. 编辑 `docker-compose.yml`,把 `volumes` 左侧宿主路径改成 fnOS 上「webhook_share」这个 WebDAV 共享**实际映射的目录**(即本机通过 WebDAV 能读到的那个文件夹),并确认 `BOT_UID` 与你的 bot 一致(当前为 `7`)。

4. 在该目录**构建并启动**:

   ```bash
   docker compose up -d --build
   docker compose logs -f          # 观察启动
   ```

5. 在 VoceChat 的 bot 设置里,把 **webhook URL** 指向 `http(s)://<NAS 公网地址>:8091/`(GET `/` 返回 `ok`,可用于探活)。

## 运行大脑(本机)

> **v2 起推荐用「可靠调度器 + Cursor 消费者」取代固定 `/loop` 轮询**(见下一节)。
> 常驻调度器独立负责拉取、入队、SLA 通知与恢复,Cursor 只在有任务时领取并生成正式
> 回复。固定 `/loop` 仅作应急模式保留(见 `skill/loop_prompt.md` 的「应急模式」)。

历史流程(应急/参考):在 Cursor 会话中用 `/loop`(建议 30~60s 间隔)执行
`skill/loop_prompt.md` 描述的流程:拉取(PROPFIND + 条件 GET)→ 选出待处理入站 →
读历史生成回复 → 发回 → 记账。应急模式不得与调度器同时消费同一队列。

手动发送测试:

```powershell
python send.py --target-uid <uid> --text "hi from bot"
python send.py --target-gid <gid> --text "**hi**" --markdown
echo "多行内容" | python send.py --target-uid <uid> --text -   # 从 stdin 读长文本
```

## 可靠调度器 + Cursor 消费者(v2)

固定 `/loop` 的根因问题:**可靠调度与智能处理耦合,并依赖 Cursor 会话持续存活**。
一旦 Cursor 调度/连接/耗时任务停顿,轮询随之停止,消息可能长时间无响应
(2026-07-09 夜间实测:`mid=1431` 被发现后正式回复延迟 74 分钟)。v2 将其拆为两个
独立生命周期,并用 10s/45s SLA 保证联网消息不再长时间"石沉大海"。

设计规格:`docs/superpowers/specs/2026-07-10-reliable-scheduler-online-response-design.md`;
实施计划:`docs/superpowers/plans/2026-07-10-reliable-scheduler-online-response.md`。

### 架构

```
用户 → VoceChat → receiver(NAS, Docker) → NAS 目录(WebDAV 暴露)
                                              │ ① 自适应 WebDAV 轮询 + 条件 GET
                                              ▼
                        ┌──────────── 独立调度器(常驻 Python) ───────────┐
                        │  每轮:恢复过期租约 → 拉取(熔断)→ 幂等入队      │
                        │        → 10s/45s SLA 通知 → 健康快照            │
                        │  SQLite 持久队列 data/queue.db(WAL)            │
                        └───────────────────────┬────────────────────────┘
                                                 │ ② 按会话领取任务(租约)
                                                 ▼
                        ┌──────────── Cursor 消费者(在线时) ────────────┐
                        │  build_context → 分类 network_mode             │
                        │  ③ 优先 HTTP 快路径(online_fetch,逐步写证据) │
                        │     仅必须交互时回退 browser-use               │
                        │  ④ send-final(按 mid 幂等,只发一次)→ 记账   │
                        └────────────────────────────────────────────────┘
```

- **调度器**不依赖 Cursor:Cursor 断线/卡死时仍持续收件、入队、发占位与状态。
- **SQLite 队列**保证幂等入队(`UNIQUE(conv_id,mid)`)、租约恢复、崩溃/重启不丢。
- **Cursor 消费者**只在有任务时被唤醒;会话内严格 FIFO,不同会话最多 3 路并行。

### SLA(从"调度器发现消息"开始计时,非用户发送时刻)

- `<10s`:等待正式回复。
- `>=10s` 未完成:发送一次"已收到,正在处理"(占位,不推进正式回复游标)。
- `>=45s` 未完成:已有结构化证据→用确定性模板发送部分结果;无证据→发送"仍在
  排队/查询"的状态。占位/部分结果各自最多发一次,均不写 history、不推进游标。
- 正式结果由 Cursor 完成后,经 `send-final` 按 `mid` 幂等发送(只发一次)。

### 夜间静默(quiet hours)

`00:00–07:00` 固定 300s 轮询:仍会入队并按 SLA 发占位/状态,但不启动耗时联网任务;
`07:00` 后任务重新可处理。其余时段自适应 15s(刚有消息)/30s(普通)/120s(长空闲)。

### Windows 生命周期(任务计划程序)

用仓库内固定 Python 与绝对工作目录运行 `python scripts/scheduler.py run`;登录自启、
异常每 1 分钟重启(最多 999 次),`MultipleInstances=IgnoreNew` 叠加进程内 PID 文件
锁双重保证单实例。

```powershell
powershell -File scripts/scheduler_install.ps1 -WhatIf   # 干跑,只打印将写入的任务定义
powershell -File scripts/scheduler_install.ps1           # 注册(幂等;更新定义需 -Force)
powershell -File scripts/scheduler_start.ps1             # Start-ScheduledTask
powershell -File scripts/scheduler_status.ps1            # 任务状态 + 健康快照(scheduler.py health)
powershell -File scripts/scheduler_stop.ps1              # Stop-ScheduledTask
powershell -File scripts/scheduler_uninstall.ps1         # 停止并注销(需确认;-Force 跳过)
```

调度器 CLI 直接用法:

```powershell
python scripts/scheduler.py init-db   # 创建/迁移 SQLite schema
python scripts/scheduler.py once      # 跑一轮(不加锁;勿与 run 守护进程同时执行)
python scripts/scheduler.py run       # 常驻循环(单实例)
python scripts/scheduler.py health    # 打印最近健康快照(不含任何敏感字段)
```

### Cursor 队列消费者流程

Cursor 在线时按 `skill/queue_consumer.md` 消费(默认入口见 `skill/loop_prompt.md`)。
同一任务的所有命令使用同一个唯一 `<owner>`;每个会话独立上下文,严禁跨会话泄漏。

```powershell
python scripts/queue_cli.py next --owner <owner> --limit 3          # 领取(不同会话)
python scripts/build_context.py --conv <conv_id>                    # 取该会话有界上下文
python scripts/queue_cli.py mode --job-id <id> --owner <owner> --value fast_http
python scripts/queue_cli.py renew --job-id <id> --owner <owner>     # 处理期间续租
python scripts/queue_cli.py send-final --job-id <id> --owner <owner> --reply-file reply.txt
python scripts/queue_cli.py list [--status pending]                 # 脱敏摘要
```

- `send-final` 先在 SQLite 预约 final 并保存 reply record,再发 HTTP,原子完成
  `done`。**返回后不得再 `fail`**,它已完成 final 状态转换。
- 联网优先 HTTP 快路径,证据逐条落库供 45s 部分结果使用:

  ```powershell
  python scripts/online_fetch.py json <url> --job-id <id> --owner <owner>
  python scripts/online_fetch.py text <url> --job-id <id> --owner <owner>
  ```

  仅当 `online_fetch` 返回 `fallback=browser`,或页面必须点击/滚动/填表时,才用
  browser-use;浏览器证据存为 UTF-8 JSON 后 `queue_cli.py evidence ... --file`。

### 故障恢复与运维

- **队列积压 / 租约过期**:调度器每轮 `recover_expired` 把过期 `processing` 任务
  回退为 `retry_wait` 自动重试;查看 `queue_cli.py list --status retry_wait`。
- **本地记账失败但已发送**:`send-final` 返回 `record_pending=true` 表示消息已发、
  任务已 `done`,仅本地 history 待补。执行 `queue_cli.py repair-record --job-id <id>`
  (只重放本地记录,永不重发)。
- **uncertain 人工 reconcile**:发送结果未知(超时/3xx/429/5xx/连接异常)会冻结为
  `uncertain`,**永不自动重发**。人工核对 VoceChat 后显式收敛:

  ```powershell
  python scripts/queue_cli.py reconcile --job-id <id> --action mark-done --confirm   # 已送达
  python scripts/queue_cli.py reconcile --job-id <id> --action cancel --confirm      # 未送达并放弃
  python scripts/queue_cli.py reconcile --job-id <id> --action retry --confirm --confirm-duplicate-risk
  ```

- **任务计划状态**:`scheduler_status.ps1` 查看 `State/LastRunTime/LastTaskResult` 与
  健康快照;`LastTaskResult` 非 0 或长期无 `LastRunTime` 说明未正常常驻。
- **重启不丢**:队列与三类回复标志全部持久化,进程/网络恢复后未完成任务自动续跑,
  占位不重复、正式回复按 `mid` 幂等只发一次。

### 与旧 `/loop` 的关系与迁移

1. 保留现有 `data/state.json` 的 WebDAV ETag 与消息游标;首启自动创建 SQLite schema,
   不迁移历史。
2. 新消息先入 SQLite,`send-final` 成功后继续写现有 `data/history/*.jsonl`。
3. 迁移期可保留手动 `/loop` 作为应急入口,但**不得与调度器同时消费同一队列**,否则
   可能破坏会话 FIFO 或重复回复。
4. 稳定后固定轮询职责由调度器接管;`skill/loop_prompt.md` 默认指向队列消费者,旧手动
   流程降级到「应急模式」。

## 浏览器能力(browser-use skill,可选)

让大脑(Cursor)能直接驱动本地 Chrome 上网:导航、截图、提取文本、执行 JS、按坐标点击。
用于回答"需要上网"的消息。**不需要额外 LLM/API Key**,经 CDP 直连,进程干净退出。

一次性安装(需 Python 3.12 + uv;`thrid-party/browser-use` 为 browser-use 本地克隆):

```powershell
uv venv .venv-browseruse --python 3.12
uv pip install --python .venv-browseruse -e ./thrid-party/browser-use
```

启动专用 Chrome(CDP:9222,独立 profile,不碰日常登录态)并调用:

```powershell
powershell -File scripts/browser_chrome.ps1
.venv-browseruse/Scripts/python.exe scripts/browse.py open "https://example.com"
.venv-browseruse/Scripts/python.exe scripts/browse.py info      # 标题/URL
.venv-browseruse/Scripts/python.exe scripts/browse.py text      # 可见正文
.venv-browseruse/Scripts/python.exe scripts/browse.py js "1+2"  # 执行 JS
.venv-browseruse/Scripts/python.exe scripts/browse.py shot out.png
.venv-browseruse/Scripts/python.exe scripts/browse.py click <x> <y>
```

- Cursor Skill:`.cursor/skills/browser-use/`(`SKILL.md` / `reference.md` / `examples.md`);会话中可用 `/browser-use` 调用。
- 设计与决策(为何用 CDP 库封装而非 `bu` CLI):`docs/superpowers/specs/2026-07-09-browser-use-skill-design.md`。
- `thrid-party/`、`.venv-browseruse/`、`.browser-use-profile/` 均已 `.gitignore`,不进仓库。

## 数据流与落盘

- receiver 侧(NAS,DATA_DIR):`conversations/<conv_id>.jsonl`(仅入站,append)、`raw/<ts>_<mid>.json`(RAW_DUMP 开启时)、`seen_mids.json`(去重)。
- 本机侧:`data/inbound/`(WebDAV 拉取镜像,只读)、`data/history/`(权威 in+out 历史,大脑写)、`data/state.json`(处理游标 + etag + seen_mids)。
- `conv_id`:私聊 `u<from_uid>`,群聊 `g<gid>`。
- 消息记录字段:`mid, conv_id, direction(in/out), from_uid, content_type, content, mentioned_bot, created_at, recorded_at`,出站另有 `in_reply_to`。

## 测试

```powershell
python -m pytest -q        # 全部单测(应全绿)
```

## 分阶段验收(Phase)

- **Phase 0**:receiver `RAW_DUMP=true`,给 bot 发消息 → NAS 上出现 raw payload;核对 `properties`/`target`/`content_type` 真实结构。`send.py` 能发出回复。
- **Phase 1**:receiver 产出 `conversations/*.jsonl`;本机经 WebDAV 能拉到。
- **Phase 2**:端到端私聊——来消息 → 生成回复 → 发回 → 记账,不自我循环。
- **Phase 3**:群里被 @ 才应答。

## 安全

- `.env`、`share.env`(以及可选的 `bot.config`)均已在 `.gitignore`,不进仓库;`*.tar.gz` 构建产物同样忽略。
- 仅 `.env.example` / `share.env.example` 两个**占位模板**(无真实凭据)进仓库,供他人按流程复制填写。
- `VOCECHAT_API_KEY` / WebDAV 密码不落盘到 JSONL、不打印到会话输出。
- WebDAV 走 HTTPS + Basic;对自签名证书 `webdav_check.py` 默认 `verify=False`(见规格 §12)。
- **待办**:WebDAV 密码目前在 `share.env` 明文;计划改用 Windows 凭据管理器 / `keyring`(见规格 §14),待联调稳定后实施。

## 故障排查

- receiver 容器起不来:`docker compose logs`;确认 `BOT_UID` 已设(缺失会启动即报错)。
- 收不到消息:确认 VoceChat webhook URL 指向 `:8091`;GET `/` 应返回 `ok`;检查 NAS 端口放行。
- 本机拉不到:`python scripts/webdav_check.py --path / --no-write` 看是否 401(凭据)或路径不对;`share.env` 的 `url` 需以 `/` 结尾。
- 自我循环:确认 `BOT_UID` 与 bot 实际 uid 一致(过滤掉 bot 自己发的消息)。
