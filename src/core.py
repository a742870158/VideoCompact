import bisect
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from datetime import datetime

# ================= 配置区 =================
# 打包成 exe 后，可写目录（input/output/detect）应位于 exe 所在目录；
# 只读资源（ffmpeg/ffprobe/图标）可能在 exe 同级或 PyInstaller 解包目录 _MEIPASS。
# 源码态：本文件位于 src/，运行根目录在上一级，静态资源在 assets/。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    _RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
    ASSETS_DIR = _RESOURCE_DIR
else:
    _SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = os.path.dirname(_SOURCE_DIR)
    _RESOURCE_DIR = _SOURCE_DIR
    ASSETS_DIR = os.path.join(BASE_DIR, "assets")


def _resolve_tool(name):
    # 优先在 bin/ 下查找（打包后 exe 同级的 bin 目录，或项目目录下的 bin）。
    candidates = []
    for base in (_RESOURCE_DIR, BASE_DIR):
        candidates.append(os.path.join(base, "bin", name))
    for base in (_RESOURCE_DIR, BASE_DIR):
        candidates.append(os.path.join(base, name))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(BASE_DIR, "bin", name)


FFMPEG_PATH = _resolve_tool("ffmpeg.exe")
FFPROBE_PATH = _resolve_tool("ffprobe.exe")
INPUT_DIR = os.path.join(BASE_DIR, "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
# 检测结果（sidecar）缓存目录，检测与编码经由它解耦。
DETECT_DIR = os.path.join(BASE_DIR, "detect")

# 沿用原脚本的 mpdecimate 判静止思路。
DECIMATE_PARAMS = "hi=5000:lo=3000:frac=0.02"

# 静止段处理模式：
# "drop"   = 直接从时间线上裁掉静止段（默认，最快）
# "speedup" = 保留静止画面，但压缩成倍速段（仅 OUTPUT_MODE="reencode" 支持）
STATIC_SEGMENT_MODE = "drop"

# 输出模式：
# "reencode" = 重编码（CQ + 码率上限）。体积最小，但 HEVC 重编码必然有微小画质损失。
# "lossless" = 无损裁剪：不重编码，直接流复制，只能在关键帧处切。
#              画质与源 100% 一致、速度极快，但静止段只能按 GOP 整数倍删除，
#              会有残留静止、省得少。小米摄像机 GOP 固定 6 秒，务必知晓此限制。
# "smart"    = 智能（默认）：先算无损实际能删掉的比例，够多就走无损（零损失且更小），
#              删不动就走重编码。判据见 SMART_LOSSLESS_MIN_REMOVAL。
OUTPUT_MODE = "smart"

# 智能模式判据：无损实际可删时长占比达到该阈值就用无损裁切，否则重编码。
# 无损删不动时体积≈源，此时重编码更划算；10% 是实测交叉点附近偏保守的取值。
SMART_LOSSLESS_MIN_REMOVAL = 0.10

# 仅用于静止检测，不参与最终输出。
# 先降采样到 5fps，再缩小并轻微模糊，能明显减少大批量处理的检测成本。
DETECTION_FPS = 5
DETECTION_SCALE_WIDTH = 640
DETECTION_CPU_PRE_FILTER = (
    f"fps={DETECTION_FPS},"
    f"scale={DETECTION_SCALE_WIDTH}:-1:flags=fast_bilinear,"
    "avgblur=3:3"
)
# GPU 检测：NVDEC 硬解 + GPU 缩放后回读内存，再走同一套 CPU 判静止滤镜。
# 实测比纯 CPU 解码快一倍以上，且判定结果一致。
DETECTION_GPU_PRE_FILTER = (
    f"fps={DETECTION_FPS},"
    "hwdownload,format=nv12,"
    f"scale={DETECTION_SCALE_WIDTH}:-1:flags=fast_bilinear,"
    "avgblur=3:3"
)
# 优先用 GPU 检测；若失败会自动全局回退到 CPU 检测。
DETECTION_USE_GPU = True

# 连续静止超过 3 秒才进入压缩流程。
STATIC_MIN_SECONDS = 3.0

# 静止段压缩倍数。仅在 STATIC_SEGMENT_MODE="speedup" 时生效。
STATIC_SPEED = 8.0

# 为了避免把运动边缘误切进静止段，前后各留一点保护时间。
MOTION_GUARD_SECONDS = 0.20

# NVENC 速度优先。p5 比 p7 明显更快，结合体积兜底更适合大批量任务。
ENCODE_PRESET = "p5"

# CPU 编码（libx265）preset。fast 在速度与压缩率之间较均衡。
ENCODE_CPU_PRESET = "fast"

# 编码器候选链，按顺序尝试：NVIDIA NVENC -> AMD AMF -> Intel QSV -> CPU libx265。
# 某个编码器编码失败后全局切换到下一个，后续文件直接使用可用的编码器。
ENCODER_CANDIDATES = [
    {"name": "hevc_nvenc", "label": "NVIDIA NVENC"},
    {"name": "hevc_amf", "label": "AMD AMF"},
    {"name": "hevc_qsv", "label": "Intel QSV"},
    {"name": "libx265", "label": "CPU libx265"},
]
_ENCODER_STATE = {"index": 0}
_ENCODER_LOCK = threading.Lock()

# CQ 作为画质上限。注意：监控源本身码率很低，纯 CQ 会把运动画面编得远大于源文件
# （实测 CQ20 可达源的 4 倍以上），因此必须配合下面的码率上限一起使用。
FIXED_CQ = 20

# 码率上限策略：
#   maxrate = 源视频码率 × (总时长 / 保留时长) × ENCODE_SIZE_RATIO
# 其中 (总时长 / 保留时长) 会随裁掉的静止段自动放大每分钟预算，
# 从而保证「输出总体积 ≤ 源体积 × ENCODE_SIZE_RATIO」，避免体积膨胀。
# 1.0 = 最多与原文件等大；想更激进地压缩可调小（如 0.7）。
ENCODE_SIZE_RATIO = 1.0

# 码率上限的下限，避免源码率极低时把保留的运动画面压得过惨。
ENCODE_MIN_MAXRATE = 500000

# 码率上限的上限兜底。当保留时长极短时 (总时长/保留时长) 会把 maxrate 放大到爆表
# （实测 >500Mbps~885Mbps 会让 NVENC 报 "InitializeEncoder failed: invalid param"）。
# 4K 监控重编码给 100 Mbps 已非常充裕。
ENCODE_MAX_MAXRATE = 100000000

# AMF 的 CQP 是固定 QP、不吃码率上限，体积超标时不会自动收敛。
# 兜底改用 vbr_peak 目标码率时，AMF 实测平均码率会高于目标约 8%，
# 因此目标按额度打折，保证兜底方案总体积不超过源文件。
ENCODE_CAPPED_TARGET_RATIO = 0.9

# libopus 只支持这些采样率，其余（如 44100）会直接打不开编码器，需就近吸附。
OPUS_SAMPLE_RATES = (8000, 12000, 16000, 24000, 48000)

# 编码并发槽位（NVENC 限流，建议 1-2）。
MAX_ENCODE_JOBS = 1

# ---- 按天合并 ----
# 是否在所有编码任务结束后自动按天合并（把同一天录制的视频拼成 output/daily/<日期>.mp4）。
# 由 GUI 的「自动按天合并」卡片写回，默认关闭：不勾选「按天合并」就不会自动合并。
MERGE_BY_DAY = False
# 合并时视频流复制（无损、快），音频统一转成该编码，消除不同来源音频编码不一致的问题。
DAILY_MERGE_AUDIO_CODEC = "aac"
# 「合并后删除源文件」开关：自动合并成功时删除 output 里对应的 compact_*.mp4（由 GUI 写回）。
# 仅作用于自动合并，手动「开始合并」永不删除输入文件。
DAILY_DELETE_SOURCE = False

# 从文件名提取「开始 / 结束」时间戳（14 位：YYYYMMDDHHMMSS），兼容 compact_ 前缀。
TIMESTAMP_RE = re.compile(r"^(?P<stem>.*?)_(?P<start>\d{14})_(?P<end>\d{14})$")

SHOWINFO_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
# ========================================


ENCODE_SEMAPHORE = threading.Semaphore(MAX_ENCODE_JOBS)

# GPU 检测可用性开关；任一文件用 GPU 检测失败后全局关闭，后续走 CPU。
_GPU_STATE = {"enabled": DETECTION_USE_GPU}
_GPU_LOCK = threading.Lock()


# ---------------- 日志与事件回调 ----------------
_LOG_SINK = [print]
_EVENT_SINK = [None]


def set_log_sink(sink):
    _LOG_SINK[0] = sink


def set_event_sink(sink):
    _EVENT_SINK[0] = sink


def log(message=""):
    try:
        _LOG_SINK[0](message)
    except Exception:
        pass


def emit_event(event_type, **kwargs):
    if _EVENT_SINK[0]:
        try:
            _EVENT_SINK[0](event_type, kwargs)
        except Exception:
            pass


# ---------------- 配置持久化 ----------------
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_user_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}
    return {}


