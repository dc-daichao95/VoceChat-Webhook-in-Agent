# AnsweringMachine 待办(TODO)

待办来自 v1 上线联调期间的真实使用反馈(私聊/群多用户实测)。
原先记录在设计规格 `docs/superpowers/specs/2026-07-08-vocechat-answering-machine-design.md` §14,现独立到本文件维护。

## 已完成

- [x] **上下文压缩 / 超长历史处理** — 回复上下文改为「事实卡片 + 滚动摘要 + 最近 N 条」,超阈值(raw>40,留 20)gzip 归档旧记录到 `data/archive/`。
  设计:`docs/superpowers/specs/2026-07-09-context-compaction-design.md`;实现:`brain/compaction.py`、`brain/context.py`、`scripts/compact.py`、`scripts/build_context.py`。
- [x] **接入实时数据(联网)** — 通过 **browser-use skill**:Cursor 经 CDP 驱动本地 Chrome 联网查询(天气、时事等),无需额外 LLM Key。
  设计:`docs/superpowers/specs/2026-07-09-browser-use-skill-design.md`;技能:`.cursor/skills/browser-use/`。

## 待办

### 高

- [ ] **会话级隔离修复**:数据按会话隔离(独立 JSONL + 游标),但"大脑"是同一个 Cursor 会话同时看到所有对话,存在跨会话信息泄漏(实测:私聊里说出了群里设置的称呼)。方案:每个会话一个独立 subagent,或在 loop 里严格只把"当前会话上下文"喂给回复生成(上下文压缩的 `build_context` 已为此打下基础)。
- [x] **重构 /loop 为可靠调度器 + Cursor 消费者**（Task 1–9 全部完成,严格 TDD + 规格/质量双评审）:
  - **设计规格**:`docs/superpowers/specs/2026-07-10-reliable-scheduler-online-response-design.md`;**实施计划**:`docs/superpowers/plans/2026-07-10-reliable-scheduler-online-response.md`。
  - **实施进度**(严格 TDD + 规格/质量双评审;全量测试 559 passed):
    - [x] Task 1 SQLite 持久队列、租约、幂等状态机 — `scheduler/db.py`、`scheduler/schema.py`。
    - [x] Task 2 轮询/退避/SLA 纯策略 — `scheduler/policy.py`。
    - [x] Task 3 WebDAV 拉取入队 — `scheduler/ingest.py`。
    - [x] Task 4 10s ack / 45s partial 幂等通知 + 事务性 outbox — `scheduler/notifier.py`、`scheduler/outbox.py`。
    - [x] Task 5 有界 HTTP 快路径与渐进证据(SSRF 固定 IP 拨号、URL 脱敏、DOM 回退) — `scheduler/online.py`、`scheduler/_online_transport.py`、`scripts/online_fetch.py`。
    - [x] Task 6 队列 CLI 与消费者流程(prepare→send→原子 done→可修复 record、uncertain 人工 reconcile、证据幂等) — `scripts/queue_cli.py`、`scheduler/consumer.py`、`scheduler/final_delivery.py`。
    - [x] Task 7 独立调度器服务循环(每轮 recover→pull 熔断→ingest→notify→metrics、自适应轮询、单实例、指数退避不崩溃) — `scheduler/service.py`、`scripts/scheduler.py`。
    - [x] Task 8 Windows 任务计划程序生命周期脚本(install/start/stop/status/uninstall,登录自启、失败自愈、单实例、幂等) — `scripts/scheduler_*.ps1`。
    - [x] Task 9 配置、文档与夜间失败场景端到端回放验收 — `.env.example`(调度器/SLA/quiet/轮询/退避/路径,含 SchedulerConfig 一致性说明)、`.gitignore`(队列 DB/WAL/lock/日志)、`README.md`(「可靠调度器 + Cursor 消费者」架构/SLA/quiet/Windows 生命周期/消费者流程/故障恢复/迁移)、`tests/test_scheduler_replay.py`(注入时钟 + mock WebDAV/send 的夜间事故端到端回放:落盘→发现→10s ack→45s partial/status→卡死持久排队→过期回队→FIFO 幂等 send-final 只发一次→WebDAV 故障不阻塞→重启不丢→uncertain 人工 reconcile)。
  - **已确认根因(2026-07-09 夜间实测)**:`mid=1431` 于 23:50:48 落盘,23:50:49 即被轮询发现,但正式回复到 01:05:04 才发出(延迟 74.3 分钟)。Webhook、NAS 落盘、WebDAV 拉取和 30 秒检查均正常;延迟发生在消息被发现后交给 Cursor 智能体同步执行联网查询/生成回复的阶段。当前轮询发现消息后会退出等待并让出控制权,Cursor 调度、连接或耗时任务停顿时,轮询也随之停止;没有处理超时、占位回复、持久队列或自动恢复。根因是**可靠调度与智能处理耦合,且依赖 Cursor 会话持续存活**,不是轮询间隔本身。
  - **目标架构**:拆为两个进程:
    1. **独立常驻调度器**:WebDAV 拉取 → 持久化任务队列 → 必要时发送一次占位回复 → 重试/退避;不依赖 Cursor 在线。
    2. **Cursor 消费者**:在线时持续领取队列任务 → 生成正式回复 → 按 `mid` 幂等发送 → 确认任务完成。Cursor 离线时只排队与占位,恢复后自动续处理;不引入外部 LLM/API。
  - **轮询策略**:活跃会话/刚有消息 15s;普通状态 30s;连续空闲逐步退避到 2min;发现新消息立即恢复 15s。
  - **夜间静默**:00:00–07:00 每 5min 检查;收到消息立即占位并排队,不启动耗时联网任务,07:00 后正式处理。
  - **10s / 45s 回复时限**:从调度器发现消息起,10s 内无正式回复则发送一次"已收到,正在处理";45s 时已有结构化证据则发送部分结果,无证据则发送仍在排队/查询的状态;正式任务继续后台执行,不得阻塞调度器继续收件。
  - **队列与并发**:会话内严格 FIFO;不同会话最多 3 路并行,为后续会话级隔离预留边界。
  - **失败恢复**:持久化任务状态(`pending/processing/acked/done`);失败无限指数退避(设置合理最大间隔,如 30min);占位最多一次、正式回复按 `mid` 幂等,避免重复/刷屏;用户可取消任务。
  - **生命周期**:Windows 任务计划程序在开机/登录时自动启动调度器,异常退出自动重启;提供 `start/stop/status` 管理脚本。
  - **联网快路径**:公开 JSON/API、静态网页优先直接 HTTP;轻量抓取搜索标题/摘要;只有 JS 页面、点击或交互才启用 browser-use;证据逐步写入队列,供 45s 部分回复使用。
  - **验收重点**:30s/15s 是“发现延迟”而非回复 SLA;即使 Cursor 断线或联网查询卡住,调度器仍持续收件,发现后 10s 内确认、45s 内给部分结果或状态,重启后队列不丢且可自动续跑。

