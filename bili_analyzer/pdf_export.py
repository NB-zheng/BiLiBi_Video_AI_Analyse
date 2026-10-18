from __future__ import annotations

import html as html_lib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import markdown

from .utils import warn

PAGE_MARGIN_MM = 16


def _base_css(font_family: str, for_edge: bool) -> str:
    page_css = (
        f"@page {{ size: A4; margin: {PAGE_MARGIN_MM}mm 15mm; }}"
        if for_edge
        else ""
    )
    return f"""
{page_css}
body {{
    font-family: {font_family};
    font-size: 10.8pt;
    line-height: 1.7;
    color: #1f2328;
}}
h1 {{
    font-size: 21pt;
    line-height: 1.3;
    color: #0b5cad;
    margin: 0 0 12pt 0;
}}
h2 {{
    font-size: 14.5pt;
    color: #0b5cad;
    border-bottom: 1px solid #d0d7de;
    margin: 18pt 0 8pt 0;
    padding-bottom: 3pt;
}}
h3 {{
    font-size: 12pt;
    color: #24292f;
    margin: 13pt 0 5pt 0;
}}
p {{ margin: 0 0 7pt 0; text-align: justify; }}
ul, ol {{ margin: 0 0 7pt 0; padding-left: 18pt; }}
li {{ margin: 0 0 2.5pt 0; }}
blockquote {{
    margin: 8pt 0;
    padding: 7pt 10pt;
    background-color: #f6f8fa;
    border-left: 3pt solid #0b5cad;
    color: #444;
}}
blockquote p {{ margin: 0 0 3pt 0; }}
code {{
    font-family: {font_family};
    background-color: #f6f8fa;
    font-size: 9.5pt;
}}
pre {{
    font-family: {font_family};
    background-color: #f6f8fa;
    padding: 8pt;
    margin: 7pt 0;
    white-space: pre-wrap;
    font-size: 9.5pt;
}}
img {{ margin: 7pt 0; }}
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 7pt 0;
}}
th, td {{
    border: 1px solid #d0d7de;
    padding: 4pt 6pt;
    font-size: 9.5pt;
    vertical-align: top;
}}
th {{ background-color: #f6f8fa; }}
a {{ color: #0b5cad; }}
hr {{ border: none; border-top: 1px solid #d0d7de; margin: 13pt 0; }}
"""


def markdown_to_html(markdown_text: str, title: str = "") -> str:
    converter = markdown.Markdown(
        extensions=["extra", "sane_lists", "toc", "nl2br"],
        output_format="html",
    )
    body = converter.convert(markdown_text)

    # 模型输出属于不可信内容：去掉脚本/iframe 和事件属性，避免 Edge 渲染时执行。
    body = re.sub(r"<script\b[^>]*>.*?</script>", "", body, flags=re.S | re.I)
    body = re.sub(r"<iframe\b[^>]*>.*?</iframe>", "", body, flags=re.S | re.I)
    body = re.sub(
        r'\s+on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)',
        "",
        body,
        flags=re.I,
    )
    body = re.sub(
        r'(?P<attr>href|src)\s*=\s*(?P<quote>["\'])\s*javascript:[^"\']*(?P=quote)',
        "",
        body,
        flags=re.I,
    )

    # PyMuPDF Story 对百分比宽度支持一般，给图片一个明确的打印宽度。
    def add_width(match: re.Match) -> str:
        attrs = match.group(1).rstrip()
        self_closing = attrs.endswith("/")
        if self_closing:
            attrs = attrs[:-1].rstrip()
        if re.search(r"\bwidth\s*=", attrs, re.I):
            return match.group(0)
        ending = " /" if self_closing else ""
        return f'<img{attrs} width="480"{ending}>'

    body = re.sub(r"<img\b([^>]*)>", add_width, body, flags=re.I)
    safe_title = html_lib.escape(title or "视频分析报告")
    return (
        "<!DOCTYPE html>"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{safe_title}</title></head><body>{body}</body></html>"
    )


