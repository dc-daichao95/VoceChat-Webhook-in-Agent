# AnsweringMachine 大脑轮询手册

每轮执行:

1. 拉取:用 `share.env`(url/user/passwd)构造 `brain.pull.WebDAVClient`,调用
   `pull.pull_conversations(client, "conversations/", state, "data/inbound")`,
   其中 `state` 从 `data/state.json` 读取(不存在则 `{"conversations": {}, "seen_mids": []}`),完成后写回。
2. 扫描:对 `data/inbound/<conv_id>.jsonl` 逐文件读取记录(每行一个 JSON),用
   `brain.select.select_pending(conv_id, records, last_processed_mid, seen_mids)`
   选出待处理入站消息(`last_processed_mid` 取自 `state["conversations"][conv_id]`,
   `seen_mids` 取自 `state["seen_mids"]`)。
3. 逐条(按 conv_id、mid 升序)处理:
   a. 读 `data/history/<conv_id>.jsonl` 作为上下文(全量)。
   b. 由你(大脑)基于历史 + 本条消息生成回复文本。
   c. 把该入站记录追加进 `data/history/<conv_id>.jsonl`(direction=in)。
   d. 发送:私聊 `python send.py --target-uid <uid> --text -`(经 stdin 传文本);
      群聊 `python send.py --target-gid <gid> --text -`;需要 markdown 加 `--markdown`。
   e. 发送成功(退出码 0):把出站记录追加进 history(direction=out, in_reply_to=<mid>),
      更新 `state`:`conversations[conv_id].last_processed_mid=<mid>`、`seen_mids += <mid>`,写回 `data/state.json`。
   f. 发送失败:记日志,不推进游标(下轮重试);连续失败超过 3 次则跳过该 mid 并告警。
4. 无新消息则本轮结束。

注意:任何一步异常都只跳过当前条目,不中断整轮;游标只在"发送成功"后推进。
