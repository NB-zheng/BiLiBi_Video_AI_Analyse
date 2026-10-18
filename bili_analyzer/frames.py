from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .asr import Sentence
from .chunking import Chunk
from .utils import find_ffmpeg, format_compact, format_hms, run_quiet, warn


# 画面相关词：分数越高，越可能值得截一张图。
VISUAL_CUES: dict[str, int] = {
    # 强提示
    "如图": 4,
    "图中": 4,
    "画面": 4,
    "屏幕": 4,
    "镜头": 3,
    "图表": 4,
    "表格": 4,
    "代码": 4,
    "界面": 4,
    "幻灯片": 4,
    "ppt": 4,
    "左边": 3,
    "右边": 3,
    "箭头": 3,
    "红色": 2,
    "绿色": 2,
    "蓝色": 2,
    # 中等提示
    "看看": 2,
    "看一下": 2,
    "这里": 2,
    "这个": 2,
    "这张": 3,
    "展示": 3,
    "演示": 3,
    "示例": 2,
    "对比": 2,
    "效果": 2,
    "样子": 2,
    "结构": 2,
    "流程": 2,
    "点击": 2,
    "打开": 1,
    "拖动": 2,
    "网页": 3,
    "上面": 1,
    "下面": 1,
    "中间": 1,
    "输入": 1,
    "选择": 1,
    # 英文
    "figure": 4,
    "screenshot": 4,
    "screen": 3,
    "chart": 4,
    "table": 4,
    "slide": 4,
    "diagram": 4,
    "shown": 3,
    "look": 2,
    "left": 2,
    "right": 2,
    "here": 1,
    "image": 2,
    "picture": 2,
}


@dataclass
class Shot:
    index: int
    time: float
    chunk_index: int
    text: str
    reason: str
    path: Path | None = None
    sentence_start: float = 0.0
    sentence_end: float = 0.0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "time": round(self.time, 3),
            "time_text": format_hms(self.time),
            "chunk_index": self.chunk_index,
            "text": self.text,
            "reason": self.reason,
            "path": f"frames/{self.path.name}" if self.path else "",
            "sentence_start": round(self.sentence_start, 3),
            "sentence_end": round(self.sentence_end, 3),
        }


def _score_sentence(sentence: Sentence) -> tuple[int, list[str]]:
    text = sentence.text.lower()
    matched: list[str] = []
    score = 0
    for cue, weight in VISUAL_CUES.items():
        if cue.isascii():
            if re.search(rf"\b{re.escape(cue)}\b", text):
                matched.append(cue)
                score += weight
        elif cue in text:
            matched.append(cue)
            score += weight

    if 12 <= len(sentence.text) <= 90:
        score += 1
    if re.search(r"\d+(?:\.\d+)?\s*(?:%|倍|万|亿|元|块|米|毫米|秒|分钟)", sentence.text):
        score += 1
    return score, matched


def allocate_shot_counts(
    chunk_count: int,
    per_segment: int,
    max_shots: int,
) -> list[int]:
    """把总截图数尽量均匀地分配到各段，避免只截视频前半段。"""
    if chunk_count <= 0 or per_segment <= 0 or max_shots <= 0:
        return [0] * max(0, chunk_count)

    max_shots = min(max_shots, chunk_count * per_segment)
    counts = [0] * chunk_count

    if max_shots >= chunk_count:
        base = max_shots // chunk_count
        remainder = max_shots % chunk_count
        counts = [min(per_segment, base)] * chunk_count
        if remainder:
            for index in _evenly_spaced(chunk_count, remainder):
                if counts[index] < per_segment:
                    counts[index] += 1
    else:
        for index in _evenly_spaced(chunk_count, max_shots):
            counts[index] = 1

    # 如果因为 per_segment 限制仍有剩余，补到还没满的段
    remaining = max_shots - sum(counts)
    if remaining > 0:
        for index in _evenly_spaced(chunk_count, chunk_count):
            if remaining <= 0:
                break
            if counts[index] < per_segment:
                counts[index] += 1
                remaining -= 1
    return counts


