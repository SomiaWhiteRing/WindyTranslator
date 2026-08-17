# 翻译文本字数统计

这是一个 CLI 小工具。主程序会将当前游戏对应的：

- `untranslated/translation.json`
- `translated/translation_translated.json`

作为两个输入参数传入。原文或译文文件缺失时，工具会报告对应路径并返回非零退出码。
