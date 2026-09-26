# 本站更新接口与客户端接入

网站管理软件发布，GitHub 仅负责构建。管理员从 GitHub 下载原始 ZIP，再上传网站并发布；网站自动读取包身份。客户端检查、校验、锁定主窗口、自动替换并重启。当前默认构建连接 https://viprpg.org，由构建变量 WINDY_UPDATE_SITE 写入包内 updateSite；普通更新窗口不提供地址编辑。预生产调试可在单独构建时显式设置 WINDY_UPDATE_SITE=https://staging.viprpg.org。此前指向预生产的旧安装包需手动安装一次新版；正式站仍须上传软件包并发布频道后才能提供更新。

## 请求

`GET https://<网站>/api/tools/windy-translator/updates/stable/windows-x64`

正式安装包会追加 URL 编码的 `applicationBuildId` 查询参数。开发源码或元数据缺失时不传该参数。参数仅用于识别实际构建，不决定网站版本号、说明或排序。接口公开可读，不依赖浏览器、登录 cookie、GitHub 令牌或网页挑战。

客户端仅接受 HTTPS 网站首页地址。更新文件与说明网页必须使用同一 HTTPS origin，重定向也必须保持同源；说明网页允许页内定位符（例如 `/resources#windy-translator`），接口和下载地址不允许。如将来部署独立下载域名，需要明确调整这一契约。响应使用 `Content-Type: application/json` 和 `Cache-Control: no-store`，UTF-8 JSON 上限 256 KiB。

## 可下载响应

```json
{
  "schemaVersion": 1,
  "tool": "windy-translator",
  "channel": "stable",
  "target": "windows-x64",
  "status": "available",
  "selectionRevision": 3,
  "releaseId": "release-002",
  "releaseSequence": 2,
  "version": "九月修订版",
  "publishedAt": "2026-09-18T10:00:00Z",
  "notes": "本版本的更新说明。可使用 Markdown，客户端以纯文本显示。",
  "notesUrl": "https://downloads.example.com/downloads/windy-translator/releases/release-002",
  "artifact": {
    "id": "artifact-002",
    "applicationBuildId": "windy:提交SHA:运行ID.尝试序号",
    "url": "https://downloads.example.com/api/tool-artifacts/artifact-002/download",
    "filename": "WindyTranslator.zip",
    "sizeBytes": 80378472,
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "format": "zip"
  },
  "installedRelease": {
    "applicationBuildId": "与请求参数完全相同的构建标识",
    "releaseId": "release-001",
    "releaseSequence": 1,
    "version": "九月初版"
  }
}
```

示例域名和 SHA-256 是占位值，实际必须使用当前网站和原始 ZIP 的摘要。`notes` 可省略；未知本机构建时提供前往 `notesUrl` 的按钮。其他可下载字段必填；文件名必须是 Windows 安全文件名，当前只支持大于 0 且不超过 95,000,000 字节的 ZIP。

`releaseSequence` 是本站不可变发布序号，正整数，不能使用 GitHub run number、tag 或日历版本代替。`selectionRevision` 是非负整数，表示网站推荐选择的修订。更高序号才提示新版；序号相同且 release ID 相同才显示已是推荐版本；推荐序号相同或更低时启动检查静默略过，手动检查显示暂无可用更新。字段中的版本名可由网站管理员自由命名。

`installedRelease` 可省略或为 `null`。只有当前工具／平台／渠道下，网站已为请求的构建标识建立唯一发布映射时才能返回，且必须原样返回该 `applicationBuildId`。未知构建、没有映射或存在多义映射时，不得把当前最新记录冒充已安装记录。客户端此时启动检查静默略过；手动检查提示无法确认当前版本，并提供网站下载入口，不执行自动替换。网站使用不带 cookie 的公开构建映射，不接收本机路径或用户数据。

## 暂停响应

```json
{
  "schemaVersion": 1,
  "tool": "windy-translator",
  "channel": "stable",
  "target": "windows-x64",
  "status": "paused",
  "selectionRevision": 4
}
```

