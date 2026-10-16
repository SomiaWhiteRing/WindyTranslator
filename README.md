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

## 启动与校对检查

翻译 SDK 和 Excel 依赖在使用对应功能时加载。主程序启动、JSON/TXT 校对无需提前加载它们；首次获取模型列表、测试连接、翻译或使用 Excel 功能时，可能有一次依赖加载等待。

校对工具读取数据后仍自动执行全部启动检查，数据分析在后台进行，结果按原顺序逐项显示。检查期间可以继续操作；重新载入数据或调整检查选项后使用最新状态的结果，手动检查结果不会被较早的后台结果覆盖。字体像素测量和列表更新仍由界面线程执行。备份规则、译文即时原子保存、失败暂停与续跑规则保持原样。

## RM2000/2003 输出方式

选择 RM2000/2003 游戏后，会立即创建对应的翻译工程目录和 `project.json`，游戏路径下方显示本作品的输出方式；轻松模式和专业模式共用这一设置。手动输入或修改路径时，停止输入约 300 毫秒后检测；无效路径或其他引擎不会创建该工程文件。

- **UTF-8（推荐）**：使用 EasyRPG 或支持 UTF-8 的专用 RPG_RT，保留原文件名和音符等符号，目标编码自动设置；初始化不再将侧载 TXT 转为 GBK，第 2 步显示“无需执行”。
- **传统兼容（默认 GBK）**：沿用原版兼容流程，也保留 Big5 等已有目标编码。符号兼容替换仅作用于导入前的临时副本，保存的翻译 JSON 和 TXT 保留原符号。

全新安装首次选择作品时会显示选择框。新用户和旧版升级用户的新作品初始默认方式均为 UTF-8；升级时仅将旧版导入编码用作传统兼容编码的初始值。已有工程的模式选择保留，用户主动保存的新作品默认方式也会保留。旧用户只显示一次可关闭的页内提示；点击“更改…”可随时调整。“同时设为今后新作品的默认方式”只影响新作品，不覆盖已经记录的作品。旧版工程只按 Works 判定：对应工程目录已存在但没有工程输出设置时，使用传统兼容方式和保存的传统目标编码。游戏目录中的导出脚本不参与新旧判定。

作品的输出方式、传统目标编码和旧版工程标记保存在 `Works/<游戏目录名>/project.json` 的 `output_settings` 中，与译文、词典属于同一翻译工程；当前数据编码保存在同一文件的 `game_paths` 中，以规范化的完整游戏路径分别记录。同名目录中的不同副本不会共用当前编码。主配置 `app_config.json` 中的输出设置只保留全局默认和引导状态。创建工程时会记下是否来自旧版 Works 工程；新建的工程不会因为已有目录而被重新判成旧工程。

只有“已有工程、当前游戏路径没有记录”时，才结合 `RPG_RT.ini` 的明确编码声明和现有 EasyRPG 检测为该路径补充记录；两者冲突或无法确定时，弹窗请用户指定当前游戏的数据编码，取消则不登记。已有路径继续使用原记录，新工程沿用原来的创建流程。旧版未关联路径的编码也需在首次登记时重新确认，已有输出偏好保留。无需指定以前的游戏目录，也不向游戏目录添加工程标识文件。

旧配置的 `pro_mode_settings.import_encoding` 仅用于升级时初始化 `legacy_encoding`，读取后即移除。正常使用中的编码设置读写新版全局设置或工程设置，不再生成、保存该旧字段。任务中的 `source_encoding` 表示当前数据的读取编码，`target_encoding` 表示本次实际写入编码；符号兼容替换等业务行为由输出方式决定。

切换输出方式不会立即改写游戏数据，下次导入时生效。转码和导入均先在临时副本中完成，底层读取、保存失败或拒绝重写时返回失败，不写回游戏、INI 或成功编码记录；全部处理成功后才开始写回。成功导入后在作品锁内更新当前路径的实际编码，供后续读取使用；关闭窗口或随后切换输出方式不会改变这项记录。工程设置的保存和失败回滚也使用同一作品锁，并只更新用户实际修改的字段。启动时按当前数据编码选择播放器；源编码为自动检测时，先检测当前游戏文件。已经重写的文件名、旧版译文中已经替换的符号不会自动恢复；若需完整原名，应从原始游戏重新制作。VX Ace、MV/MZ 和 WOLF 沿用各自的 Unicode 流程。

## OpenAI 兼容接口配置

在翻译配置，或世界观字典配置的“OpenAI 兼容端点”模式中，填写 API 基础地址和 Key 后，停止输入约 0.7 秒即会自动获取模型列表。基础地址应包含服务商要求的路径（例如 `https://example.com/v1`），不要填写完整的 `/chat/completions` 地址。打开已有配置时也会自动获取，可以点击“刷新模型”重新获取。

OpenAI 兼容模式的模型下拉框只显示当前接口返回的模型，不再提供固定示例列表；新建翻译配置的 API 地址和模型名称均为空。已有模型选择会保留，接口不支持模型列表、返回为空或获取失败时，仍可手动输入模型名称，再测试连接并保存。获取列表本身不会发起翻译请求。

## 自动更新

WindyTranslator 默认在启动时检查一次更新，仅当网站发布序号更高时提示；没有更新或检查失败时不打扰。主窗口右下角“检查更新…”可手动检查，同一窗口中的“启动时自动检查更新”开关立即保存。

选择“立即更新”后主窗口锁定，显示下载、校验和准备进度，随后自动关闭、替换程序并重新启动。配置、字典、Works 和自装工具保持原样；受管文件有本地修改时会停止并提示。请先完成当前任务并关闭其他编辑窗口及外部工具。更新失败时保留恢复备份和日志。

GitHub 自动构建的完整 ZIP 含构建身份、发行文件清单和独立更新助手。管理员下载 ZIP 后上传网站，后台自动识别，无需手工填写构建标识。首个支持自动安装的版本需要手动安装一次。详情见[网站更新接口说明](docs/website-updates.md)。

当前构建统一连接 `https://viprpg.org`；地址由 CI 的 `WINDY_UPDATE_SITE` 写入安装包，不在普通窗口中编辑，不回退 GitHub。此前指向预生产的旧安装包需手动安装一次新版，之后由正式站提供更新。

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
