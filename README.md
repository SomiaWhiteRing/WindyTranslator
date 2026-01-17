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
      - `modules/EasyRPG`, `modules/RPGRewriter`, `modules/RTPCollection`, `modules/dict`
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

本项目包含以下第三方组件，它们有自己的许可证条款：

- **EasyRPG Player**：GPL-3.0许可证 - [https://easyrpg.org/](https://easyrpg.org/)
- **RPGRewriter**：由vgperson创建，保留所有权利

使用本软件意味着您同意各组件的相应许可证条款。详细的第三方许可证信息请参阅 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。 
