# core/tasks/dict_generation.py
import os
import json
import csv
import io # 用于将字符串模拟成文件给 csv reader
import logging
from core.api_clients import gemini # 导入 Gemini API 客户端模块
from core.utils import file_system, text_processing

log = logging.getLogger(__name__)

# --- 主任务函数 ---
def run_generate_dictionary(game_path, works_dir, world_dict_config, message_queue):
    """
    使用 Gemini API 从提取的文本生成世界观字典 (CSV 文件)。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 工作目录的根路径。
        world_dict_config (dict): 包含 Gemini API Key、模型和 Prompt 的配置字典。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    try:
        message_queue.put(("status", "正在生成世界观字典..."))
        message_queue.put(("log", ("normal", "步骤 4: 开始生成世界观字典 (使用 Gemini API)...")))

        # --- 确定文件路径 ---
        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)
        untranslated_dir = os.path.join(work_game_dir, "untranslated")
        json_path = os.path.join(untranslated_dir, "translation.json")
        dict_csv_path = os.path.join(work_game_dir, "world_dictionary.csv")
        temp_text_path = os.path.join(work_game_dir, "_temp_world_text.txt") # 临时聚合文本文件

        if not os.path.exists(json_path):
            message_queue.put(("error", f"未找到未翻译的 JSON 文件: {json_path}，请先执行步骤 3。"))
            message_queue.put(("status", "生成字典失败"))
            message_queue.put(("done", None))
            return

        # --- 加载 JSON 并准备输入文本 ---
        message_queue.put(("log", ("normal", "加载 JSON 文件并提取原文...")))
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                translations = json.load(f)
            original_texts = list(translations.keys())
            message_queue.put(("log", ("normal", f"共提取 {len(original_texts)} 条原文用于分析。")))
            if not original_texts:
                 message_queue.put(("warning", "JSON 文件中没有可用于分析的原文。"))
                 message_queue.put(("status", "生成字典跳过(无文本)"))
                 message_queue.put(("done", None))
                 return

            # 将原文写入临时文件（如果文本量巨大，这可能不是最高效的方式）
            # 注意：Gemini API 对输入长度有限制，超长文本可能需要分块处理或采样
            message_queue.put(("log", ("normal", "将原文写入临时文件...")))
            with open(temp_text_path, 'w', encoding='utf-8') as f_temp:
                # 使用换行符分隔，符合 prompt 预期
                f_temp.write("\n".join(original_texts))
            log.info(f"临时原文聚合文件已创建: {temp_text_path}")

        except Exception as e:
            log.exception(f"加载 JSON 或写入临时文件时出错: {e}")
            message_queue.put(("error", f"加载 JSON 或准备输入文本失败: {e}"))
            message_queue.put(("status", "生成字典失败"))
            message_queue.put(("done", None))
            # 尝试清理临时文件
            file_system.safe_remove(temp_text_path)
            return

        # --- 调用 Gemini API ---
        api_key = world_dict_config.get("api_key", "").strip()
        model_name = world_dict_config.get("model", "").strip()
        prompt_template = world_dict_config.get("prompt", "")

        if not api_key:
            message_queue.put(("error", "Gemini API Key 未配置。"))
            message_queue.put(("status", "生成字典失败"))
            message_queue.put(("done", None))
            file_system.safe_remove(temp_text_path)
            return
        if not model_name:
            message_queue.put(("error", "Gemini 模型名称未配置。"))
            message_queue.put(("status", "生成字典失败"))
            message_queue.put(("done", None))
            file_system.safe_remove(temp_text_path)
            return
        if not prompt_template:
            message_queue.put(("error", "Gemini Prompt 模板未配置。"))
            message_queue.put(("status", "生成字典失败"))
            message_queue.put(("done", None))
            file_system.safe_remove(temp_text_path)
            return

        try:
            # 格式化 Prompt，将临时文件内容嵌入
            with open(temp_text_path, 'r', encoding='utf-8') as f_temp_read:
                game_text_content = f_temp_read.read()

            # 检查文本大小是否过大（示例性检查，阈值需要调整）
            # Gemini 1.5 Pro 支持非常长的上下文，但 API 本身可能有请求大小限制
            MAX_PROMPT_SIZE_MB = 5 # 假设 API 限制 5MB 左右（需要查阅实际文档）
            if len(game_text_content.encode('utf-8')) / (1024*1024) > MAX_PROMPT_SIZE_MB:
                 log.warning(f"聚合后的游戏文本大小 ({len(game_text_content.encode('utf-8')) / (1024*1024):.2f} MB) 可能超过 API 限制。结果可能不完整或失败。")
                 # 这里可以实现分块逻辑，但会显著增加复杂度，暂时只给警告

            final_prompt = prompt_template.format(game_text=game_text_content)

            message_queue.put(("log", ("normal", f"正在调用 Gemini API (模型: {model_name})... 请耐心等待。")))
            message_queue.put(("status", "正在调用 Gemini API 生成字典...")) # 更新状态

            client = gemini.GeminiClient(api_key)
            # 可以从配置中读取 generation_config 和 safety_settings
            # generation_config = {"temperature": 0.5} # 示例
            # safety_settings = [...] # 示例
            success, csv_response, error_message = client.generate_content(
                model_name,
                final_prompt
                # generation_config=generation_config,
                # safety_settings=safety_settings
            )

            if not success:
                message_queue.put(("error", f"Gemini API 调用失败: {error_message}"))
                message_queue.put(("status", "生成字典失败"))
                message_queue.put(("done", None))
                file_system.safe_remove(temp_text_path)
                return

            message_queue.put(("log", ("success", "Gemini API 调用成功，正在解析响应...")))

        except Exception as api_err:
            log.exception(f"调用 Gemini API 或准备请求时出错: {api_err}")
            message_queue.put(("error", f"调用 Gemini API 时出错: {api_err}"))
            message_queue.put(("status", "生成字典失败"))
            message_queue.put(("done", None))
            file_system.safe_remove(temp_text_path)
            return

        # --- 解析 CSV 响应并保存 ---
        parsed_data = []
        if csv_response:
            try:
                # 使用 io.StringIO 将字符串模拟成文件，以便 csv.reader 处理
                # 移除响应前后可能存在的空白或```csv标记
                clean_csv_response = csv_response.strip().strip('```csv').strip('```').strip()
                csv_file = io.StringIO(clean_csv_response)
                # 严格按照 Prompt 要求解析：双引号包围，逗号分隔
                reader = csv.reader(csv_file, quotechar='"', delimiter=',',
                                    quoting=csv.QUOTE_ALL, skipinitialspace=True)
                header_skipped = False # Prompt 要求不带表头
                for i, row in enumerate(reader):
                    # if not header_skipped: # 如果 Gemini 可能返回表头，取消注释这行
                    #     header_skipped = True
                    #     continue
                    if len(row) == 4:
                        parsed_data.append(row)
                    else:
                        log.warning(f"跳过格式错误的 CSV 行 #{i+1} (期望4列，得到{len(row)}列): {row}")
                        message_queue.put(("log", ("warning", f"跳过格式错误的 CSV 行 #{i+1} (期望4列): {row}")))
            except Exception as parse_err:
                log.error(f"解析 Gemini 返回的 CSV 数据时出错: {parse_err}")
                log.error(f"原始 CSV 响应内容:\n{csv_response}") # 记录原始响应供调试
                message_queue.put(("error", f"解析 Gemini 返回的 CSV 数据失败: {parse_err}"))
                # 即使解析出错，也尝试保存已成功解析的部分
        else:
            log.warning("Gemini API 未返回任何内容。")
            message_queue.put(("log", ("warning", "Gemini API 未返回任何内容。")))

        # --- 保存为 CSV 文件 ---
        message_queue.put(("log", ("normal", f"正在保存世界观字典到: {dict_csv_path}")))
        try:
            with open(dict_csv_path, 'w', newline='', encoding='utf-8-sig') as f_csv: # utf-8-sig 确保 Excel 能正确打开
                writer = csv.writer(f_csv, quoting=csv.QUOTE_ALL)
                # 写入表头 (即使 Prompt 要求不带，我们自己加上方便编辑)
                writer.writerow(['原文', '译文', '类别', '描述'])
                if parsed_data:
                    writer.writerows(parsed_data)

            num_entries = len(parsed_data)
            message_queue.put(("success", f"世界观字典生成成功，共 {num_entries} 条有效条目已保存: {dict_csv_path}"))
            message_queue.put(("status", "生成字典完成"))
            message_queue.put(("done", None))

        except Exception as write_err:
            log.exception(f"保存 CSV 文件失败: {dict_csv_path} - {write_err}")
            message_queue.put(("error", f"保存 CSV 文件失败: {write_err}"))
            message_queue.put(("status", "生成字典失败"))
            message_queue.put(("done", None))

    except Exception as e:
        log.exception("生成字典任务执行期间发生意外错误。")
        message_queue.put(("error", f"生成字典过程中发生严重错误: {e}"))
        message_queue.put(("status", "生成字典失败"))
        message_queue.put(("done", None))
    finally:
        # 确保临时文件被删除
        if 'temp_text_path' in locals() and os.path.exists(temp_text_path):
            if file_system.safe_remove(temp_text_path):
                 log.info(f"已清理临时原文聚合文件: {temp_text_path}")