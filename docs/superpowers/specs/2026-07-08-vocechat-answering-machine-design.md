# VoceChat 自动应答机器人(AnsweringMachine)设计规格

- 日期:2026-07-08
- 状态:已通过 brainstorming,待写实现计划
- 方案:A(接收端 dumb,Cursor 会话当大脑)+ 拓扑 B(receiver 部署公网,本机拉取)

## 1. 目标与范围

构建一个 VoceChat 自动应答机器人:接收 VoceChat bot 的 webhook 消息 → 由本机 Cursor 会话作为"大脑"、基于落盘的会话历史生成智能回复 → 出站调用 VoceChat bot API 发回。

范围(v1,YAGNI):

- 应答范围:私聊全部应答;群聊仅当被 @ 时应答。
- 消息类型:仅处理 `text/plain` 与 `text/markdown`;`vocechat/file` 及图片忽略。
- 回复智能来源:当前 Cursor 会话本体,仅基于该会话自身的落盘历史,不跨会话检索、不接外部知识库。
- 回复产物:文本 / markdown。
- 防自我循环:忽略 `from_uid == BOT_UID` 的消息。

非目标(留待后续版本):文件/图片收发、跨会话知识检索、历史截断策略、多 bot 编排、方案 C(SDK headless)自动化。

## 2. 架构与组件

三个解耦单元,通过文件系统这一明确接口通信。整体拓扑采用方案 B:dumb receiver 部署在公网服务器,本机 Cursor 定时拉取。

```
┌────────────── 公网服务器(可与 VoceChat 同机) ──────────────┐
│  VoceChat ──POST 新消息──► receiver(FastAPI, dumb)          │
│                              │ 追加写 spool                 │
│                              ▼                             │
│                   server_data/conversations/<conv_id>.jsonl│  ← 仅入站, append-only
└──────────────────────────────┬─────────────────────────────┘
                               │  本机定时 rsync/scp over SSH 拉取
                               ▼
┌────────────────────────── 本机(Cursor 所在) ───────────────────────┐
│  data/inbound/<conv_id>.jsonl   ← spool 的本地镜像(只读, rsync 覆盖) │
│  data/history/<conv_id>.jsonl   ← 权威完整历史(in+out, 大脑写)       │
│  data/state.json                ← 处理游标 + seen_mids               │
│                                                                     │
│  Cursor 会话(大脑, /loop):                                         │
│    1. rsync 拉 spool → data/inbound/                                │
│    2. 找 mid > last_processed_mid 的新入站                          │
│    3. 读 history 作上下文 → 生成回复                                 │
│    4. send.py 出站 POST /api/bot/... 回 VoceChat ───────────────────┼──► VoceChat
│    5. 追加 out 到 history + 更新 state                              │
└─────────────────────────────────────────────────────────────────────┘
```

组件职责:

| 单元 | 位置 | 做什么 | 依赖 |
|---|---|---|---|
| `receiver`(FastAPI, dumb) | 公网服务器 | 收 webhook、过滤(type/scope/自身 uid)、落盘 spool。不含任何智能、不负责发送。 | fastapi/uvicorn + 标准库;不依赖 Cursor |
| 落盘存储 | 双端 | 服务器 spool(仅入站)+ 本机 history(权威 in+out)+ state | 无 |
| `send.py` | 本机 | 把一段文本出站发回指定 uid/gid | requests + `x-api-key` |
| Cursor 会话(大脑) | 本机 | 轮询、生成回复、调 send.py、记账 | 读 `data/`、调 `send.py`、rsync |

"dumb" 指 receiver 只做机械搬运(收→过滤→落盘),不做任何语义理解或回复决策;智能全在大脑侧。

## 3. 连通性(方案 B)

