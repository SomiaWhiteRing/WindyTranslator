# WindyTranslator

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/SomiaWhiteRing/WindyTranslator)

## 下载与安装

1. 克隆此仓库：
   ```
   git clone https://github.com/SomiaWhiteRing/WindyTranslator.git
   cd WindyTranslator
   ```

2. 确保已安装Python 3.9或更高版本

3. （可选）创建并激活虚拟环境：
   ```
   python -m venv venv
   venv\Scripts\activate  # Windows
   source venv/bin/activate  # Linux/Mac
   ```

4. 直接运行程序：
   ```
   python scripts/pack_rtp.py
   python main.py
   ```

   首次运行或更新 RTP 源文件后，先执行 `pack_rtp.py` 生成离线资源库。

## OpenAI 兼容接口配置

在翻译配置，或世界观字典配置的“OpenAI 兼容端点”模式中，填写 API 基础地址和 Key 后，停止输入约 0.7 秒即会自动获取模型列表。基础地址应包含服务商要求的路径（例如 `https://example.com/v1`），不要填写完整的 `/chat/completions` 地址。打开已有配置时也会自动获取，可以点击“刷新模型”重新获取。

OpenAI 兼容模式的模型下拉框只显示当前接口返回的模型，不再提供固定示例列表；新建翻译配置的 API 地址和模型名称均为空。已有模型选择会保留，接口不支持模型列表、返回为空或获取失败时，仍可手动输入模型名称，再测试连接并保存。获取列表本身不会发起翻译请求。

## 自动更新

WindyTranslator 默认在启动时检查一次更新，仅当网站发布序号更高时提示；没有更新或检查失败时不打扰。主窗口右下角“检查更新…”可手动检查，同一窗口中的“启动时自动检查更新”开关立即保存。

选择“立即更新”后主窗口锁定，显示下载、校验和准备进度，随后自动关闭、替换程序并重新启动。配置、字典、Works 和自装工具保持原样；受管文件有本地修改时会停止并提示。请先完成当前任务并关闭其他编辑窗口及外部工具。更新失败时保留恢复备份和日志。

GitHub 自动构建的完整 ZIP 含构建身份、发行文件清单和独立更新助手。管理员下载 ZIP 后上传网站，后台自动识别，无需手工填写构建标识。首个支持自动安装的版本需要手动安装一次。详情见[网站更新接口说明](docs/website-updates.md)。

当前构建统一连接 `https://staging.viprpg.org`；地址由 CI 的 `WINDY_UPDATE_SITE` 写入安装包，不在普通窗口中编辑，不回退 GitHub。正式域名须另行核实后配置。

## 版本与自动发布

由人类手动编辑仓库根目录的 [RELEASE.md](RELEASE.md)：第一行填写 `# 年.月.当月序号`（例如 `# 2026.9.1`），下面填写当前版本的 Markdown 更新日志。月份为 1–12，序号从 1 开始，均不补零；CI 会校验格式和非空日志。版本号只能由人类手动调整，AI 代理、脚本和 CI 不得自动修改或递增版本号；日常开发可以整理更新日志正文。

每次提交到 `main`，或在该分支手动运行 **Build and Release (Windows)**，都会构建，并按以下规则发布：

- `nightly`：跟随发布分支的最新成功构建，标记为预发布。
- 正式 Release：只有 `RELEASE.md` 的版本号高于所有已发布正式版本时才发布，并标记为 GitHub **Latest**。沿用 `v` 标签前缀，例如人类将 `2026.9.1` 提高到 `2026.9.2` 后才发布 `v2026.9.2`；首次发布使用人类填写的版本号。版本号未提高、回退，或对应 Release 已公开时，只更新 nightly，保留正式版的标签、日志、下载文件和 manifest。

构建固定使用触发工作流的提交；过时构建会跳过发布，等待新提交的构建。正式版 ZIP 命名为 `WindyTranslator-windows-2026.9.1.zip`，与同次 Nightly 的包内容一致。Pull Request 只构建和上传 Actions 产物。

