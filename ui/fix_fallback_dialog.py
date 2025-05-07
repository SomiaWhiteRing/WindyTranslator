# ui/fix_fallback_dialog.py
import tkinter as tk
from tkinter import ttk, messagebox
import csv
import os
import logging
import json
from core.utils import file_system # 用于确保目录存在 (虽然理论上应已存在)

log = logging.getLogger(__name__)

class FixFallbackDialog(tk.Toplevel):
    """用于修正翻译回退项的对话框。"""

    def __init__(self, parent, app_controller, fallback_csv_path, translated_json_path):
        """
        初始化修正回退对话框。

        Args:
            parent (tk.Widget): 父窗口。
            app_controller (RPGTranslatorApp): 应用控制器实例 (用于状态更新)。
            fallback_csv_path (str): 回退修正 CSV 文件路径。
            translated_json_path (str): 已翻译 JSON 文件路径。
        """
        super().__init__(parent)
        self.app = app_controller
        self.fallback_csv_path = fallback_csv_path
        self.translated_json_path = translated_json_path

        # --- 窗口设置 ---
        self.title("修正翻译回退项")
        self.geometry("950x600") # 调整尺寸
        self.transient(parent)
        self.grab_set()

        # --- 列定义 ---
        self.columns = ('original', 'last_attempt', 'correction')
        self.headers = ["原文", "最终尝试结果", "修正译文"]

        # --- 编辑状态变量 (复用 DictEditor 逻辑) ---
        self._edit_widget = None
        self._edit_item_id = None
        self._edit_column_id_str = None # 存储 '#1', '#2' 等字符串形式的列标识
        self._edit_column_name = None # 存储 'original', 'correction' 等列名
        self._edit_widget_type = None

        # --- 创建控件 ---
        self._create_widgets()

        # --- 加载数据 ---
        self._load_data_from_csv()

        # 绑定关闭事件
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_widgets(self):
        """创建窗口中的所有控件。"""
        main_frame = ttk.Frame(self, padding="5")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Treeview 表格 ---
        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self.table = ttk.Treeview(
            table_frame,
            columns=self.columns,
            show='headings',
            selectmode='browse' # 通常只编辑单行
        )

        # 定义列标题和宽度
        for col, header in zip(self.columns, self.headers):
            self.table.heading(col, text=header)
            if col == 'original':
                self.table.column(col, width=300, anchor='w', stretch=True)
            elif col == 'last_attempt':
                self.table.column(col, width=300, anchor='w', stretch=True)
            elif col == 'correction':
                self.table.column(col, width=300, anchor='w', stretch=True) # 修正列也给足够宽度

        # 滚动条
        scrollbar_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar_y.pack(side="right", fill="y")
        scrollbar_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        scrollbar_x.pack(side="bottom", fill="x")
        self.table.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        self.table.pack(side="left", fill="both", expand=True)

        # --- 绑定事件 ---
        self.table.bind('<Double-1>', self._on_cell_double_click) # 双击编辑

        # --- 按钮区域 ---
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=5)

        # 保存按钮
        save_button = ttk.Button(button_frame, text="保存修正并关闭", command=self._save_corrections)
        save_button.pack(side=tk.RIGHT, padx=5)

        # 关闭按钮
        cancel_button = ttk.Button(button_frame, text="关闭", command=self._on_close)
        cancel_button.pack(side=tk.RIGHT, padx=5)


    def _load_data_from_csv(self):
        """从 fallback_corrections.csv 加载数据。"""
        # 清空现有数据
        for item in self.table.get_children():
            self.table.delete(item)

        if not os.path.exists(self.fallback_csv_path):
            log.warning(f"回退修正文件未找到: {self.fallback_csv_path}")
            messagebox.showwarning("未找到文件", "未找到回退修正文件。\n可能没有需要修正的项目。", parent=self)
            self.destroy() # 直接关闭对话框
            return

        try:
            with open(self.fallback_csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f, quoting=csv.QUOTE_ALL)
                header = next(reader, None) # 读取表头
                if not header or len(header) < 2 : # 至少需要原文和最终尝试结果
                    raise ValueError("CSV 文件表头无效或列数不足。")

                # 动态确定修正列的索引 (如果存在的话)
                correction_col_index = -1
                if len(header) >= 3 and header[2].strip() == "修正译文":
                    correction_col_index = 2

                for i, row in enumerate(reader):
                    if not row or len(row) < 2: continue # 跳过空行或列数不足的行

                    original = row[0]
                    last_attempt = row[1]
                    # 如果CSV中有第三列（修正列），则读取；否则留空
                    correction = row[correction_col_index] if correction_col_index != -1 and len(row) > correction_col_index else ""

                    # 使用唯一 iid
                    iid = f"fallback_row_{i}_{os.urandom(4).hex()}"
                    self.table.insert('', 'end', values=(original, last_attempt, correction), iid=iid)

            log.info(f"已从 {self.fallback_csv_path} 加载回退项。")
        except FileNotFoundError:
            # 理论上不会发生，因为前面检查过
            log.error(f"回退修正文件加载时未找到 (意外): {self.fallback_csv_path}")
            messagebox.showerror("错误", "加载时未找到回退修正文件。", parent=self)
            self.destroy()
        except Exception as e:
            log.exception(f"加载回退修正文件失败: {self.fallback_csv_path} - {e}")
            messagebox.showerror("加载失败", f"无法加载回退修正文件:\n{self.fallback_csv_path}\n{e}", parent=self)
            self.destroy() # 加载失败也关闭


    # --- 编辑逻辑 (复用并修改自 DictEditor) ---
    def _on_cell_double_click(self, event):
        """处理单元格双击事件，允许所有列进入编辑状态。""" # <--- 修改说明
        target_table = self.table
        if self._edit_widget:
            self._commit_edit()
            if self._edit_widget: return

        region = target_table.identify_region(event.x, event.y)
        if region != "cell": return

        column_id_str = target_table.identify_column(event.x) # '#1', '#2', '#3'
        item_id = target_table.identify_row(event.y)

        if not item_id: # 确保选中了有效的行
            return

        # --- 不再限制只编辑第三列 ---
        log.debug(f"启动编辑: Item={item_id}, Column={column_id_str}")
        self._start_edit(item_id, column_id_str, target_table)


    def _start_edit(self, item_id, column_id_str, table_widget):
        """在指定的单元格启动编辑。"""
        try:
            if self._edit_widget: self._destroy_edit_widget()

            column_index = int(column_id_str.replace('#', '')) - 1
            if not (0 <= column_index < len(self.columns)):
                log.warning(f"无效的列索引: {column_index}")
                return

            self._edit_column_id_str = column_id_str
            self._edit_column_name = self.columns[column_index] # 获取实际列名
            self._edit_item_id = item_id

            # ... (bbox 获取逻辑不变) ...
            try:
                 bbox = table_widget.bbox(item_id, column=column_id_str)
                 if not bbox:
                     table_widget.see(item_id)
                     self.update_idletasks()
                     bbox = table_widget.bbox(item_id, column=column_id_str)
                     if not bbox:
                         # messagebox.showwarning("提示", "无法编辑当前不可见的单元格。", parent=self) # 暂时注释掉，看是否干扰
                         self._reset_edit_state()
                         return
                 x, y, width, height = bbox
            except tk.TclError as e:
                 log.error(f"获取单元格bbox时出错: {e}")
                 self._reset_edit_state()
                 return


            current_value = table_widget.set(item_id, column=self._edit_column_name)

            self._edit_widget_type = 'text'
            self._edit_widget = tk.Text(
                table_widget, wrap=tk.WORD, height=3, bd=1, relief=tk.SUNKEN,
                font=ttk.Style().lookup("TEntry", "font")
            )
            self._edit_widget.insert("1.0", current_value)
            actual_height = max(height, self._edit_widget.winfo_reqheight())
            self._edit_widget.place(x=x, y=y, width=width, height=actual_height)

            # --- 通用绑定 ---
            self._edit_widget.bind('<Control-Return>', self._commit_edit) # Text 和 Entry 都支持
            self._edit_widget.bind('<Return>', self._commit_edit) # Entry 主要用这个
            self._edit_widget.bind('<Escape>', self._cancel_edit)
            self._edit_widget.bind('<FocusOut>', self._commit_edit)

            self._edit_widget.focus_force()
            log.debug(f"编辑器 ({self._edit_widget_type}) 已放置于列 '{self._edit_column_name}'。")

        except Exception as e:
            log.exception(f"启动编辑时发生错误: {e}")
            messagebox.showerror("编辑错误", f"无法开始编辑单元格:\n{e}", parent=self)
            self._reset_edit_state()


    def _commit_edit(self, event=None):
        """提交编辑结果。只将 '修正译文' 列的更改写回 Treeview。"""
        if not self._edit_widget: return "break"
        try:
            item_id = self._edit_item_id
            column_name_being_edited = self._edit_column_name # 当前编辑的列名
            widget = self._edit_widget

            new_value = ""
            if self._edit_widget_type == 'text':
                new_value = widget.get("1.0", tk.END + "-1c").strip()
            elif self._edit_widget_type == 'entry':
                new_value = widget.get().strip() # Entry 的值也 strip 一下
            else:
                 log.warning(f"未知的编辑控件类型: {self._edit_widget_type}")
                 self._cancel_edit() # 取消编辑
                 return "break"

            # 检查表格和行是否仍然存在
            if not self.table.exists(item_id):
                 log.warning(f"提交编辑失败：行 {item_id} 已不存在。")
                 self._destroy_edit_widget()
                 return "break"

            # --- 关键修改: 只更新 "修正译文" 列 ---
            if column_name_being_edited == 'correction':
                self.table.set(item_id, column=column_name_being_edited, value=new_value)
                log.debug(f"已提交修正: Item={item_id}, 列='{column_name_being_edited}', Value='{new_value[:50]}...'")
            else:
                log.debug(f"忽略对列 '{column_name_being_edited}' 的编辑提交，仅用于临时查看/复制。")
                # 对于其他列，我们不更新 Treeview，编辑仅是临时的

        except tk.TclError as e:
             log.warning(f"提交编辑失败 (TclError): {e}")
        except Exception as e:
             log.exception(f"提交编辑时发生未知错误: {e}")
        finally:
            self._destroy_edit_widget() # 无论如何都销毁编辑器
        return "break" # 阻止事件进一步传播
    
    # --- 取消编辑 ---
    def _cancel_edit(self, event=None):
        """取消编辑。"""
        log.debug("取消编辑。")
        self._destroy_edit_widget()
        return "break"

    # _destroy_edit_widget 和 _reset_edit_state 复用 DictEditor 的即可
    def _destroy_edit_widget(self):
        widget = self._edit_widget
        if widget:
            try:
                # 解绑事件
                try: widget.unbind('<FocusOut>')
                except tk.TclError: pass
                try: widget.unbind('<Return>')
                except tk.TclError: pass
                try: widget.unbind('<Control-Return>')
                except tk.TclError: pass
                try: widget.unbind('<Escape>')
                except tk.TclError: pass
                # 销毁控件
                try: widget.destroy()
                except tk.TclError: pass
            except Exception as e:
                 log.exception(f"销毁编辑控件时发生未知错误: {e}")
        self._reset_edit_state()

    def _reset_edit_state(self):
        self._edit_widget = None
        self._edit_item_id = None
        self._edit_column_id_str = None
        self._edit_column_name = None
        self._edit_widget_type = None
        log.debug("编辑状态已重置。")

    # --- 保存逻辑 ---
    def _save_corrections(self):
        """收集修正，更新 JSON 文件，并重写 CSV 文件，移除已修正的行。"""
        # 1. 提交可能正在进行的编辑
        if self._edit_widget:
            self._commit_edit()
            if self._edit_widget: # 提交失败
                messagebox.showwarning("保存提示", "请先完成或取消当前单元格的编辑。", parent=self)
                return

        # 2. 收集修正结果
        corrections = {}
        corrected_item_ids = set() # 记录被修正的行 ID，用于过滤 CSV
        for item_id in self.table.get_children():
            # 获取修正列 (#3) 的值
            corrected_value = self.table.set(item_id, column=self.columns[2]) # 'correction'
            # 只有当修正列有非空内容时才视为修正
            if corrected_value and corrected_value.strip():
                original_key = self.table.set(item_id, column=self.columns[0]) # 'original'
                if original_key: # 确保原文 key 存在
                    corrections[original_key] = corrected_value.strip() # 存储修正值
                    corrected_item_ids.add(item_id) # 记录此行已被修正
                else:
                    log.warning(f"跳过修正，行 {item_id} 没有有效的原文 Key。")

        if not corrections:
            messagebox.showinfo("无需保存", "没有检测到任何修正。", parent=self)
            return

        log.info(f"准备保存 {len(corrections)} 条修正...")

        # 3. 读取原始 CSV 数据 (用于过滤)
        original_csv_data = []
        try:
            with open(self.fallback_csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f, quoting=csv.QUOTE_ALL)
                original_csv_data = list(reader) # 读取所有行
        except FileNotFoundError:
            messagebox.showerror("错误", f"无法读取原始回退文件:\n{self.fallback_csv_path}", parent=self)
            return
        except Exception as e:
            log.exception(f"读取原始 CSV 时出错: {e}")
            messagebox.showerror("错误", f"读取原始回退文件时出错:\n{e}", parent=self)
            return

        if not original_csv_data: # 文件为空或只有表头
             messagebox.showerror("错误", f"原始回退文件为空或无效:\n{self.fallback_csv_path}", parent=self)
             return

        csv_header = original_csv_data[0] # 获取表头

        # 4. 读取并更新 JSON 数据
        json_data = {}
        try:
            with open(self.translated_json_path, 'r', encoding='utf-8') as f_json:
                json_data = json.load(f_json)

            # 应用修正
            update_count = 0
            for original_key, corrected_value in corrections.items():
                if original_key in json_data:
                    json_data[original_key] = corrected_value
                    update_count += 1
                else:
                    log.warning(f"尝试修正 JSON，但未找到 Key: {original_key}")

            log.info(f"已将 {update_count} 条修正应用到 JSON 数据。")

        except FileNotFoundError:
            messagebox.showerror("错误", f"无法读取已翻译的 JSON 文件:\n{self.translated_json_path}", parent=self)
            return
        except Exception as e:
            log.exception(f"读取或更新 JSON 时出错: {e}")
            messagebox.showerror("错误", f"读取或更新 JSON 文件时出错:\n{e}", parent=self)
            return

        # 5. 过滤 CSV 数据 (构建新列表，保留未修正的行)
        new_csv_data = [csv_header] # 从表头开始
        # 创建一个映射：行ID -> 原文Key，方便查找
        item_id_to_original = {item_id: self.table.set(item_id, column=self.columns[0])
                               for item_id in self.table.get_children()}

        # 遍历原始 CSV 数据行（跳过表头）
        # 注意：这里不能依赖 Treeview 的 item_id 来过滤原始 CSV，因为原始 CSV 没有 item_id
        # 我们需要根据“原文”来判断是否被修正
        original_keys_in_corrections = set(corrections.keys())
        kept_row_count = 0
        for row_index, original_row in enumerate(original_csv_data[1:], start=1):
            if not original_row: continue # 跳过空行
            row_original_key = original_row[0] if len(original_row) > 0 else None
            if row_original_key and row_original_key not in original_keys_in_corrections:
                # 如果这一行的原文不在修正字典里，保留它
                new_csv_data.append(original_row)
                kept_row_count += 1

        log.info(f"过滤后的 CSV 将保留 {kept_row_count} 条未修正的回退项。")

        # 6. 保存文件
        try:
            # 保存 JSON
            # 确保目录存在
            file_system.ensure_dir_exists(os.path.dirname(self.translated_json_path))
            with open(self.translated_json_path, 'w', encoding='utf-8') as f_json_out:
                json.dump(json_data, f_json_out, ensure_ascii=False, indent=4)
            log.info("已保存更新后的 JSON 文件。")

            # 保存过滤后的 CSV
            # 确保目录存在
            file_system.ensure_dir_exists(os.path.dirname(self.fallback_csv_path))
            # 如果过滤后只剩表头，相当于清空了需要修正的回退项
            with open(self.fallback_csv_path, 'w', newline='', encoding='utf-8-sig') as f_csv_out:
                writer = csv.writer(f_csv_out, quoting=csv.QUOTE_ALL)
                writer.writerows(new_csv_data)
            log.info("已保存过滤后的回退修正 CSV 文件。")

            messagebox.showinfo("保存成功", f"成功应用了 {len(corrections)} 条修正。\nJSON 文件已更新，剩余回退项已保存。", parent=self)

            # 7. 通知 App 更新状态
            if self.app and hasattr(self.app, '_check_and_update_ui_states'):
                # 使用 after 确保 UI 更新在当前事件处理之后执行
                self.after(50, self.app._check_and_update_ui_states)

            # 8. 关闭对话框
            self.destroy()

        except Exception as e:
            log.exception(f"保存修正文件时出错: {e}")
            messagebox.showerror("保存失败", f"保存文件时出错:\n{e}", parent=self)

    def _on_close(self):
        """处理窗口关闭事件，检查是否有未保存的编辑。"""
        # 简单起见，直接关闭。如果需要检查未保存的编辑，逻辑会更复杂。
        # （可以遍历 Treeview 检查是否有编辑状态的单元格，但比较麻烦）
        self.destroy()