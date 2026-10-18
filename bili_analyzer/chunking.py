from __future__ import annotations

from dataclasses import dataclass

from .asr import Sentence
from .utils import format_hms


@dataclass
class Chunk:
    index: int
    start: float
    end: float
    sentences: list[Sentence]

    @property
    def text(self) -> str:
        return "".join(sentence.text for sentence in self.sentences)

    @property
    def chars(self) -> int:
        return len(self.text)


def make_chunks(
    sentences: list[Sentence],
    target_seconds: int = 300,
    max_chars: int = 1800,
    max_seconds: int | None = None,
) -> list[Chunk]:
    """按句子边界切分，避免把一个语义单元从中间截断。"""
    if not sentences:
        raise ValueError("没有可用于分段的转写文本。")

    if max_seconds is None:
        max_seconds = max(target_seconds + 120, int(target_seconds * 1.5))
    soft_chars = int(max_chars * 0.65)

    groups: list[list[Sentence]] = []
    current: list[Sentence] = []

    for sentence in sentences:
        if not current:
            current = [sentence]
            continue

        start = current[0].start
        chars = sum(len(item.text) for item in current)
        duration = sentence.end - start

        if (
            duration > max_seconds
            or chars + len(sentence.text) > max_chars
            or (duration >= target_seconds and chars >= soft_chars)
        ):
            groups.append(current)
            current = [sentence]
        else:
            current.append(sentence)

    if current:
        groups.append(current)

    chunks: list[Chunk] = []
    for index, group in enumerate(groups, start=1):
        start = group[0].start
        end = max(group[-1].end, start)
        chunks.append(
            Chunk(
                index=index,
                start=start,
                end=end,
                sentences=group,
            )
        )
    return chunks


def render_chunk_transcript(chunk: Chunk, with_header: bool = True) -> str:
    lines: list[str] = []
    if with_header:
        lines.append(
            f"时间范围：{format_hms(chunk.start)} - {format_hms(chunk.end)}"
        )
        lines.append("")
    for sentence in chunk.sentences:
        lines.append(f"[{format_hms(sentence.start)}] {sentence.text}")
    return "\n".join(lines).strip()


def render_full_transcript(sentences: list[Sentence]) -> str:
    return "\n".join(
        f"[{format_hms(sentence.start)}] {sentence.text}"
        for sentence in sentences
    ).strip()
