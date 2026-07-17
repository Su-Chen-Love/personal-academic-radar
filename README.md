# Personal Academic Assistant

[![Tests](https://github.com/Su-Chen-Love/personal-academic-radar/actions/workflows/test.yml/badge.svg)](https://github.com/Su-Chen-Love/personal-academic-radar/actions/workflows/test.yml)

Personal Academic Assistant 是面向单个研究者的本地优先文献助手。代码可以公开；配置、研究兴趣、SQLite、反馈、队列、摘要结果、日志和 PDF 始终保存在私有状态目录，默认是 `~/.local/share/personal-academic-radar`。

语义判断只使用 Codex 宿主的“导出队列 → 判断 → 原子导入”流程，不需要也不支持独立模型 API。摘要只从可追溯元数据或官方论文页获取，绝不生成或改写后冒充原始摘要。

## 最短安装流程

需要 Python 3.9 或更高版本：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
academic-radar setup
```

`setup` 会初始化或升级私有状态、在升级前在线备份 SQLite、运行完整性与配置验证，并在 macOS 安装可逆的用户级后台服务。完成后打开 <http://127.0.0.1:8765>。

已有旧状态位于其他目录时使用 `academic-radar setup --from-state /path/to/old/state`。程序不会静默导入仓库内的 `state/`，以免把示例或历史数据误当成当前私有库。

Linux 和 Windows 会完成初始化与验证，但不会自动安装后台服务；请按 [部署说明](references/deployment.md) 配置用户级启动项。非技术用户可以把 [Codex 辅助安装提示词](docs/ai-assisted-install.md) 交给 Codex。

## 六个页面

- 今日推荐：最近一次成功导入中新入选的论文，默认只显示紧凑推荐信息。
- 我的文献：只显示出版类型合格且达到当前阈值的论文，支持排序、筛选、收藏、导入 PDF，以及只粘贴 APA 引用和摘要的手动添加；新条目会进入下一次每日评分。
- 监测来源：实时搜索并预览可验证的期刊/会议来源，支持安全添加和移除。
- 研究兴趣：当前画像、反馈触发的更新建议和版本回退/切换；系统不会替用户生成固定提示词。
- 偏好反馈：编辑兴趣、理由、收藏和阅读状态；历史审计不作为用户模块展示。
- 更新与检查：根据当前缺口生成一份可交给 Codex 的完整更新任务。

所有用户可见筛选都排除 Editorial、Corrigendum、Extended Abstract 等非正式研究内容，以及低于相关性阈值的记录。被排除记录只在内部保留最小身份和审计证据，避免反复抓取。

## 每日 Codex 流程

每天一次的本地 Codex 自动任务先以 14 天窗口调用元数据 API，再按已验证映射核验每种期刊截至当天已经出版的最近两期。确定性流程覆盖 Springer Nature、Taylor & Francis、IEEE Xplore、ACM Digital Library 与 SAGE；摘要按官网原文、出版商提交的 Crossref 完整摘要、DOI 精确匹配的 OpenAlex 完整摘要顺序回退并保留 provenance。ScienceDirect 与 INFORMS 使用官网卷期页批量展开摘要并逐篇补漏。所有结果先严格预览、去重并原子导入；搜索片段、Highlights 或生成式改写不会被当作摘要。之后才按需检查画像反馈、建立一次冻结队列、逐篇判断并原子导入结果。核心命令为：

```bash
python scripts/paper_monitor.py collect-only \
  --config ~/.local/share/personal-academic-radar/config.toml

academic-radar official plan \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --output ~/.local/share/personal-academic-radar/official/plan-latest.json

academic-radar official collect-supported \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --output ~/.local/share/personal-academic-radar/official/supported-latest.json

# 按计划完成官网核验并生成严格 JSON 后：
academic-radar official import \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --file /path/to/official-results.json
academic-radar official import \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --file /path/to/official-results.json --apply

python scripts/paper_monitor.py agent-export \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --no-collect --batch-run <collect-only 返回的 run_id>

python scripts/paper_monitor.py agent-import \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --results ~/.local/share/personal-academic-radar/agent-results/<results.json>
```

官网卷期导入要求同一期中的研究论文具有完整官网摘要，Editorial 等非研究内容可无摘要并由治理规则排除。队列与结果必须覆盖相同的全部 identity；画像哈希、反馈快照、运行编号或来源失败信息不一致时，导入会整体拒绝。我的文献只显示相关性分数大于或等于 0.70 的论文，低分记录留在内部用于去重和审计。

确定性官网适配器会直接读取出版商的卷期目录与论文结构化元数据；尚需浏览器的出版商继续逐篇核验。受阻卷期使用 `academic-radar official fail` 留下可恢复的失败证据，并可用 `academic-radar official status` 查看，不会把不完整卷期伪装成成功。

## 摘要补全与人工证据

```bash
academic-radar abstracts enrich \
  --config ~/.local/share/personal-academic-radar/config.toml

academic-radar abstracts export-missing \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --output missing-abstracts.json
```

自动流程依次复用同 DOI 本地记录，并访问 Crossref、OpenAlex、Semantic Scholar、Europe PMC、PubMed 和出版商结构化元数据。每次尝试都记录来源、URL、时间和失败原因；缺失项会反映在“更新与检查”的 Codex 任务中。人工导入接受严格 JSON/CSV 证据包，并校验 identity、URL、重复项和明显截断内容。

## 清洗、备份与恢复

清洗必须先预览：

```bash
academic-radar cleanup preview --config ~/.local/share/personal-academic-radar/config.toml
academic-radar cleanup apply --config ~/.local/share/personal-academic-radar/config.toml --report <preview.json>
```

预览使用 SQLite 在线备份并运行 `PRAGMA integrity_check`，输出逐项身份、原因、前后统计和恢复命令。应用只更新治理状态，不删库、不抹除收藏、反馈或 PDF。

常规检查：

```bash
academic-radar verify --config ~/.local/share/personal-academic-radar/config.toml
academic-radar service status --config ~/.local/share/personal-academic-radar/config.toml
academic-radar service logs --config ~/.local/share/personal-academic-radar/config.toml
```

## 安全边界

- GitHub 只保存代码、测试、迁移和通用示例。
- 不提交 `state/`、配置、SQLite/WAL、备份、队列、结果、PDF、日志、`.env` 或任何密钥。
- 网页默认只绑定 `127.0.0.1`；本版本不提供公共网站、云同步、共享数据库或多用户部署。
- 来源配置以原子方式写入并先备份；SQLite 恢复要求显式 `--replace`，且会先保存当前库。
- PDF 保存在私有目录，校验类型和大小，并用 SHA-256 去重。

## 开发与验证

```bash
PYTHONPATH=src PYTHONWARNINGS=error python -m unittest discover -s tests -v
git diff --check
python -m build
```

系统边界与运维细节见 [架构说明](docs/architecture.md) 和 [部署说明](references/deployment.md)。
