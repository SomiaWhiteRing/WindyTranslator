# 本站更新接口与客户端接入

当前客户端实现检查、完整 ZIP 下载、校验和引导手动安装。网站接口尚未上线；本文件是与待开发下载中心对齐的首版契约。更新网站默认值来自网站仓库的生产 `APP_ORIGIN`，用户可在更新窗口修改。未配置、404、非 JSON、协议不符、暂停和网络失败均有明确状态。

## 请求

`GET https://<网站>/api/tools/windy-translator/updates/stable/windows-x64`

正式安装包会追加 URL 编码的 `applicationBuildId` 查询参数。开发源码或元数据缺失时不传该参数。参数仅用于识别实际构建，不决定网站版本号、说明或排序。接口公开可读，不依赖浏览器、登录 cookie、GitHub 令牌或网页挑战。

客户端仅接受 HTTPS 网站首页地址。更新文件与说明网页必须使用同一 HTTPS origin，重定向也必须保持同源；如将来部署独立下载域名，需要明确调整这一契约。响应使用 `Content-Type: application/json` 和 `Cache-Control: no-store`，UTF-8 JSON 上限 256 KiB。

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

示例域名和 SHA-256 是占位值，实际必须使用当前网站和原始 ZIP 的摘要。`notes` 可省略，客户端仍提供打开 `notesUrl` 的按钮。其他可下载字段必填；文件名必须是 Windows 安全文件名，当前只支持大于 0 且小于 100,000,000 字节的 ZIP。

`releaseSequence` 是本站不可变发布序号，正整数，不能使用 GitHub run number、tag 或日历版本代替。`selectionRevision` 是非负整数，表示网站推荐选择的修订。更高序号才提示新版；序号相同且 release ID 相同才显示已是推荐版本；推荐序号更低时明确提示为较旧版本。字段中的版本名可由网站管理员自由命名。

`installedRelease` 可省略或为 `null`。只有当前工具／平台／渠道下，网站已为请求的构建标识建立唯一发布映射时才能返回，且必须原样返回该 `applicationBuildId`。未知构建、没有映射或存在多义映射时，不得把当前最新记录冒充已安装记录。客户端此时显示来源未知，允许手动下载，不自动弹出新版提示。网站使用不带 cookie 的公开构建映射，不接收本机路径或用户数据。

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

## 构建身份与下载行为

PyInstaller 自动生成并打包 `_internal/build-info.json`。CI 标识形式为 `windy:<提交 SHA>:<工作流 run ID>.<构建 attempt>`，本地构建使用独立 UUID，避免将修改中的源码或不同构建误认为同一安装包。源码直接运行不发送已安装身份。网站管理员可从 ZIP 内或该次 GitHub Release 的 `release-manifest.json` 读取 `applicationBuildId`；它只作为人工发布时的来源元数据，不触发网站同步。

每次下载前重新读取推荐接口，确认 release ID、序号和文件身份未变更。文件 GET 返回原始字节，若提供 Content-Length 必须与 `sizeBytes` 一致，不做内容编码转换；网站文件端点仍需检查撤回状态。客户端以临时文件流式下载，展示进度，支持取消；完整长度、SHA-256 和 ZIP 检查通过后才替换用户选定的 ZIP 目标。不会自动解压、执行或替换软件文件。失败、取消不将下载记录当作安装记录，重试从头下载。

启动检查可关闭，默认关闭。检查与下载在后台线程运行，界面只在 Tk 主线程更新。启动检查失败或来源未知时只写日志；发现已识别安装对应的更高本站序号时展示更新窗口。用户至少要手动安装一次带此功能的客户端，旧程序不会凭空获得更新入口。

自动安装、签名协议、断点续传、更新助手和回滚属于后续功能。本阶段校验的是来自所配置 HTTPS 网站的文件长度与摘要，不把下载校验宣称为发布者的离线数字签名。
