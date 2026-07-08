# VoceChat 自动应答机器人(AnsweringMachine)设计规格

- 日期:2026-07-08
- 状态:已通过 brainstorming,待写实现计划
- 方案:A(接收端 dumb,Cursor 会话当大脑)+ 拓扑 B'(receiver 作 Docker 跑在 fnOS NAS,本机经 WebDAV 拉取)
- 传输:WebDAV over HTTPS(替代早期设想的 SSH/rsync);本机大脑用 Python 直连 WebDAV,不依赖 Windows WebClient/Redirector

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

三个解耦单元,通过 WebDAV 目录这一明确接口通信。整体拓扑采用方案 B':dumb receiver 作 Docker 跑在 fnOS NAS 上,把入站消息落盘到 NAS 上一个由 WebDAV 暴露的 spool 目录;本机 Cursor 经 WebDAV(HTTPS)定时拉取。

```
┌────────────── fnOS NAS(公网可达, duckdns) ──────────────┐
│  VoceChat ──POST 新消息──► receiver(FastAPI, dumb, Docker) │
│                              │ 追加写本地目录             │
│                              ▼                            │
│   /webhook_share/conversations/<conv_id>.jsonl  ← 仅入站   │
│                     (该目录由 fnOS WebDAV 暴露)           │
└──────────────────────────────┬────────────────────────────┘
                               │  本机经 WebDAV(HTTPS, Basic 鉴权)拉取
                               ▼
┌────────────────────────── 本机(Cursor 所在, Windows) ───────────────┐
│  data/inbound/<conv_id>.jsonl   ← WebDAV GET 下载的本地镜像(只读)     │
│  data/history/<conv_id>.jsonl   ← 权威完整历史(in+out, 大脑写)       │
│  data/state.json                ← 处理游标 + seen_mids               │
│                                                                     │
│  Cursor 会话(大脑, /loop):                                         │
│    1. WebDAV PROPFIND 列表 + GET 下载 → data/inbound/               │
│    2. 找 mid > last_processed_mid 的新入站                          │
│    3. 读 history 作上下文 → 生成回复                                 │
│    4. send.py 出站 POST /api/bot/... 回 VoceChat ───────────────────┼──► VoceChat
│    5. 追加 out 到 history + 更新 state                              │
└─────────────────────────────────────────────────────────────────────┘
```

组件职责:

| 单元 | 位置 | 做什么 | 依赖 |
|---|---|---|---|
| `receiver`(FastAPI, dumb) | fnOS NAS(Docker) | 收 webhook、过滤(type/scope/自身 uid)、落盘到 WebDAV spool 目录。不含任何智能、不负责发送。 | fastapi/uvicorn + 标准库;不依赖 Cursor |
| WebDAV spool | fnOS NAS | 仅入站 spool(receiver 本地写、本机远程读) | fnOS WebDAV 服务 |
| 本机存储 | 本机 | history(权威 in+out)+ state + inbound 镜像 | 无 |
| `send.py` | 本机 | 把一段文本出站发回指定 uid/gid | requests + `x-api-key` |
| Cursor 会话(大脑) | 本机 | WebDAV 拉取、生成回复、调 send.py、记账 | WebDAV 客户端、`send.py` |

"dumb" 指 receiver 只做机械搬运(收→过滤→落盘),不做任何语义理解或回复决策;智能全在大脑侧。

## 3. 连通性(方案 B' · WebDAV)

| 链路 | 方向 | 怎么通 | 备注 |
|---|---|---|---|
| VoceChat → receiver | 公网 | 直接 HTTP(S) | webhook URL = NAS 上 receiver 的公网地址/端口 |
| WebDAV spool → 本机 | 本机拉取 | WebDAV over HTTPS(PROPFIND 列 + GET 下),Basic 鉴权 | 本机出站 HTTPS,防火墙友好;凭证见 `share.env` |
| 本机 send.py → VoceChat | 本机出站 | 出站 HTTP POST + `x-api-key` | 出站,通 |

