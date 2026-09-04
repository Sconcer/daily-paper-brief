# Daily Paper Brief

> 面向 AI 基础设施、HPC 系统与 AI4Sci 的证据优先论文监控与评审报告流水线。

简体中文 · [English](README.md) · [项目网页](https://sconcer.github.io/daily-paper-brief/)

Daily Paper Brief 将每日 arXiv 来源论文转成一份可追踪的研究报告：筛选论文、保存来源证据、组织结构化评审、执行确定性校验、生成离线 HTML，并以哈希幂等方式发送一次。

Daily Paper Brief 是独立项目，与 arXiv 或 Cornell University 无隶属、授权或背书关系。

> **发布状态：源码已通过本地门禁和公开 CI，可供所有者最终审阅。** 自有代码采用 Apache-2.0；`humanizer-zh` 保持 MIT。论文内容和运行时生成报告不自动获得代码许可证授权。详见[开源风险评估](docs/OPEN_SOURCE_RISK_ASSESSMENT.md)。

## 它解决什么问题

```text
每日论文源
  → AI Infra / HPC Systems / AI4Sci 分主题筛选
  → PDF、正文、主图和来源记录
  → 中文为主的多视角评审
  → claim–evidence、复现性和写作信号审计
  → 严格 JSON 校验
  → 单文件 HTML
  → 飞书附件
```

它不等同于正式同行评审，不证明引用正确，不判断学术不端，也不输出所谓“AI 生成百分比”。

## 五分钟验证

### 1. 运行离线检查

```bash
python3 scripts/verify_bundle.py
python3 scripts/security_audit.py
python3 -m unittest discover -s tests -v
```

默认测试不会访问 arXiv 或飞书。

### 2. 创建本地配置

```bash
cp config/arxiv-monitor-config-phd.example.json arxiv-monitor-config-phd.json
```

检查分类、关键词、关注作者/机构、权重和每日论文上限。真实配置由 Git 忽略。

### 3. 构建环境

```bash
./scripts/bootstrap.sh
```

该脚本会创建 `.venv`、安装带哈希锁的 Python 二进制包、检查 Poppler、补充缺失的本地配置，并执行离线安全门禁和测试。

## 每日流水线

| 阶段 | 入口 | 关键产物 |
|---|---|---|
| Monitor | `arxiv_monitor_phd.py` | 冻结后的论文集合、去重状态、每日 Markdown |
| Prepare | `arxiv_report_pipeline.py prepare` | PDF、文本、主图、来源清单、写作描述量、评审模板 |
| Review | OpenClaw 主任务与有限批次 | 结构化评审批次 |
| Gate | `merge-batches`、`build` | 完整 `reviews.json`、纵向贡献图、离线 HTML、构建收据 |
| Deliver | `arxiv_send_html_to_feishu.py` | 收据校验、按目标群幂等的飞书附件、发送状态 |

“Agent 正常返回”不代表流水线成功。只有以下两种情况可以结束：

- 已验证当天没有新论文；
- `reviews.json` 完整、HTML 构建成功、附件 dry-run 成功，并获得新 `message_id` 或已存在的幂等 `message_id`。

## 目录速查

| 路径 | 用途 |
|---|---|
| `src/arxiv_monitor_phd.py` | API/RSS 抓取、评分、主题排序和每日去重 |
| `src/arxiv_report_pipeline.py` | 资产下载、安全清洗、结构校验、图和 HTML 构建 |
| `src/ai_writing_metrics.py` | 可复现描述量，不是作者身份分类器 |
| `src/arxiv_send_html_to_feishu.py` | HTML/构建收据校验和按目标群幂等发送 |
| `config/` | 示例监控配置；真实本地配置留在仓库根且被 Git 忽略 |
| `cron/` | 可移植的提示词、政策和任务模板 |
| `skills/` | Apache-2.0 学术风格基线和单独采用 MIT 的写作模式目录 |
| `tests/` | 离线单测与显式触发的联网探针 |
| `docs/` | 安全、来源、验证、命名和开源风险记录 |

## 手工运行确定性阶段

运行一次监控：

```bash
.venv/bin/python src/arxiv_monitor_phd.py
```

准备某天的论文资产：

```bash
.venv/bin/python src/arxiv_report_pipeline.py prepare \
  --input ./papers_to_expand.json \
  --date YYYY-MM-DD
```

评审批次、合并、构建、dry-run 和发送的精确命令位于渲染后的 cron 运行手册中。

仅在需要时运行联网探针：

```bash
.venv/bin/python tests/network_smoke_arxiv.py
```

## 安装 OpenClaw cron

设置本地运行值。持续访问前，应把 User-Agent 中的联系地址换成真实联系方式：

```bash
export FEISHU_CHAT_ID='YOUR_FEISHU_CHAT_ID'
export ARXIV_REVIEW_MODEL='openai/gpt-5.6-sol'
export DAILY_PAPER_BRIEF_USER_AGENT='DailyPaperBrief/1.0 (independent research tool; contact: you@example.org)'
```

只渲染私有运行文件，不修改 OpenClaw：

```bash
python3 scripts/install_openclaw_cron.py --render-only
```

人工检查被 Git 忽略的 `runtime/`，再显式创建任务：

```bash
python3 scripts/install_openclaw_cron.py --apply --acknowledge-local-agent-trust
```

该确认参数用于说明：OpenClaw 的 isolated 会话不是操作系统沙箱。安装器还会缩小任务工具面、拒绝 Daily Paper Brief 与旧 arXiv 任务重复，并且不会自动编辑或删除现有任务。

## 不得进入 Git 的内容

- `~/.openclaw/openclaw.json`、OAuth 数据库、token 和服务环境；
- 真实飞书目标和投递历史；
- `arxiv_pushed_ids.json`、`papers_to_expand.json`、`sent.json` 和锁文件；
- 下载的 PDF、全文、图片、评审批次、HTML 和会话记录；
- 真实的个人研究配置。

发送器只在 HTML 与构建收据一致后读取私有 OpenClaw 配置；上传的是已校验的同一份字节，目标群存放在权限为 `0600` 的运行时文件中，并按“内容 + 目标群”去重。`--dry-run` 不读取凭据，也不访问飞书。

## 来源、版权与 AI 写作信号

元数据与论文内容不是同一权利层。每篇 PDF、图片、代码和数据集可能采用不同许可证。没有逐项权利依据时，不应公开托管包含论文主图或全文摘录的生成报告。

按来源方要求保留致谢：

> Thank you to arXiv for use of its open access interoperability.

`AI-writing-signals-v1.0` 只审计可观察的写作辅助信号。覆盖门、证据位置、反证、干扰因素以及“不证明作者身份、不端、抄袭或研究有效性”的声明不得删除。

## 文档

- [Agent skill](SKILL.md)——仓库本身即一个可加载的 skill（克隆或软链到技能目录即可）
- [开源风险评估](docs/OPEN_SOURCE_RISK_ASSESSMENT.md)
- [命名决策](docs/NAME_DECISION.md)
- [安全模型](docs/SECURITY.md)
- [来源清单](docs/SOURCE_MANIFEST.md)
- [验证记录](docs/VALIDATION.md)
- [来源与改写边界](docs/PROVENANCE.md)
- [第三方声明](THIRD_PARTY_NOTICES.md)

## 许可证

项目自有文件采用 [Apache-2.0](LICENSE)。随仓库提供的 `humanizer-zh`
快照继续使用其原有 MIT 许可证，具体边界见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