def _evenly_spaced(total: int, count: int) -> list[int]:
    if count <= 0:
        return []
    if count >= total:
        return list(range(total))
    if count == 1:
        return [total // 2]
    return sorted(
        {
            min(total - 1, max(0, round((i + 0.5) * total / count - 0.5)))
            for i in range(count)
        }
    )


def choose_shots(chunk: Chunk, count: int) -> list[Shot]:
    if count <= 0 or not chunk.sentences:
        return []

    duration = max(0.1, chunk.end - chunk.start)
    margin = min(15.0, duration * 0.08)
    candidates = [
        sentence
        for sentence in chunk.sentences
        if sentence.text.strip()
        and len(sentence.text.strip()) >= 6
        and sentence.start >= chunk.start + margin
        and sentence.end <= chunk.end - margin
    ]
    if not candidates:
        candidates = chunk.sentences

    midpoint = (chunk.start + chunk.end) / 2
    scored: list[tuple[int, list[str], Sentence, float]] = []
    for sentence in candidates:
        score, matched = _score_sentence(sentence)
        offset = min(0.8, max(0.25, (sentence.end - sentence.start) * 0.25))
        shot_time = min(chunk.end, max(chunk.start, sentence.start + offset))
        scored.append((score, matched, sentence, shot_time))

    scored.sort(
        key=lambda item: (
            -item[0],
            abs(item[2].start - midpoint),
        )
    )

    selected: list[tuple[int, list[str], Sentence, float]] = []
    min_gap = max(6.0, duration / (count + 2) * 0.55)
    for item in scored:
        shot_time = item[3]
        if any(abs(shot_time - chosen[3]) < min_gap for chosen in selected):
            continue
        selected.append(item)
        if len(selected) >= count:
            break

    # 时间点太挤时放宽间隔，保证数量
    if len(selected) < count:
        for item in scored:
            if item in selected:
                continue
            selected.append(item)
            if len(selected) >= count:
                break

    shots: list[Shot] = []
    for score, matched, sentence, shot_time in selected:
        if matched:
            reason = "画面关键词：" + "、".join(matched[:3])
        else:
            reason = "段落中点附近"
        shots.append(
            Shot(
                index=0,
                time=round(shot_time, 3),
                chunk_index=chunk.index,
                text=sentence.text,
                reason=reason,
                sentence_start=sentence.start,
                sentence_end=sentence.end,
            )
        )

    shots.sort(key=lambda shot: shot.time)
    return shots


def plan_shots(
    chunks: list[Chunk],
    per_segment: int,
    max_shots: int,
) -> list[Shot]:
    if per_segment <= 0 or max_shots <= 0:
        return []
    counts = allocate_shot_counts(len(chunks), per_segment, max_shots)
    shots: list[Shot] = []
    for chunk, count in zip(chunks, counts):
        shots.extend(choose_shots(chunk, count))
    for index, shot in enumerate(shots, start=1):
        shot.index = index
    return shots


def extract_shots(
    video_path: Path,
    shots: list[Shot],
    frames_dir: Path,
    duration: float = 0,
) -> list[Shot]:
    if not shots:
        return []
    ffmpeg = find_ffmpeg()
    frames_dir.mkdir(parents=True, exist_ok=True)
    result: list[Shot] = []

    for shot in shots:
        if duration > 0 and shot.time >= duration:
            warn(
                f"截图时间 {format_hms(shot.time)} 超出视频时长 "
                f"{format_hms(duration)}，跳过"
            )
            continue
        shot_time = min(shot.time, duration - 0.5) if duration > 0 else shot.time
        shot_time = max(0.0, shot_time)
        shot.time = round(shot_time, 3)
        filename = f"shot_{shot.index:03d}_{format_compact(shot_time)}.jpg"
        output = frames_dir / filename
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{shot_time:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "3",
            "-an",
            "-sn",
            "-y",
            str(output),
        ]
        try:
            run_quiet(cmd, timeout=120)
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("ffmpeg 没有生成截图")
            shot.path = output
            result.append(shot)
        except Exception as exc:
            warn(f"截图失败 [{format_hms(shot.time)}]：{exc}")

    return result
