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

## 检查与下载更新

主窗口右下角“检查更新…”打开更新窗口。可以手动检查、查看网站推荐版本和说明、选择保存位置、下载或取消下载，并打开下载文件夹。网站地址可修改，也可启用“启动时检查更新”（默认关闭）。默认地址采用本站当前生产配置 `https://viprpg-zh-archive.q578235562.workers.dev`。

更新源仅限所配置的网站，不回退到 GitHub。网站下载中心和更新 API 尚待实现；接口未上线、返回网页或网络失败时，会明确报错，不显示“已是最新版”。网站就绪后按[网站更新接口说明](docs/website-updates.md)提供接口即可接入。

客户端用本站发布序号比较新旧，不比较 GitHub 版本号。程序包内的 `_internal/build-info.json` 和 GitHub `release-manifest.json` 提供 `applicationBuildId`，供网站管理员登记实际构建身份。网站不能唯一识别当前构建时，客户端显示“当前本站版本未知”，仍可查看、下载推荐包。

下载先写入临时 `.part` 文件，只有长度、SHA-256 与 ZIP 格式检查通过后才保存正式文件。取消或失败会清理本次临时文件，并保留已有目标文件。下载完成不代表已安装：先退出程序，将 ZIP 解压到新目录运行，迁移配置、字典、Works 和自装工具前做好备份。此阶段不自动解压或替换正在运行的程序，也不因下载成功就修改已安装版本身份。

## 版本与自动发布

在仓库根目录编辑 [RELEASE.md](RELEASE.md)：第一行填写 `# 年.月.当月序号`（例如 `# 2026.9.1`），下面填写当前版本的 Markdown 更新日志。月份为 1–12，序号从 1 开始，均不补零；CI 会校验格式和非空日志。

每次提交到 `main`，或在该分支手动运行 **Build and Release (Windows)**，都会构建并更新两类 GitHub Release：

- `nightly`：跟随发布分支的最新成功构建，标记为预发布。
- `RELEASE.md` 指定的版本：沿用 `v` 标签前缀，例如 `v2026.9.1`。版本号相同就覆盖标签、日志和下载文件；改成 `2026.9.2` 就新建 Release，并保留 `v2026.9.1`。数值最大的正式版本标记为 GitHub **Latest**。

构建固定使用触发工作流的提交；过时构建会跳过发布，等待新提交的构建。正式版 ZIP 命名为 `WindyTranslator-windows-2026.9.1.zip`，与同次 Nightly 的包内容一致。Pull Request 只构建和上传 Actions 产物。

每个 Release 附带 `release-manifest.json`，记录提交 SHA、工作流运行、版本、文件大小和 SHA-256，发布日志也包含这些校验值。同版本覆盖不会保留旧包，复现问题时请同时记录版本号与提交 SHA；历史构建另受 GitHub Actions 产物保留期限制。单个附件上传限时 5 分钟、最多尝试 3 次，发布任务总限时 30 分钟；仍失败时工作流会失败，可在分支仍指向该提交时重跑失败任务；GitHub 对已有 Release 的多个附件替换不提供原子操作。

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
   - 发行 ZIP 必须小于 100,000,000 字节；CI 会在上传和发布之前检查大小。
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
