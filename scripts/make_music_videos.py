"""
音乐室一图流视频自动生成工具

从 RPG Maker 2000/2003 的 Map0009.lmu 中解析 PlayBGM 指令和中文歌名，
将截图与音频配对，用 ffmpeg 生成每首歌的独立 MP4 视频。
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def read_ber(data: bytes, offset: int) -> tuple[int, int]:
    """读取 BER 变长整数，返回 (值, 新偏移)。"""
    val = 0
    while data[offset] & 0x80:
        val = (val << 7) | (data[offset] & 0x7F)
        offset += 1
    val = (val << 7) | (data[offset] & 0x7F)
    offset += 1
    return val, offset


def parse_lmu(map_path: str) -> list[tuple[str, str, int]]:
    """
    解析 Map0009.lmu，提取 PlayBGM 文件名、中文歌名和 tempo 参数。
    返回 [(bgm_filename, chinese_name, tempo), ...] 按游戏内顺序排列。
    tempo=100 为原速，RPG Maker 的 tempo 会同时改变速度和音高。
    """
    with open(map_path, "rb") as f:
        data = f.read()

    PLAY_BGM = b"\xd9\x76"  # RM2K3 PlayBGM (11510) 的 BER 编码
    BRACKET_OPEN = "\u3010".encode("gbk")   # 【
    BRACKET_CLOSE = "\u3011".encode("gbk")  # 】

    results = []
    for m in re.finditer(re.escape(PLAY_BGM), data):
        pos = m.start()
        ctx = data[pos : pos + 120]

        idx = 2  # 跳过指令码
        indent, idx = read_ber(ctx, idx)
        if indent != 1:
            continue

        # 读取 BGM 文件名（字符串长度 + 内容）
        slen, idx = read_ber(ctx, idx)
        bgm_name = ctx[idx : idx + slen].decode("ascii", errors="replace")
        idx += slen

        # 读取 int 参数列表：[fadein, volume, tempo, balance, ...]
        param_count, idx = read_ber(ctx, idx)
        params = []
        for _ in range(param_count):
            p, idx = read_ber(ctx, idx)
            params.append(p)
        tempo = params[2] if len(params) > 2 else 100

        # 在 PlayBGM 后 ~500 字节内搜索【中文歌名】
        after = data[pos : pos + 500]
        bo = after.find(BRACKET_OPEN)
        if bo < 0:
            continue
        bc = after.find(BRACKET_CLOSE, bo)
        if bc < 0:
            continue
        song_name_bytes = after[bo + len(BRACKET_OPEN) : bc]
        try:
            chinese_name = song_name_bytes.decode("gbk")
        except UnicodeDecodeError:
            chinese_name = bgm_name  # fallback

        results.append((bgm_name, chinese_name, tempo))

    return results


def get_sample_rate(ogg_path: Path) -> int:
    """用 ffprobe 获取音频文件的采样率。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0",
         str(ogg_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return int(result.stdout.strip())


def sanitize_filename(name: str) -> str:
    """替换文件系统非法字符。"""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name


def main():
    parser = argparse.ArgumentParser(description="音乐室一图流视频生成工具")
    parser.add_argument("--map", required=True, help="Map0009.lmu 路径")
    parser.add_argument("--music", required=True, help="Music 文件夹路径")
    parser.add_argument("--images", required=True, help="MusicRoom 截图文件夹路径")
    parser.add_argument("--output", required=True, help="输出视频文件夹路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印映射表，不生成视频")
    args = parser.parse_args()

    # Step 1: 解析 LMU
    print("正在解析", args.map, "...")
    songs = parse_lmu(args.map)
    print(f"解析到 {len(songs)} 首歌曲。\n")

    # Step 2: 收集截图（按文件名排序）
    image_dir = Path(args.images)
    screenshots = sorted(
        [f for f in image_dir.iterdir() if f.suffix.lower() == ".png"]
    )

    if len(screenshots) != len(songs):
        print(f"错误：截图数量 ({len(screenshots)}) 与歌曲数量 ({len(songs)}) 不匹配！")
        sys.exit(1)

    # Step 3: 建立映射并输出/生成视频
    music_dir = Path(args.music)
    output_dir = Path(args.output)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'序号':>4}  {'中文歌名':<36}  {'BGM文件':<45}  {'tempo':>5}  {'截图'}")
    print("-" * 140)

    for i, ((bgm_name, chinese_name, tempo), screenshot) in enumerate(
        zip(songs, screenshots), start=1
    ):
        ogg_path = music_dir / f"{bgm_name}.ogg"
        safe_name = sanitize_filename(chinese_name)
        out_name = f"{i:02d}_{safe_name}.mp4"
        tempo_str = str(tempo) if tempo != 100 else ""

        print(f"{i:4d}  {chinese_name:<36}  {bgm_name}.ogg{' ' * max(0, 40 - len(bgm_name))}  {tempo_str:>5}  {screenshot.name}")

        if not ogg_path.exists():
            print(f"  ⚠ 音频文件不存在: {ogg_path}")
            continue

        if args.dry_run:
            continue

        out_path = output_dir / out_name
        if out_path.exists():
            print(f"  跳过（已存在）")
            continue

        # 构建 ffmpeg 命令
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(screenshot),
            "-i", str(ogg_path),
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        ]

        # tempo≠100 时用 asetrate+aresample 模拟 RPG Maker 的变速变调
        if tempo != 100:
            sr = get_sample_rate(ogg_path)
            ratio = tempo / 100.0
            new_rate = int(sr * ratio)
            cmd += ["-af", f"asetrate={new_rate},aresample={sr}"]

        cmd += [
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
            str(out_path),
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            print(f"  ✗ ffmpeg 失败: {result.stderr[-200:]}")
        else:
            print(f"  ✓ -> {out_name}")

    print("\n完成。")


if __name__ == "__main__":
    main()
