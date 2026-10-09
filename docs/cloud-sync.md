# Sites 部署与数据同步

本地 SQLite 是采集、评分、来源配置、研究画像确认及 PDF 管理的主库。可选的 Sites 版以 D1 保存应用记录，支持论文、推荐日期、来源状态、本人画像和反馈。云端反馈回传本地，按 UTC 修改时间解决冲突；相同事件不会重复写入历史。评分导出时冻结反馈事件游标；导出后收到的新偏好（包括延迟回传）或证据变化，在导入后仍保留待重评。收藏及阅读状态更新不改变语义偏好。

## 访问

公开站点允许免登录浏览论文和推荐。研究画像、偏好反馈、检查页面与修改接口仅接受站点所有者的 ChatGPT 身份；所有者邮箱放在 Sites 的私有运行配置中。匿名响应不包含反馈或完整研究画像。访问范围由 Sites 控制，修改为公开必须得到用户明确授权。

本项目站点：<https://personal-academic-radar.thuscuf.chatgpt.site>。

## 本地配置

配置与凭据放在私有状态目录，不进 Git。在现有 config.toml 加入：

```toml
[cloud_sync]
enabled = true
endpoint = "https://your-site.your-workspace.chatgpt.site"
credentials_file = "cloud-sync.json"
```

凭据文件权限设为 `0600`，包含 `sync_token` 与私有站点自动同步使用的 `sites_token`。同步凭据由所有者安全配置，并作为 Sites 的 `SYNC_TOKEN` 私密运行变量；不要把实际值写入命令参数、日志、仓库或文档。程序拒绝非 Sites HTTPS origin 和重定向，避免向其他地址转发凭据。

```bash
academic-radar sync --config /path/to/private/config.toml
academic-radar service install-sync --config /path/to/private/config.toml --interval 300
```

macOS 同步服务为 `com.personal-academic-radar.sync`，每 5 分钟执行一次，并在登录后启动。每日评分完整导入后也自动调用同步。并行调用通过锁合并；网络故障保留本地库和云端上一完整快照，后续调用可恢复。电脑离线时云端仍可读，但采集与 host 评分等待电脑恢复。周期是同步检查间隔，不保证断网时或首次大批上传时立即一致。

同步先拉取反馈，再使用 SQLite 一致性读取导出记录。上传采用分片、逐记录 SHA-256 和完整快照校验，只有全部匹配才原子切换活动版本。后续快照复用相同校验和的已有记录，仅上传变化部分。云端保留上一完整版本作为恢复依据。

GitHub 和 Sites 源仓库只保存代码、测试、schema 和迁移。SQLite/WAL、PDF、配置、画像原文件、队列、结果、日志、备份与凭据不上传源仓库。向 D1 的应用记录同步是用户明确启用的独立通道。公开来源链接移除采集者联系信息和凭据查询参数，原始溯源仍留在本机。

## 部署与验证

站点源码在 `sites/`，逻辑 D1 绑定为 `DB`。数据库迁移由 Drizzle 生成并随部署包保存，不能把个人数据写进迁移。使用 Sites 插件的发布流程推送和打包精确源码，再部署保存版本。

```bash
cd sites
npm ci
npm run db:generate
npm run build
npm run validate
node scripts/test-worker.mjs
```

站点修改应同步到 GitHub 的对应源码，并重新发布。仅更新论文数据或反馈不需要重新部署源码。

`verify` 会提示评分是否陈旧、旧判定标准剩余量、来源降级与云同步故障。私有 `cloud-sync-status.json`、`sync.stderr.log` 和 `sync.stdout.log` 提供恢复信息。摘要溯源缺口可使用 `abstracts verify-provenance` 在线核验；只有与本地原摘要全文一致的 DOI 元数据才补入真实来源，不改写摘要。其余缺口保留，并通过后续补全或人工证据核验。

云端不接收 PDF，也不直接操作本地来源配置或画像版本；这些操作在本地完成后同步。语义评分仍由 Codex 宿主执行，不引入独立模型 API。
