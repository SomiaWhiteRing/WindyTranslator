"""Headless tool entry: save selection dialog, optional backups, host logs."""

import argparse
from datetime import datetime
from pathlib import Path
import re
import sys

from refresh_core import RefreshError, apply_refresh, plan_refresh, restore_backup


WARNING = (
    "下次读档时将重新加载当前地图和事件，保留玩家位置、开关、变量和队伍数据。\n"
    "事件执行进度会重置，NPC 位置、移动路线和临时删除状态可能重置，并行事件可能重新执行。\n"
    "请先关闭游戏，勿使用剧情、强制移动或乘降载具过程中的存档。"
)


def describe(plan):
    state = "可标记重载" if plan.changed else "无需重复标记重载"
    return (
        f"存档：{plan.save_path}\n"
        f"地图：{plan.map_path.name}　玩家坐标：({plan.x}, {plan.y})\n"
        f"状态：{state}"
    )


def confirm(title, message):
    import tkinter as tk
    from tkinter import messagebox

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        return messagebox.askokcancel(title, message, icon="warning", default="cancel", parent=root)
    except tk.TclError as exc:
        raise RefreshError(f"无法显示确认弹窗，未执行写入：{exc}") from exc
    finally:
        if root is not None:
            root.destroy()


def select_saves(project, entries, *, create_backup=True):
    import tkinter as tk
    from tkinter import ttk

    root = None
    result = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.title("存档地图重载")
        root.attributes("-topmost", True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        body = ttk.Frame(root, padding=16)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        directory = ttk.Label(body, text=f"当前游戏目录：{project}", wraplength=760)
        directory.grid(sticky="w", pady=(0, 10))
        ttk.Label(body, text="选择存档（最新在前）").grid(sticky="w", pady=(0, 6))

        listing = ttk.Frame(body)
        listing.grid(sticky="nsew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        canvas = tk.Canvas(listing, height=240, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        rows = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        rows.columnconfigure(0, weight=1)
        for col, label in enumerate(("存档", "地图", "修改时间", "状态")):
            ttk.Label(rows, text=label).grid(row=0, column=col, sticky="w", padx=(0, 16), pady=(0, 6))

        selected = []
        variables = []

        def update_selection():
            count = sum(variable.get() for plan, variable in selected)
            submit.configure(text=f"更新所选存档（{count}）", state="normal" if count else "disabled")

        for index, entry in enumerate(entries):
            plan = entry["plan"]
            enabled = plan is not None and plan.changed
            variable = tk.BooleanVar(master=root, value=index == 0 and enabled)
            variables.append(variable)
            checkbox = ttk.Checkbutton(rows, text=entry["path"].name, variable=variable,
                                       command=update_selection)
            checkbox.grid(row=index + 1, column=0, sticky="w", padx=(0, 16), pady=5)
            if enabled:
                selected.append((plan, variable))
            else:
                checkbox.state(["disabled"])
            timestamp = (datetime.fromtimestamp(entry["mtime"] / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
                         if entry["mtime"] is not None else "无法读取")
            values = (str(plan.map_id) if plan else "—", timestamp, entry["status"])
            for col, value in enumerate(values, start=1):
                ttk.Label(rows, text=value).grid(row=index + 1, column=col, sticky="w", padx=(0, 16), pady=5)

        backup = tk.BooleanVar(master=root, value=create_backup)
        ttk.Checkbutton(body, text="处理前备份", variable=backup).grid(sticky="w", pady=(12, 8))
        warning = ttk.Label(body, text=WARNING, wraplength=760, justify="left")
        warning.grid(sticky="w", pady=(0, 12))
        body.bind("<Configure>", lambda event: (directory.configure(wraplength=max(200, event.width - 32)),
                                               warning.configure(wraplength=max(200, event.width - 32))))
        buttons = ttk.Frame(body)
        buttons.grid(sticky="e")

        def accept():
            nonlocal result
            plans = [plan for plan, variable in selected if variable.get()]
            if plans:
                result = (plans, backup.get())
                root.quit()

        cancel = ttk.Button(buttons, text="取消", command=root.quit)
        cancel.grid(row=0, column=0, padx=(0, 8))
        submit = ttk.Button(buttons, command=accept)
        submit.grid(row=0, column=1)
        update_selection()
        root.protocol("WM_DELETE_WINDOW", root.quit)
        root.bind("<Escape>", lambda event: root.quit())
        root.update_idletasks()
        width = min(max(800, root.winfo_reqwidth()), root.winfo_screenwidth() - 80)
        height = min(root.winfo_reqheight(), root.winfo_screenheight() - 100)
        root.geometry(f"{width}x{height}+{max(0, (root.winfo_screenwidth() - width) // 2)}+{max(0, (root.winfo_screenheight() - height) // 2)}")
        root.deiconify()
        cancel.focus_set()
        root.mainloop()
        return result
    except tk.TclError as exc:
        raise RefreshError(f"无法显示存档选择弹窗，未执行写入：{exc}") from exc
    finally:
        if root is not None:
            root.destroy()


def run_refresh(project, save=None, *, dry_run=False, acknowledged=False, create_backup=True):
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise RefreshError(f"当前游戏目录不存在：{project}")
    if save:
        selected = Path(save).expanduser()
        saves = [selected if selected.is_absolute() else project / selected]
    else:
        saves = []
        for path in sorted(project.iterdir(), key=lambda p: p.name.casefold()):
            if path.is_file() and re.fullmatch(r"Save[0-9]+\.lsd", path.name, re.IGNORECASE):
                if path.resolve().parent != project:
                    print(f"跳过指向当前目录之外的存档：{path.name}", flush=True)
                    continue
                saves.append(path)
    print(f"当前游戏目录：{project}", flush=True)
    if not saves:
        print("根目录未找到 Save数字.lsd，未修改任何文件。", flush=True)
        return 0

    pending = []
    entries = []
    failures = 0
    unchanged = 0
    for path in saves:
        entry = {"path": path, "plan": None, "mtime": None, "status": "预检失败（见日志）"}
        entries.append(entry)
        try:
            entry["mtime"] = path.stat().st_mtime_ns
            plan = plan_refresh(project, path)
            entry["plan"] = plan
            entry["status"] = "可更新" if plan.changed else "无需更新"
            print(describe(plan), flush=True)
            if plan.changed:
                pending.append(plan)
            else:
                unchanged += 1
        except (OSError, RefreshError) as exc:
            failures += 1
            print(f"预检失败，跳过 {path.name}：{exc}", flush=True)
    print(f"预检完成：待处理 {len(pending)} 个，无需修改 {unchanged} 个，失败 {failures} 个。", flush=True)
    if dry_run:
        print("只读预览，未写入，也未弹出确认窗口。", flush=True)
        return 1 if failures else 0
    if not pending:
        print("没有需要写入的存档。", flush=True)
        return 1 if failures else 0

    if not acknowledged:
        entries.sort(key=lambda entry: (-(entry["mtime"] if entry["mtime"] is not None else -1),
                                        entry["path"].name.casefold()))
        selection = select_saves(project, entries, create_backup=create_backup)
        if selection is None:
            print("已取消，未修改任何存档。", flush=True)
            return 0
        pending, create_backup = selection

    print(f"本次选择 {len(pending)} 个存档，处理前备份：{'开启' if create_backup else '关闭'}。", flush=True)

    changed = 0
    for plan in pending:
        try:
            backup = apply_refresh(plan, create_backup=create_backup)
            if plan.changed:
                changed += 1
                backup_note = f"原存档备份：{backup}" if backup else "未创建备份。"
                print(f"已标记重载：{plan.save_path.name}\n{backup_note}", flush=True)
            else:
                unchanged += 1
        except (OSError, RefreshError) as exc:
            failures += 1
            print(f"处理失败 {plan.save_path.name}：{exc}", flush=True)
    print(f"处理结束：更新 {changed} 个，无需修改 {unchanged} 个，失败 {failures} 个。", flush=True)
    if changed:
        print("请在游戏中读档确认效果；确认正常后重新保存。", flush=True)
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="让 RM2000/2003 旧存档在读档时重新加载当前地图和事件，处理汉化后仍显示旧对话的情况。")
    parser.add_argument("--project", help="当前游戏目录，由主程序自动传入")
    parser.add_argument("--save", help="可选：只处理一个 LSD；相对路径基于 --project")
    parser.add_argument("--dry-run", action="store_true", help="只读预览，不弹窗、不写入")
    parser.add_argument("--apply", action="store_true", help="与 --acknowledge-reset 一起使用，明确确认后免弹窗写入")
    parser.add_argument("--no-backup", action="store_true", help="仅限免弹窗重载：不创建处理前备份")
    parser.add_argument("--restore-backup", help="将本工具备份恢复到 --save 指定的现有文件")
    parser.add_argument("--acknowledge-reset", action="store_true", help="命令行确认已关闭游戏并了解重置／恢复范围")
    args = parser.parse_args(argv)
    if args.no_backup and (not args.apply or args.restore_backup or args.dry_run):
        parser.error("--no-backup 仅可与 --apply --acknowledge-reset 一起用于重载。")
    if args.apply and args.restore_backup:
        parser.error("--apply 与 --restore-backup 不能同时使用。")
    if args.dry_run and (args.apply or args.restore_backup or args.acknowledge_reset):
        parser.error("--dry-run 不能与写入或恢复选项同时使用。")
    if args.apply and not args.acknowledge_reset:
        parser.error("免弹窗写入必须同时提供 --apply --acknowledge-reset。")
    if args.acknowledge_reset and not (args.apply or args.restore_backup):
        parser.error("--acknowledge-reset 需要与 --apply 或 --restore-backup 一起使用。")
    if args.restore_backup and not args.save:
        parser.error("恢复备份必须用 --save 指定目标存档。")
    if not args.restore_backup and (not args.project or not args.project.strip()):
        parser.error("请从主程序的当前游戏工作区启动，或用 --project 指定游戏目录。")
    try:
        if args.restore_backup:
            target = Path(args.save).expanduser()
            if not target.is_absolute() and args.project:
                target = Path(args.project).expanduser() / target
            target = target.resolve()
            if not args.acknowledge_reset:
                if not confirm("恢复存档警告", f"请先关闭游戏。\n\n使用备份：\n{Path(args.restore_backup).resolve()}\n\n"
                               f"恢复到：\n{target}\n\n恢复前会另行备份当前文件。继续恢复？"):
                    print("已取消，未修改存档。", flush=True)
                    return 0
            backup = restore_backup(target, args.restore_backup)
            print(f"已恢复：{target}\n恢复前备份：{backup}" if backup else "内容已相同，无需写入。", flush=True)
            return 0
        return run_refresh(args.project, args.save, dry_run=args.dry_run, acknowledged=args.acknowledge_reset,
                           create_backup=not args.no_backup)
    except (OSError, RefreshError) as exc:
        print(f"未完成：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