| 链路 | 方向 | 怎么通 | 备注 |
|---|---|---|---|
| VoceChat → receiver | 公网内部 | 直接 HTTP,无需内网穿透 | webhook URL = `http(s)://<服务器>:<端口>/` |
| server spool → 本机 | 本机拉取 | `rsync`/`scp` over SSH,定时 | 本机出站 SSH,内网访问公网可行 |
| 本机 send.py → VoceChat | 本机出站 | 出站 HTTP POST + `x-api-key` | 内网访问公网,通 |

跨 IP 只发生在"VoceChat → receiver"(公网内部,直连)与"本机 ↔ 服务器/VoceChat"(本机出站,通)。Cursor 读消息为纯本地磁盘读取,不走网络。

## 4. VoceChat 接口事实(已核实)

Webhook 载荷(POST 到 webhook URL):

```json
{
  "created_at": 1672048481664,
  "detail": {
    "content": "hello this is my message to you",
    "content_type": "text/plain",
    "expires_in": null,
    "properties": null,
    "type": "normal"
  },
  "from_uid": 7910,
  "mid": 2978,
  "target": { "gid": 2 }
}
```

- `content_type`:`text/plain` / `text/markdown` / `vocechat/file`。
- `type`:`normal`(新消息)/ `edit` / `deletion` / `reply` / `like`;另有 signup 等事件。仅处理 `normal`。
- `target`:含 `gid`(群)或 `uid`(私聊)。
- `properties`:携带 @ 提及信息与图片元数据;确切结构需 Phase 0 用真实 payload 核实。

发送 API:

- 私聊:`POST {server_url}/api/bot/send_to_user/{uid}`
- 群聊:`POST {server_url}/api/bot/send_to_group/{gid}`
- header:`x-api-key: <bot api key>`,`content-type: text/plain` 或 `text/markdown`
- body:原始文本

保存 webhook 时 VoceChat 可能以 GET 探活,需返回 200。

## 5. 数据模型

会话 ID(`conv_id`):私聊 `u<from_uid>`(如 `u7910`);群聊 `g<gid>`(如 `g2`)。

目录:

```
# 服务器
server_data/
  conversations/<conv_id>.jsonl   # 仅入站, append-only
  raw/<timestamp>_<mid>.json      # Phase 0: 原样 raw payload

# 本机
data/
  inbound/<conv_id>.jsonl         # rsync 镜像(只读, 会被覆盖)
  history/<conv_id>.jsonl         # 权威完整历史(in + out)
  state.json                      # 处理游标 + seen_mids
```

`conversations/<conv_id>.jsonl` / `history/<conv_id>.jsonl` 每行一条:

```json
{"mid": 2978, "conv_id": "u7910", "direction": "in", "from_uid": 7910, "content_type": "text/markdown", "content": "你好,在吗?", "mentioned_bot": false, "created_at": 1672048481664, "recorded_at": 1672048482000}
{"mid": null, "conv_id": "u7910", "direction": "out", "from_uid": 0, "content_type": "text/markdown", "content": "在的,有什么可以帮你?", "in_reply_to": 2978, "created_at": 1672048490000, "recorded_at": 1672048490000}
```

- `direction`:`in`(用户→bot)/ `out`(bot→用户),大脑读历史时两者都作上下文。
- `mentioned_bot`:群消息是否 @ 了 bot(receiver 解析 `properties`);私聊恒 `false`。
- `in_reply_to`:出站回复对应的入站 `mid`。

`state.json`:

```json
{
  "conversations": {
    "u7910": { "last_processed_mid": 2978, "last_processed_at": 1672048490000 },
    "g2": { "last_processed_mid": 3010, "last_processed_at": 1672048500000 }
  },
  "seen_mids": [2978, 3010]
}
```

- `last_processed_mid`:该会话已回复到的最后入站 `mid`;大脑仅处理 `mid > last_processed_mid` 的入站。
- `seen_mids`:入站去重(VoceChat 偶尔重推)。

## 6. dumb receiver 行为

端点:

