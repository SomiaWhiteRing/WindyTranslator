import tkinter as tk
from tkinter import ttk, messagebox
import csv
import os
import logging
# 确保导入 text_processing 用于清理游戏名
from core.utils import file_system, text_processing

log = logging.getLogger(__name__)

class DictEditorWindow(tk.Toplevel):
    """世界观字典编辑器窗口。"""

    def __init__(self, parent, app_controller, works_dir, game_path):
        """
        初始化字典编辑器窗口。

        Args:
            parent (tk.Widget): 父窗口。
            app_controller (RPGTranslatorApp): 应用控制器实例 (可能用于日志或状态)。
            works_dir (str): Works 目录根路径。
            game_path (str): 当前游戏路径，用于确定字典文件位置。
        """
        super().__init__(parent)
        self.app = app_controller # 保留引用，可能需要记录日志

        # --- 确定字典文件路径 ---
        # 使用 text_processing 清理游戏文件夹名
        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame" # 提供默认名
        work_game_dir = os.path.join(works_dir, game_folder_name)
        self.dict_csv_path = os.path.join(work_game_dir, "world_dictionary.csv")

        # --- 窗口设置 ---
        self.title(f"世界观字典编辑器 - {game_folder_name}")
        self.geometry("1000x600") # 设置较大的默认尺寸
        self.transient(parent)
        # self.grab_set() # 可选：如果希望在关闭编辑器前不能操作主窗口

        # --- 确保目录和文件存在 ---
        if not file_system.ensure_dir_exists(work_game_dir):
             messagebox.showerror("错误", f"无法创建工作目录: {work_game_dir}", parent=self)
             self.destroy()
             return
        if not os.path.exists(self.dict_csv_path):
            self._create_empty_dict_file()
            # 如果创建失败，_create_empty_dict_file 会处理错误并可能销毁窗口
            if not os.path.exists(self.dict_csv_path): # 再次检查，以防万一
                return


        # --- 状态变量 (移到控件创建前) ---
        self._edit_widget = None # 当前的编辑控件实例 (Entry 或 Text)
        self._edit_item_id = None # 正在编辑的行 ID
        self._edit_column_id = None # 正在编辑的列标识符 ('original', 'translation', etc.)
        self._edit_widget_type = None # 标记当前编辑控件类型 ('entry' or 'text')

        # --- 创建控件 ---
        self.create_widgets()

        # --- 加载数据 ---
        # 确保控件创建完毕后再加载
        self._load_data()


    def create_widgets(self):
        """创建窗口中的所有控件。"""
        # --- 主框架 ---
        main_frame = ttk.Frame(self, padding="5")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- 表格区域 ---
        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # Treeview 作为表格
        columns = ('original', 'translation', 'category', 'description')
        self.dict_table = ttk.Treeview(
            table_frame,
            columns=columns,
            show='headings', # 只显示列标题，不显示第一列的树状结构
            selectmode='browse' # 'browse' 单选, 'extended' 多选
        )

        # 定义列标题
        self.dict_table.heading('original', text='原文')
        self.dict_table.heading('translation', text='译文')
        self.dict_table.heading('category', text='类别')
        self.dict_table.heading('description', text='描述')

        # 设置列宽和对齐方式
        self.dict_table.column('original', width=200, anchor='w', stretch=True) # 允许拉伸
        self.dict_table.column('translation', width=200, anchor='w', stretch=True)
        self.dict_table.column('category', width=100, anchor='center', stretch=False) # 类别通常较短
        self.dict_table.column('description', width=400, anchor='w', stretch=True)

        # 垂直滚动条
        scrollbar_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.dict_table.yview)
        scrollbar_y.pack(side="right", fill="y")
        # 水平滚动条
        scrollbar_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.dict_table.xview)
        scrollbar_x.pack(side="bottom", fill="x")

        self.dict_table.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        self.dict_table.pack(side="left", fill="both", expand=True)

        # --- 绑定事件 ---
        self.dict_table.bind('<Double-1>', self._on_cell_double_click) # 双击编辑
        self.dict_table.bind('<Delete>', self._delete_selected_row)   # 按 Delete 键删除
        self.dict_table.bind('<ButtonRelease-1>', self._on_selection_change) # 单击释放时更新状态 **但不提交编辑**
        # 注意：<FocusOut>事件现在绑定在编辑控件上，而不是 Treeview 上

        # --- 按钮区域 ---
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=5)

        # 添加行按钮
        add_button = ttk.Button(button_frame, text="添加行", command=self._add_row)
        add_button.pack(side=tk.LEFT, padx=5)

        # 删除行按钮
        self.delete_button = ttk.Button(button_frame, text="删除选中行", command=self._delete_selected_row, state=tk.DISABLED)
        self.delete_button.pack(side=tk.LEFT, padx=5)

        # 保存按钮
        save_button = ttk.Button(button_frame, text="保存", command=self._save_data)
        save_button.pack(side=tk.RIGHT, padx=5)

        # 取消/关闭按钮
        cancel_button = ttk.Button(button_frame, text="关闭", command=self.destroy)
        cancel_button.pack(side=tk.RIGHT, padx=5)

        # --- (状态变量移到 __init__ 顶部) ---

    def _create_empty_dict_file(self):
        """如果字典文件不存在，则创建一个带表头的空文件。"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.dict_csv_path), exist_ok=True)
            with open(self.dict_csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                writer.writerow(['原文', '译文', '类别', '描述']) # 写入表头
            log.info(f"已创建空的字典文件: {self.dict_csv_path}")
            if hasattr(self.app, 'log_message'): # 通知主界面
                 self.app.log_message(f"已创建空的字典文件: {os.path.basename(self.dict_csv_path)}", "success")
        except Exception as e:
            log.exception(f"创建空字典文件失败: {self.dict_csv_path} - {e}")
            messagebox.showerror("错误", f"无法创建字典文件:\n{e}", parent=self)
            # 如果创建失败，可能需要关闭窗口，防止后续加载或保存出错
            self.after(10, self.destroy) # 延迟销毁，避免在初始化过程中直接销毁可能引发的问题

    def _load_data(self):
        """从 CSV 文件加载数据并填充到 Treeview 表格中。"""
        # 清空现有数据
        for item in self.dict_table.get_children():
            self.dict_table.delete(item)

        # 检查文件是否存在
        if not os.path.exists(self.dict_csv_path):
             log.warning(f"字典文件未找到，将创建空文件: {self.dict_csv_path}")
             self._create_empty_dict_file()
             # 如果创建失败，_create_empty_dict_file 内部会处理并可能关闭窗口
             if not os.path.exists(self.dict_csv_path):
                 log.error("创建空字典文件后仍然无法找到，加载失败。")
                 return # 无法加载，直接返回

        try:
            with open(self.dict_csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f, quoting=csv.QUOTE_ALL)
                header = next(reader, None) # 读取并跳过表头
                if not header or len(header) != 4:
                     log.warning(f"字典文件 {self.dict_csv_path} 表头无效或列数不为 4。将尝试加载。")
                     # 即使表头不对，也尝试读取数据行

                for i, row in enumerate(reader):
                    if len(row) == 4: # 确保行数据有4个元素
                        self.dict_table.insert('', 'end', values=row, iid=f"row_{i}") # 使用唯一的 iid
                    elif len(row) > 4:
                         log.warning(f"字典文件中第 {i+1} 行有多于4列，将只取前4列: {row}")
                         self.dict_table.insert('', 'end', values=row[:4], iid=f"row_{i}")
                    elif row: # 如果行不为空但列数不足，用空字符串填充
                        log.warning(f"字典文件中第 {i+1} 行少于4列，将用空值填充: {row}")
                        padded_row = list(row) + [''] * (4 - len(row))
                        self.dict_table.insert('', 'end', values=padded_row, iid=f"row_{i}")
                    # else: 空行直接跳过

            log.info(f"已从 {self.dict_csv_path} 加载数据。")
        except FileNotFoundError:
             # 这个理论上在前面已经处理了，但为了健壮性保留
             log.error(f"字典文件未找到 (意外情况): {self.dict_csv_path}")
             messagebox.showerror("加载失败", f"字典文件未找到:\n{self.dict_csv_path}", parent=self)
        except Exception as e:
            log.exception(f"加载字典文件失败: {self.dict_csv_path} - {e}")
            messagebox.showerror("加载失败", f"无法加载字典文件:\n{e}", parent=self)


    def _save_data(self):
        """将 Treeview 表格中的数据保存回 CSV 文件。"""
        # 如果当前有单元格在编辑状态，先强制保存
        if self._edit_widget:
            self._commit_edit() # 尝试提交当前编辑

        data_to_save = []
        # 添加表头行
        data_to_save.append(['原文', '译文', '类别', '描述'])

        # 获取所有行的数据
        for item_id in self.dict_table.get_children():
            values = self.dict_table.item(item_id, 'values')
            # 确保values是列表或元组，并且包含4个元素，以防万一
            if isinstance(values, (list, tuple)) and len(values) == 4:
                 data_to_save.append(list(values)) # 转换为 list
            else:
                 log.warning(f"保存时跳过无效数据行 (ID: {item_id}, Values: {values})")


        try:
            with open(self.dict_csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL) # 写入时也用 QUOTE_ALL 保持一致
                writer.writerows(data_to_save)
            log.info(f"字典数据已保存到: {self.dict_csv_path}")
            if hasattr(self.app, 'log_message'):
                self.app.log_message("世界观字典已保存。", "success")
            # 保存成功后给用户反馈，但避免过于频繁的弹窗，可以考虑状态栏提示
            # messagebox.showinfo("保存成功", "世界观字典已成功保存。", parent=self)
        except Exception as e:
            log.exception(f"保存字典文件失败: {self.dict_csv_path} - {e}")
            messagebox.showerror("保存失败", f"无法保存字典文件:\n{e}", parent=self)

    def _add_row(self):
        """在表格末尾添加一个空行。"""
        self._commit_edit() # 完成之前的编辑
        # 生成一个唯一的 iid，避免潜在冲突
        new_iid = f"row_{len(self.dict_table.get_children())}_{os.urandom(4).hex()}"
        new_item_id = self.dict_table.insert('', 'end', values=['', '', '', ''], iid=new_iid)
        self.dict_table.selection_set(new_item_id) # 选中新行
        self.dict_table.see(new_item_id) # 滚动到新行
        self._update_delete_button_state() # 添加行后可能需要启用删除按钮
        # 可以选择性地直接进入第一列的编辑状态
        # self.after(50, lambda: self._start_edit(new_item_id, '#1')) # 延迟一点启动编辑，确保UI更新

    def _delete_selected_row(self, event=None):
        """删除当前选中的行。"""
        selected_items = self.dict_table.selection()
        if not selected_items:
            return

        # 检查是否有正在编辑的行是被选中的行之一
        if self._edit_widget and self._edit_item_id in selected_items:
            self._cancel_edit() # 如果正在编辑要删除的行，先取消编辑

        if messagebox.askyesno("确认删除", f"确定要删除选中的 {len(selected_items)} 行吗？", parent=self):
            for item_id in selected_items:
                try:
                    self.dict_table.delete(item_id)
                except tk.TclError as e:
                    # 可能因为 item_id 无效等原因删除失败，记录日志
                    log.warning(f"删除行 {item_id} 失败: {e}")
            self._update_delete_button_state() # 删除后更新按钮状态

    def _update_delete_button_state(self):
        """根据是否有选中项更新删除按钮的状态"""
        if self.dict_table.selection():
            self.delete_button.config(state=tk.NORMAL)
        else:
            self.delete_button.config(state=tk.DISABLED)

    def _on_selection_change(self, event=None):
         """
         当表格选中行改变时，更新删除按钮状态。
         **重要：这里不再调用 _commit_edit() 来避免双击编辑问题。**
         编辑的提交由编辑控件的 <FocusOut> 或 <Return> (Entry) / <Control-Return> (Text) 处理。
         """
         # 仅更新按钮状态
         self._update_delete_button_state()

         # 如果单击发生在编辑控件之外，并且当前有编辑控件存在，则触发编辑控件的失去焦点事件，
         # 这将间接调用 _commit_edit 或 _cancel_edit (取决于绑定)。
         # 但我们不需要在这里显式调用 _commit_edit。Tkinter的事件机制会处理焦点变化。


    def _on_cell_double_click(self, event):
        """处理单元格双击事件，启动编辑。"""
        # 如果当前已经在编辑，先提交或取消
        if self._edit_widget:
            # 这里最好是提交，避免用户双击另一个单元格时丢失之前的修改
            self._commit_edit()
            # 如果提交失败或被取消，则不继续启动新的编辑
            if self._edit_widget:
                 return # 仍然处于编辑状态，不启动新的

        region = self.dict_table.identify_region(event.x, event.y)
        if region != "cell":
            log.debug("Double click not on cell")
            return # 没有点在单元格上

        column_id_str = self.dict_table.identify_column(event.x) # 例如 '#1', '#2'
        item_id = self.dict_table.identify_row(event.y)      # 行 ID (iid)

        if not item_id or not column_id_str:
            log.debug(f"Could not identify row ({item_id}) or column ({column_id_str})")
            return

        log.debug(f"Starting edit for item: {item_id}, column: {column_id_str}")
        self._start_edit(item_id, column_id_str)

    def _start_edit(self, item_id, column_id_str):
        """在指定的单元格启动编辑。"""
        try:
            column_index = int(column_id_str.replace('#', '')) - 1
            # 检查列索引是否有效
            if not (0 <= column_index < len(self.dict_table['columns'])):
                log.warning(f"无效的列索引: {column_index}")
                return

            self._edit_column_id = self.dict_table['columns'][column_index] # 获取列名 ('original', etc.)
            self._edit_item_id = item_id

            # 获取单元格边界和当前值
            # 使用 try-except 包裹 bbox 调用，因为如果行或列无效，它可能抛出 TclError
            try:
                 bbox = self.dict_table.bbox(item_id, column=column_id_str)
                 if not bbox: # 如果单元格不可见，bbox 可能返回空
                     log.warning(f"无法获取单元格边界框: item={item_id}, col={column_id_str}")
                     # 可以尝试滚动到该单元格再试
                     self.dict_table.see(item_id)
                     self.update_idletasks() # 等待滚动完成
                     bbox = self.dict_table.bbox(item_id, column=column_id_str)
                     if not bbox:
                         messagebox.showwarning("提示", "无法编辑当前不可见的单元格。", parent=self)
                         self._reset_edit_state()
                         return
                 x, y, width, height = bbox
            except tk.TclError as e:
                 log.error(f"获取单元格bbox时出错: {e}")
                 self._reset_edit_state()
                 return

            current_value = self.dict_table.set(item_id, column=self._edit_column_id)

            # --- 根据列选择使用 Entry 或 Text ---
            if self._edit_column_id == 'description':
                # --- 使用 Text 进行多行编辑 ---
                self._edit_widget_type = 'text'
                # 创建 Text 控件，设置边框、换行、高度
                # Note: Text widget doesn't directly support ttk themes like Entry.
                # Use tk.Text but maybe add a border to make it look like an input field.
                self._edit_widget = tk.Text(
                    self.dict_table,
                    wrap=tk.WORD, # 自动按单词换行
                    height=3,     # 显示约3行的高度，可调整
                    bd=1,         # 添加边框
                    relief=tk.SUNKEN, # 边框样式
                    font=ttk.Style().lookup("TEntry", "font") # 尝试使用类似Entry的字体
                )
                # 插入当前值
                self._edit_widget.insert("1.0", current_value)
                # 绑定 Text 特有的事件
                self._edit_widget.bind('<Control-Return>', self._commit_edit) # Ctrl+Enter 保存
                self._edit_widget.bind('<Escape>', self._cancel_edit)      # Esc 取消
                # FocusOut 仍然用于保存
                self._edit_widget.bind('<FocusOut>', self._commit_edit)
                # 调整高度以适应内容？这比较复杂，暂时固定高度
                actual_height = max(height, self._edit_widget.winfo_reqheight()) # 取单元格和Text请求高度的最大值
                # 放置 Text 控件
                self._edit_widget.place(x=x, y=y, width=width, height=actual_height)


            else:
                # --- 使用 Entry 进行单行编辑 ---
                self._edit_widget_type = 'entry'
                self._edit_widget = ttk.Entry(self.dict_table,
                                            # font=... # 可以指定字体
                                            )
                # 插入当前值
                self._edit_widget.insert(0, current_value)
                # 选中所有文本
                self._edit_widget.select_range(0, tk.END)
                # 绑定 Entry 特有的事件
                self._edit_widget.bind('<Return>', self._commit_edit)      # 回车保存
                self._edit_widget.bind('<Escape>', self._cancel_edit)      # Esc 取消
                self._edit_widget.bind('<FocusOut>', self._commit_edit)    # 失去焦点时保存
                # self._edit_widget.bind('<Tab>', self._commit_edit_and_move) # Tab 移动 (暂未实现)
                # 放置 Entry 控件
                self._edit_widget.place(x=x, y=y, width=width, height=height)

            # --- 通用操作 ---
            # 强制获取焦点
            self._edit_widget.focus_force()
            log.debug(f"Edit widget ({self._edit_widget_type}) placed and focused.")

        except Exception as e:
            log.exception(f"启动编辑时发生错误: {e}")
            messagebox.showerror("编辑错误", f"无法开始编辑单元格:\n{e}", parent=self)
            self._reset_edit_state() # 出错时清理状态


    def _commit_edit(self, event=None):
        """将编辑控件中的值写回 Treeview 并销毁编辑控件。"""
        if not self._edit_widget or not self._edit_item_id or not self._edit_column_id:
            log.debug("Commit edit called but no active edit widget or state.")
            return # 没有活动的编辑控件

        try:
            new_value = ""
            # 根据控件类型获取值
            if self._edit_widget_type == 'text':
                # 获取 Text 内容，移除末尾可能由 Text 自动添加的换行符
                new_value = self._edit_widget.get("1.0", tk.END + "-1c")
            elif self._edit_widget_type == 'entry':
                new_value = self._edit_widget.get()
            else:
                 log.warning(f"未知的编辑控件类型: {self._edit_widget_type}")
                 self._cancel_edit() # 未知类型，直接取消
                 return "break" # 阻止事件传播


            log.debug(f"Committing edit: Item={self._edit_item_id}, Column={self._edit_column_id}, Value='{new_value[:50]}...'") # 只记录部分值
            # 更新 Treeview 中的值
            self.dict_table.set(self._edit_item_id, column=self._edit_column_id, value=new_value)

        except tk.TclError as e:
             # 如果 item_id 在编辑期间被删除，set 操作会失败
             log.warning(f"提交编辑失败 (可能行已被删除): {e}")
        except Exception as e:
             log.exception(f"提交编辑时发生未知错误: {e}")
             # 可以选择弹窗提示用户
             # messagebox.showerror("错误", f"保存单元格修改时出错:\n{e}", parent=self)
        finally:
            # 无论成功与否，都销毁编辑控件并重置状态
            self._destroy_edit_widget()

        # 对于绑定到 <Return> 或 <Control-Return> 的事件，返回 "break"
        # 可以阻止默认行为（如 Entry 中的换行、Text 中的插入换行）。
        # 对于 <FocusOut>，返回 "break" 通常没有太大影响。
        return "break"

    def _cancel_edit(self, event=None):
        """销毁编辑控件而不保存更改。"""
        log.debug("Cancelling edit.")
        self._destroy_edit_widget()
        # 对于绑定到 <Escape> 的事件，返回 "break" 阻止事件传播可能有用
        return "break"

    def _destroy_edit_widget(self):
        """安全地销毁编辑控件并重置状态变量。"""
        if self._edit_widget:
            try:
                # 解除绑定，以防销毁过程中触发 FocusOut 等事件
                self._edit_widget.unbind('<FocusOut>')
                self._edit_widget.unbind('<Return>')
                self._edit_widget.unbind('<Control-Return>')
                self._edit_widget.unbind('<Escape>')
                # 销毁控件
                self._edit_widget.destroy()
            except tk.TclError as e:
                 # 控件可能已经被销毁，忽略错误
                 log.debug(f"销毁编辑控件时出现 TclError (可能已销毁): {e}")
            except Exception as e:
                 log.exception(f"销毁编辑控件时发生未知错误: {e}")
            finally:
                 # 确保状态被重置
                 self._reset_edit_state()


    def _reset_edit_state(self):
        """重置编辑相关的状态变量。"""
        self._edit_widget = None
        self._edit_item_id = None
        self._edit_column_id = None
        self._edit_widget_type = None
        log.debug("Edit state reset.")