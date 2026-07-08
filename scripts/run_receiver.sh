#!/usr/bin/env bash
# 在 fnOS NAS 上构建并运行 receiver 容器;把 WebDAV 暴露的目录挂进容器 /webhook_share
set -euo pipefail
IMAGE=answeringmachine-receiver
docker build -t "$IMAGE" .
docker run -d --name "$IMAGE" --restart unless-stopped \
  -p 8091:8091 \
  -e BOT_UID="${BOT_UID:?set BOT_UID}" \
  -e SCOPE_DM=true -e SCOPE_GROUP_MENTION=true -e RAW_DUMP=true \
  -v /vol1/webhook_share:/webhook_share \
  "$IMAGE"
