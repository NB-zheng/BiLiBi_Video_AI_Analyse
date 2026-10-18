from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .utils import PROJECT_ROOT


# B 站清晰度代码：数字越大越清晰，但文件也越大。
QUALITY_IDS = {
    "360": 16,
    "480": 32,
    "720": 64,
    "1080": 80,
    "2160": 120,
    "best": 127,
}

QUALITY_LABELS = {
    127: "8K",
    126: "杜比视界",
    125: "HDR",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
}


@dataclass
class RunConfig:
    url: str
    cookie: str = ""

    quality: str = "1080"
    out_root: Path = PROJECT_ROOT / "output"

    asr_model: str = "small"
    language: str = "zh"  # "auto" 表示自动检测
    threads: int = 4

    chunk_seconds: int = 300
    chunk_chars: int = 1800
    shots_per_segment: int = 1
    max_shots: int = 12

    base_url: str = ""
    api_key: str = ""
    llm_model: str = ""
    temperature: float = 0.3
    segment_max_tokens: int = 8000
    final_max_tokens: int = 16000
    llm_timeout: int = 180

    pdf_engine: str = "pymupdf"  # pymupdf | edge | none

    mock: bool = False
    fresh: bool = False
    force_llm: bool = False
    debug: bool = False

    @property
    def language_code(self) -> str | None:
        return None if self.language.lower() == "auto" else self.language

    @property
    def asr_model_path(self) -> Path:
        value = self.asr_model.strip()
        candidate = Path(value).expanduser()
        if candidate.exists():
            return candidate.resolve()
        return PROJECT_ROOT / "model" / f"faster-whisper-{value}"

    @property
    def llm_ready(self) -> bool:
        return bool(self.base_url.strip() and self.llm_model.strip())

    def validate(self) -> None:
        if not self.asr_model.strip():
            raise ValueError("--asr-model 不能为空")
        if not self.language.strip():
            raise ValueError("--language 不能为空")
        if self.chunk_seconds <= 0:
            raise ValueError("--chunk-seconds 必须大于 0")
        if self.chunk_chars < 200:
            raise ValueError("--chunk-chars 不能小于 200")
        if self.shots_per_segment < 0:
            raise ValueError("--shots-per-segment 不能为负数")
        if self.max_shots < 0:
            raise ValueError("--max-shots 不能为负数")
        if self.segment_max_tokens <= 0 or self.final_max_tokens <= 0:
            raise ValueError("--segment-max-tokens / --final-max-tokens 必须大于 0")
        if self.llm_timeout <= 0:
            raise ValueError("--timeout 必须大于 0")
        if self.pdf_engine not in {"pymupdf", "edge", "none"}:
            raise ValueError("--pdf-engine 只能是 pymupdf、edge 或 none")
        if not self.mock and not self.llm_ready:
            raise ValueError("未配置 LLM_BASE_URL / LLM_MODEL")


def load_env() -> None:
    """读取项目根目录的 .env；没有该文件时保持环境变量原样。"""
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)


def normalize_cookie(raw: str | None) -> str:
    """兼容用户粘贴 'Cookie: xxx' 或多行 Cookie。"""
    if not raw:
        return ""
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    parts = [part.strip() for part in re.split(r"[\r\n;]+", text) if part.strip()]
    return "; ".join(parts)


def read_cookie_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return normalize_cookie(path.read_text(encoding="utf-8", errors="replace"))