全部为本机**出站** HTTPS,不需要本机有公网 IP、不需要内网穿透。WebDAV 用 **Python 直连**(`requests`/`webdavclient3`),**不依赖 Windows WebClient/WebDAV-Redirector**(该功能在本机为 `Available` 未安装;走 Python 直连可免安装、免重启,也绕开 WebClient 的单文件大小限制)。

**前置条件(当前未满足)**:fnOS WebDAV 账号需可用——用有效 fnOS 系统用户 + 对 spool 目录有读写权限。现测得 `share.env` 中凭证返回 401,需在 fnOS 侧修正后方可联通。

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
# fnOS NAS(receiver 本地写;经 WebDAV 暴露为 /webhook_share)
/webhook_share/
  conversations/<conv_id>.jsonl   # 仅入站, append-only
  seen_mids.json                  # receiver 侧入站去重(独立于本机)
  raw/<timestamp>_<mid>.json      # Phase 0: 原样 raw payload

# 本机
data/
  inbound/<conv_id>.jsonl         # WebDAV GET 下载的本地镜像(只读, 每轮覆盖)
  history/<conv_id>.jsonl         # 权威完整历史(in + out)
  state.json                      # 处理游标 + seen_mids(本机侧, 独立于服务器)
```

去重是两层、各自独立:receiver 侧 `/webhook_share/seen_mids.json` 避免 spool 出现重复行;本机 `state.json` 的 `seen_mids` + `last_processed_mid` 避免重复回复。二者互不依赖,任一层失效另一层仍可兜底(重复行会被本机游标拦截)。

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
- `seen_mids`:本机侧入站去重(与服务器 `seen_mids.json` 独立),防止已处理的 mid 被重复回复。

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
6. mid 去重:mid ∈ /webhook_share/seen_mids.json → 丢弃;否则加入
7. 追加一行 in 记录到 /webhook_share/conversations/{conv_id}.jsonl
8. 返回 200
```

Phase 0 降级行为:`RAW_DUMP=true` 时额外把每个原始 payload 写到 `/webhook_share/raw/` 并打印;此阶段不做 scope/@ 过滤(仅 type + 自身 uid),用于确认字段真实结构。

失败隔离:任何解析/落盘异常都记本地日志、返回 200,绝不抛回 VoceChat 触发重推风暴。

## 7. Cursor 大脑轮询循环 + send.py

由 `/loop` 在当前 Cursor 会话内按 30~60s 间隔运行处理 prompt。每轮:

