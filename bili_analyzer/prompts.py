from __future__ import annotations

from .bilibili import VideoInfo
from .chunking import Chunk, render_chunk_transcript
from .frames import Shot
from .utils import format_hms, now_text


SEGMENT_SYSTEM = """你是一位严谨的视频内容分析助手。
你会收到某个视频片段的转写文本（带时间戳）、视频元信息，以及该片段中抽取的关键画面。
你的任务是把“说的内容”和“画面内容”对应起来，输出这个片段的详细笔记，供后续整合使用。

要求：
1. 只使用给定材料，不脑补、不编造。
2. 区分“转写里明确说的”和“画面里能看到的”。
3. 保留专有名词、数字、结论、步骤和因果关系。
4. 时间戳统一写成 [HH:MM:SS]。
5. 使用简体中文 Markdown，不要输出与当前片段无关的总结。
6. 如果画面没有提供有效信息，直接写“画面无明显补充”，不要强行解读。
"""


FINAL_SYSTEM = """你是一位资深的内容编辑和知识整理者。
你会收到一个视频的元信息、按时间段拆分的转写笔记，以及可选的完整转写。
你的任务是把这些材料整合成一份结构清晰、信息密度高、可独立阅读的中文 Markdown 报告。

要求：
1. 按逻辑重新组织内容，合并重复信息，不要简单拼接各段笔记。
2. 保留关键时间戳，统一写成 [HH:MM:SS]。
3. 只基于给定材料写作，不编造事实；不确定的地方标注“转写不清/待确认”。
4. 区分视频明确表达的观点和你的概括，不要添加视频中没有的结论。
5. 使用简体中文，语言简洁、具体，避免空话。
"""


def build_segment_prompt(
    video: VideoInfo,
    chunk: Chunk,
    shots: list[Shot],
    total_chunks: int,
) -> str:
    description = (video.description or "").strip()
    if len(description) > 500:
        description = description[:500] + "..."

    lines = [
        "请分析下面这个视频片段。",
        "",
        f"视频标题：{video.title}",
        f"UP主：{video.uploader}",
        f"视频简介：{description or '（无）'}",
        f"当前片段：第 {chunk.index}/{total_chunks} 段，"
        f"{format_hms(chunk.start)} - {format_hms(chunk.end)}",
        "",
        "【带时间戳的转写】",
        render_chunk_transcript(chunk, with_header=False),
        "",
    ]

    if shots:
        lines.append("【随消息附上的关键画面】")
        lines.append("以下图片按顺序附在消息中：")
        for index, shot in enumerate(shots, start=1):
            lines.append(
                f"{index}. [{format_hms(shot.time)}] "
                f"对应台词：{shot.text}"
            )
        lines.append("")
    else:
        lines.append("【画面】本段没有附画面，请仅根据转写分析。")
        lines.append("")

    lines.extend(
        [
            "请严格按下面的结构输出本段 Markdown 笔记：",
            "### 本段主题",
            "用一句话概括这一段在讲什么。",
            "",
            "### 关键要点",
            "- 3-6 条，每条尽量带上时间戳。",
            "",
            "### 画面信息",
            "- 画面中能看到什么；图表、代码、演示、人物动作等。",
            "- 如果画面没有有效信息，写“画面无明显补充”。",
            "",
            "### 重要细节",
            "- 数据、例子、步骤、结论、因果关系。",
            "",
            "### 存疑/听不清",
            "- 如果没有，写“无”。",
        ]
    )
    return "\n".join(lines).strip()


def build_final_prompt(
    video: VideoInfo,
    chunks: list[Chunk],
    notes: list[str],
    transcript_text: str,
    max_transcript_chars: int = 16000,
) -> str:
    description = (video.description or "").strip()
    if len(description) > 800:
        description = description[:800] + "..."

    lines = [
        "请把下面的分段笔记整合成一份完整的视频报告。",
        "",
        "【视频元信息】",
        f"标题：{video.title}",
        f"UP主：{video.uploader}",
        f"时长：{format_hms(video.duration)}",
        f"链接：{video.page_url}",
        f"简介：{description or '（无）'}",
        "",
        f"【分段笔记（共 {len(chunks)} 段）】",
    ]

    for chunk, note in zip(chunks, notes):
        lines.append("")
        lines.append(
            f"### 第 {chunk.index} 段："
            f"{format_hms(chunk.start)} - {format_hms(chunk.end)}"
        )
        lines.append(note.strip())

    if transcript_text.strip():
        lines.append("")
        lines.append("【完整转写（可能被截断，仅用于核对细节）】")
        if len(transcript_text) > max_transcript_chars:
            transcript_text = (
                transcript_text[:max_transcript_chars]
                + "\n...[转写过长，后续已省略]"
            )
        lines.append(transcript_text.strip())

    lines.extend(
        [
            "",
            "【写作要求】",
            "1. 不要重复输出视频标题；从 `## 一句话总结` 开始。",
            "2. 按逻辑重组内容，合并重复信息，不要逐段照搬。",
            "3. 时间戳统一写成 [HH:MM:SS]，优先保留能帮助回看的位置。",
            "4. 不要编造视频中没有的事实；不确定处标注“转写不清/待确认”。",
            "5. 建议结构（可根据内容增删）：",
            "   ## 一句话总结",
            "   ## 核心内容",
            "   ## 章节脉络",
            "   ## 关键要点与结论",
            "   ## 画面与演示",
            "   ## 值得回看的片段",
            "   ## 一句话结论",
            "6. 总长度控制在 1500-3500 字，信息密度优先。",
            "7. 使用简体中文 Markdown；可以适当使用列表、引用和表格。",
        ]
    )
    return "\n".join(lines).strip()


def build_report_header(
    video: VideoInfo,
    llm_model: str,
    mock: bool = False,
) -> str:
    model = "Mock 模式（未调用模型）" if mock else llm_model
    description = " ".join((video.description or "").split())
    if len(description) > 400:
        description = description[:400] + "..."

    lines = [
        f"# {video.title}",
        "",
        f"> **来源**：[B站原视频]({video.page_url})  ",
        f"> **UP主**：{video.uploader} · **时长**：{format_hms(video.duration)}"
        + (f" · **发布**：{video.pubdate}" if video.pubdate else "")
        + "  ",
        f"> **生成**：{now_text()} · **模型**：{model}  ",
    ]
    if description:
        lines.append(f"> **简介**：{description}  ")
    return "\n".join(lines).strip()
