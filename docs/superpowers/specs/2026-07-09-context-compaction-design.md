# 设计:上下文压缩 / 超长历史处理

- 日期:2026-07-09
- 状态:待评审
- 关联:`docs/superpowers/specs/2026-07-08-vocechat-answering-machine-design.md` §14 第 2 项;`skill/loop_prompt.md`

## 1. 背景与目标

当前 `/loop` 生成回复时,大脑**全量读取** `data/history/<conv_id>.jsonl` 作为上下文(见 `loop_prompt.md` 步骤 2a、主 spec §7 行 199「全量」)。历史只增不减 → 随对话变长,上下文膨胀、token 成本升高、最终超出窗口。

目标:让单会话喂给回复生成的上下文**有界**——用「结构化事实卡片 + 滚动摘要 + 最近 N 条逐字」替代全量历史;超阈值时把旧记录 **gzip 归档**并从活跃 JSONL 移除。

**架构约束**:大脑无外部 LLM(无 Key 架构),摘要/卡片由**大脑在 loop 内产出**(用 Write 写文件),脚本只负责检测、归档、截断、落盘。

## 2. 范围

**In:** 上下文构建压缩(事实卡片 + 摘要 + 最近 N)、本地历史归档轮转(gzip)、融入 /loop 自动触发、相关纯逻辑单测。
**Out:** NAS 侧 `conversations/*.jsonl` 轮转(不改 receiver);§14 第 1 项"会话级隔离"(独立 TODO,本设计对其有正向帮助但不在此实现)。

## 3. 数据模型(每会话)

- `data/history/<conv_id>.jsonl` — **最近 N 条**逐字原始记录(默认 N=20)。
- `data/history/<conv_id>.summary.md` — **滚动摘要**(散文;大脑维护;软上限约 1500 字)。
- `data/history/<conv_id>.facts.json` — **结构化事实卡片**:
  ```json
  {"name": "", "language": "", "preferences": [], "taboos": [], "notes": []}
  ```
  字段可留空;大脑按需补充/修订。
- `data/archive/<conv_id>/<ts>.jsonl.gz` — 被折叠的旧原始记录(gzip;`data/` 已在 `.gitignore`)。

`state.json` 每会话增加:`raw_count`(可选缓存)、`summary_updated_at`、`archived_through_mid`(最后归档到的 mid,便于追溯)。

## 4. 阈值(默认,可配)

- `RECENT_KEEP = 20`:活跃 JSONL 保留的最近条数。
- `COMPACT_TRIGGER = 40`:raw 历史条数 > 该值时触发压缩(折叠除最近 20 外的约 20 条)。
- 摘要软上限约 1500 字(大脑自控)。
- 常量集中在 `brain/compaction_config.py`(或模块级常量),便于调整。

## 5. 组件

### 5.1 `brain/context.py`(纯函数,可测)
- `build_context(conv_id, history_dir) -> dict/str`:载入 `facts.json`(缺失→空卡片)+ `summary.md`(缺失→空)+ 最近 N 条 JSONL,组装成结构化上下文(供大脑读)。
- 输出顺序:事实卡片 → 摘要 → 最近 N 条逐字。缺任一部分优雅降级。

### 5.2 `scripts/compact.py`
- `--check [--conv <id>]`:统计各会话活跃 JSONL 条数;若 > `COMPACT_TRIGGER`,打印:
  - 待折叠的旧记录(除最近 `RECENT_KEEP` 外);
  - 现有 `summary.md` 与 `facts.json`;
  - 提示大脑:更新摘要/卡片后运行 `--apply`。
  - 输出机器可读的"待压缩会话列表"(供 loop 判断)。
- `--apply --conv <id>`(在大脑写好新的 `summary.md`/`facts.json` 之后):
  1. 读活跃 JSONL,切分为"旧"(前部)与"最近 N"。
  2. **原子归档**:先把"旧"写入 `data/archive/<conv_id>/<ts>.jsonl.gz`(成功后)再将活跃 JSONL 重写为"最近 N"。顺序保证失败不丢数据。
  3. 更新 `state.json`:`summary_updated_at`、`archived_through_mid`。

### 5.3 `scripts/build_context.py`(或并入 `brain_cycle.py`)
- 打印某会话 `build_context(...)` 的结果,供大脑在步骤 2a 读取。

## 6. 流程(融入 /loop)

改 `skill/loop_prompt.md`:
- **步骤 2a(回复上下文)**:改为读 `build_context`(事实卡片 + 摘要 + 最近 N),不再全量读 JSONL。
- **新增步骤 4(每轮末压缩)**:
  1. `python scripts/compact.py --check`。
  2. 对每个"待压缩"会话:大脑读旧记录 + 现有摘要/卡片 → 用 Write 更新 `data/history/<conv>.summary.md` 与 `.facts.json`(融合旧信息,保留称呼/语言/禁忌等)。
  3. `python scripts/compact.py --apply --conv <conv>`(归档 + 截断)。
  4. 任一会话压缩失败只跳过该会话,不中断整轮。

## 7. 错误处理与边界

- 归档原子性:先 gzip 成功再截断;`--apply` 前校验大脑已产出 summary/facts(缺则拒绝并提示)。
- `context.py` 对缺失 summary/facts/JSONL 全部优雅降级。
- 并发:单机单 loop,无并发写;`--apply` 期间不与同会话回复交叉(loop 顺序执行)。
- 兼容:已有会话无 summary/facts 时,首次超阈值触发即建立;历史行为向后兼容(未超阈值的会话表现不变)。

## 8. 测试

- `brain/context.py`:有/无摘要、有/无 facts、最近 N 截取正确、缺失降级。
- `scripts/compact.py` 纯逻辑:切分点正确(留最近 N)、归档文件内容 = 旧记录、截断后 JSONL = 最近 N、gzip 可回读;用 `tmp_path`。
- 全量 `pytest` 保持 0 失败。

## 9. 验收

- 构造 >40 条历史的会话,跑一轮:回复上下文变为 卡片+摘要+最近20;`compact --apply` 后活跃 JSONL=20 条、`archive/<conv>/*.jsonl.gz` 含被折叠记录、`summary.md`/`facts.json` 存在且合理;`state` 游标与 `archived_through_mid` 正确。
- 未超阈值会话:行为与现状一致。
