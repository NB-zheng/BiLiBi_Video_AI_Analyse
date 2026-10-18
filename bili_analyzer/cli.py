from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import traceback
from pathlib import Path

from .config import (
    QUALITY_IDS,
    RunConfig,
    load_env,
    normalize_cookie,
    read_cookie_file,
)
from .pipeline import Pipeline
from .utils import PROJECT_ROOT, warn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=(
            "B站视频 → 语音转写 → 关键截图 → 多模态分段分析 → "
            "整合报告（Markdown + PDF）"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="B站视频链接，可以连同其他描述文字一起粘贴",
    )

    group = parser.add_argument_group("B站")
    group.add_argument(
        "--cookie",
        default="",
        help="完整 Cookie 字符串；也可用 BILI_COOKIE 或 cookie.txt",
    )
    group.add_argument(
        "--cookie-file",
        default="cookie.txt",
        help="Cookie 文件路径（相对项目根目录）",
    )
    group.add_argument(
        "--quality",
        default="1080",
        choices=list(QUALITY_IDS.keys()),
        help="优先请求的清晰度；没有对应清晰度时自动降级",
    )

    group = parser.add_argument_group("语音识别")
    group.add_argument(
        "--asr-model",
        default="small",
        help="faster-whisper 模型名（tiny/base/small/medium/large-v3）或本地模型目录",
    )
    group.add_argument(
        "--language",
        default="zh",
        help="音频语言；中文用 zh，英文用 en，自动检测用 auto",
    )
    group.add_argument(
        "--threads",
        type=int,
        default=4,
        help="faster-whisper CPU 线程数",
    )

    group = parser.add_argument_group("分段与截图")
    group.add_argument(
        "--chunk-seconds",
        type=int,
        default=300,
        help="每段目标时长（秒）",
    )
    group.add_argument(
        "--chunk-chars",
        type=int,
        default=1800,
        help="每段最大字数",
    )
    group.add_argument(
        "--shots-per-segment",
        type=int,
        default=1,
        help="每段最多截几张图；默认 1 张，优先选带画面关键词的句子",
    )
    group.add_argument(
        "--max-shots",
        type=int,
        default=12,
        help="整个视频最多截几张图",
    )
    group.add_argument(
        "--no-shots",
        action="store_true",
        help="完全不截图，只做文字分析",
    )

    group = parser.add_argument_group("模型")
    group.add_argument("--base-url", default="", help="OpenAI 兼容接口的 base_url")
    group.add_argument("--api-key", default="", help="接口 Key；也可用 LLM_API_KEY")
    group.add_argument("--model", default="", help="多模态模型名；也可用 LLM_MODEL")
    group.add_argument(
        "--temperature",
        type=float,
        default=0.3,
        help="模型温度，0-1",
    )
    group.add_argument(
        "--segment-max-tokens",
        type=int,
        default=8000,
        help="每段分析的最大输出 tokens（推理模型会自动翻倍重试）",
    )
    group.add_argument(
        "--final-max-tokens",
        type=int,
        default=16000,
        help="最终整合的最大输出 tokens（推理模型会自动翻倍重试）",
    )
    group.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="单次模型请求超时（秒）",
    )

    group = parser.add_argument_group("输出")
    group.add_argument(
        "--pdf-engine",
        default="pymupdf",
        choices=["pymupdf", "edge", "none"],
        help="PDF 引擎；pymupdf 失败时自动尝试 Edge",
    )
    group.add_argument(
        "--outdir",
        default=str(PROJECT_ROOT / "output"),
        help="输出根目录；中间产物在其 cache/ 下",
    )

    group = parser.add_argument_group("调试")
    group.add_argument(
        "--mock",
        action="store_true",
        help="不调用真实模型，用占位内容验证完整流程",
    )
    group.add_argument(
        "--fresh",
        action="store_true",
        help="忽略已有音视频和转写缓存，重新下载/识别",
    )
    group.add_argument(
        "--force-llm",
        action="store_true",
        help="忽略已有分段笔记，重新调用模型",
    )
    group.add_argument(
        "--debug",
        action="store_true",
        help="出错时打印完整堆栈",
    )
    return parser