def save_user_config(config_dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config_dict, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- 孤儿临时目录清理 ----------------
def clean_stale_temp_dirs(max_age_hours=2):
    """清理因异常终止或崩溃残留的 compact_video_* 临时文件夹。"""
    temp_root = tempfile.gettempdir()
    now = time.time()
    cleaned = 0
    try:
        for entry in os.listdir(temp_root):
            if entry.startswith("compact_video_") or entry.startswith("compact_video_ll_"):
                dir_path = os.path.join(temp_root, entry)
                if os.path.isdir(dir_path):
                    try:
                        mtime = os.path.getmtime(dir_path)
                        if now - mtime > max_age_hours * 3600:
                            shutil.rmtree(dir_path, ignore_errors=True)
                            cleaned += 1
                    except Exception:
                        pass
    except Exception:
        pass
    return cleaned


# ---------------- 暂停 / 继续 / 停止 ----------------
class JobCancelled(Exception):
    pass


def _suspend_process(pid):
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
        if not handle:
            return False
        try:
            ctypes.windll.ntdll.NtSuspendProcess(handle)
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _resume_process(pid):
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0800, False, pid)
        if not handle:
            return False
        try:
            ctypes.windll.ntdll.NtResumeProcess(handle)
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


class JobControl:
    def __init__(self):
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._procs = set()
        self._suspended = set()

    def reset(self):
        with self._lock:
            self._procs.clear()
            self._suspended.clear()
        self._stop_event.clear()
        self._resume_event.set()

    def is_paused(self):
        return not self._resume_event.is_set()

    def is_stopped(self):
        return self._stop_event.is_set()

    def pause(self):
        self._resume_event.clear()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            if proc.poll() is None and _suspend_process(proc.pid):
                with self._lock:
                    self._suspended.add(proc.pid)

    def resume(self):
        with self._lock:
            pids = list(self._suspended)
            self._suspended.clear()
        for pid in pids:
            _resume_process(pid)
        self._resume_event.set()

    def stop(self):
        self._stop_event.set()
        self._resume_event.set()
        with self._lock:
            procs = list(self._procs)
            pids = list(self._suspended)
            self._suspended.clear()
        for pid in pids:
            _resume_process(pid)
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass

    def register(self, proc):
        with self._lock:
            self._procs.add(proc)

    def unregister(self, proc):
        with self._lock:
            self._procs.discard(proc)

    def checkpoint(self):
        while not self._resume_event.wait(timeout=0.2):
            if self._stop_event.is_set():
                break
        if self._stop_event.is_set():
            raise JobCancelled()


CONTROL = JobControl()


def run_command(cmd, check=True):
    CONTROL.checkpoint()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    CONTROL.register(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        CONTROL.unregister(proc)

    if CONTROL.is_stopped():
        raise JobCancelled()

    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
    return result


def run_streaming(cmd, on_stdout=None, on_stderr=None, capture_stderr=True):
    # 同时读取 stdout/stderr，边跑边回调，用于上报进度、避免长时间无输出。
    CONTROL.checkpoint()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    CONTROL.register(proc)
    stderr_lines = []

    def pump(stream, handler, collect):
        try:
            for line in stream:
                if handler is not None:
                    handler(line)
                if collect:
                    stderr_lines.append(line)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, on_stdout, False), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, on_stderr, capture_stderr), daemon=True)
    t_out.start()
    t_err.start()
    try:
        proc.wait()
    finally:
        t_out.join(timeout=10)
        t_err.join(timeout=10)
        CONTROL.unregister(proc)
    if CONTROL.is_stopped():
        raise JobCancelled()
    return proc.returncode, "".join(stderr_lines)


def make_progress_handler(total_seconds, prefix, min_pct=10, min_interval=15.0):
    # 解析 ffmpeg `-progress pipe:1` 输出，按百分比/时间节流地上报进度。
    state = {
        "last_pct": 0,
        "last_t": time.time(),
        "speed": "",
        "emitted": False,
        "last_ui_pct": -1,
        "last_ui_t": 0.0,
    }

    def handler(line):
        line = line.strip()
        if line.startswith("out_time_us="):
            if total_seconds <= 0:
                return
            try:
                microseconds = int(line.split("=", 1)[1])
            except ValueError:
                return
            pct = max(0, min(int(microseconds / (total_seconds * 1_000_000) * 100), 100))
            now = time.time()

            # UI 进度条事件上报（每 1% 或 0.5s 触发一次，保持平滑）
            if pct != state["last_ui_pct"] or now - state["last_ui_t"] >= 0.5:
                state["last_ui_pct"] = pct
                state["last_ui_t"] = now
                emit_event("video_progress", percent=pct, prefix=prefix, speed=state["speed"])

            # 控制台日志节流输出
            if (
                not state["emitted"]
                or pct >= state["last_pct"] + min_pct
                or now - state["last_t"] >= min_interval
            ):
                state["last_pct"] = pct
                state["last_t"] = now
                state["emitted"] = True
                speed = f"，速度 {state['speed']}" if state["speed"] else ""
                log(f"{prefix}进度 {pct}%{speed}")
        elif line.startswith("speed="):
            state["speed"] = line.split("=", 1)[1].strip()

    return handler


def ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def ensure_detect_dir():
    os.makedirs(DETECT_DIR, exist_ok=True)


def validate_environment():
    if not os.path.isfile(FFMPEG_PATH):
        raise FileNotFoundError(f"未找到 ffmpeg: {FFMPEG_PATH}")
    if not os.path.isfile(FFPROBE_PATH):
        raise FileNotFoundError(f"未找到 ffprobe: {FFPROBE_PATH}")

    if not os.path.isdir(INPUT_DIR):
        raise FileNotFoundError(f"未找到输入目录: {INPUT_DIR}")

    test_paths = ((FFMPEG_PATH, "ffmpeg"), (FFPROBE_PATH, "ffprobe"))
    for tool_path, tool_name in test_paths:
        result = run_command([tool_path, "-version"], check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"{tool_name} 无法执行，退出码 {result.returncode}。"
            )


def list_input_videos():
    return sorted(glob.glob(os.path.join(INPUT_DIR, "*.mp4")))


def output_video_path(filename):
    return os.path.join(OUTPUT_DIR, f"compact_{filename}")


def empty_marker_path(filename):
    return os.path.join(OUTPUT_DIR, f"compact_{filename}.empty")


def detection_path(filename):
    return os.path.join(DETECT_DIR, f"{filename}.json")


def probe_media(video_path):
    cmd = [
        FFPROBE_PATH,
        "-hide_banner",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        video_path,
    ]
    result = run_command(cmd)
    return json.loads(result.stdout)


def pick_stream(info, codec_type):
    for stream in info.get("streams", []):
        if stream.get("codec_type") == codec_type:
            return stream
    return None


def build_detect_command(video_path, use_cuda):
    pre_filter = DETECTION_GPU_PRE_FILTER if use_cuda else DETECTION_CPU_PRE_FILTER
    # 用 split 分两路：主路 [all] 保留全部帧用于 -progress 反映真实输入进度；
    # 检测路 [det] 经 mpdecimate 判静止、showinfo 输出保留帧，再由 nullsink 丢弃。
    # 这样既拿到保留帧时间点，又能让编码/检测过程有正确的百分比进度。
    filter_complex = (
        f"[0:v]{pre_filter},split=2[all][det];"
        f"[det]mpdecimate={DECIMATE_PARAMS},showinfo[kept];"
        f"[kept]nullsink;[all]null[out]"
    )
    cmd = [FFMPEG_PATH, "-hide_banner"]
    if use_cuda:
        cmd.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])
    cmd.extend(
        [
            "-i",
            video_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-an",
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "null",
            "NUL",
        ]
    )
    return cmd


def run_detect_stream(cmd, duration=0.0, prefix=""):
    # 检测时 showinfo 会输出海量行，逐行流式读取，避免一次性缓冲整个 stderr 占用过多内存。
    timestamps = []

    def on_stderr(line):
        match = SHOWINFO_RE.search(line)
        if match:
            timestamps.append(float(match.group(1)))

    on_stdout = make_progress_handler(duration, prefix) if duration > 0 else None
    returncode, _ = run_streaming(
        cmd, on_stdout=on_stdout, on_stderr=on_stderr, capture_stderr=False
    )
    return returncode, timestamps


def detect_kept_frame_times(video_path, duration=0.0, prefix=None):
    if prefix is None:
        prefix = f"[{os.path.basename(video_path)}]    - "

    with _GPU_LOCK:
        use_gpu = _GPU_STATE["enabled"]

    if use_gpu:
        returncode, timestamps = run_detect_stream(
            build_detect_command(video_path, True), duration, prefix
        )
        if returncode == 0:
            return timestamps, "cuda"
        with _GPU_LOCK:
            _GPU_STATE["enabled"] = False
        log(
            f"[{os.path.basename(video_path)}]    - GPU 检测不可用，"
            "已全局切换为 CPU 检测。"
        )

    returncode, timestamps = run_detect_stream(
        build_detect_command(video_path, False), duration, prefix
    )
    if returncode != 0:
        raise RuntimeError(f"ffmpeg 静止检测失败，退出码 {returncode}")
    return timestamps, "cpu"


