# AnsweringMachine 大脑轮询手册(供 /loop 使用)

## 回复准则(生成回复时遵循)

- **角色**:智能助手——用你的推理能力 + 该会话历史,实打实回答用户的问题。
- **语气/长度**:友好且简洁,默认 1~3 句;需要时再展开(复杂问题可分点或给步骤)。
- **语言**:跟随用户——用户用什么语言就用什么语言回复。
- **格式**:需要列表/代码/步骤时用 markdown(此时 `reply_and_record.py` 加 `--markdown`);否则纯文本。
- **诚实**:不确定或信息不足时,礼貌地追问或说明不确定,不要编造事实。
- **不复读身份**:不必每条都自报"我是助手";被问到再说明即可。

每轮(建议间隔 10~30s)执行:

1. **拉取 + 列待处理**:运行
   `python scripts/brain_cycle.py`
   它会经 WebDAV 条件 GET 把 `conversations/*.jsonl` 同步到 `data/inbound/`,
   更新 `data/state.json` 的 etag,并打印每个会话的待处理入站消息(mid + 内容预览)。
   - 若输出"没有待处理消息",本轮结束。

2. **逐条(按 conv_id、mid 升序)生成并发送回复**:对每条待处理消息:
   a. 读 `data/history/<conv_id>.jsonl`(若存在)作为上下文,再结合本条 `data/inbound/<conv_id>.jsonl` 里的内容,
      由你(大脑)生成回复文本。
   b. 把回复文本写入 `data/_reply.txt`(UTF-8;用编辑器/Write,不要用 shell echo,避免中文编码问题)。
   c. 运行:
      `python scripts/reply_and_record.py --conv <conv_id> --mid <mid> --reply-file data/_reply.txt`
      (需要 markdown 加 `--markdown`)。
      该脚本会发送回复、把入站+出站记录追加进 `data/history/<conv_id>.jsonl`、并推进
      `data/state.json`(`last_processed_mid`、`seen_mids`)。
   d. 脚本退出码 0 = 成功(已记账);非 0 = 发送失败,**不要**手动推进游标,下轮会重试。

3. 全部处理完,本轮结束。

## 约定与注意

- 会话 id:私聊 `u<对方uid>`,群聊 `g<gid>`;`reply_and_record.py` 会据前缀自动选 send_to_user / send_to_group。
- 游标只在"发送成功"后推进,保证至少处理一次、不重复回复、不自我循环(receiver 侧已过滤 bot 自身 uid=7)。
- 群消息仅在被 @ 时才会出现在 `conversations/`(receiver 侧 `SCOPE_GROUP_MENTION` 已处理),所以能进入待处理的群消息都应回复。
- 任何单条异常只跳过该条,不中断整轮。
- 配置:发送凭据在 `.env`(`VOCECHAT_SERVER_URL`/`VOCECHAT_API_KEY`/`BOT_UID`),WebDAV 凭据在 `share.env`。