def _resolve_cookie(args: argparse.Namespace, allow_prompt: bool = True) -> str:
    raw = args.cookie or os.getenv("BILI_COOKIE", "")
    if not raw:
        cookie_path = Path(args.cookie_file).expanduser()
        if not cookie_path.is_absolute():
            cookie_path = PROJECT_ROOT / cookie_path
        raw = read_cookie_file(cookie_path)

    if not raw and allow_prompt and sys.stdin.isatty():
        print(
            "未检测到 B 站 Cookie。直接回车可匿名访问（清晰度通常会受限）。\n"
            "也可以把完整 Cookie 保存到项目根目录的 cookie.txt。"
        )
        try:
            raw = getpass.getpass("B站 Cookie（输入不回显，回车跳过）: ")
        except (EOFError, KeyboardInterrupt):
            raw = ""
    return normalize_cookie(raw)


def _configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    for name in ("urllib3", "requests", "faster_whisper", "huggingface_hub"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _missing_llm_message() -> str:
    return (
        "未配置多模态模型。\n"
        "  请复制 .env.example 为 .env，并填写：\n"
        "    LLM_BASE_URL=https://api.openai.com/v1\n"
        "    LLM_API_KEY=sk-...\n"
        "    LLM_MODEL=gpt-4o-mini\n"
        "  如果只是想先验证流程，可以加 --mock 运行（不会调用模型）。"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env()
    _configure_logging(args.debug)

    url = (args.url or "").strip()
    if not url:
        if not sys.stdin.isatty():
            print("[错误] 请提供 B 站视频链接。", file=sys.stderr)
            return 2
        print("请输入 B 站视频链接（可以连同描述文字一起粘贴）：")
        try:
            url = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 130
    if not url:
        print("[错误] 视频链接为空。", file=sys.stderr)
        return 2

    cookie = _resolve_cookie(args, allow_prompt=not bool(args.url))

    base_url = (args.base_url or os.getenv("LLM_BASE_URL", "")).strip()
    api_key = (args.api_key or os.getenv("LLM_API_KEY", "")).strip()
    model = (args.model or os.getenv("LLM_MODEL", "")).strip()

    if not args.mock and (not base_url or not model):
        print(f"[错误] {_missing_llm_message()}", file=sys.stderr)
        return 2
    if not args.mock and not api_key:
        warn("LLM_API_KEY 为空；如果接口需要鉴权，请先在 .env 中填写。")

    shots_per_segment = 0 if args.no_shots else args.shots_per_segment
    config = RunConfig(
        url=url,
        cookie=cookie,
        quality=args.quality,
        out_root=Path(args.outdir).expanduser().resolve(),
        asr_model=args.asr_model,
        language=args.language,
        threads=max(1, args.threads),
        chunk_seconds=args.chunk_seconds,
        chunk_chars=args.chunk_chars,
        shots_per_segment=shots_per_segment,
        max_shots=args.max_shots,
        base_url=base_url,
        api_key=api_key,
        llm_model=model,
        temperature=max(0.0, min(1.0, args.temperature)),
        segment_max_tokens=args.segment_max_tokens,
        final_max_tokens=args.final_max_tokens,
        llm_timeout=args.timeout,
        pdf_engine=args.pdf_engine,
        mock=args.mock,
        fresh=args.fresh,
        force_llm=args.force_llm,
        debug=args.debug,
    )

    try:
        Pipeline(config).run()
    except KeyboardInterrupt:
        print("\n\n已取消。已下载的媒体和已生成的片段会保留，可重新运行续跑。")
        return 130
    except Exception as exc:
        print(f"\n[错误] {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1
    return 0