def build_segments(duration, kept_times):
    if not kept_times:
        return [{"kind": "normal", "start": 0.0, "end": duration}]

    segments = []
    cursor = 0.0
    previous_kept = kept_times[0]

    if previous_kept > STATIC_MIN_SECONDS:
        static_end = max(previous_kept - MOTION_GUARD_SECONDS, 0.0)
        if static_end > STATIC_MIN_SECONDS:
            segments.append({"kind": "static", "start": 0.0, "end": static_end})
            cursor = static_end

    for current_kept in kept_times[1:]:
        gap = current_kept - previous_kept
        static_start = previous_kept + MOTION_GUARD_SECONDS
        static_end = current_kept - MOTION_GUARD_SECONDS
        static_duration = static_end - static_start

        if gap >= STATIC_MIN_SECONDS and static_duration >= STATIC_MIN_SECONDS:
            if static_start > cursor:
                segments.append({"kind": "normal", "start": cursor, "end": static_start})
            segments.append({"kind": "static", "start": static_start, "end": static_end})
            cursor = static_end

        previous_kept = current_kept

    tail_static_start = previous_kept + MOTION_GUARD_SECONDS
    tail_static_duration = duration - tail_static_start
    if tail_static_duration >= STATIC_MIN_SECONDS:
        if tail_static_start > cursor:
            segments.append({"kind": "normal", "start": cursor, "end": tail_static_start})
        segments.append({"kind": "static", "start": tail_static_start, "end": duration})
        cursor = duration

    if cursor < duration:
        segments.append({"kind": "normal", "start": cursor, "end": duration})

    cleaned = []
    for segment in segments:
        start = max(0.0, segment["start"])
        end = min(duration, segment["end"])
        if end - start >= 0.05:
            cleaned.append({"kind": segment["kind"], "start": start, "end": end})

    if not cleaned:
        cleaned.append({"kind": "normal", "start": 0.0, "end": duration})

    merged = [cleaned[0]]
    for segment in cleaned[1:]:
        last = merged[-1]
        same_kind = segment["kind"] == last["kind"]
        contiguous = abs(segment["start"] - last["end"]) < 0.02
        if same_kind and contiguous:
            last["end"] = segment["end"]
        else:
            merged.append(segment)
    return merged


def format_seconds(value):
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def format_hms(total_seconds):
    hours, remainder = divmod(int(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} 小时 {minutes} 分 {seconds} 秒"


def format_eta(seconds):
    if seconds <= 0:
        return "0 分 0 秒"
    minutes, remain_seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours} 小时 {minutes} 分 {remain_seconds} 秒"
    return f"{minutes} 分 {remain_seconds} 秒"


def get_effective_segments(segments):
    if STATIC_SEGMENT_MODE != "drop":
        return list(segments)

    kept_segments = [segment for segment in segments if segment["kind"] == "normal"]
    if kept_segments:
        return kept_segments

    # 整段都被判定为静止时，保留极短占位片段，避免输出空文件。
    first = segments[0]
    placeholder_end = min(first["start"] + 0.10, first["end"])
    return [{"kind": "normal", "start": first["start"], "end": placeholder_end}]


def is_entire_video_static(segments, duration):
    if duration <= 0:
        return False
    static_total = sum(
        segment["end"] - segment["start"]
        for segment in segments
        if segment["kind"] == "static"
    )
    if static_total < STATIC_MIN_SECONDS:
        return False
    # build_segments 会在片头/片尾为运动边缘保留一小段保护时间，
    # 因此整段静止时通常还残留一段 <= MOTION_GUARD_SECONDS 的极短 normal 片段。
    normal_total = sum(
        segment["end"] - segment["start"]
        for segment in segments
        if segment["kind"] == "normal"
    )
    return normal_total <= MOTION_GUARD_SECONDS + 0.05


def build_filter_complex(segments, has_audio, audio_stream):
    segments = get_effective_segments(segments)
    filter_parts = []
    concat_inputs = []
    sample_rate = int(audio_stream.get("sample_rate", 48000)) if audio_stream else 48000
    channel_layout = audio_stream.get("channel_layout") if audio_stream else None
    if not channel_layout:
        channels = int(audio_stream.get("channels", 1)) if audio_stream else 1
        channel_layout = "mono" if channels == 1 else "stereo"

    for index, segment in enumerate(segments):
        start = format_seconds(segment["start"])
        end = format_seconds(segment["end"])
        duration = segment["end"] - segment["start"]
        output_duration = duration if segment["kind"] == "normal" else (duration / STATIC_SPEED)

        if segment["kind"] == "normal":
            video_chain = (
                f"[0:v]trim=start={start}:end={end},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
        else:
            video_chain = (
                f"[0:v]trim=start={start}:end={end},"
                f"setpts=(PTS-STARTPTS)/{STATIC_SPEED}[v{index}]"
            )
        filter_parts.append(video_chain)
        concat_inputs.append(f"[v{index}]")

        if not has_audio:
            continue

        if segment["kind"] == "normal":
            audio_chain = (
                f"[0:a]atrim=start={start}:end={end},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            silence_duration = format_seconds(output_duration)
            audio_chain = (
                f"anullsrc=r={sample_rate}:cl={channel_layout},"
                f"atrim=duration={silence_duration},"
                f"asetpts=N/SR/TB[a{index}]"
            )
        filter_parts.append(audio_chain)
        concat_inputs.append(f"[a{index}]")

    if has_audio:
        filter_parts.append(
            "".join(concat_inputs)
            + f"concat=n={len(segments)}:v=1:a=1[vcat][acat]"
        )
        filter_parts.append("[vcat]format=yuv420p[vout]")
        return ";".join(filter_parts), "vout", "acat"

    filter_parts.append(
        "".join(concat_inputs) + f"concat=n={len(segments)}:v=1:a=0[vcat]"
    )
    filter_parts.append("[vcat]format=yuv420p[vout]")
    return ";".join(filter_parts), "vout", None


def build_video_encoder_attempts(encoder, cq_value, maxrate):
    """返回该编码器的参数候选，按「画质优先 -> 码率上限兜底」排序。

    - NVENC / libx265 原生支持「画质 + 码率上限」，一次即可。
    - AMF 的 CQP 是固定 QP、不吃码率上限，体积超标时不会自动收敛，
      因此追加一个 vbr_peak 上限方案兜底，避免直接回退为复制源文件。
    """
    if encoder == "hevc_nvenc":
        args = [
            "-c:v",
            encoder,
            "-preset",
            ENCODE_PRESET,
            "-rc",
            "vbr",
            "-cq",
            str(cq_value),
        ]
        # 叠加坡率上限，防止纯 CQ 在小码率监控源上体积膨胀。
        if maxrate > 0:
            args.extend(["-maxrate", str(maxrate), "-bufsize", str(maxrate * 2)])
        return [("CQ", args)]

    if encoder == "hevc_amf":
        attempts = [
            (
                "CQP",
                [
                    "-c:v",
                    encoder,
                    "-quality",
                    "speed",
                    "-rc",
                    "cqp",
                    "-qp_i",
                    str(cq_value),
                    "-qp_p",
                    str(cq_value),
                    "-qp_b",
                    str(cq_value),
                ],
            )
        ]
        if maxrate > 0:
            target = max(1, int(maxrate * ENCODE_CAPPED_TARGET_RATIO))
            attempts.append(
                (
                    "VBR 上限",
                    [
                        "-c:v",
                        encoder,
                        "-quality",
                        "speed",
                        "-rc",
                        "vbr_peak",
                        "-b:v",
                        str(target),
                        "-maxrate",
                        str(maxrate),
                        "-bufsize",
                        str(maxrate * 2),
                    ],
                )
            )
        return attempts

    if encoder == "hevc_qsv":
        args = [
            "-c:v",
            encoder,
            "-preset",
            "fast",
            "-global_quality",
            str(cq_value),
        ]
        if maxrate > 0:
            args.extend(["-maxrate", str(maxrate), "-bufsize", str(maxrate * 2)])
        return [("ICQ", args)]

    # libx265：CRF 定画质，VBV 限制峰值码率（单位 kbps）。
    args = [
        "-c:v",
        encoder,
        "-preset",
        ENCODE_CPU_PRESET,
        "-crf",
        str(cq_value),
    ]
    if maxrate > 0:
        vbv_maxrate = max(1, int(maxrate / 1000))
        args.extend(
            [
                "-x265-params",
                f"vbv-maxrate={vbv_maxrate}:vbv-bufsize={vbv_maxrate * 2}",
            ]
        )
    return [("CRF", args)]


def _snap_opus_sample_rate(sample_rate):
    """把采样率吸附到 libopus 支持的值（44100 -> 48000），否则编码器打不开。"""
    try:
        rate = int(sample_rate)
    except (TypeError, ValueError):
        return 48000
    if rate in OPUS_SAMPLE_RATES:
        return rate
    return min(OPUS_SAMPLE_RATES, key=lambda candidate: abs(candidate - rate))


def build_ffmpeg_command(video_path, output_path, audio_info, segments, video_encoder_args):
    has_audio = audio_info is not None

    filter_complex, video_label, audio_label = build_filter_complex(
        segments, has_audio, audio_info
    )

    cmd = [
        FFMPEG_PATH,
        "-hide_banner",
        "-y",
        "-i",
        video_path,
        "-filter_complex",
        filter_complex,
        "-map",
        f"[{video_label}]",
    ]

    if audio_label:
        cmd.extend(["-map", f"[{audio_label}]"])

    cmd.extend(["-map_metadata", "0"])
    cmd.extend(video_encoder_args)

    cmd.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "main",
            "-tag:v",
            "hvc1",
            "-movflags",
            "+faststart",
        ]
    )

    if has_audio:
        sample_rate = _snap_opus_sample_rate(audio_info.get("sample_rate", "48000"))
        channels = audio_info.get("channels", 1)
        audio_bitrate = audio_info.get("bit_rate") or 64000
        cmd.extend(
            [
                "-c:a",
                "libopus",
                "-b:a",
                str(audio_bitrate),
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
            ]
        )
    else:
        cmd.append("-an")

    # 保留源时间基，避免强行补帧到固定帧率导致体积和帧数膨胀。
    # -progress 让编码过程可实时上报进度。
    cmd.extend(["-fps_mode", "vfr", "-progress", "pipe:1", "-nostats", output_path])
    return cmd


