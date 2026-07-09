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
│  └─ webdav_check.py        # WebDAV 连通性 / 往返 / 轮询成本自检
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
| `.env` | `.env.example` | 本机 `send.py` + receiver 环境变量参考 | `VOCECHAT_SERVER_URL` / `VOCECHAT_API_KEY`;`BOT_UID` 等 receiver 项 |
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

在 Cursor 会话中用 `/loop`(建议 30~60s 间隔)执行 `skill/loop_prompt.md` 描述的流程:
拉取(PROPFIND + 条件 GET)→ 选出待处理入站 → 读历史生成回复 → `send.py` 发回 → 记账(更新 `data/state.json`、`data/history/`)。

手动发送测试:

```powershell
python send.py --target-uid <uid> --text "hi from bot"
python send.py --target-gid <gid> --text "**hi**" --markdown
echo "多行内容" | python send.py --target-uid <uid> --text -   # 从 stdin 读长文本
```

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