def _find_cjk_font() -> Path | None:
    env_path = os.getenv("PDF_FONT_PATH", "").strip()
    candidates: list[str] = [env_path] if env_path else []
    candidates.extend(
        [
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\Deng.ttf",
            r"C:\Windows\Fonts\simkai.ttf",
            r"C:\Windows\Fonts\msyh.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ]
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def _find_browser() -> str | None:
    env_path = os.getenv("EDGE_BIN", "").strip() or os.getenv("BROWSER_BIN", "").strip()
    if env_path and Path(env_path).is_file():
        return env_path

    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found

    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/microsoft-edge",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def export_pdf(
    md_path: Path,
    pdf_path: Path,
    title: str,
    engine: str = "pymupdf",
) -> Path | None:
    if engine == "none":
        return None

    md_path = md_path.resolve()
    pdf_path = pdf_path.resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.unlink(missing_ok=True)

    if engine == "pymupdf":
        try:
            _export_with_pymupdf(md_path, pdf_path, title)
        except Exception as exc:
            warn(f"PyMuPDF 导出失败：{exc}")
            warn("改用 Edge/Chrome 无头打印...")
        else:
            try:
                _compress_pdf(pdf_path, title)
            except Exception as exc:
                warn(f"PDF 压缩/元数据写入失败，保留原文件：{exc}")
            return pdf_path

    if engine in {"pymupdf", "edge"}:
        try:
            _export_with_edge(md_path, pdf_path, title)
            return pdf_path
        except Exception as exc:
            raise RuntimeError(
                "PDF 导出失败。\n"
                f"      PyMuPDF/Edge 均不可用：{exc}\n"
                "      可尝试 --pdf-engine none 只生成 Markdown。"
            ) from exc

    raise RuntimeError(f"未知 PDF 引擎：{engine}")


def _export_with_pymupdf(md_path: Path, pdf_path: Path, title: str) -> None:
    import fitz

    font_path = _find_cjk_font()
    if not font_path:
        raise RuntimeError(
            "找不到中文字体，可设置 PDF_FONT_PATH 指向一个 .ttf/.ttc 字体文件。"
        )

    markdown_text = md_path.read_text(encoding="utf-8")
    html_text = markdown_to_html(markdown_text, title)

    archive = fitz.Archive(str(md_path.parent))
    archive.add(font_path.read_bytes(), "report-font.ttf")
    css = _base_css('"ReportFont"', for_edge=False)
    css = (
        '@font-face { font-family: "ReportFont"; src: url("report-font.ttf"); }\n'
        + css
    )

    story = fitz.Story(html=html_text, user_css=css, archive=archive)
    page_rect = fitz.paper_rect("a4")
    margin = 40
    where = page_rect + (margin, margin, -margin, -margin)

    writer = fitz.DocumentWriter(str(pdf_path))
    pages = 0
    try:
        while pages < 500:
            device = writer.begin_page(page_rect)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
            pages += 1
            if not more:
                break
    finally:
        writer.close()

    if pages == 0 or not pdf_path.is_file():
        raise RuntimeError("PyMuPDF 没有生成任何页面。")


def _compress_pdf(pdf_path: Path, title: str) -> None:
    """压缩字体流并写入元数据；在 DocumentWriter 完全退出后单独执行。"""
    import fitz

    temp_path = pdf_path.with_name(pdf_path.stem + ".compressed.pdf")
    temp_path.unlink(missing_ok=True)
    try:
        with fitz.open(pdf_path) as doc:
            doc.set_metadata(
                {
                    "title": title,
                    "author": "bili-analyzer",
                    "creator": "bili-analyzer",
                    "producer": "PyMuPDF",
                }
            )
            doc.save(temp_path, garbage=4, deflate=True, clean=True)
        for attempt in range(3):
            try:
                temp_path.replace(pdf_path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.3)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _export_with_edge(md_path: Path, pdf_path: Path, title: str) -> None:
    browser = _find_browser()
    if not browser:
        raise RuntimeError(
            "找不到 Edge/Chrome，可设置 EDGE_BIN 指向浏览器可执行文件。"
        )

    markdown_text = md_path.read_text(encoding="utf-8")
    html_text = markdown_to_html(markdown_text, title)
    css = _base_css(
        '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "SimHei", sans-serif',
        for_edge=True,
    )
    html_text = html_text.replace("</head>", f"<style>{css}</style></head>")

    temp_html = Path(
        tempfile.NamedTemporaryFile(
            prefix="bili-report-",
            suffix=".html",
            dir=str(md_path.parent),
            delete=False,
        ).name
    )
    temp_html.write_text(html_text, encoding="utf-8")

    profile_dir = Path(
        tempfile.mkdtemp(prefix="edge-profile-", dir=str(md_path.parent))
    )
    try:
        base_cmd = [
            browser,
            "--disable-gpu",
            "--disable-crash-reporter",
            "--disable-breakpad",
            "--no-first-run",
            "--no-default-browser-check",
            "--allow-file-access-from-files",
            f"--user-data-dir={profile_dir}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            temp_html.as_uri(),
        ]
        last_error = ""
        for headless in ("--headless=new", "--headless"):
            pdf_path.unlink(missing_ok=True)
            cmd = [browser, headless, *base_cmd[1:]]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                last_error = "浏览器打印超时"
                continue
            if pdf_path.is_file() and pdf_path.stat().st_size > 1000:
                return
            last_error = (result.stderr or result.stdout or "").strip()[:800]
        raise RuntimeError(last_error or "浏览器没有生成 PDF")
    finally:
        temp_html.unlink(missing_ok=True)
        shutil.rmtree(profile_dir, ignore_errors=True)