def summarize_segments(segments):
    static_segments = [item for item in segments if item["kind"] == "static"]
    normal_segments = [item for item in segments if item["kind"] == "normal"]
    static_input_total = sum(item["end"] - item["start"] for item in static_segments)
    if STATIC_SEGMENT_MODE == "drop":
        static_output_total = 0.0
    else:
        static_output_total = sum((item["end"] - item["start"]) / STATIC_SPEED for item in static_segments)
    return {
        "normal_count": len(normal_segments),
        "static_count": len(static_segments),
        "static_input_total": static_input_total,
        "static_output_total": static_output_total,
    }


def extract_audio_info(media_info):
    audio_stream = pick_stream(media_info, "audio")
    if audio_stream is None:
        return None
    return {
        "sample_rate": audio_stream.get("sample_rate", "48000"),
        "channels": audio_stream.get("channels", 1),
        "channel_layout": audio_stream.get("channel_layout"),
        "bit_rate": audio_stream.get("bit_rate") or 64000,
    }


def extract_source_video_bitrate(media_info):
    video_stream = pick_stream(media_info, "video")
    if video_stream and video_stream.get("bit_rate"):
        return int(video_stream["bit_rate"])
    format_info = media_info.get("format", {})
    if format_info.get("bit_rate"):
        return int(format_info["bit_rate"])
    return 0


def compute_encode_maxrate(source_video_bitrate, duration, kept_duration):
    # maxrate = 源码率 × (总时长 / 保留时长) × ENCODE_SIZE_RATIO
    # 保留时长越短，允许的瞬时码率越高，从而保证输出总体积不超过源的 ENCODE_SIZE_RATIO 倍。
    if source_video_bitrate <= 0:
        return 0
    if kept_duration <= 0:
        kept_duration = duration
    if kept_duration <= 0:
        return 0
    keep_scale = duration / kept_duration
    budget = source_video_bitrate * keep_scale * ENCODE_SIZE_RATIO
    budget = min(budget, ENCODE_MAX_MAXRATE)
    return int(max(budget, ENCODE_MIN_MAXRATE))


