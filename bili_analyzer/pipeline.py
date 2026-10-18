from __future__ import annotations

import time
from pathlib import Path

from .asr import Transcript, ensure_model, load_transcript, save_transcript, transcribe
from .bilibili import BilibiliClient, VideoInfo
from .chunking import Chunk, make_chunks, render_full_transcript
from .config import RunConfig
from .frames import Shot, extract_shots, plan_shots
from .llm import LLMClient, LLMError, MockLLM
from .pdf_export import export_pdf
from .prompts import (
    FINAL_SYSTEM,
    SEGMENT_SYSTEM,
    build_final_prompt,
    build_report_header,
    build_segment_prompt,
)
from .utils import (
    display_path,
    format_hms,
    human_size,
    now_text,
    safe_filename,
    step,
    strip_code_fence,
    strip_leading_h1,
    warn,
    write_json,
    write_text,
)


class Pipeline:
    def __init__(self, config: RunConfig):
        config.validate()
        self.cfg = config
        self.client = BilibiliClient(config.cookie)
        if config.mock:
            self.llm = MockLLM()
        else:
            self.llm = LLMClient(
                base_url=config.base_url,
                api_key=config.api_key,
                model=config.llm_model,
                timeout=config.llm_timeout,
            )
        self.video: VideoInfo | None = None
        self.work_dir: Path | None = None
        self.folder_name = ""
        self.transcript: Transcript | None = None
        self.transcript_cached = False
        self.chunks: list[Chunk] = []
        self.shots: list[Shot] = []
        self.notes: list[str] = []

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(self) -> tuple[Path, Path | None]:
        mode = "Mock" if self.cfg.mock else self.cfg.llm_model
        print(f"B站视频 → 多模态分析  ·  {mode}")

        step(1, 6, "解析")
        video = self.client.fetch(self.cfg.url, quality=self.cfg.quality)
        self.video = video
        quality = video.quality_text
        if not self.cfg.cookie:
            quality += " · 未提供 Cookie"
        page = f" · P{video.page_index}" if video.page_index > 1 else ""
        print(f"      {video.title}")
        print(
            f"      {video.uploader} · {format_hms(video.duration)} · "
            f"{quality} · {video.bvid}{page}"
        )

        self.work_dir = self._prepare_dirs(video)
        self._write_meta()

        step(2, 6, "下载")
        video_path, audio_path = self.client.download(
            video,
            self.work_dir / "media",
            fresh=self.cfg.fresh,
        )
        audio_size = human_size(audio_path.stat().st_size) if audio_path else "无"
        print(
            f"      视频 {human_size(video_path.stat().st_size)} · "
            f"音频 {audio_size}"
        )

        step(3, 6, "识别")
        started = time.time()
        transcript = self._transcribe(audio_path or video_path, video)
        self.transcript = transcript
        if video.duration <= 0:
            video.duration = transcript.duration
        elapsed = time.time() - started
        state = "缓存" if self.transcript_cached else f"{elapsed:.1f}s"
        print(
            f"      {len(transcript.sentences)} 句 · "
            f"{format_hms(transcript.duration)} · {state}"
        )

        step(4, 6, "分段与截图")
        self.chunks = make_chunks(
            transcript.sentences,
            target_seconds=self.cfg.chunk_seconds,
            max_chars=self.cfg.chunk_chars,
        )
        self.shots = plan_shots(
            self.chunks,
            per_segment=self.cfg.shots_per_segment,
            max_shots=self.cfg.max_shots,
        )
        if self.shots:
            self.shots = extract_shots(
                video_path,
                self.shots,
                self.work_dir / "frames",
                duration=video.duration,
            )
        shot_text = f"截图 {len(self.shots)} 张" if self.shots else "不截图"
        print(f"      {len(self.chunks)} 段 · {shot_text}")
        write_json(
            self.work_dir / "shots.json",
            [shot.to_dict() for shot in self.shots],
        )
        self._write_meta()

        step(5, 6, "分析")
        self.notes = self._analyze_chunks()

        step(6, 6, "导出")
        report_path, pdf_path = self._finalize()
        print(f"      {display_path(report_path)}")
        if pdf_path:
            print(
                f"      {display_path(pdf_path)} · "
                f"{human_size(pdf_path.stat().st_size)}"
            )
        else:
            print("      未生成 PDF")

        return report_path, pdf_path

    # ------------------------------------------------------------------
    # 目录与元信息
    # ------------------------------------------------------------------

    def _prepare_dirs(self, video: VideoInfo) -> Path:
        folder = safe_filename(video.title)
        self.folder_name = folder
        work_dir = (self.cfg.out_root / "cache" / folder).resolve()
        for sub in ("media", "frames", "segments"):
            (work_dir / sub).mkdir(parents=True, exist_ok=True)
        return work_dir

    def _write_meta(self) -> None:
        if not self.video or not self.work_dir:
            return
        video = self.video
        folder = self.folder_name or safe_filename(video.title)
        final_md = self.cfg.out_root / f"{folder}.md"
        final_pdf = self.cfg.out_root / f"{folder}.pdf"
        write_json(
            self.work_dir / "meta.json",
            {
                "title": video.title,
                "bvid": video.bvid,
                "cid": video.cid,
                "url": video.page_url,
                "uploader": video.uploader,
                "duration": round(video.duration, 3),
                "duration_text": format_hms(video.duration),
                "description": video.description,
                "pubdate": video.pubdate,
                "quality": video.quality_text,
                "generated_at": now_text(),
                "asr_model": self.cfg.asr_model,
                "llm_model": "mock" if self.cfg.mock else self.cfg.llm_model,
                "chunks": len(self.chunks),
                "sentences": len(self.transcript.sentences) if self.transcript else 0,
                "shots": len(self.shots),
                "files": {
                    "report": f"../../{final_md.name}",
                    "pdf": f"../../{final_pdf.name}" if final_pdf.is_file() else "",
                    "transcript": "transcript.md",
                    "shots": "shots.json",
                    "media": "media",
                    "frames": "frames",
                    "segments": "segments",
                },
            },
        )

    # ------------------------------------------------------------------
    # 语音识别
    # ------------------------------------------------------------------

    def _transcribe(self, media_path: Path, video: VideoInfo) -> Transcript:
        if not self.work_dir:
            raise RuntimeError("工作目录未初始化。")
        transcript_path = self.work_dir / "transcript.json"
        self.transcript_cached = False

        if not self.cfg.fresh:
            cached = load_transcript(transcript_path)
            if cached:
                self.transcript_cached = True
                return cached

        model = ensure_model(self.cfg.asr_model_path, self.cfg.threads)
        transcript = transcribe(
            media_path,
            model,
            language=self.cfg.language_code,
            duration_hint=video.duration,
        )
        save_transcript(transcript_path, transcript)
        return transcript

    # ------------------------------------------------------------------
    # 分段分析
    # ------------------------------------------------------------------

    def _analyze_chunks(self) -> list[str]:
        if not self.work_dir:
            raise RuntimeError("工作目录未初始化。")
        segments_dir = self.work_dir / "segments"
        notes: list[str] = []
        total = len(self.chunks)

        for chunk in self.chunks:
            chunk_shots = [
                shot
                for shot in self.shots
                if shot.chunk_index == chunk.index and shot.path is not None
            ]
            image_paths = [shot.path for shot in chunk_shots if shot.path]
            segment_path = segments_dir / f"segment_{chunk.index:02d}.md"

            if segment_path.is_file() and not self.cfg.force_llm:
                note = segment_path.read_text(encoding="utf-8")
                state = "缓存"
            else:
                prompt = build_segment_prompt(
                    self.video,
                    chunk,
                    chunk_shots,
                    total,
                )
                started = time.time()
                note = self.llm.chat_vision(
                    SEGMENT_SYSTEM,
                    prompt,
                    image_paths,
                    max_tokens=self.cfg.segment_max_tokens,
                    temperature=self.cfg.temperature,
                )
                note = strip_code_fence(note)
                if not note.strip():
                    raise LLMError(f"第 {chunk.index} 段分析结果为空。")
                write_text(segment_path, note)
                state = f"{time.time() - started:.1f}s"

            notes.append(note.strip())
            print(
                f"      [{chunk.index}/{total}] "
                f"{format_hms(chunk.start)}-{format_hms(chunk.end)} · "
                f"{len(image_paths)} 图 · {state}"
            )

        return notes

    # ------------------------------------------------------------------
    # 最终整合与导出
    # ------------------------------------------------------------------

    def _finalize(self) -> tuple[Path, Path | None]:
        if not self.video or not self.transcript or not self.work_dir:
            raise RuntimeError("流程状态不完整。")

        transcript_text = render_full_transcript(self.transcript.sentences)
        final_prompt = build_final_prompt(
            self.video,
            self.chunks,
            self.notes,
            transcript_text,
        )

        try:
            body = self.llm.chat(
                [
                    {"role": "system", "content": FINAL_SYSTEM},
                    {"role": "user", "content": final_prompt},
                ],
                max_tokens=self.cfg.final_max_tokens,
                temperature=self.cfg.temperature,
            )
            body = strip_code_fence(body)
        except Exception as exc:
            warn(f"最终整合失败：{exc}")
            warn("改为直接拼接分段笔记，仍会生成报告和 PDF。")
            body = "## 分段笔记汇总\n\n" + "\n\n".join(
                f"### 第 {chunk.index} 段\n{note}"
                for chunk, note in zip(self.chunks, self.notes)
            )

        body = strip_leading_h1(body)
        if not body.strip():
            raise LLMError("最终整合结果为空。")

        header = build_report_header(
            self.video,
            self.cfg.llm_model,
            mock=self.cfg.mock,
        )
        report_text = header + "\n\n" + body.strip() + "\n"

        folder = self.folder_name or safe_filename(self.video.title)
        self.cfg.out_root.mkdir(parents=True, exist_ok=True)
        report_path = (self.cfg.out_root / f"{folder}.md").resolve()
        write_text(report_path, report_text)

        transcript_path = self.work_dir / "transcript.md"
        write_text(
            transcript_path,
            f"# 完整转写：{self.video.title}\n\n"
            f"> 来源：[B站原视频]({self.video.page_url})  \n"
            f"> 时长：{format_hms(self.video.duration)} · "
            f"语言：{self.transcript.language}  \n"
            f"> 生成：{now_text()}\n\n"
            f"## 转写\n\n{transcript_text}\n",
        )

        pdf_path = (self.cfg.out_root / f"{folder}.pdf").resolve()
        if self.cfg.pdf_engine == "none":
            pdf_path.unlink(missing_ok=True)
        try:
            result = export_pdf(
                report_path,
                pdf_path,
                self.video.title,
                self.cfg.pdf_engine,
            )
        finally:
            self._write_meta()
        return report_path, result
