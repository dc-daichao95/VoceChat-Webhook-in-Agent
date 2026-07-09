# VoceChat 自动应答机器人(AnsweringMachine)设计规格

- 日期:2026-07-08
- 状态:已通过 brainstorming;传输链路已实测验证,待写实现计划
- 方案:A(接收端 dumb,Cursor 会话当大脑)+ 拓扑 B'(receiver 作 Docker 跑在 fnOS NAS,本机经 WebDAV 拉取)
- 传输:WebDAV over HTTPS(替代早期设想的 SSH/rsync);本机大脑用 Python 直连(`requests`),不依赖 Windows WebClient/Redirector
- 验证:`scripts/webdav_check.py` 已跑通 PROPFIND=207 / PUT=201 / GET=200(字节一致)/ DELETE=204;轮询开销实测极小(见 §13)

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
│    1. WebDAV PROPFIND 列表 + 条件 GET(仅下载变化文件)→ data/inbound/│
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
| WebDAV spool → 本机 | 本机拉取 | WebDAV over HTTPS(PROPFIND 列 + 条件 GET 下),Basic 鉴权 | 本机出站 HTTPS,防火墙友好;凭证见 `share.env` |
| 本机 send.py → VoceChat | 本机出站 | 出站 HTTP POST + `x-api-key` | 出站,通 |

全部为本机**出站** HTTPS,不需要本机有公网 IP、不需要内网穿透。WebDAV 用 **Python 直连**(`requests` + 标准库 `xml.etree` 解析 PROPFIND),**不依赖 Windows WebClient/WebDAV-Redirector**。

**验证结论(已通过 `scripts/webdav_check.py` 实测)**:凭证修正后 `PROPFIND=207`、`PUT=201`、`GET=200`(下载字节与上传一致)、`DELETE=204`,读写删往返全部可用。自签名证书用 `verify=False` 跳过(见 §12 选型理由)。

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
    "u7910": { "last_processed_mid": 2978, "last_processed_at": 1672048490000, "etag": "\"a1b2c3\"" },
    "g2": { "last_processed_mid": 3010, "last_processed_at": 1672048500000, "etag": "\"d4e5f6\"" }
  },
  "seen_mids": [2978, 3010]
}
```

- `last_processed_mid`:该会话已回复到的最后入站 `mid`;大脑仅处理 `mid > last_processed_mid` 的入站。
- `seen_mids`:本机侧入站去重(与服务器 `seen_mids.json` 独立),防止已处理的 mid 被重复回复。
- `etag`:该会话 spool 文件上次拉取到的 WebDAV `ETag`(或 `Last-Modified`),用于**条件 GET**;下轮 PROPFIND 若该文件 etag 未变则跳过下载,变了才 `GET`(带 `If-None-Match`,服务器可回 304 零传输)。

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
1. 拉取: WebDAV PROPFIND(Depth:1)列出 /webhook_share/conversations/ 下各 <conv_id>.jsonl 及其 etag/大小;
         对 etag 变化的文件才 GET(带 If-None-Match)下载到 data/inbound/,并更新 state 的 etag;
         复用单条 keep-alive HTTPS 会话;失败则记日志、指数退避、跳过本轮
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
- **Phase 0.5(测通 WebDAV)· 已完成**:`scripts/webdav_check.py` 实测 PROPFIND=207 / PUT=201 / GET=200(字节一致)/ DELETE=204,读写删往返可用;`--bench` 已验证轮询开销极小(见 §13)。
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
├─ requirements.txt              # fastapi, uvicorn, requests, python-dotenv, pytest, responses
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
│  ├─ webdav_check.py            # WebDAV 连通性/往返/轮询成本探测(已用于验证)
│  └─ loop_prompt.md             # 供 /loop 使用的大脑处理 prompt
├─ tests/
│  ├─ test_filters.py
│  ├─ test_send.py
│  └─ test_select.py
└─ docs/superpowers/specs/
```

`filters.py` / `select.py` 抽为纯函数以便单测;`loop_prompt.md` 是大脑每轮操作手册。

## 12. 传输选型:Python 直连 vs 盘符挂载

WAN-only 场景下评估过两条 WebDAV 接入路径,选定 **Python 直连**:

| 维度 | 盘符挂载(WebDAV Redirector/WebClient) | Python 直连(`requests`,已采用) |
|---|---|---|
| 每次操作往返 | 更啰嗦(open 常触发 PROPFIND+HEAD+GET,有时 LOCK) | 精确控制:PROPFIND 列 + 条件 GET |
| 缓存新鲜度 | 目录/属性缓存(TTL 数十秒)→ **新消息可能延迟可见** | etag/If-None-Match 自控,新消息即刻可见 |
| 单文件上限 | 默认 ~50MB(需改注册表) | 无限制 |
| 自签名证书 | 严格校验,需导入证书 | `verify=False` 跳过 |
| 前置成本 | 装功能 + **重启** + 注册表 + 证书 | 无(`pip install requests`) |
| 可靠性/可观测 | 偶发卡死/掉盘,错误笼统 | 显式超时/重试/退避,错误码清晰 |

