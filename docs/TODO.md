# AnsweringMachine 待办(TODO)

待办来自 v1 上线联调期间的真实使用反馈(私聊/群多用户实测)。
原先记录在设计规格 `docs/superpowers/specs/2026-07-08-vocechat-answering-machine-design.md` §14,现独立到本文件维护。

## 已完成

- [x] **上下文压缩 / 超长历史处理** — 回复上下文改为「事实卡片 + 滚动摘要 + 最近 N 条」,超阈值(raw>40,留 20)gzip 归档旧记录到 `data/archive/`。
  设计:`docs/superpowers/specs/2026-07-09-context-compaction-design.md`;实现:`brain/compaction.py`、`brain/context.py`、`scripts/compact.py`、`scripts/build_context.py`。
- [x] **接入实时数据(联网)** — 通过 **browser-use skill**:Cursor 经 CDP 驱动本地 Chrome 联网查询(天气、时事等),无需额外 LLM Key。
  设计:`docs/superpowers/specs/2026-07-09-browser-use-skill-design.md`;技能:`.cursor/skills/browser-use/`。

## 待办

### 高

- [ ] **会话级隔离修复**:数据按会话隔离(独立 JSONL + 游标),但"大脑"是同一个 Cursor 会话同时看到所有对话,存在跨会话信息泄漏(实测:私聊里说出了群里设置的称呼)。方案:每个会话一个独立 subagent,或在 loop 里严格只把"当前会话上下文"喂给回复生成(上下文压缩的 `build_context` 已为此打下基础)。

### 中

- [ ] **敏感操作审批机制**:为将来会产生副作用的能力设计审批——bot 先回确认提示,用户回复确认关键词 + 通过 uid 权限白名单才执行,否则默认不做。
- [ ] **带权限的「清除历史」命令**:允许授权用户清除某会话历史,需权限控制防止被随意清空。
- [ ] **跨会话记忆**:跨私聊/群记住同一用户(如 uid → 称呼/偏好);与"会话级隔离"的边界需一起权衡。
- [ ] **WebDAV 密码不明文存储**:当前 `share.env` 明文保存 `passwd`(已 gitignore,但仍明文落盘)。改为 Windows 凭据管理器 / `keyring`(DPAPI 加密、绑定当前用户),`share.env` 仅留 `url`+`user`;运行时按 `环境变量 > keyring > share.env(兼容回退)` 解析,`scripts/webdav_check.py` 与大脑拉取共用该逻辑。
- [ ] **优化 /loop 定时逻辑**(**待澄清后再实现**):当前痛点——① 固定 60s 轮询不够灵活;② 夜间无人使用仍在轮询,浪费;③ loop 会自动中断,续跑不可靠。可选方向(**需与用户确认**):
  - **间隔策略**:a) 自适应退避(有消息时快 ~15–30s,连续空闲逐步拉长到 ~2–5min,来消息再变快);b) 固定但更长(如 120s);c) 维持固定。
  - **静默时段**:夜间(如 00:00–07:00,时段可配)暂停或大幅拉长轮询;是否需要"时段窗口"配置。
  - **稳定性 / 自动中断**:先定位"自动中断"根因(会话结束 / harness / shell 退出?);目标是 loop 可靠续跑或自动恢复;是否改用更稳的调度(OS 计划任务 / 常驻后台脚本)而非依赖 `/loop` 会话存活。
  > 备注:因转入后台多任务模式、无法交互提问,以上澄清项暂以选项形式记录;确认选择后再开 SDD(spec→plan→实现)。

### 低 / 可选

- [ ] **精确 token 用量统计**:大脑侧无法拿到精确 token 计数;需在服务侧记录每次模型调用的 usage 才能统计。
- [ ] **多平台接入**:Telegram(官方 Bot API,最简单)、企业微信 / 微信公众号(官方回调),复用现有"接收→大脑→回发"架构;个人微信无官方接口、不建议。

## 实施顺序建议

先做剩余「高」项(会话级隔离,纠正跨会话泄漏),再按需推进其余。