| 方法 | 路径 | 用途 | 返回 |
|---|---|---|---|
| GET | `/` | webhook 保存探活 | `200 "ok"` |
| POST | `/` | 接收新消息 | `200 {"status":"ok"}`(总是尽快返回 200) |
| GET | `/health` | 运维健康检查(可选) | `200 {"status":"healthy"}` |

POST `/` 流程(纯机械):

```
1. 读 JSON body(失败 → 记日志, 仍返回 200)
2. 只保留 detail.type == "normal";其它丢弃
3. 只保留 content_type ∈ {text/plain, text/markdown};其它丢弃
4. from_uid == BOT_UID → 丢弃(防自我循环)
5. 判定 conv_id 与 scope:
     target.uid 存在 → 私聊 → conv_id="u{from_uid}", 纳入
     target.gid 存在 → 群聊 → conv_id="g{gid}";
        仅当 properties 的 mentions 含 BOT_UID 才纳入, 否则丢弃
6. mid 去重:mid ∈ seen_mids → 丢弃;否则加入 seen_mids
7. 追加一行 in 记录到 server_data/conversations/{conv_id}.jsonl
8. 返回 200
```

Phase 0 降级行为:`RAW_DUMP=true` 时额外把每个原始 payload 写到 `server_data/raw/` 并打印;此阶段不做 scope/@ 过滤(仅 type + 自身 uid),用于确认字段真实结构。

失败隔离:任何解析/落盘异常都记本地日志、返回 200,绝不抛回 VoceChat 触发重推风暴。

## 7. Cursor 大脑轮询循环 + send.py

由 `/loop` 在当前 Cursor 会话内按 30~60s 间隔运行处理 prompt。每轮:

```
1. 拉取: rsync -az user@server:server_data/conversations/ data/inbound/
         (SSH 密钥登录;失败则记日志、跳过本轮)
2. 扫描: 遍历 data/inbound/<conv_id>.jsonl, 选出:
           direction=="in" 且 mid > last_processed_mid 且 mid ∉ seen_mids
3. 逐条处理(按 conv_id、mid 升序):
     a. 读 data/history/<conv_id>.jsonl 作上下文(全量)
     b. 大脑基于历史 + 本条生成回复文本(text/markdown)
     c. 追加 in 记录到 history
     d. 调 send.py:
          私聊 → send.py --target-uid <uid> --text "..." [--markdown]
          群聊 → send.py --target-gid <gid> --text "..." [--markdown]
     e. 成功 → 追加 out 记录到 history(in_reply_to=mid), 更新 state
     f. 失败 → 记日志, 不推进游标(下轮重试);retry_count 超阈值则跳过并告警
4. 无新消息 → 空转结束
```

send.py(独立 CLI):

```
用法:
  python send.py --target-uid <uid> --text <文本> [--markdown]
  python send.py --target-gid <gid> --text <文本> [--markdown]
行为:
  - 读配置(server_url + api_key)
  - uid → POST {server_url}/api/bot/send_to_user/{uid}
    gid → POST {server_url}/api/bot/send_to_group/{gid}
  - header: x-api-key + content-type(text/markdown 带 --markdown, 否则 text/plain)
  - body: 原始文本;--text - 时从 stdin 读(避免超长命令行/转义)
  - 成功 2xx → exit 0;失败 → exit 非 0 + stderr
```

## 8. 配置与密钥

服务器 `.env`(不进 git):

```
BOT_UID=0
SCOPE_DM=true
SCOPE_GROUP_MENTION=true
LISTEN_HOST=0.0.0.0
LISTEN_PORT=8091
DATA_DIR=./server_data
RAW_DUMP=true
```

本机 `.env`(不进 git):

```
VOCECHAT_SERVER_URL=https://chat.example.com
VOCECHAT_API_KEY=xxxxxxxxxxxxxxxx
SSH_TARGET=user@server
SSH_REMOTE_DATA=/opt/answeringmachine/server_data/conversations/
LOCAL_INBOUND_DIR=./data/inbound
HISTORY_DIR=./data/history
STATE_FILE=./data/state.json
SEND_MAX_RETRY=3
```