def write_json_atomic(path, data):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def load_detection(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def detection_is_fresh(video_path):
    sidecar = detection_path(os.path.basename(video_path))
    if not os.path.isfile(sidecar):
        return False
    try:
        record = load_detection(sidecar)
    except (OSError, ValueError):
        return False
    return record.get("source_size") == os.path.getsize(video_path)


def render_with_encoder(
    video_path, output_path, temp_output, encoder, audio_info, segments,
    maxrate, source_size, on_stdout, logger,
):
    """用单个编码器渲染，按候选参数依次尝试。

    返回 ("encoded", None) 成功并已写出 output；
    返回 ("oversize", None) 编码成功但体积仍大于源文件；
    返回 ("failed", 原因) 编码器不可用。
    """
    rate_note = f"，码率上限 {maxrate / 1000:.0f} kbps" if maxrate > 0 else ""
    attempts = build_video_encoder_attempts(encoder["name"], FIXED_CQ, maxrate)

    for attempt_index, (label, video_args) in enumerate(attempts):
        cmd = build_ffmpeg_command(
            video_path, temp_output, audio_info, segments, video_args
        )
        logger(
            f"    - 使用 {encoder['label']} {label} (CQ={FIXED_CQ}{rate_note}) 编码中..."
        )
        returncode, stderr_text = run_streaming(cmd, on_stdout=on_stdout)
        if returncode != 0:
            logger(stderr_text)
            return "failed", f"退出码 {returncode}"

        output_size = os.path.getsize(temp_output)
        logger(
            f"    - {label} 输出 {output_size / 1024 / 1024:.2f} MB，"
            f"原始大小 {source_size / 1024 / 1024:.2f} MB"
        )
        if output_size <= source_size:
            shutil.move(temp_output, output_path)
            return "encoded", None
        if attempt_index + 1 < len(attempts):
            logger("    - 输出大于原文件，改用码率上限兜底重编。")

    return "oversize", None


def render_with_size_guard(
    video_path, output_path, audio_info, segments, maxrate, logger,
    output_duration=0.0, progress_prefix="",
):
    source_size = os.path.getsize(video_path)
    temp_dir = tempfile.mkdtemp(prefix="compact_video_")

    try:
        temp_output = os.path.join(temp_dir, f"render_cq_{FIXED_CQ}.mp4")
        on_stdout = make_progress_handler(output_duration, progress_prefix)

        while True:
            with _ENCODER_LOCK:
                encoder_index = _ENCODER_STATE["index"]
            encoder = ENCODER_CANDIDATES[encoder_index]

            status, detail = render_with_encoder(
                video_path, output_path, temp_output, encoder, audio_info,
                segments, maxrate, source_size, on_stdout, logger,
            )
            if status == "encoded":
                return "encoded"
            if status == "oversize":
                shutil.copy2(video_path, output_path)
                logger("    - 输出大于原文件，已回退为直接复制源文件到 output。")
                return "copied_fallback"

            # 编码器不可用：全局切换为下一个候选编码器。
            with _ENCODER_LOCK:
                can_fallback = (
                    _ENCODER_STATE["index"] == encoder_index
                    and encoder_index + 1 < len(ENCODER_CANDIDATES)
                )
                if can_fallback:
                    _ENCODER_STATE["index"] = encoder_index + 1
            if not can_fallback:
                raise RuntimeError(f"ffmpeg 编码失败，{detail}")
            logger(
                f"    - {encoder['label']} 编码不可用，"
                f"回退到 {ENCODER_CANDIDATES[encoder_index + 1]['label']}。"
            )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def get_keyframe_times(video_path):
    cmd = [
        FFPROBE_PATH,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-skip_frame",
        "nokey",
        "-show_entries",
        "frame=pts_time",
        "-of",
        "csv=p=0",
        video_path,
    ]
    result = run_command(cmd, check=False)
    times = []
    for token in result.stdout.split():
        try:
            times.append(float(token))
        except ValueError:
            continue
    times.sort()
    return times


def build_lossless_kept_ranges(segments, keyframes, duration):
    # 无损裁剪只能在关键帧处开启新片段。删除 [ss, se] 时：
    #   - 删除起点 ss 可以任意（前一片段结束于 ss 无妨）
    #   - 删除终点 b 必须是关键帧（后一片段从 b 开始解码）
    # 因此每个静止段最多删到「不超过 se 的最后一个关键帧」，残留 [b, se] 无法无损删除。
    removals = []
    for segment in segments:
        if segment["kind"] != "static":
            continue
        ss, se = segment["start"], segment["end"]
        index = bisect.bisect_right(keyframes, se) - 1
        if index < 0:
            continue
        b = keyframes[index]
        if b > ss + 0.001:
            removals.append((ss, b))

    kept = []
    cursor = 0.0
    for start, end in removals:
        if start > cursor + 0.001:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration - 0.001:
        kept.append((cursor, duration))

    # 清洗：过短片段丢弃（避免空片段导致拼接失败）
    cleaned = [(s, e) for s, e in kept if e - s >= 0.05]
    return cleaned


def plan_lossless(segments, keyframes, duration):
    """按关键帧对齐算出无损可保留区间，以及实际可删时长占比。

    返回 (kept_ranges, kept_duration, removable_ratio)。
    removed = duration - kept_duration，反映无损真正能省下的比例。
    """
    kept_ranges = build_lossless_kept_ranges(segments, keyframes, duration)
    kept_duration = sum(end - start for start, end in kept_ranges)
    removable = (duration - kept_duration) / duration if duration > 0 else 0.0
    return kept_ranges, kept_duration, removable


def build_lossless_part_command(video_path, part_path, start, end):
    duration = end - start
    cmd = [FFMPEG_PATH, "-hide_banner", "-y"]
    if start > 0.001:
        cmd.extend(["-ss", format_seconds(start)])
    cmd.extend(
        [
            "-t",
            format_seconds(duration),
            "-i",
            video_path,
            "-map",
            "0",
            "-c",
            "copy",
            part_path,
        ]
    )
    return cmd


def render_lossless(video_path, output_path, kept_ranges, logger):
    source_size = os.path.getsize(video_path)
    temp_dir = tempfile.mkdtemp(prefix="compact_video_ll_")

    try:
        part_paths = []
        for index, (start, end) in enumerate(kept_ranges):
            part_path = os.path.join(temp_dir, f"part_{index:05d}.mp4")
            result = run_command(
                build_lossless_part_command(video_path, part_path, start, end),
                check=False,
            )
            if result.returncode != 0 or not os.path.isfile(part_path):
                logger(result.stderr)
                raise RuntimeError(f"无损切割失败，退出码 {result.returncode}")
            part_paths.append(part_path)

        list_path = os.path.join(temp_dir, "concat_list.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            for part_path in part_paths:
                safe_path = os.path.abspath(part_path).replace("\\", "/")
                handle.write(f"file '{safe_path}'\n")

        temp_output = os.path.join(temp_dir, "output.mp4")
        concat_cmd = [
            FFMPEG_PATH,
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            temp_output,
        ]
        result = run_command(concat_cmd, check=False)
        if result.returncode != 0:
            logger(result.stderr)
            raise RuntimeError(f"无损拼接失败，退出码 {result.returncode}")

        output_size = os.path.getsize(temp_output)
        logger(
            f"    - 无损输出大小 {output_size / 1024 / 1024:.2f} MB，"
            f"原始大小 {source_size / 1024 / 1024:.2f} MB"
        )

        if output_size <= source_size:
            shutil.move(temp_output, output_path)
            return "encoded"
        else:
            shutil.copy2(video_path, output_path)
            logger("    - 输出大于原文件，已回退为直接复制源文件到 output。")
            return "copied_fallback"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ================= 阶段一：静止检测 =================
def detect_one_video(video_path):
    started_at = time.perf_counter()
    filename = os.path.basename(video_path)

    def vlog(message):
        log(f"[{filename}] {message}")

    result = "detected"
    try:
        CONTROL.checkpoint()
        media_info = probe_media(video_path)
        duration = float(media_info["format"]["duration"])

        kept_times, method = detect_kept_frame_times(
            video_path, duration, prefix=f"[{filename}]    - 检测"
        )
        segments = build_segments(duration, kept_times)
        summary = summarize_segments(segments)
        all_static = is_entire_video_static(segments, duration)

        record = {
            "source": filename,
            "source_size": os.path.getsize(video_path),
            "duration": duration,
            "detected_at": datetime.now().isoformat(timespec="seconds"),
            "method": method,
            "all_static": all_static,
            "summary": summary,
            "segments": segments,
            "audio": extract_audio_info(media_info),
            "source_video_bitrate": extract_source_video_bitrate(media_info),
        }
        write_json_atomic(detection_path(filename), record)

        vlog(
            "    - 检测结果: "
            f"{summary['static_count']} 段静止区间, "
            f"合计 {summary['static_input_total']:.2f}s -> {summary['static_output_total']:.2f}s "
            f"(解码方式: {'GPU' if method == 'cuda' else 'CPU'})"
        )

        if all_static:
            with open(empty_marker_path(filename), "w", encoding="utf-8") as handle:
                handle.write("整个视频均为静止内容，已跳过。\n")
            vlog("    - 整个视频均为静止，已写出 .empty 标记。")
            result = "all_static"
    except JobCancelled:
        return "cancelled"
    except Exception as exc:
        vlog(f"    - 检测失败: {exc}")
        return "failed"
    finally:
        elapsed_seconds = int(time.perf_counter() - started_at)
        minutes, seconds = divmod(elapsed_seconds, 60)
        vlog(f"    - 当前视频检测耗时: {minutes} 分 {seconds} 秒")
    return result


# ================= 阶段二：编码输出 =================
def encode_one_video(video_path):
    started_at = time.perf_counter()
    filename = os.path.basename(video_path)
    output_path = output_video_path(filename)

    def vlog(message):
        log(f"[{filename}] {message}")

    log(f"\n[{filename}] >>> 正在编码")
    try:
        CONTROL.checkpoint()
        record = load_detection(detection_path(filename))
        segments = record["segments"]
        # 用当前配置重新汇总，避免检测后修改 STATIC_SEGMENT_MODE 导致日志不一致。
        summary = summarize_segments(segments)

        vlog(
            "    - 使用检测结果: "
            f"{summary['static_count']} 段静止区间, "
            f"合计 {summary['static_input_total']:.2f}s -> {summary['static_output_total']:.2f}s"
        )
        mode_labels = {
            "reencode": "重编码（CQ + 码率上限）",
            "lossless": "无损裁剪（流复制，只能在关键帧处切）",
            "smart": "智能（按无损可删比例自动选择 无损 / 重编码）",
        }
        vlog("    - 输出模式: " + mode_labels.get(OUTPUT_MODE, OUTPUT_MODE))

        if record.get("all_static"):
            with open(empty_marker_path(filename), "w", encoding="utf-8") as handle:
                handle.write("整个视频均为静止内容，已跳过。\n")
            vlog("    - 整个视频均为静止，已跳过，不输出视频。")
            return "all_static"

        if summary["static_count"] == 0:
            shutil.copy2(video_path, output_path)
            vlog("    - 未发现超过 3 秒的静止区间，已直接复制源文件到 output。")
            return "copied_no_static"

        duration = record.get("duration", 0.0)
        use_lossless = OUTPUT_MODE == "lossless"
        if OUTPUT_MODE in ("lossless", "smart"):
            keyframes = get_keyframe_times(video_path)
            kept_ranges, kept_duration, removable = plan_lossless(segments, keyframes, duration)
            vlog(
                f"    - 无损可删静止约 {duration - kept_duration:.2f}s"
                f"（占 {removable * 100:.1f}%，GOP 对齐，剩余静止保留在原速）"
            )
            if OUTPUT_MODE == "smart":
                use_lossless = removable >= SMART_LOSSLESS_MIN_REMOVAL
                vlog(
                    f"    - 智能判定: 无损可删 {removable * 100:.1f}% "
                    + ("≥" if use_lossless else "<")
                    + f" 阈值 {SMART_LOSSLESS_MIN_REMOVAL * 100:.0f}%，采用"
                    + ("无损裁剪。" if use_lossless else "重编码。")
                )

        if use_lossless:
            if kept_duration <= 0:
                shutil.copy2(video_path, output_path)
                vlog("    - 无法无损删除任何区间，已直接复制源文件到 output。")
                return "copied_no_static"
            with ENCODE_SEMAPHORE:
                result = render_lossless(video_path, output_path, kept_ranges, vlog)
            vlog(f"    - 完成输出: {os.path.basename(output_path)}")
            return "lossless" if result == "encoded" else result

        effective_segments = get_effective_segments(segments)
        kept_duration = sum(s["end"] - s["start"] for s in effective_segments)
        output_duration = sum(
            (s["end"] - s["start"]) if s["kind"] == "normal"
            else (s["end"] - s["start"]) / STATIC_SPEED
            for s in effective_segments
        )
        maxrate = compute_encode_maxrate(
            record.get("source_video_bitrate", 0), duration, kept_duration
        )

        with ENCODE_SEMAPHORE:
            result = render_with_size_guard(
                video_path, output_path, record.get("audio"), segments, maxrate, vlog,
                output_duration=output_duration,
                progress_prefix=f"[{filename}]    - 编码",
            )
        vlog(f"    - 完成输出: {os.path.basename(output_path)}")
        return "reencoded" if result == "encoded" else result
    except JobCancelled:
        return "cancelled"
    finally:
        elapsed_seconds = int(time.perf_counter() - started_at)
        minutes, seconds = divmod(elapsed_seconds, 60)
        vlog(f"    - 当前视频耗时: {minutes} 分 {seconds} 秒")


# ================= 任务模式：单个视频「检测 -> 编码」 =================
def process_one_video(video_path):
    """一个完整任务：先检测（命中缓存则跳过），检测成功后立即编码。"""
    filename = os.path.basename(video_path)
    if not detection_is_fresh(video_path):
        detect_result = detect_one_video(video_path)
        if detect_result in ("cancelled", "failed"):
            return detect_result
    return encode_one_video(video_path)


def run_pipeline_tasks():
    started_at = time.perf_counter()
    clean_stale_temp_dirs()
    ensure_detect_dir()
    ensure_output_dir()
    video_files = list_input_videos()

    if not video_files:
        log(f"错误: 未在 {INPUT_DIR} 中找到 mp4 文件。")
        emit_event("task_progress", completed=0, total=0)
        return

    pending_video_files = []
    skipped_output_count = 0
    for video_path in video_files:
        filename = os.path.basename(video_path)
        if os.path.exists(output_video_path(filename)) or os.path.exists(empty_marker_path(filename)):
            skipped_output_count += 1
        else:
            pending_video_files.append(video_path)

    log(
        f"[任务] 找到 {len(video_files)} 个视频，"
        f"{len(pending_video_files)} 个待处理，{skipped_output_count} 个已有输出跳过。"
    )
    emit_event("task_progress", completed=0, total=len(pending_video_files))
    if not pending_video_files:
        log("[任务] 无需处理。")
        return

    log("[任务] 流水线处理：视频编码与下一视频静止检测并行交叠。")

    stats = {"lossless": 0, "reencoded": 0, "copied_fallback": 0, "copied_no_static": 0, "all_static": 0}
    failed_videos = []
    completed_count = 0
    total_source_bytes = 0
    total_output_bytes = 0

    prefetch_job = {"path": None, "thread": None, "result": [None], "error": [None]}

    def _start_prefetch(target_path):
        res_box = [None]
        err_box = [None]

        def _worker():
            try:
                res_box[0] = detect_one_video(target_path)
            except JobCancelled:
                res_box[0] = "cancelled"
            except Exception as exc:
                err_box[0] = exc
                res_box[0] = "failed"

        t = threading.Thread(target=_worker, name=f"PrefetchDetect-{os.path.basename(target_path)}", daemon=True)
        t.start()
        prefetch_job["path"] = target_path
        prefetch_job["thread"] = t
        prefetch_job["result"] = res_box
        prefetch_job["error"] = err_box

    try:
        for idx, video_path in enumerate(pending_video_files):
            filename = os.path.basename(video_path)
            try:
                CONTROL.checkpoint()
            except JobCancelled:
                log("[任务] 已停止。")
                return

            # --- 步骤 1：获取当前视频的静止检测结果 ---
            detect_result = None
            if prefetch_job["path"] == video_path and prefetch_job["thread"] is not None:
                prefetch_job["thread"].join()
                if prefetch_job["error"][0]:
                    failed_videos.append((filename, str(prefetch_job["error"][0])))
                    log(f"[{filename}]    - 预检测失败: {prefetch_job['error'][0]}")
                    completed_count += 1
                    continue
                detect_result = prefetch_job["result"][0]
                prefetch_job["path"] = None
                prefetch_job["thread"] = None
            else:
                if not detection_is_fresh(video_path):
                    detect_result = detect_one_video(video_path)
                else:
                    detect_result = "detected"

            if detect_result == "cancelled":
                log("[任务] 已停止。")
                return
            if detect_result == "failed":
                failed_videos.append((filename, "检测失败"))
                completed_count += 1
                emit_event("task_progress", completed=completed_count, total=len(pending_video_files))
                continue
            if detect_result == "all_static":
                stats["all_static"] += 1
                completed_count += 1
                emit_event("task_progress", completed=completed_count, total=len(pending_video_files))
                continue

            # --- 步骤 2：在当前视频编码期间，提前在后台检测下一个需要检测的视频 ---
            next_target = None
            for probe_idx in range(idx + 1, len(pending_video_files)):
                candidate = pending_video_files[probe_idx]
                if not detection_is_fresh(candidate):
                    next_target = candidate
                    break

            if next_target and (prefetch_job["thread"] is None or not prefetch_job["thread"].is_alive()):
                _start_prefetch(next_target)

            # --- 步骤 3：主线程执行当前视频的编码 ---
            try:
                result = encode_one_video(video_path)
                if result == "cancelled":
                    log("[任务] 已停止。")
                    return
                if result in stats:
                    stats[result] += 1
                    try:
                        src_sz = os.path.getsize(video_path)
                        total_source_bytes += src_sz
                        out_p = output_video_path(filename)
                        if os.path.isfile(out_p):
                            total_output_bytes += os.path.getsize(out_p)
                        else:
                            total_output_bytes += src_sz
                    except Exception:
                        pass
                elif result == "failed":
                    failed_videos.append((filename, "编码失败"))
            except JobCancelled:
                log("[任务] 已停止。")
                return
            except Exception as exc:
                failed_videos.append((filename, str(exc)))
                log(f"[{filename}]    - 失败: {exc}")

            completed_count += 1
            emit_event("task_progress", completed=completed_count, total=len(pending_video_files))
            elapsed = time.perf_counter() - started_at
            avg_per_video = elapsed / completed_count if completed_count else 0
            remaining = len(pending_video_files) - completed_count
            log(
                f"[任务进度] {completed_count}/{len(pending_video_files)} 已完成, "
                f"ETA 约 {format_eta(avg_per_video * remaining)}"
            )
    finally:
        if prefetch_job["thread"] is not None and prefetch_job["thread"].is_alive():
            prefetch_job["thread"].join(timeout=2)

    total_elapsed_seconds = int(time.perf_counter() - started_at)
    success_count = sum(stats.values())
    saved_bytes = max(0, total_source_bytes - total_output_bytes)
    saved_ratio = (saved_bytes / total_source_bytes * 100) if total_source_bytes > 0 else 0.0

    log("\n" + "=" * 36)
    log("[全部任务完成]")
    log(f"处理完成: {success_count}/{len(pending_video_files)}")
    log(f"总视频数: {len(video_files)}")
    log(f"已有输出跳过数: {skipped_output_count}")
    log(f"无损裁剪输出数: {stats['lossless']}")
    log(f"重编码输出数: {stats['reencoded']}")
    log(f"直接复制源文件数(合计): {stats['copied_no_static'] + stats['copied_fallback']}")
    log(f"  - 无静止区间直接复制: {stats['copied_no_static']}")
    log(f"  - 编码变大回退复制: {stats['copied_fallback']}")
    log(f"全静止跳过数: {stats['all_static']}")
    if total_source_bytes > 0:
        log(f"源文件总体积: {total_source_bytes / (1024 * 1024):.2f} MB")
        log(f"输出文件总体积: {total_output_bytes / (1024 * 1024):.2f} MB")
        log(f"节省空间: {saved_bytes / (1024 * 1024):.2f} MB (节省率 {saved_ratio:.1f}%)")
    log(f"总耗时(时分秒): {format_hms(total_elapsed_seconds)}")
    log(f"输出目录: {OUTPUT_DIR}")
    log(f"检测缓存目录: {DETECT_DIR}")
    if failed_videos:
        log("失败清单:")
        for failed_name, failed_reason in failed_videos:
            log(f"  - {failed_name}: {failed_reason}")
    else:
        log("失败清单: 无")
    log("=" * 36)

    emit_event(
        "task_summary",
        total_source_bytes=total_source_bytes,
        total_output_bytes=total_output_bytes,
        saved_bytes=saved_bytes,
        saved_ratio=saved_ratio,
        success_count=success_count,
        total_count=len(pending_video_files),
    )


# ================= 阶段三：按天合并 =================
def daily_output_dir():
    return os.path.join(OUTPUT_DIR, "daily")


def parse_filename_times(filename):
    """从文件名解析「开始 / 结束」时间戳，兼容 compact_ 前缀；解析失败返回 None。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    match = TIMESTAMP_RE.match(stem)
    if not match:
        return None
    try:
        start = datetime.strptime(match.group("start"), "%Y%m%d%H%M%S")
        end = datetime.strptime(match.group("end"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return start, end


def group_videos_by_day(video_paths):
    """按开始时间所在的天分组。

    返回 ({date: [(start, name, path), ...]}, 无法解析的路径列表)；
    跨午夜的文件整体归到开始那天，组内按开始时间升序（同刻再按文件名）。
    """
    groups = {}
    unparsed = []
    for path in video_paths:
        times = parse_filename_times(path)
        if times is None:
            unparsed.append(path)
            continue
        start = times[0]
        groups.setdefault(start.date(), []).append((start, os.path.basename(path), path))
    for day in groups:
        groups[day].sort(key=lambda item: (item[0], item[1]))
    return groups, unparsed


def _reencode_concat_filter(inputs, want_audio, out_path, logger, expected_duration, progress_prefix):
    """兜底：用 concat 滤镜逐段独立解码后整体重编码（复用编码器候选链），成功返回 True。

    注意：这里不能用 concat demuxer + 重编码 —— demuxer 在输入编码不一致时
    会静默丢弃后续片段的音频；concat 滤镜逐段独立解码则不受影响。
    """
    count = len(inputs)
    if want_audio:
        inputs_labels = "".join(f"[{index}:v][{index}:a]" for index in range(count))
        filter_complex = f"{inputs_labels}concat=n={count}:v=1:a=1[v][a]"
        map_args = ["-map", "[v]", "-map", "[a]"]
    else:
        inputs_labels = "".join(f"[{index}:v]" for index in range(count))
        filter_complex = f"{inputs_labels}concat=n={count}:v=1:a=0[v]"
        map_args = ["-map", "[v]"]

    on_stdout = (
        make_progress_handler(expected_duration, progress_prefix)
        if expected_duration > 0
        else None
    )
    while True:
        with _ENCODER_LOCK:
            encoder_index = _ENCODER_STATE["index"]
        encoder = ENCODER_CANDIDATES[encoder_index]

        succeeded = False
        for label, video_args in build_video_encoder_attempts(encoder["name"], FIXED_CQ, 0):
            cmd = [FFMPEG_PATH, "-hide_banner", "-y"]
            for path in inputs:
                cmd.extend(["-i", path])
            cmd.extend(["-filter_complex", filter_complex])
            cmd.extend(map_args)
            cmd.extend(video_args)
            cmd.extend(["-pix_fmt", "yuv420p", "-profile:v", "main", "-tag:v", "hvc1"])
            if want_audio:
                cmd.extend(["-c:a", DAILY_MERGE_AUDIO_CODEC])
            else:
                cmd.append("-an")
            cmd.extend(["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out_path])
            logger(f"    - 使用 {encoder['label']} {label} (CQ={FIXED_CQ}) 整体重编码合并中...")
            returncode, stderr_text = run_streaming(cmd, on_stdout=on_stdout)
            if (
                returncode == 0
                and os.path.isfile(out_path)
                and os.path.getsize(out_path) > 0
            ):
                succeeded = True
                break
            logger(stderr_text)

        if succeeded:
            return True

        with _ENCODER_LOCK:
            can_fallback = (
                _ENCODER_STATE["index"] == encoder_index
                and encoder_index + 1 < len(ENCODER_CANDIDATES)
            )
            if can_fallback:
                _ENCODER_STATE["index"] = encoder_index + 1
        if not can_fallback:
            return False
        logger(f"    - {encoder['label']} 合并重编码失败，回退到下一个编码器。")


def _probe_duration(path):
    try:
        return float(probe_media(path)["format"]["duration"])
    except Exception:
        return 0.0


def _video_extradata(path):
    """返回视频流的解码配置（VPS/SPS/PPS 等 extradata）；失败返回 None。

    多段视频只有 extradata 完全一致时，才能安全地用 concat demuxer 流复制拼接
    （MP4 每条轨只能存一份参数集，参数集不同会导致拼接后的码流解码错乱）。
    """
    cmd = [
        FFPROBE_PATH, "-v", "error",
        "-select_streams", "v:0",
        "-show_streams", "-show_data",
        "-of", "json", path,
    ]
    result = run_command(cmd, check=False)
    if result.returncode != 0:
        return None
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except ValueError:
        return None
    if not streams:
        return None
    return streams[0].get("extradata")


def _media_signature(path):
    """返回 (视频签名, 音频签名)，用于判断多段是否可直接流复制拼接。"""
    info = probe_media(path)
    video = pick_stream(info, "video")
    audio = pick_stream(info, "audio")
    video_sig = None
    if video is not None:
        video_sig = (
            video.get("codec_name"),
            video.get("width"),
            video.get("height"),
            video.get("pix_fmt"),
            _video_extradata(path),
        )
    audio_sig = None
    if audio is not None:
        audio_sig = (
            audio.get("codec_name"),
            audio.get("sample_rate"),
            audio.get("channels"),
        )
    return video_sig, audio_sig


def _write_concat_list(list_path, paths):
    with open(list_path, "w", encoding="utf-8") as handle:
        for path in paths:
            safe_path = os.path.abspath(path).replace("\\", "/")
            handle.write(f"file '{safe_path}'\n")


def _concat_copy(list_path, out_path, on_stdout=None):
    cmd = [
        FFMPEG_PATH, "-hide_banner", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        out_path,
    ]
    return run_streaming(cmd, on_stdout=on_stdout)


def _duration_ok(path, expected, tolerance=1.0):
    """校验合并结果总时长、以及音轨时长是否接近预期，防止某段流被静默丢弃。"""
    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
        return False
    if expected <= 0:
        return True
    try:
        info = probe_media(path)
    except Exception:
        return False
    try:
        if float(info["format"]["duration"]) < expected - tolerance:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        try:
            audio_duration = float(stream.get("duration", 0))
        except (TypeError, ValueError):
            audio_duration = 0.0
        if audio_duration and audio_duration < expected - tolerance:
            return False
    return True


def _is_benign_mux_warning(line):
    """复用器层面的时间戳提示（拼接处 PTS/DTS 重复等），不是解码错误。"""
    markers = (
        "[null @",
        "non monotonically increasing dts",
        "Application provided invalid",
    )
    return any(marker in line for marker in markers)


def _joins_ok(merged_path, boundaries, logger=None):
    """在拼接点附近解码一小段，确认没有真正的解码错误（防止不同编码器拼接后码流错乱）。"""
    for boundary in boundaries:
        start = max(0.0, boundary - 0.5)
        cmd = [FFMPEG_PATH, "-hide_banner", "-v", "error"]
        if start > 0.001:
            cmd.extend(["-ss", format_seconds(start)])
        cmd.extend(["-t", "1.2", "-i", merged_path, "-map", "0:v:0", "-f", "null", "NUL"])
        result = run_command(cmd, check=False)
        if result.returncode != 0:
            return False
        for line in result.stderr.splitlines():
            line = line.strip()
            if not line or _is_benign_mux_warning(line):
                continue
            if logger is not None:
                logger(f"    - 拼接处解码告警: {line}")
            return False
    return True


def _target_audio_params(signatures):
    """取各段中最高的采样率 / 声道数作为统一目标，尽量避免降质。"""
    rates, channels = [], []
    for _, audio_sig in signatures:
        if audio_sig is None:
            continue
        if audio_sig[1]:
            rates.append(int(audio_sig[1]))
        if audio_sig[2]:
            channels.append(int(audio_sig[2]))
    return (max(rates) if rates else 48000), (max(channels) if channels else 1)


def _normalize_all(paths, signatures, temp_dir, logger):
    """把各段统一成一致的流布局：视频复制（无损）、音频统一转码、缺音频的补静音。

    统一后各段可直接用 concat demuxer 流复制拼接。返回中间文件列表，失败返回 None。
    """
    want_audio = any(sig[1] is not None for sig in signatures)
    target_sr, target_ch = _target_audio_params(signatures)
    layout = "mono" if target_ch == 1 else "stereo"
    normalized = []
    for index, src in enumerate(paths):
        dst = os.path.join(temp_dir, f"norm_{index:05d}.mp4")
        has_audio = signatures[index][1] is not None
        cmd = [FFMPEG_PATH, "-hide_banner", "-y", "-i", src]
        if not want_audio:
            cmd.extend(["-map", "0:v:0", "-c:v", "copy", "-an"])
        elif has_audio:
            cmd.extend(
                [
                    "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                    "-c:a", DAILY_MERGE_AUDIO_CODEC,
                    "-ar", str(target_sr), "-ac", str(target_ch),
                ]
            )
        else:
            # 该段没有音频：补一路静音，保证与其它段流布局一致。
            cmd.extend(
                [
                    "-f", "lavfi", "-i", f"anullsrc=r={target_sr}:cl={layout}",
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                    "-c:a", DAILY_MERGE_AUDIO_CODEC,
                    "-ar", str(target_sr), "-ac", str(target_ch), "-shortest",
                ]
            )
        cmd.extend(["-movflags", "+faststart", dst])
        result = run_command(cmd, check=False)
        if result.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            if result.stderr:
                logger(result.stderr)
            return None
        normalized.append(dst)
    return normalized


def _ts_join(paths, signatures, temp_dir, logger, out_path, expected_duration, progress_prefix, boundaries):
    """无损 TS 拼接：各段参数集不同也能拼，且视频全程流复制（不重编码）。

    原理：MP4 只能存一份视频参数集，参数集不同的片段直接拼会解码错乱；
    先逐段统一音频并转成 MPEG-TS（把参数集内联进码流），再拼接，最后以
    `-tag:v hev1`（允许参数集在码流中变化）回封装成 MP4。
    """
    normalized = _normalize_all(paths, signatures, temp_dir, logger)
    if not normalized:
        return False

    ts_parts = []
    for index, src in enumerate(normalized):
        logger(f"    - 无损 TS 拼接：转封装 {index + 1}/{len(normalized)} 段...")
        ts_path = os.path.join(temp_dir, f"part_{index:05d}.ts")
        result = run_command(
            [FFMPEG_PATH, "-hide_banner", "-y", "-i", src,
             "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "mpegts", ts_path],
            check=False,
        )
        if result.returncode != 0 or not os.path.isfile(ts_path):
            if result.stderr:
                logger(result.stderr)
            return False
        ts_parts.append(ts_path)

    ts_list = os.path.join(temp_dir, "ts_list.txt")
    _write_concat_list(ts_list, ts_parts)
    joined_ts = os.path.join(temp_dir, "joined.ts")
    result = run_command(
        [FFMPEG_PATH, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
         "-i", ts_list, "-c", "copy", joined_ts],
        check=False,
    )
    if result.returncode != 0 or not os.path.isfile(joined_ts):
        if result.stderr:
            logger(result.stderr)
        return False

    on_stdout = (
        make_progress_handler(expected_duration, progress_prefix)
        if expected_duration > 0
        else None
    )
    returncode, stderr_text = run_streaming(
        [FFMPEG_PATH, "-hide_banner", "-y", "-i", joined_ts,
         "-c", "copy", "-tag:v", "hev1", "-movflags", "+faststart",
         "-progress", "pipe:1", "-nostats", out_path],
        on_stdout=on_stdout,
    )
    if returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        if stderr_text.strip():
            logger(stderr_text)
        return False
    if not _duration_ok(out_path, expected_duration):
        logger("    - TS 拼接后时长异常。")
        return False
    if not _joins_ok(out_path, boundaries, logger):
        logger("    - TS 拼接后拼接处仍有解码错误。")
        return False
    return True


def merge_day_videos(out_path, items, logger, progress_prefix):
    """把同一天的一组视频合并为 out_path，返回 'merged' / 'failed'。

    items 为 (start, name, path) 列表，已按时间升序。
    优先视频流复制（无损），音频按需统一转码；逐级兜底，最后整体重编码。
    """
    if len(items) == 1:
        # 单文件的天也照样产出当天文件，直接复制一份。
        shutil.copy2(items[0][2], out_path)
        return "merged"

    paths = [item[2] for item in items]
    durations = [_probe_duration(path) for path in paths]
    expected_duration = sum(durations)
    boundaries = []
    cumulative = 0.0
    for duration in durations[:-1]:
        cumulative += duration
        boundaries.append(cumulative)

    temp_dir = tempfile.mkdtemp(prefix="merge_day_")
    try:
        temp_output = os.path.join(temp_dir, "merged.mp4")
        on_stdout = (
            make_progress_handler(expected_duration, progress_prefix)
            if expected_duration > 0
            else None
        )

        raw_list = os.path.join(temp_dir, "concat_list.txt")
        _write_concat_list(raw_list, paths)

        signatures = []
        for path in paths:
            try:
                signatures.append(_media_signature(path))
            except Exception:
                signatures = None
                break

        if signatures is None:
            logger("    - 无法读取流信息，跳过该天。")
            return "failed"

        want_audio = any(sig[1] is not None for sig in signatures)
        # 只有各段视频解码配置（参数集）完全一致，才能安全地流复制拼接；
        # 否则（来自不同编码器 / 参数的片段）必须重编码，否则拼接处码流会解码错乱。
        video_sigs = [sig[0] for sig in signatures]
        video_copy_safe = (
            all(sig is not None and sig[-1] is not None for sig in video_sigs)
            and len(set(video_sigs)) == 1
        )
        if not video_copy_safe:
            logger("    - 各段视频参数不一致（来自不同编码器），改用无损 TS 拼接（不重编码）。")

        # 尝试一：视频 + 音频都完全一致时，直接流复制拼接（最快、无损）。
        if video_copy_safe and len(set(signatures)) == 1:
            if os.path.exists(temp_output):
                os.remove(temp_output)
            returncode, stderr_text = _concat_copy(raw_list, temp_output, on_stdout)
            if (
                returncode == 0
                and _duration_ok(temp_output, expected_duration)
                and _joins_ok(temp_output, boundaries, logger)
            ):
                shutil.move(temp_output, out_path)
                return "merged"
            logger("    - 直接流复制拼接不可靠，改用统一音频后拼接。")
            if returncode != 0 and stderr_text.strip():
                logger(stderr_text)

        # 尝试二：视频参数一致时，先逐段统一音频（缺音轨补静音）再流复制拼接。
        normalized = None
        if video_copy_safe:
            normalized = _normalize_all(paths, signatures, temp_dir, logger)
            if normalized:
                norm_list = os.path.join(temp_dir, "normalized_list.txt")
                _write_concat_list(norm_list, normalized)
                if os.path.exists(temp_output):
                    os.remove(temp_output)
                returncode, stderr_text = _concat_copy(norm_list, temp_output, on_stdout)
                if (
                    returncode == 0
                    and _duration_ok(temp_output, expected_duration)
                    and _joins_ok(temp_output, boundaries, logger)
                ):
                    shutil.move(temp_output, out_path)
                    return "merged"
                logger("    - 音频统一后拼接仍不可靠，改用无损 TS 拼接。")
                if returncode != 0 and stderr_text.strip():
                    logger(stderr_text)

        # 尝试三：无损 TS 拼接（各段参数集不同也能拼，视频全程流复制，不重编码）。
        if os.path.exists(temp_output):
            os.remove(temp_output)
        if _ts_join(
            paths, signatures, temp_dir, logger, temp_output,
            expected_duration, progress_prefix, boundaries,
        ):
            shutil.move(temp_output, out_path)
            return "merged"
        logger("    - 无损 TS 拼接不可用，回退整体重编码。")

        # 尝试四：concat 滤镜整体重编码（最稳，但视频会有二次损失）。
        if os.path.exists(temp_output):
            os.remove(temp_output)
        presence = [sig[1] is not None for sig in signatures]
        audio_uniform = all(presence) or not any(presence)
        reencode_inputs = paths
        if not audio_uniform:
            if normalized is None:
                normalized = _normalize_all(paths, signatures, temp_dir, logger)
            if normalized:
                reencode_inputs = normalized
        if _reencode_concat_filter(
            reencode_inputs, want_audio, temp_output, logger,
            expected_duration, progress_prefix,
        ) and _duration_ok(temp_output, expected_duration):
            shutil.move(temp_output, out_path)
            return "merged"
        logger("    - 合并重编码也失败，已跳过该天。")
        return "failed"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def merge_videos_by_day(source_paths, logger=log, delete_source=False):
    """按天分组合并：每天一个 <YYYY-MM-DD>.mp4，输出到 OUTPUT_DIR/daily/。"""
    groups, unparsed = group_videos_by_day(source_paths)
    stats = {"merged": 0, "failed": 0, "skipped": 0}

    for path in unparsed:
        log(f"[合并] 跳过无法解析时间的文件: {os.path.basename(path)}")
        stats["skipped"] += 1

    if not groups:
        log("[合并] 没有可按天合并的视频。")
        return stats

    dest_dir = daily_output_dir()
    os.makedirs(dest_dir, exist_ok=True)
    total_days = len(groups)
    completed_days = 0
    emit_event("task_progress", completed=0, total=total_days)

    for day in sorted(groups):
        CONTROL.checkpoint()
        items = groups[day]
        out_path = os.path.join(dest_dir, f"{day.isoformat()}.mp4")
        log(f"[合并] {day.isoformat()}: {len(items)} 个文件 -> {out_path}")
        prefix = f"[合并 {day.isoformat()}]    - "
        try:
            result = merge_day_videos(out_path, items, logger, prefix)
        except JobCancelled:
            raise
        except Exception as exc:
            log(f"[合并] {day.isoformat()} 失败: {exc}")
            result = "failed"

        if result == "merged":
            stats["merged"] += 1
            log(f"[合并] {day.isoformat()} 完成。")
            if delete_source:
                removed = 0
                for _, _, path in items:
                    # 只删自动合并产出的 compact_*，绝不误删其它来源。
                    if os.path.basename(path).startswith("compact_"):
                        try:
                            os.remove(path)
                            removed += 1
                        except OSError:
                            pass
                if removed:
                    log(f"[合并] 已删除 {removed} 个源文件。")
        else:
            stats["failed"] += 1

        completed_days += 1
        emit_event("task_progress", completed=completed_days, total=total_days)

    return stats


def merge_outputs_by_day():
    """自动合并：把 output 里的 compact_*.mp4 按天合并到 output/daily/。"""
    ensure_output_dir()
    source_paths = sorted(glob.glob(os.path.join(OUTPUT_DIR, "compact_*.mp4")))
    if not source_paths:
        log("[合并] output 中没有 compact_*.mp4，跳过按天合并。")
        return
    log(f"[合并] 开始按天合并 {len(source_paths)} 个输出文件（来源: {OUTPUT_DIR}）...")
    stats = merge_videos_by_day(source_paths, logger=log, delete_source=DAILY_DELETE_SOURCE)
    log(
        f"[合并] 完成: 生成 {stats['merged']} 个按天文件，"
        f"失败 {stats['failed']} 个，跳过 {stats['skipped']} 个。"
    )
    log(f"[合并] 输出目录: {daily_output_dir()}")


def run_merge_only():
    """手动合并：对输入目录里的视频按天合并到输出目录的 daily/（不删除输入文件）。"""
    validate_environment()
    ensure_output_dir()
    video_files = list_input_videos()
    if not video_files:
        log(f"错误: 未在 {INPUT_DIR} 中找到 mp4 文件。")
        return
    log(f"[合并] 开始按天合并 {len(video_files)} 个视频（来源: {INPUT_DIR}）...")
    stats = merge_videos_by_day(video_files, logger=log, delete_source=False)
    log(
        f"[合并] 完成: 生成 {stats['merged']} 个按天文件，"
        f"失败 {stats['failed']} 个，跳过 {stats['skipped']} 个。"
    )
    log(f"[合并] 输出目录: {daily_output_dir()}")


def run_pipeline():
    validate_environment()
    run_pipeline_tasks()
    if MERGE_BY_DAY and not CONTROL.is_stopped():
        merge_outputs_by_day()

