# 小工具插件接入

WindyTranslator 会扫描程序目录下的 `tools` 文件夹。每个直接子目录必须包含一个 `manifest.json` 才会被识别。

## 目录结构

```text
tools/<tool_id>/
  manifest.json
  entry.py
  vendor/              # 可选：工具独有的 Python 依赖
  resources/           # 可选：工具资源或外部程序
```

## 运行方式

`category` 区分 `gui` 和 `cli`：GUI 工具保留自己的窗口，CLI 工具的标准输出会显示在小工具日志框中。`runtime` 为 `host_python` 时，主程序会以子进程方式运行：

```text
WindyTranslator.exe --run-tool <tool_dir> <entry> <arguments...>
```

开发环境使用当前 Python；发布版使用 WindyTranslator 自身的宿主入口。`runtime` 为 `exe` 时，主程序直接启动 `entry`。

工具入口应保持原有 GUI 行为：不带参数时按原流程打开；收到 `--initial-*` 参数时只预填或自动读取初始路径。

## 参数类型

支持 `game_path`、`current_game_file` 和 `fixed`。主程序只按这些声明生成参数，不执行 Manifest 中的 shell 模板。

## 发布要求

不要在启动时安装依赖。工具独有依赖放入 `vendor/`，外部二进制文件列入 `resources`。必须随工具保留许可证和第三方声明。

完整字段和示例见 `manifest.schema.json` 与 `example-manifest.json`。
