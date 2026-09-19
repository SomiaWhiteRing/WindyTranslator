# RM2K 存档地图重载

让旧存档在读档时重新加载当前地图和事件，用于处理汉化后仍显示旧对话的情况。

## 使用

1. 完成汉化导入并关闭游戏。
2. 在 WindyTranslator 中选择游戏工作区，启动“小工具”中的 **RM2K3 存档地图重载**。
3. 勾选要处理的存档，选择是否“处理前备份”，点击“更新所选存档”。
4. 重新启动游戏并读档，确认文本和进度正常后保存。

工具扫描游戏根目录的 `Save数字.lsd`。默认只勾选文件修改时间最新的一份，备份默认开启。无需更新或检查失败的存档不可选；最新存档不可处理时，需手动选择其他存档。取消或关闭弹窗不修改文件。结果与备份路径显示在小工具日志中。

## 作用范围

存档可能保留公共并行事件的旧指令，即使剧情尚未触发，读档后也可能继续显示旧文本。工具通过修改地图兼容标记，让引擎在读档时放弃相关事件缓存，加载当前游戏文件中的事件。

- **保留：**存档中的玩家地图、坐标、朝向、开关、变量、金钱、物品、队伍及角色数据。
- **重置：**前台事件和普通地图、公共并行事件的执行状态。NPC 位置、移动路线和临时删除状态可能重置，自动运行与并行事件可能重新执行。
- **限制：**不更新角色重命名、字符串变量等存档数据中的文字。补丁引擎、自定义存档和动态／克隆事件的行为需单独确认。

请使用与存档对应的游戏目录，并避开剧情、强制移动或乘降载具过程中的存档。此功能适用于事件缓存导致的旧文本残留，无需在每次汉化导入后使用。

## 备份与恢复

开启“处理前备份”时，备份保存在原存档旁，已有备份不会覆盖。关闭后不创建备份。

```text
Save01.lsd.map-refresh.<UTC时间>.<校验值>.bak
```

可使用下方命令恢复本工具生成的备份，也可关闭游戏后手动将备份复制回原存档。命令行恢复会校验备份，并在写入前另行备份当前文件。

## 命令行

以下命令在小工具目录中运行。

```powershell
# 选择存档并处理
python save_refresh.py --project "D:\Game"

# 只读预览
python save_refresh.py --project "D:\Game" --dry-run

# 只处理指定存档
python save_refresh.py --project "D:\Game" --save "Save01.lsd"

# 已关闭游戏并了解重置范围，免弹窗处理
python save_refresh.py --project "D:\Game" --save "Save01.lsd" --apply --acknowledge-reset

# 免弹窗处理，不创建备份
python save_refresh.py --project "D:\Game" --save "Save01.lsd" --apply --acknowledge-reset --no-backup

# 恢复备份，BACKUP 替换为备份的完整路径
python save_refresh.py --project "D:\Game" --save "Save01.lsd" --restore-backup "BACKUP"
```

免弹窗重载时，未指定 `--save` 则处理全部待更新存档。`--no-backup` 仅用于免弹窗重载；恢复备份可用 `--acknowledge-reset` 跳过确认。

## 参考

[EasyRPG 读档与事件状态恢复实现](https://github.com/EasyRPG/Player/blob/master/src/game_map.cpp)
