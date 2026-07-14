# 架构

Personal Academic Radar v0.8.0 固定为本地优先、单用户、私有数据和本地 SQLite。云端同步、共享数据库、公共网站与多租户不在本版本范围内。

## 信任边界

- **公开仓库**：代码、迁移、测试和通用示例。
- **私有状态目录**：配置、画像、SQLite、反馈、摘要证据、Codex 队列/结果、PDF、日志、报告和备份。
- **外部元数据服务**：只用于采集可追溯论文元数据和原始摘要；不会接收反馈或完整研究画像。
- **Codex 宿主**：读取本地队列、完整画像和反馈样例，返回严格 JSON 判断；应用本身没有直接模型提供商。
- **本地网页**：默认仅监听回环地址，使用 CSRF、CSP、安全响应头、原子写入和任务并发锁。

## 数据流

1. Crossref/OpenAlex 采集并用 DOI 或规范化标题哈希去重。
2. 出版类型治理组合 Crossref/OpenAlex 类型、来源种类、标题规则和证据；只允许 Journal Article 与 Conference/Proceedings Article。
3. 摘要补全复用本地同 DOI 记录，再访问官方公共元数据和结构化出版商页面；原始摘要与 AI 推荐理由存放在不同字段。
4. 合格且需要判断的记录导出为 Codex 队列；导入必须完整覆盖队列且匹配画像版本与反馈快照。
5. 默认产品视图同时应用出版类型 allowlist 和相关性阈值。

## 核心表

- `papers`：论文身份、元数据、摘要证据、出版类型证据、治理状态与重判标记。
- `observations`、`source_runs`：来源观测与每次采集结果。
- `screenings`：Codex 判断、模型标签、画像版本、运行编号与理由。
- `run_papers`：运行中的 collected/new/candidate/selected/selected_new 关系，定义“今日”。
- `paper_feedback`、`feedback_events`：当前反馈与内部恢复审计。
- `abstract_attempts`：每个摘要渠道的结果、URL、证据类型与失败原因。
- `cleanup_audits`：清洗预览、备份、报告和恢复说明。
- `task_runs`：本地后台任务状态、进度和互斥锁。
- `fulltext_files`：私有 PDF 路径、文件名、SHA-256、大小和论文绑定。

## 恢复模型

迁移和清洗都先调用 SQLite 在线备份并检查完整性。迁移只向前追加且幂等；清洗不物理删除论文。配置修改先备份再原子替换，失败时原文件保持不变。所有可恢复操作在私有状态中留下报告，不进入 Git。