暂停时客户端禁用下载。未知工具或未配置目标应返回 404；故障返回明确的 4xx／5xx，不能把故障包装成已是最新版。

## 构建身份与自动安装

PyInstaller 自动生成 `_internal/build-info.json`，含 schemaVersion、version、applicationBuildId、target、updaterProtocol: 1、autoUpdateProtocol: 1 和 updateSite。CI 身份仍为 `windy:<提交 SHA>:<run ID>.<attempt>`，本地构建使用独立 UUID。网站版本名和发布顺序不依赖 GitHub 版本号。人工版本号规则保持不变。

网站浏览器读取元数据用于填表，服务器通过 R2 范围读取 ZIP 索引及小型 build-info.json，再核对登记身份；同样用于恢复确认和发布校验。重复构建定位原记录，不产生第二个发布身份。旧包仍可登记历史映射，但没有自动安装协议与文件清单的客户端须先手动安装完整新版。

CI 在主程序外构建独立的 onefile/windowed `WindyUpdater.exe`，复制到发行目录的 `_internal/WindyUpdater.exe`，不再放在根目录；全部程序文件完成后生成 `package-files.json`，生成时检查助手位置。清单包括 build ID 以及每个发行文件的路径、长度、SHA-256，排除清单自身。构建和客户端均校验清单。ZIP 必须包含一个 `WindyTranslator/` 根目录，不使用 GitHub artifact 外层 ZIP。

更新时将助手复制到 `.windy-update-*` 临时目录后启动，保持其独立于被替换文件。新版兼容旧布局的文件清单，以便校验、备份和恢复；旧版客户端要求更新包根目录存在助手，无法自动安装新布局，须先手动安装一次完整新版。手动安装请使用新目录，避免覆盖解压留下旧的根目录助手。

每次下载前重新检查推荐版本，确认 release ID、序号与文件身份未变化；随后校验原始字节长度、SHA-256、ZIP 结构及包内 build ID。主程序和更新助手仅管理 EXE、_internal 与清单中的 tools 文件；保护 app_config.json、Works、dict、logs、自装工具和额外文件。已修改的受管文件或新文件与额外文件冲突时停止。不同大小写路径、路径越界、链接、重复条目和超过 2 GiB 的解压内容均拒绝。

启动自动检查默认开启，更新窗口内即时保存开关；保留已有显式关闭值。启动后只检查一次，无运行期间轮询。只有本机构建已映射且网站序号更高时自动展示窗口；未知、暂停、网络失败静默记录日志。手动入口保留。窗口只显示当前/新版本、说明、立即更新/稍后及开关。

点击立即更新前要求任务、外部工具和其他编辑窗口均已结束。保存配置后锁定主窗口，显示下载 0–80%、校验 80–90%、解压准备 90–100%；100% 表示准备完成即将重启。主程序等待临时目录中的独立助手就绪后退出；助手等待原进程结束，取得安装目录互斥锁，完成备份后记录事务并替换文件，启动新版并等待最多 90 秒的初始化确认。确认前失败则恢复原文件并启动旧版，抑制本次重启自动检查。

事务位于安装目录中的 `.windy-update-*`，助手自身不依赖被替换目录。成功确认后清理包与备份，下一次正常启动在助手退出后清理剩余临时文件。失败保留备份及日志；异常中断的替换由下次启动调用独立助手恢复。正常用户无需选择保存位置或手动解压。源码运行不执行自动替换。

## 验证与发布

先部署网站，再发布完整支持上述协议的客户端。当前目标仅为 staging；正式域名须另行核实后修改构建配置，不宣称生产已接通。新版本号仍由人类手动调整。校验来自 HTTPS 网站的摘要，不宣称为离线数字签名。

既有非 UI 检查及打包检查不能代替 Windows 自动替换/重启验收。新增测试代码、人工 UI 和真实更新演练遵守任务授权边界。没有进行的验收必须在交付说明中列明。
