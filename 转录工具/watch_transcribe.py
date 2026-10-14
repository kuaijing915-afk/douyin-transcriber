from pathlib import Path
"""增量监听转录 — 手动启动，轮询目录，新文件自动转录。
启动: python watch_transcribe.py
停止: Ctrl+C
"""
import os
os.environ["MODELSCOPE_CACHE"] = str(Path(__file__).resolve().parents[1] / ".runtime" / "models")
os.environ["HF_HOME"] = str(Path(__file__).resolve().parents[1] / ".runtime" / "huggingface")
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_paths import model_path
import subprocess
import time
from datetime import datetime

# ==================== 配置 ====================
FFMPEG_BIN = str(Path(__file__).resolve().parents[1] / ".runtime" / "ffmpeg" / "bin")
VIDEO_DIR = str(Path(__file__).resolve().parents[1] / "video and audio")
AUDIO_DIR = str(Path(__file__).resolve().parents[1] / "video and audio" / "临时音频")
OUTPUT_DIR = str(Path(__file__).resolve().parents[1] / "输出" / "outcome_watch")
WATCHED_LOG = str(Path(__file__).resolve().parents[1] / "watched.log")
ERROR_LOG = str(Path(__file__).resolve().parents[1] / "logs" / "watch_errors.log")
POLL_INTERVAL = 120  # 秒
# =============================================

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["PATH"] = FFMPEG_BIN + os.pathsep + os.environ["PATH"]

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def log_error(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}\n"
    print(f"[{timestamp}] ERROR: {msg}")
    try:
        with open(ERROR_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError:
        pass


def load_watched() -> set:
    """读取已处理文件名集合（从日志解析）"""
    if not os.path.exists(WATCHED_LOG):
        return set()
    watched = set()
    with open(WATCHED_LOG, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and ' | ' in line:
                filename = line.split(" | ", 2)[-1]
                watched.add(filename)
    return watched


def save_watched(filename: str, status: str):
    """追加到已处理日志。status: 时长如 '05:09' 或 'SKIPPED'"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(WATCHED_LOG, 'a', encoding='utf-8') as f:
        f.write(f"{timestamp} | {status} | {filename}\n")


def scan_new_videos(watched: set) -> list:
    """扫描目录，返回新文件路径列表。跳过正在写入的文件（10秒内有修改）"""
    new_files = []
    now = time.time()
    for f in os.listdir(VIDEO_DIR):
        fpath = os.path.join(VIDEO_DIR, f)
        if not os.path.isfile(fpath):
            continue
        if f.endswith(('.wav', '.txt', '.json', '.log', '.md')):
            continue
        # 跳过正在写入的文件
        if now - os.path.getmtime(fpath) < 10:
            continue
        if f not in watched:
            new_files.append(fpath)
    return sorted(new_files)


def load_model():
    """懒加载 FunASR 模型"""
    print("加载 FunASR 模型（首次 ~3GB，之后秒开）...")
    from funasr import AutoModel
    model = AutoModel(
        model=model_path("paraformer-zh"),
        vad_model=model_path("fsmn-vad"),
        punc_model=model_path("ct-punc"),
        device="cpu",
        disable_update=True,
    )
    print("模型加载完成\n")
    return model


def transcribe_one(model, video_path: str, index: int) -> tuple:
    """转录单个视频。返回 (success, mm, ss, error_msg)"""
    video_name = os.path.basename(video_path)

    # 去扩展名
    base_name = video_name
    for ext in (".mp4", ".MP4", ".mkv", ".avi", ".mov", ".flv", ".m4s"):
        if base_name.endswith(ext):
            base_name = base_name[:-len(ext)]
            break

    short_name = base_name[:80] if len(base_name) > 80 else base_name
    audio_path = os.path.join(AUDIO_DIR, f"_watch_tmp_{index}.wav")
    output_path = os.path.join(OUTPUT_DIR, f"{short_name}.txt")

    # Step 1: ffmpeg 提取音频
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
         "-ar", "16000", "-ac", "1", audio_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        if os.path.exists(audio_path):
            os.remove(audio_path)
        return (False, 0, 0, f"ffmpeg 失败: {result.stderr[:200]}")

    # Step 2: FunASR 转录
    try:
        rec_result = model.generate(
            input=audio_path,
            language="zh",
            batch_size_s=300,
        )
    except Exception as e:
        if os.path.exists(audio_path):
            os.remove(audio_path)
        return (False, 0, 0, f"转录异常: {e}")

    # Step 3: 检查结果
    if not rec_result or len(rec_result) == 0:
        if os.path.exists(audio_path):
            os.remove(audio_path)
        return (False, 0, 0, "转录结果为空")

    # Step 4: 写入文本
    first_seg = rec_result[0]
    last_seg = rec_result[-1]
    last_ts = last_seg.get("timestamp", [[0, 0]])
    total_dur = last_ts[-1][1] / 1000 if last_ts else 0
    mm, ss = int(total_dur // 60), int(total_dur % 60)

    with open(output_path, 'w', encoding='utf-8') as f:
        title = base_name[20:] if len(base_name) > 20 else base_name
        f.write(f'{title}\n\n')
        f.write(f'总时长: {mm:02d}:{ss:02d}\n')
        f.write(f'{"="*60}\n\n')
        for seg in rec_result:
            seg_ts_list = seg.get("timestamp", [[0, 0]])
            ts = seg_ts_list[0][0] / 1000
            m, s = int(ts // 60), int(ts % 60)
            text = seg.get("text", "").strip()
            if text:
                f.write(f'[{m:02d}:{s:02d}] {text}\n\n')

    if os.path.exists(audio_path):
        os.remove(audio_path)

    return (True, mm, ss, "")


def handle_error(video_path: str, error_msg: str) -> str:
    """错误暂停，返回 'retry' | 'skip' | 'quit'"""
    video_name = os.path.basename(video_path)
    log_error(f"{video_name}: {error_msg}")
    print(f"\n  >>> 转录失败 <<<")
    print(f"  文件: {video_name}")
    print(f"  错误: {error_msg}")
    print(f"  [R] 重试  [S] 跳过  [Q] 退出")

    while True:
        try:
            choice = input("  > ").strip().upper()
            if choice in ('R', 'S', 'Q'):
                return {'R': 'retry', 'S': 'skip', 'Q': 'quit'}[choice]
            print("  请输入 R / S / Q")
        except (EOFError, KeyboardInterrupt):
            return 'quit'


def main():
    print("=" * 50)
    print("  增量监听转录")
    print("=" * 50)
    log(f"监看目录: {VIDEO_DIR}")
    log(f"输出目录: {OUTPUT_DIR}")
    log(f"错误日志: {ERROR_LOG}")
    log(f"轮询间隔: {POLL_INTERVAL}s")
    log(f"停止方式: Ctrl+C")
    print()

    model = None
    scan_count = 0

    try:
        while True:
            scan_count += 1
            watched = load_watched()
            new_videos = scan_new_videos(watched)

            if not new_videos:
                if scan_count % 15 == 1:  # 每15轮(30分钟)报告一次
                    log(f"扫描 #{scan_count}: 无新文件，继续等待...")
                time.sleep(POLL_INTERVAL)
                continue

            log(f"扫描 #{scan_count}: 发现 {len(new_videos)} 个新视频")

            # 懒加载
            if model is None:
                model = load_model()

            for i, video_path in enumerate(new_videos):
                video_name = os.path.basename(video_path)

                while True:
                    print(f"\n  [{i+1}/{len(new_videos)}] {video_name[:60]}")
                    t_start = time.time()

                    success, mm, ss, error_msg = transcribe_one(model, video_path, i)

                    if success:
                        elapsed = time.time() - t_start
                        duration_str = f"{mm:02d}:{ss:02d}"
                        save_watched(video_name, duration_str)
                        log(f"  完成! 时长 {duration_str}, 耗时 {elapsed:.0f}s")
                        break
                    else:
                        choice = handle_error(video_path, error_msg)
                        if choice == 'retry':
                            log(f"  重试: {video_name}")
                            continue
                        elif choice == 'skip':
                            save_watched(video_name, "SKIPPED")
                            log(f"  跳过: {video_name}")
                            break
                        else:
                            log(f"用户退出")
                            return

            # 模型保持常驻，避免反复加载/卸载（~3GB 每次加载 30s）
            log(f"本轮完成，等待 {POLL_INTERVAL}s 后下次扫描...")

    except KeyboardInterrupt:
        print()
        log("收到 Ctrl+C，停止监听")
    finally:
        model = None


if __name__ == "__main__":
    main()