每个 Release 附带 `release-manifest.json`，记录提交 SHA、工作流运行、版本、文件大小和 SHA-256，发布日志也包含这些校验值。正式版发布后不再自动覆盖，nightly 覆盖不会保留旧包，复现问题时请同时记录版本号与提交 SHA；历史构建另受 GitHub Actions 产物保留期限制。单个附件上传限时 5 分钟、最多尝试 3 次，发布任务总限时 30 分钟；仍失败时工作流会失败，可在分支仍指向该提交时重跑失败任务，继续上传尚未公开的正式版草稿。正式版标签不会强制移动；GitHub 对 nightly 的多个附件替换不提供原子操作。

## 打包发布

使用 PyInstaller 生成“分体式（onedir）”可执行文件（Windows 打包需在 Windows 上执行）。

1. 创建并激活虚拟环境（建议）
   ```
   python -m venv venv
   venv\Scripts\activate  # Windows
   source venv/bin/activate  # Linux/Mac
   ```

2. 安装依赖
   ```
   pip install -r requirements.txt
   ```

3. 使用已维护好的 spec 构建（默认 onedir，GUI，无控制台）
   ```
   python -m PyInstaller --noconfirm --clean WindyTranslator.spec
   ```

   spec 自动将六套 RTP 源 ZIP 按内容去重，生成 `modules/RTPCollection/rtp-content.zip`；发行包只包含该资源库，保留六套资源各自的文件名和安装顺序。源码 ZIP 仍保留在仓库作为构建输入，无需联网安装 RTP。

4. 产物位置与启动方式
   - 可执行文件：`dist/WindyTranslator/WindyTranslator.exe`
   - 运行：双击上述 exe，或在命令行执行：
     ```
     .\dist\WindyTranslator\WindyTranslator.exe
     ```

5. 打包内容说明（由 spec 与打包步骤配置）
   - 代码入口：`main.py`
   - 资源与数据（运行库、`modules` 和 `assets` 位于 `_internal`；`tools` 与可执行文件同级）：
      - `modules/EasyRPG`, `modules/RPGRewriter`, `modules/UberWolf`, `modules/WOLF`, `modules/RTPCollection`, `modules/dict`
      - `tools`（翻译辅助工具目录）
      - `assets/icon.ico`（应用图标）
   - 运行时自动生成（与可执行文件同级）：
     - `dict`（可编辑基础字典，缺失时从内置模板复制，已有文件保持不变）
     - `Works`（工作目录）
     - `app_config.json`（配置文件）

6. 常见问题
   - 构建日志可能提示：`Hidden import "tzdata" not found!`，通常可忽略。如需消除，可将 `tzdata` 加入 `requirements.txt` 或在 spec 的 `hiddenimports` 中显式添加。
   - 跨平台打包需在目标平台执行（例如 Windows 可执行文件应在 Windows 上构建）。

7. 发布建议
   - 将 `dist/WindyTranslator/` 目录整体打包为 zip 分发。
   - 初次运行会在程序同级目录生成 `logs/`，日志与配置会写到可执行文件同级目录。
   - 发行 ZIP 必须不超过 95,000,000 字节；CI 会在上传和发布之前检查大小。
   - “安装乱码 RTP”使用同一资源库中的 `2000fix`，仍只补充缺失文件，并保留已有 XYZ 图像。

## 贡献指南

我们欢迎各种形式的贡献！

1. Fork本仓库
2. 创建您的特性分支: `git checkout -b my-new-feature`
3. 提交您的更改: `git commit -am 'Add some feature'`
4. 推送到分支: `git push origin my-new-feature`
5. 提交Pull Request

## 致谢

本工具基于以下项目构建：

- [RPGRewriter](https://www.vgperson.com/) by vgperson
- [EasyRPG](https://easyrpg.org/) 项目

## 许可证

本项目自身代码采用MIT许可证 - 详见 [LICENSE](LICENSE) 文件

### 第三方组件许可证

本项目使用的主要第三方组件有各自的许可证条款：

- **EasyRPG Player**：GPL-3.0许可证 - [https://easyrpg.org/](https://easyrpg.org/)
- **RPGRewriter**：由vgperson创建，保留所有权利

使用本软件意味着您同意各组件的相应许可证条款。详细的第三方许可证信息请参阅 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。 