对小 JSON、30~60s 轮询的负载,两者原始带宽差异都可忽略;但挂载的**缓存陈旧**会拖慢应答,且带来重启/证书/50MB 等前置与可靠性代价,故不采用。(本机已安装 `WebDAV-Redirector` 功能但挂起待重启;本项目不依赖它。)

## 13. 轮询成本与网络/存储防护

`scripts/webdav_check.py --bench` 实测(经公网 duckdns):

- 单次 PROPFIND 响应 ~609 B;延迟 avg 67ms / p50 45ms / p95 489ms。
- 每 30s 轮询 → 2880 次/天 ≈ **1.7 MB/天**;每 60s → 1440 次/天 ≈ **856 KB/天**(仅列目录)。

**网络防护:**

- **条件 GET**:仅下载 etag 变化的文件;未变则 304 零传输,稳态几乎只剩 PROPFIND 开销。
- **复用单条 keep-alive HTTPS 会话**,避免每轮 TLS 握手(p95 尖峰多为新建连接/NAS 唤醒)。
- 轮询间隔 30~60s(不做亚秒轮询);失败**指数退避**,避免打爆 NAS/触发限流。
- 请求设显式超时(PROPFIND 15s、GET 30s),超时即跳过本轮。

**存储防护:**

- 本机 `inbound/` 每轮覆盖(有界);`history/`、`state.json` 为小文本;消息均为小 JSON。
- Phase 0 后关闭 `RAW_DUMP`;处理完的 inbound 及时清理。
- 长期隐患:NAS 侧会话 JSONL 可能随时间无限增长 → 后续版本加**定期轮转/压缩**(超过阈值切分或归档),v1 暂不实现。

结论:传输开销极小,frequent polling 不会造成网络或存储异常。

## 14. 后续增强(TODO)

以下待办来自 v1 上线联调期间的真实使用反馈(私聊/群多用户实测),按建议优先级排列:

1. **会话级隔离修复(高)**:当前"数据"按会话隔离(独立 JSONL + 游标),但"大脑"是同一个 Cursor 会话同时看到所有对话,存在跨会话信息泄漏(实测:私聊里说出了群里设置的称呼)。方案:**每个会话一个独立 subagent**,或在 loop 里严格只把"当前会话历史"喂给回复生成。
2. **上下文压缩 / 超长历史处理(高)**:当前无压缩、无截断,生成回复时全量读该会话历史,长期会撑大上下文、无限增长。方案:上下文只取最近 N 条 + 对更早的做摘要;NAS 侧会话 JSONL 定期轮转/归档(超阈值切分);存储侧 gzip。
3. **敏感操作审批机制(中)**:为将来会产生副作用的能力设计审批——bot 先回确认提示,用户回复确认关键词 + 通过 **uid 权限白名单**才执行,否则默认不做。
4. **带权限的「清除历史」命令(中)**:允许授权用户清除某会话历史,需权限控制防止被随意清空。
5. **跨会话记忆(中)**:跨私聊/群记住同一用户(如 uid → 称呼/偏好);与第 1 项的隔离需求需一起权衡边界。
6. **接入实时数据(中)**:如实时天气等——接天气 API,或让大脑对时事/事实类问题用联网搜索(时效/准确性权衡)。
7. **WebDAV 密码不明文存储(中)**:当前 `share.env` 明文保存 `passwd`(已 gitignore,不进仓库,但仍明文落盘)。改为 **Windows 凭据管理器 / `keyring`**:密码存入 OS 凭据保险库(DPAPI 加密、绑定当前用户),`share.env` 仅保留 `url`+`user`;运行时按 `环境变量 > keyring > share.env(兼容回退)` 的优先级解析,`scripts/webdav_check.py` 与大脑拉取共用该解析逻辑。
8. **精确 token 用量统计(低)**:大脑侧无法拿到精确 token 计数;需在服务侧记录每次模型调用的 usage 才能统计。
9. **多平台接入(可选)**:Telegram(官方 Bot API,最简单)、企业微信 / 微信公众号(官方回调),复用现有"接收→大脑→回发"架构;个人微信无官方接口、不建议。

实施顺序建议:先 1、2(纠正隔离缺口 + 控制上下文膨胀),再按需推进其余。上述均待正式安排开发迭代;不在 v1 范围内。
