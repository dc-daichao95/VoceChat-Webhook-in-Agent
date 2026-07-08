# AnsweringMachine

VoceChat 自动应答机器人:dumb receiver(FastAPI, 部署在 fnOS NAS 的 Docker)收 webhook 并落盘到 WebDAV spool;本机 Cursor 会话作为"大脑",经 WebDAV 拉取新消息、基于历史生成回复,再用 `send.py` 经 bot API 发回。

设计文档见 `docs/superpowers/specs/2026-07-08-vocechat-answering-machine-design.md`。

## 安装

```bash
python -m pip install -r requirements.txt
```

## 配置

- receiver:复制 `.env.example` 为 `.env`,填 `BOT_UID` 等。
- 本机发送:同 `.env` 填 `VOCECHAT_SERVER_URL` / `VOCECHAT_API_KEY`。
- 本机拉取:`share.env` 填 `url` / `user` / `passwd`(fnOS WebDAV)。

`.env` 与 `share.env` 均不进 git。

## 部署 receiver(NAS)

```bash
BOT_UID=<bot uid> bash scripts/run_receiver.sh
```

在 VoceChat 的 bot 设置里把 webhook URL 指向 `http(s)://<nas>:8091/`。

## 运行大脑(本机)

在 Cursor 会话里用 `/loop`(30~60s)执行 `scripts/loop_prompt.md`。

## 连通性自检

```bash
python scripts/webdav_check.py --roundtrip   # PUT->GET->DELETE 往返
python scripts/webdav_check.py --bench 20    # 轮询成本
```

## 测试

```bash
python -m pytest -q
```