### 中

- [ ] **敏感操作审批机制**:为将来会产生副作用的能力设计审批——bot 先回确认提示,用户回复确认关键词 + 通过 uid 权限白名单才执行,否则默认不做。
- [ ] **带权限的「清除历史」命令**:允许授权用户清除某会话历史,需权限控制防止被随意清空。
- [ ] **跨会话记忆**:跨私聊/群记住同一用户(如 uid → 称呼/偏好);与"会话级隔离"的边界需一起权衡。
- [ ] **WebDAV 密码不明文存储**:当前 `share.env` 明文保存 `passwd`(已 gitignore,但仍明文落盘)。改为 Windows 凭据管理器 / `keyring`(DPAPI 加密、绑定当前用户),`share.env` 仅留 `url`+`user`;运行时按 `环境变量 > keyring > share.env(兼容回退)` 解析,`scripts/webdav_check.py` 与大脑拉取共用该逻辑。

### 低 / 可选

- [ ] **精确 token 用量统计**:大脑侧无法拿到精确 token 计数;需在服务侧记录每次模型调用的 usage 才能统计。
- [ ] **多平台接入**:Telegram(官方 Bot API,最简单)、企业微信 / 微信公众号(官方回调),复用现有"接收→大脑→回发"架构;个人微信无官方接口、不建议。

## 实施顺序建议

先做剩余「高」项(会话级隔离,纠正跨会话泄漏),再按需推进其余。