```
1. 拉取: WebDAV PROPFIND 列出 /webhook_share/conversations/ 下各 <conv_id>.jsonl,
         GET 下载到 data/inbound/(Basic 鉴权, HTTPS;失败则记日志、跳过本轮)
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

receiver `.env`(NAS 上,不进 git):

```
BOT_UID=0
SCOPE_DM=true
SCOPE_GROUP_MENTION=true
LISTEN_HOST=0.0.0.0
LISTEN_PORT=8091
DATA_DIR=/webhook_share        # 指向 fnOS WebDAV 暴露的目录(容器内挂载)
RAW_DUMP=true
```

本机 `.env`(不进 git):

```
VOCECHAT_SERVER_URL=https://chat.example.com
VOCECHAT_API_KEY=xxxxxxxxxxxxxxxx
LOCAL_INBOUND_DIR=./data/inbound
HISTORY_DIR=./data/history
STATE_FILE=./data/state.json
SEND_MAX_RETRY=3
```

本机 WebDAV 凭证(`share.env`,不进 git):

```
url=https://<nas-host>:<port>            # fnOS WebDAV 根
user=<fnOS 系统用户>
passwd=<对应密码>
# 约定 spool 相对路径:/webhook_share/conversations/
```

安全:

- `.env` 与 `share.env` 均加入 `.gitignore`,仓库仅放 `.env.example`(占位)。
- `VOCECHAT_API_KEY`、WebDAV 密码只存本机,不落盘 JSONL、不打印到会话、不进 git。
- WebDAV 走 HTTPS + Basic;建议在 fnOS 给该账号最小权限(仅 spool 目录读写)。
- 用 `python-dotenv` 加载;缺必填项启动即报错,不静默用空值。

## 9. 错误处理

| 场景 | 策略 |
|---|---|
| receiver 坏 JSON / 缺字段 | 记日志, 返回 200 |
| receiver 落盘失败 | 记日志 + 返回 200;因 seen_mids 未记录, 重推可自愈 |
| 重复 mid | seen_mids 去重丢弃 |
| WebDAV 拉取失败(网络/401/超时) | 记日志, 跳过本轮, 下轮重试, 不推进游标 |
| send.py 失败 | 非 0 退出;不推进游标, 下轮重试;超 SEND_MAX_RETRY 则跳过并告警 |
| 配置缺失 | 启动即失败并提示 |
| 大脑生成异常 | 记日志, 跳过该条, 不推进游标, 不中断整轮 |

幂等原则:游标仅在"回复发送成功"后推进,保证至少处理一次、不漏;极端情况(发送成功但记账失败)可能重复发送,概率低、危害小,v1 接受。

## 10. 测试策略与里程碑

- **Phase 0(测通 webhook)**:receiver `RAW_DUMP=true`,VoceChat 给 bot 发消息 → `/webhook_share/raw/` 出现该 JSON,确认 `properties`/`target`/`content_type` 真实结构;本机 `python send.py --target-uid <uid> --text "hi"` → VoceChat 收到回复。分别验证入站落盘与出站发送。
- **Phase 0.5(测通 WebDAV)**:用有效 fnOS 凭证 PROPFIND 列出 `/webhook_share/`、GET 下载已上传的测试文件、PUT 一个测试文件验证写入(当前 `share.env` 凭证 401,需先修正)。
- **Phase 1(落盘+拉取)**:receiver 按 conv_id 落盘到 WebDAV spool;本机经 WebDAV 拉到 `data/inbound/`,能看到消息。
- **Phase 2(端到端私聊)**:接大脑 loop,私聊来消息 → 生成回复 → 发回 → 记账;几十秒内收到基于历史的回复,state/history 正确,不自我循环。
- **Phase 3(群 @ 应答)**:据 Phase 0 结构实现 @ 检测;群里 @ 才回。

自动化测试(pytest):

- `filters`:各类 payload(normal/edit、text/file、自身 uid、群 @/未 @、重复 mid)→ 断言是否落盘 + 落盘内容。
- `send.py`:mock HTTP,断言端点选择、header、content-type、退出码。
- `select`:给定 inbound + state → 断言选出的待处理 mid 集合。
- WebDAV 拉取 / 真实 VoceChat 属手动冒烟,不进单测。

## 11. 项目结构

```
AnsweringMachine/
├─ README.md
├─ requirements.txt              # fastapi, uvicorn, requests, webdavclient3, python-dotenv, pytest, responses
├─ .gitignore                    # .env, share.env, data/, server_data/, __pycache__
├─ .env.example
├─ app/
│  ├─ __init__.py
│  ├─ receiver.py                # FastAPI: GET/POST /, 过滤, 落盘 (dumb)
│  ├─ filters.py                 # 纯函数: type/scope/@/uid 判定 + conv_id 计算
│  ├─ storage.py                 # JSONL 追加、state.json 读写、seen_mids 去重
│  └─ config.py                  # dotenv 加载 + 必填校验
├─ send.py                       # 出站发送 CLI(本机)
├─ brain/
│  ├─ pull.py                    # WebDAV 拉取封装(PROPFIND 列 + GET 下)
│  └─ select.py                  # 从 inbound+state 选待处理(纯函数)
├─ scripts/
│  ├─ run_receiver.sh            # NAS(Docker)启动 receiver
│  └─ loop_prompt.md             # 供 /loop 使用的大脑处理 prompt
├─ tests/
│  ├─ test_filters.py
│  ├─ test_send.py
│  └─ test_select.py
└─ docs/superpowers/specs/
```

`filters.py` / `select.py` 抽为纯函数以便单测;`loop_prompt.md` 是大脑每轮操作手册。
