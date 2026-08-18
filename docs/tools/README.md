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

支持 `game_path`、`current_game_file`、`modules` 和 `fixed`。`game_path` 可选 `source`，用于传递游戏目录内的固定子路径；`modules` 必须填写 `source`，用于指向主程序 `modules` 根目录下的文件，宿主会根据开发运行或打包运行解析成实际绝对路径。主程序只按这些声明生成参数，不执行 Manifest 中的 shell 模板。

## 启动预设

`launch_options` 是可选的固定启动预设列表。定义预设时，每一项提供完整的 `arguments`；工具页面默认选中第一项。顶层 `arguments` 必须为空或省略，且不会与预设参数合并。

| 字段 | 规则 |
| --- | --- |
| `id` | ASCII 字母、数字和下划线组成的唯一标识。 |
| `name` | 下拉框显示的非空名称。 |
| `arguments` | 非空的完整参数数组。 |

可选的顶层 `launch_options_label` 决定下拉框左侧的名称，省略时为“启动选项”。它只能与 `launch_options` 一起使用。

## 发布要求

不要在启动时安装依赖。工具独有依赖放入 `vendor/`，外部二进制文件列入 `resources`。必须随工具保留许可证和第三方声明。

完整字段和示例见 `manifest.schema.json` 与 `example-manifest.json`。
