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
   python main.py
   ```

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

4. 产物位置与启动方式
   - 可执行文件：`dist/WindyTranslator/WindyTranslator.exe`
   - 运行：双击上述 exe，或在命令行执行：
     ```
     .\dist\WindyTranslator\WindyTranslator.exe
     ```

5. 打包内容说明（由 spec 配置）
   - 代码入口：`main.py`
   - 资源与数据（打包进入 `_internal`）：
      - `modules/EasyRPG`, `modules/RPGRewriter`, `modules/UberWolf`, `modules/WOLF`, `modules/RTPCollection`, `modules/dict`
      - `assets/icon.ico`（应用图标）
   - 运行时自动生成（与可执行文件同级）：
     - `Works`（工作目录）
     - `app_config.json`（配置文件）

6. 常见问题
   - 构建日志可能提示：`Hidden import "tzdata" not found!`，通常可忽略。如需消除，可将 `tzdata` 加入 `requirements.txt` 或在 spec 的 `hiddenimports` 中显式添加。
   - 跨平台打包需在目标平台执行（例如 Windows 可执行文件应在 Windows 上构建）。

7. 发布建议
   - 将 `dist/WindyTranslator/` 目录整体打包为 zip 分发。
   - 初次运行会在程序同级目录生成 `logs/`，日志与配置会写到可执行文件同级目录。

## WOLF RPG Editor 支持

- 识别条件：游戏目录包含 `Game.exe`，以及 `Data.wolf` 或已解包的 `Data`。
- 初始化：调用随包提供的 `UberWolfCli.exe` 解包 `Data.wolf`。如果已有 `Data`，程序会校验它与归档是否同源；旧版导入留下的 `Data` 会在哈希可证明归档是最近一次成功导入结果时自动同步。
- 重写文件名：WOLF 流程跳过此步骤。
- 词典：WOLF 不加载内置默认数据库映射，也不应用内置基础人物/事物词典；仅使用当前项目生成的词典。
- 导出/写回：`WolfRPGText.exe` 基于 UberWolf 的结构化读写能力处理地图、公共事件、数据库值和外部 TXT/CSV；`UberWolfCli.exe` 本身只负责解包与重新封包。公共事件调用中的短按钮、Tips、日志等显示参数会按调用用途导出。
- 逻辑保护：条件比较、文件路径和公共事件引用等内部值使用独立的 `WOLFLogic` 脚本保存；数据库识别名会按运行时字符串流向区分显示与逻辑用途。显示名、说明和按钮文字仍可正常翻译，相同字面量在逻辑位置和显示位置不会互相覆盖。
- 发布：缺失译文、`fallback`、控制码损坏、WOLF 固定行数变化或逻辑判断字面量被翻译时，会在恢复 `StringScripts` 前停止。
- 导入：在临时目录写回并封包，再重新解包逐文件校验；通过后才替换 `Data.wolf` 和 `Data`。首次替换分别保留 `Data.wolf.windy-original.bak` 与 `Data.windy-original.bak`。字体覆盖不足只会记录警告，不会在翻译流程中自动替换字体。
- 字体修订：选择 WOLF 游戏目录后会出现独立标签页，可并排预览并分别替换 `MainFont` 与三个 `SubFonts`。候选字体来自 Windows、游戏根目录和 `modules/WOLF`；应用时会复制所选字体、重新封包并执行相同的重解包校验。复制 Windows 系统字体前请自行确认其再分发许可。

系统数据库和可证明是内部识别值的名称不会自动翻译；用户数据库中没有绑定内部识别值的记录名会作为显示文本导出。图片内文字不属于当前文本管线，需要单独进行图片本地化。

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
- [UberWolf](https://github.com/Sinflower/UberWolf) by Sinflower

## 许可证

本项目自身代码采用MIT许可证 - 详见 [LICENSE](LICENSE) 文件

### 第三方组件许可证

本项目包含以下第三方组件，它们有自己的许可证条款：

- **EasyRPG Player**：GPL-3.0许可证 - [https://easyrpg.org/](https://easyrpg.org/)
- **RPGRewriter**：由vgperson创建，保留所有权利
- **UberWolf**：MIT许可证

使用本软件意味着您同意各组件的相应许可证条款。详细的第三方许可证信息请参阅 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。 