安全:

- 两份 `.env` 加入 `.gitignore`,仓库仅放 `.env.example`(占位)。
- `VOCECHAT_API_KEY` 只存本机 `.env`,不落盘 JSONL、不打印到会话、不进 git。
- SSH 密钥登录;建议服务器给 receiver 单独受限用户,rsync 只读 spool。
- 用 `python-dotenv` 加载;缺必填项启动即报错,不静默用空值。

## 9. 错误处理

| 场景 | 策略 |
|---|---|
| receiver 坏 JSON / 缺字段 | 记日志, 返回 200 |
| receiver 落盘失败 | 记日志 + 返回 200;因 seen_mids 未记录, 重推可自愈 |
| 重复 mid | seen_mids 去重丢弃 |
| rsync 失败 | 记日志, 跳过本轮, 下轮重试, 不推进游标 |
| send.py 失败 | 非 0 退出;不推进游标, 下轮重试;超 SEND_MAX_RETRY 则跳过并告警 |
| 配置缺失 | 启动即失败并提示 |
| 大脑生成异常 | 记日志, 跳过该条, 不推进游标, 不中断整轮 |

幂等原则:游标仅在"回复发送成功"后推进,保证至少处理一次、不漏;极端情况(发送成功但记账失败)可能重复发送,概率低、危害小,v1 接受。

## 10. 测试策略与里程碑

- **Phase 0(测通 webhook)**:receiver `RAW_DUMP=true`,VoceChat 给 bot 发消息 → `server_data/raw/` 出现该 JSON,确认 `properties`/`target`/`content_type` 真实结构;本机 `python send.py --target-uid <uid> --text "hi"` → VoceChat 收到回复。分别验证入站落盘与出站发送。
- **Phase 1(落盘+拉取)**:receiver 按 conv_id 落盘;本机 rsync 拉到 `data/inbound/`,能看到消息。
- **Phase 2(端到端私聊)**:接大脑 loop,私聊来消息 → 生成回复 → 发回 → 记账;几十秒内收到基于历史的回复,state/history 正确,不自我循环。
- **Phase 3(群 @ 应答)**:据 Phase 0 结构实现 @ 检测;群里 @ 才回。

自动化测试(pytest):

- `filters`:各类 payload(normal/edit、text/file、自身 uid、群 @/未 @、重复 mid)→ 断言是否落盘 + 落盘内容。
- `send.py`:mock HTTP,断言端点选择、header、content-type、退出码。
- `select`:给定 inbound + state → 断言选出的待处理 mid 集合。
- rsync / 真实 VoceChat 属手动冒烟,不进单测。

## 11. 项目结构

```
AnsweringMachine/
├─ README.md
├─ requirements.txt              # fastapi, uvicorn, requests, python-dotenv, pytest, responses
├─ .gitignore                    # .env, data/, server_data/, __pycache__
├─ .env.example
├─ app/
│  ├─ __init__.py
│  ├─ receiver.py                # FastAPI: GET/POST /, 过滤, 落盘 (dumb)
│  ├─ filters.py                 # 纯函数: type/scope/@/uid 判定 + conv_id 计算
│  ├─ storage.py                 # JSONL 追加、state.json 读写、seen_mids 去重
│  └─ config.py                  # dotenv 加载 + 必填校验
├─ send.py                       # 出站发送 CLI(本机)
├─ brain/
│  ├─ pull.py                    # rsync 拉取封装
│  └─ select.py                  # 从 inbound+state 选待处理(纯函数)
├─ scripts/
│  ├─ run_receiver.sh            # 服务器启动 receiver
│  └─ loop_prompt.md             # 供 /loop 使用的大脑处理 prompt
├─ tests/
│  ├─ test_filters.py
│  ├─ test_send.py
│  └─ test_select.py
└─ docs/superpowers/specs/
```

`filters.py` / `select.py` 抽为纯函数以便单测;`loop_prompt.md` 是大脑每轮操作手册。
