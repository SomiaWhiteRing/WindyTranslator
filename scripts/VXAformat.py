import json
import os

INPUT_FILE = "translation_translated.json"
OUTPUT_FILE = "RubyImport.txt"

def to_ruby_str(obj):
    """递归将Python对象转换为Ruby语法的字符串"""
    if isinstance(obj, dict):
        items = []
        for k, v in obj.items():
            # Ruby 1.9.2 Hash 语法: "Key" => Value
            items.append(f'{to_ruby_str(k)} => {to_ruby_str(v)}')
        return '{' + ', '.join(items) + '}'
    elif isinstance(obj, list):
        items = [to_ruby_str(x) for x in obj]
        return '[' + ', '.join(items) + ']'
    elif isinstance(obj, str):
        # 使用 json.dumps 处理字符串转义，Ruby 和 JSON 的字符串格式基本兼容
        return json.dumps(obj, ensure_ascii=False)
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif obj is None:
        return "nil"
    else:
        return str(obj)

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"错误：找不到 {INPUT_FILE}")
        return

    print("正在读取 JSON...")
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print("正在转换为 Ruby 格式...")
    ruby_content = to_ruby_str(data)

    print("正在保存为 RubyImport.txt ...")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        # 加上 UTF-8 BOM，防止 RMVA 读取中文乱码
        f.write('\ufeff') 
        f.write(ruby_content)

    print(f"完成！请将 {OUTPUT_FILE} 放入游戏根目录并运行新的导入脚本。")

if __name__ == "__main__":
    main()