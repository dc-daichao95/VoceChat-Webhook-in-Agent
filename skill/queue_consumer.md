# Cursor 队列消费者手册

独立调度器负责持续拉取、入队和 SLA 通知；Cursor 只消费已领取任务，不执行固定
间隔轮询。

## 不可破坏的约束

- 每个 Cursor 会话生成唯一 `owner`，同一任务的所有命令使用该值，禁止跨会话复用。
- 一次最多领取 3 个不同会话。每个 `conv_id` 使用独立工作上下文，保持会话隔离；
  同一会话严格按队列 FIFO 顺序处理，不并行、不跳过较早 `mid`。
- 处理期间至少每 60 秒 `renew` 一次，并在 HTTP、browser-use 和发送前后续租。
- CLI 只输出脱敏元数据，不含 payload。用 `conv_id`、`mid` 从
  `data/inbound/<conv_id>.jsonl` 定位正文。
- 正式发送必须先预约 final；禁止无预约发送后再直接完成任务。

以下命令可按部署配置添加 `--db <path>`；同一任务始终使用同一个 `<owner>`。

## 单轮流程

1. 领取任务：

   `python scripts/queue_cli.py next --owner <owner> --limit 3`

   `event=empty` 时结束本轮。有多个任务时，不同会话可并行，但每个会话必须使用
   独立上下文。

2. 对每个任务先构建上下文：

   `python scripts/build_context.py --conv <conv_id>`

   结合对应 `mid` 的入站记录回答，禁止带入其他会话的上下文、证据或事实。

3. 分类并保存 `network_mode`：

   `python scripts/queue_cli.py mode --job-id <id> --owner <owner> --value <none|fast_http|browser>`

4. 需要联网时优先运行（`--job-id` 与 `--owner` 必须同时提供）：

   `python scripts/online_fetch.py <json|text> <url> --job-id <id> --owner <owner>`

   只有 `online_fetch.py` 明确返回 `fallback=browser`，或页面必须点击、滚动、填写
   表单时，才使用 browser-use。浏览器证据保存为 UTF-8 JSON 后执行：

   `python scripts/queue_cli.py evidence --job-id <id> --owner <owner> --file <evidence.json>`

5. 工作期间定期续租：

   `python scripts/queue_cli.py renew --job-id <id> --owner <owner>`

   返回非零时立即停止该任务，不得继续发送。final 预约存在时，此命令会原子续期
   job 和 final 的租约。

## 正式回复、修复与 at-most-once

1. 将回复写入 UTF-8 文件，再次 `renew`，然后只调用阶段化发送入口：

   `python scripts/queue_cli.py send-final --job-id <id> --owner <owner> --reply-file <reply.txt>`

   Markdown 回复添加 `--markdown`。该命令先在 SQLite 保存必要的 reply record 并
   预约 final，再发 HTTP。只有保守 allowlist 内、明确未处理的 4xx 才可重试；
   3xx、429、5xx、timeout、连接异常和未知结果都进入 uncertain。
   `send-final` 返回后不得再执行 `fail`；它已经完成 final 状态转换。

2. `send-final` 返回 `status=done, record_pending=true` 时，消息已经发送且任务已经
   完成，只是本地 history/state 尚待修复。执行：

   `python scripts/queue_cli.py repair-record --job-id <id>`

   repair 只使用 SQLite 中预存的 reply record，不会再次调用 sender，可安全重试。

3. 仅限调用 `send-final` 之前的普通处理失败，才可执行：

   `python scripts/queue_cli.py fail --job-id <id> --owner <owner> --error <class>`

   `fail` 直接使用数据库内一基 `attempts` 计算退避，不再次加一。错误只写类别，
   不得包含 payload、API key、URL 或 DB 路径。

4. uncertain 永久阻止自动重发。人工核对 VoceChat 后显式处理：

   - 已发送：`python scripts/queue_cli.py reconcile --job-id <id> --action mark-done --confirm`
   - 未发送并取消：`python scripts/queue_cli.py reconcile --job-id <id> --action cancel --confirm`
   - 仅在接受重复风险时重试：追加
     `--action retry --confirm --confirm-duplicate-risk`

   人工动作会写入 SQLite 审计；禁止绕过确认参数。

## 取消与排查

- 当前有效 owner 可在 final 预约前取消：
  `python scripts/queue_cli.py cancel --job-id <id> --owner <owner>`
- 查看安全摘要：`python scripts/queue_cli.py list [--status pending]`
- 任一命令非零都表示没有获得所需状态保证。不要手改 SQLite，不要换 owner 接管
  未过期任务，也不要回退到无预约的直接发送。
