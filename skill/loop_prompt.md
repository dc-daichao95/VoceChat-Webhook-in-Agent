# AnsweringMachine Cursor 消费者入口(供 /loop 使用)

## 回复准则(生成回复时遵循)

- **角色**:智能助手——用你的推理能力 + 该会话历史,实打实回答用户的问题。
- **语气/长度**:友好且简洁,默认 1~3 句;需要时再展开(复杂问题可分点或给步骤)。
- **语言**:跟随用户——用户用什么语言就用什么语言回复。
- **格式**:需要列表/代码/步骤时用 markdown（`send-final` 加 `--markdown`）；否则纯文本。
- **诚实**:不确定或信息不足时,礼貌地追问或说明不确定,不要编造事实。
- **不复读身份**:不必每条都自报"我是助手";被问到再说明即可。

## 默认模式:可靠队列消费者

默认严格执行 `skill/queue_consumer.md`。由独立调度器持续拉取和入队，Cursor 只在队列
有任务时领取；正式回复只走 `queue_cli.py send-final`，本地记录失败走
`repair-record`，不确定发送走显式 `reconcile`。不要再按固定 30/60 秒运行旧手动
轮询入口。

## 应急模式

仅在可靠调度器和队列消费者已停止、并确认没有任务正在 `processing` 时，才使用以下
旧手动流程。应急模式不得与队列消费者同时运行，否则可能破坏会话 FIFO 或重复回复。

每轮(建议间隔 60s)执行:

1. **拉取 + 列待处理**:运行
   `python scripts/brain_cycle.py`
   它会经 WebDAV 条件 GET 把 `conversations/*.jsonl` 同步到 `data/inbound/`,
   更新 `data/state.json` 的 etag,并打印每个会话的待处理入站消息(mid + 内容预览)。
   - 若输出"没有待处理消息",本轮结束。

2. **逐条(按 conv_id、mid 升序)检查消息**:对每条待处理消息:
   a. 运行 `python scripts/build_context.py --conv <conv_id>` 取上下文(**用户事实卡片 + 早期摘要 + 最近 N 条逐字**,已压缩、有界),
      再结合本条 `data/inbound/<conv_id>.jsonl` 里的内容,由你(大脑)生成回复文本。
   b. 把回复文本写入 `data/_reply.txt`(UTF-8;用编辑器/Write,不要用 shell echo,避免中文编码问题)。
   c. 应急模式不直接发送正式回复。恢复调度器将消息入队后，只能按
      `skill/queue_consumer.md` 使用 `send-final`，避免发送成功与本地记账之间形成
      重复发送窗口。

3. 全部处理完,本轮结束。

4. **每轮末压缩超长历史**(保持上下文有界):
   a. 运行 `python scripts/compact.py --check`。输出 `COMPACT_NONE` 则跳过本步。
   b. 对每个 `COMPACT_NEEDED <conv> raw=<n> keep=<N>`:
      - 读 `data/history/<conv>.jsonl` 的**较早部分**(除最近 N 条外)+ 现有 `data/history/<conv>.summary.md`、`data/history/<conv>.facts.json`(若有);
      - 用 Write **更新** `data/history/<conv>.summary.md`(融合旧信息,≤~1500 字,保留关键事实/进展)与 `data/history/<conv>.facts.json`(`{name/称呼, language, preferences, taboos, notes}`,按需补充);
      - 运行 `python scripts/compact.py --apply --conv <conv>`(把旧记录 gzip 归档到 `data/archive/<conv>/`,活跃 JSONL 截到最近 N)。
   c. 单个会话压缩失败只跳过该会话,不中断整轮。

## 约定与注意

- 会话 id:私聊 `u<对方uid>`,群聊 `g<gid>`；`send-final` 会据前缀选择发送目标。
- 游标只在"发送成功"后推进,保证至少处理一次、不重复回复、不自我循环(receiver 侧已过滤 bot 自身 uid=7)。
- 群消息仅在被 @ 时才会出现在 `conversations/`(receiver 侧 `SCOPE_GROUP_MENTION` 已处理),所以能进入待处理的群消息都应回复。
- 任何单条异常只跳过该条,不中断整轮。
- 配置:发送凭据在 `.env`(`VOCECHAT_SERVER_URL`/`VOCECHAT_API_KEY`/`BOT_UID`),WebDAV 凭据在 `share.env`。
