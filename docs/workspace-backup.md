# StockSeek 工作空间备份与恢复

关闭使用该工作空间的 StockSeek，并等待其后台进程退出。备份命令持有工作空间实例锁；应用仍占用时拒绝备份。以下示例使用新的备份文件名和不存在的恢复目录，父目录需已存在：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.backup create .\data\desktop .\output\stockseek-backup.zip
.\.venv\Scripts\python.exe -m tradingagents.desktop.backup verify .\output\stockseek-backup.zip
.\.venv\Scripts\python.exe -m tradingagents.desktop.backup restore .\output\stockseek-backup.zip .\output\restored-workspace
.\.venv\Scripts\python.exe -m tradingagents.desktop --data-dir .\output\restored-workspace --env-file .env
```

命令成功返回含 `status: ok`、格式版本和文件数量的 JSON，失败以非零状态退出。备份文件必须在源工作空间外，已有文件不覆盖。发布使用同卷硬链接，目标文件系统需支持硬链接；不支持时失败，源数据保留。

## 保存内容

通过 SQLite 备份 API 获取数据库一致副本，检查完整性及工作空间基本表。数据库内的持仓、报告登记、书籍、解析文本、策略历史和条件检查等记录随副本保留；工作空间内的书籍原文和研究产物一并保存。文件清单记录相对路径、字节数和 SHA-256，格式为 `stockseek-backup/1`。不读取真实凭据或触发模型调用。

排除根目录的 `webview/`、`workspace.lock`、SQLite WAL/SHM，以及任何层级名称以 `.env` 开头的文件和目录。系统凭据库、外部 `.env`、外部目录中的行情缓存和 SEC 快照不在备份范围；恢复后需在目标电脑重新配置。工作空间内其他文件会入包，包括可能含个人研究内容的文件，请自行妥善保存。压缩包未加密；指纹用于检查损坏，不证明来源可信。

拒绝符号链接、重解析点、非普通文件、不可读取目录，以及会造成跨平台路径歧义的文件名。最多 100000 个文件、合计未压缩 10 GiB；空目录不保存。文件访问失败、超限或数据库检查失败时不发布新备份。备份完成前应保持其他程序不修改工作空间文件；实例锁不能约束直接写文件的外部程序。

## 校验和恢复

校验将包中内容解至临时目录，核对完整清单、文件类型、大小、指纹及数据库完整性；拒绝越界路径、重复路径、大小写或 Unicode 规范化冲突、未声明内容和未知格式。完整校验通过后，恢复命令才独占创建目标目录，并以不覆盖已有文件的方式写入。

恢复期间存在 `RESTORE_INCOMPLETE` 标记，成功后移除。磁盘满等写入错误可能留下新目录和标记；StockSeek 拒绝打开带此标记的工作空间。保留失败现场，排除磁盘或权限问题后，从原备份恢复到另一个不存在的新目录；不要把部分恢复目录当成完整工作空间。不会自动删除失败现场或覆盖任何已有持仓和报告。

当前只做相同备份格式的恢复，不包含数据库版本迁移、恢复前界面向导、加密或外部缓存打包。已在 Windows 使用临时工作空间和合成文件回归；未完成 macOS、跨版本和真实书籍验收，因此路线图 D07 保持未完成。
