from pathlib import Path
"""批量转录视频为文本 — 姜胡说视频（FunASR 引擎）
替换原来的 Whisper，中文准确率更高（CER ~2% vs ~10%），CPU 快 5-6 倍（15x vs 2.6x 实时）。
"""
import os
os.environ["MODELSCOPE_CACHE"] = str(Path(__file__).resolve().parents[1] / ".runtime" / "models")
os.environ["HF_HOME"] = str(Path(__file__).resolve().parents[1] / ".runtime" / "huggingface")
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_paths import model_path
import subprocess
import time

# ==================== 配置 ====================
FFMPEG_BIN = str(Path(__file__).resolve().parents[1] / ".runtime" / "ffmpeg" / "bin")
VIDEO_DIR = str(Path(__file__).resolve().parents[1] / "video and audio")
AUDIO_DIR = str(Path(__file__).resolve().parents[1] / "video and audio" / "临时音频")
OUTPUT_DIR = str(Path(__file__).resolve().parents[1] / "输出" / "batch_manual")
FILE_PREFIX = "like_"  # 只处理 like_ 开头的文件（点赞视频），改 "" 处理全部
# =============================================

# 国内镜像，否则模型下载可能失败
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["PATH"] = FFMPEG_BIN + os.pathsep + os.environ["PATH"]

# ── 查找所有视频（含无扩展名的） ──
video_files = set()
for f in os.listdir(VIDEO_DIR):
    fpath = os.path.join(VIDEO_DIR, f)
    if not os.path.isfile(fpath):
        continue
    if f.endswith(('.wav', '.txt', '.json', '.log', '.md')):
        continue
    if FILE_PREFIX and not f.startswith(FILE_PREFIX):
        continue
    video_files.add(fpath)
video_files = sorted(video_files)
print(f"找到 {len(video_files)} 个视频（前缀: {FILE_PREFIX or '全部'}）\n")

if not video_files:
    print("没有找到视频文件！")
    sys.exit(1)

# ── 加载模型（只加载一次） ──
# FunASR 自动从 HuggingFace 下载模型到 ~/.cache/modelscope/
# paraformer-zh: 中文识别（非自回归，220M 参数，CER 1.95%）
# fsmn-vad: 语音活动检测，自动切分静音
# ct-punc: 标点恢复
print("加载 FunASR 模型（首次运行会自动下载 ~2GB，之后秒开）...")
from funasr import AutoModel

model = AutoModel(
    model=model_path("paraformer-zh"),
    vad_model=model_path("fsmn-vad"),
    punc_model=model_path("ct-punc"),
    device="cpu",
    disable_update=True,  # 禁止自动检查更新
)
print("模型加载完成\n")

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 逐个转录 ──
for i, video_path in enumerate(video_files, 1):
    video_name = os.path.basename(video_path)

    # 去掉已知扩展名作为输出文件名
    base_name = video_name
    for ext in (".mp4", ".MP4", ".mkv", ".avi", ".mov", ".flv"):
        if base_name.endswith(ext):
            base_name = base_name[:-len(ext)]
            break

    # 截断文件名避免超 Windows MAX_PATH（260 字符）
    short_name = base_name[:80] if len(base_name) > 80 else base_name
    audio_path = os.path.join(AUDIO_DIR, f"{i:04d}_{short_name}.wav")
    output_path = os.path.join(OUTPUT_DIR, f"{i:04d}_{short_name}.txt")

    print(f"[{i}/{len(video_files)}] {video_name[:60]}...")
    t_start = time.time()

    # 跳过已完成的
    if os.path.exists(output_path):
        with open(output_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
        print(f"  跳过（已存在）: {first_line[:50]}")
        continue

    # Step 1: 提取音频
    print(f"  提取音频...")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
         "-ar", "16000", "-ac", "1", audio_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        print(f"  ffmpeg 失败: {result.stderr[:200]}")
        continue

    # Step 2: 转录（VAD 自动切句 + 标点恢复）
    print(f"  转录中...")
    try:
        rec_result = model.generate(
            input=audio_path,
            language="zh",
            batch_size_s=300,  # 长音频分批处理
        )
    except Exception as e:
        print(f"  转录失败: {e}")
        if os.path.exists(audio_path):
            os.remove(audio_path)
        continue

    # Step 3: 写入文本
    if not rec_result or len(rec_result) == 0:
        print("  转录结果为空，跳过")
        if os.path.exists(audio_path):
            os.remove(audio_path)
        continue

    # FunASR 1.3.14 返回 list[dict]（VAD 分段），每 dict 有 key/text/timestamp
    # 注意：VAD 通常返回单段（len=1），段级时间戳不可用（全 00:00）
    # timestamp 是字符级 [[start_ms, end_ms], ...]，与 text 逐字符对齐
    # 修复（2026-08-18）：用字符级时间戳按标点切句重建 [mm:ss]，精度 ~100ms
    all_text = ""
    all_ts = []
    for seg in rec_result:
        t = seg.get("text", "") or ""
        t_ts = seg.get("timestamp", []) or []
        all_text += t
        all_ts.extend(t_ts)

    # 逐字符对齐：timestamp 条数可能略少于文本长度（尾部无语音字符），缺失的沿用前值
    char_starts = []  # 每字符的 start_ms
    for i, ch in enumerate(all_text):
        if i < len(all_ts) and all_ts[i]:
            char_starts.append(all_ts[i][0])
        elif char_starts:
            char_starts.append(char_starts[-1])
        else:
            char_starts.append(0)

    total_dur = (all_ts[-1][1] / 1000) if all_ts else 0  # 毫秒 → 秒
    mm, ss = int(total_dur // 60), int(total_dur % 60)

    def fmt(ms):
        m_, s_ = int(ms // 60000), int((ms % 60000) // 1000)
        return f"{m_:02d}:{s_:02d}"

    # 按标点切句（保留标点），每句时间戳 = 句首字符的 start_ms
    sentences = []
    cur = ""
    for i, ch in enumerate(all_text):
        cur += ch
        if ch in "。！？!?；;":
            sentences.append((cur.strip(), char_starts[i]))
            cur = ""
    if cur.strip():
        idx = len(all_text) - len(cur)
        sentences.append((cur.strip(), char_starts[idx] if idx < len(char_starts) else char_starts[-1]))

    with open(output_path, 'w', encoding='utf-8') as f:
        title = base_name[20:] if len(base_name) > 20 else base_name
        f.write(f'{title}\n\n')
        f.write(f'总时长: {mm:02d}:{ss:02d}\n')
        f.write(f'{"="*60}\n\n')

        for sent, start_ms in sentences:
            if sent:
                f.write(f'[{fmt(start_ms)}] {sent}\n\n')

    # 清理临时音频
    if os.path.exists(audio_path):
        os.remove(audio_path)

    elapsed = time.time() - t_start
    print(f"  完成! 时长 {mm:02d}:{ss:02d}, 耗时 {elapsed:.0f}s -> {output_path}\n")

print("全部转录完成!")
