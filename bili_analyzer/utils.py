from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 命令行输出
# ---------------------------------------------------------------------------

def step(index: int, total: int, title: str) -> None:
    print(f"\n[{index}/{total}] {title}")


def info(label: str, value: Any) -> None:
    print(f"      {label}: {value}")


def warn(message: str) -> None:
    print(f"      [提示] {message}")


def rule() -> None:
    print("\n" + "=" * 62)


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def format_hms(seconds: float | int | None) -> str:
    """秒数 -> HH:MM:SS。"""
    if seconds is None:
        return "--:--:--"
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_compact(seconds: float | int) -> str:
    """秒数 -> HH-MM-SS，用于文件名。"""
    return format_hms(seconds).replace(":", "-")


def human_size(size: float | int | None) -> str:
    if size is None:
        return "未知"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def display_path(path: Path) -> str:
    """终端里尽量显示项目内相对路径。"""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def safe_filename(name: str, max_length: int = 80) -> str:
    """把视频标题变成 Windows 可用的文件夹名。"""
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:max_length].rstrip(" .")
    if not name:
        return "video"
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if name.upper() in reserved:
        name = "_" + name
    return name


# ---------------------------------------------------------------------------
# 文件 / JSON
# ---------------------------------------------------------------------------

def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------

def strip_code_fence(text: str) -> str:
    """去掉模型偶尔包在外面的 ```markdown ... ```。"""
    text = (text or "").strip()
    match = re.fullmatch(
        r"```(?:markdown|md)?\s*\n(.*?)\n```",
        text,
        flags=re.S | re.I,
    )
    return match.group(1).strip() if match else text


def strip_leading_h1(text: str) -> str:
    """去掉模型重复输出的 H1 标题（报告标题由程序统一添加）。"""
    lines = (text or "").lstrip().splitlines()
    if lines and re.match(r"^#\s+\S", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).lstrip()


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

_ffmpeg_path: str | None = None


def find_ffmpeg() -> str:
    """按常见位置查找 ffmpeg；找不到时给出明确提示。"""
    global _ffmpeg_path
    if _ffmpeg_path:
        return _ffmpeg_path

    candidates: list[str] = []
    env_path = os.getenv("FFMPEG_BIN", "").strip().strip('"').strip("'")
    if env_path:
        candidates.append(env_path)

    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)

    candidates.extend(
        [
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            str(Path.home() / "scoop" / "apps" / "ffmpeg" / "current" / "bin" / "ffmpeg.exe"),
        ]
    )

    winget_root = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget_root.is_dir():
        candidates.extend(str(p) for p in winget_root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            _ffmpeg_path = str(path)
            return _ffmpeg_path

    raise RuntimeError(
        "找不到 ffmpeg。请安装 ffmpeg 并加入 PATH，"
        "或在 .env 中设置 FFMPEG_BIN=ffmpeg.exe 的完整路径。"
    )


def run_quiet(cmd: list[str], timeout: int = 120) -> None:
    """执行命令，失败时把 stderr 带进异常信息。"""
    import subprocess

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"命令执行超时: {cmd[0]}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"命令执行失败（{result.returncode}）: {detail[:500]}")
