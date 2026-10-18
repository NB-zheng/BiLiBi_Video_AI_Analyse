from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import read_json, warn, write_json

# 必须在导入 faster_whisper 之前设置
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

for _logger_name in (
    "faster_whisper",
    "huggingface_hub",
    "httpx",
    "httpcore",
    "ctranslate2",
):
    logging.getLogger(_logger_name).setLevel(logging.ERROR)


SENTENCE_END = re.compile(r"[。！？!?]")
SOFT_END = re.compile(r"[，,；;、]")
GAP_SECONDS = 0.8
SOFT_SPLIT_CHARS = 36
HARD_SPLIT_CHARS = 120


@dataclass
class Sentence:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(float(self.start), 3),
            "end": round(float(self.end), 3),
            "text": self.text,
        }


@dataclass
class Transcript:
    language: str
    duration: float
    sentences: list[Sentence]

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration": round(float(self.duration), 3),
            "sentences": [sentence.to_dict() for sentence in self.sentences],
        }


def save_transcript(path: Path, transcript: Transcript) -> None:
    write_json(path, transcript.to_dict())


def load_transcript(path: Path) -> Transcript | None:
    if not path.is_file():
        return None
    try:
        raw = read_json(path)
    except Exception as exc:
        warn(f"转写缓存读取失败，将重新识别：{exc}")
        return None

    if isinstance(raw, dict):
        language = str(raw.get("language") or "unknown")
        duration = float(raw.get("duration") or 0)
        items = raw.get("sentences") or []
    elif isinstance(raw, list):
        language = "unknown"
        duration = 0
        items = raw
    else:
        return None

    sentences: list[Sentence] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            continue
        sentences.append(Sentence(start=start, end=max(start, end), text=text))

    if not sentences:
        return None

    if duration <= 0:
        duration = max(sentence.end for sentence in sentences)
    return Transcript(language=language, duration=duration, sentences=sentences)


def model_is_ready(model_dir: Path) -> bool:
    required = ("config.json", "model.bin", "tokenizer.json")
    if not all((model_dir / name).is_file() for name in required):
        return False
    if (model_dir / "model.bin").stat().st_size < 1024 * 1024:
        return False
    return any(model_dir.glob("vocabulary.*"))


def ensure_model(model_dir: Path, threads: int):
    """加载本地 faster-whisper 模型；不存在时自动下载到 model/。"""
    from faster_whisper import WhisperModel, download_model

    model_dir.mkdir(parents=True, exist_ok=True)
    if not model_is_ready(model_dir):
        if not model_dir.name.startswith("faster-whisper-"):
            raise RuntimeError(
                f"模型目录不完整：{model_dir}\n"
                "请检查 config.json / model.bin / tokenizer.json / vocabulary.* 是否存在。"
            )
        size = model_dir.name.removeprefix("faster-whisper-")
        warn(f"首次运行，正在下载模型 faster-whisper-{size}...")
        download_model(size, output_dir=str(model_dir))
        if not model_is_ready(model_dir):
            raise RuntimeError(f"模型下载后仍不完整：{model_dir}")

    threads = max(1, int(threads))
    return WhisperModel(
        str(model_dir),
        device="cpu",
        compute_type="int8",
        cpu_threads=threads,
        num_workers=1,
    )


def transcribe(
    audio_path: Path,
    model,
    language: str | None = "zh",
    duration_hint: float = 0,
) -> Transcript:
    """调用 faster-whisper，输出带时间戳的句子列表。"""
    initial_prompt = None
    if language == "zh":
        initial_prompt = "以下是简体中文的普通话转写，使用简体中文和中文标点。"

    segments, whisper_info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        initial_prompt=initial_prompt,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,
    )

    duration = float(getattr(whisper_info, "duration", 0) or duration_hint or 0)
    results: list[Sentence] = []
    parts: list[str] = []
    start: float | None = None
    end: float | None = None
    prev_end: float | None = None

    def flush() -> None:
        nonlocal parts, start, end, prev_end
        text = "".join(parts).strip()
        if text and start is not None and end is not None:
            results.append(
                Sentence(
                    start=round(max(0.0, start), 3),
                    end=round(max(start, end), 3),
                    text=text,
                )
            )
        parts = []
        start = end = prev_end = None

    for segment in segments:
        words = segment.words or []
        if not words:
            flush()
            text = (segment.text or "").strip()
            if text:
                results.append(
                    Sentence(
                        start=round(float(segment.start or 0), 3),
                        end=round(float(segment.end or segment.start or 0), 3),
                        text=text,
                    )
                )
            continue

        for word in words:
            piece = word.word or ""
            if not piece.strip():
                continue

            if (
                parts
                and prev_end is not None
                and word.start - prev_end >= GAP_SECONDS
            ):
                flush()

            if start is None:
                start = float(word.start)
            parts.append(piece)
            end = float(word.end)
            prev_end = end

            text_length = len("".join(parts))
            if (
                SENTENCE_END.search(piece)
                or (text_length >= SOFT_SPLIT_CHARS and SOFT_END.search(piece))
                or text_length >= HARD_SPLIT_CHARS
            ):
                flush()

    flush()

    if not results:
        warn(
            "没有识别出任何文字。将用一条占位文本继续流程；"
            "如果是语言设置不对，请用 --language auto 或正确的语言代码重试。"
        )
        results = [
            Sentence(
                start=0.0,
                end=max(1.0, duration),
                text="（未识别到清晰语音，以下分析主要基于画面。）",
            )
        ]

    if duration <= 0:
        duration = max(sentence.end for sentence in results)

    detected = str(getattr(whisper_info, "language", "") or language or "unknown")
    return Transcript(language=detected, duration=duration, sentences=results)
