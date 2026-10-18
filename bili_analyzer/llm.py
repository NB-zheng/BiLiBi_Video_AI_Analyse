from __future__ import annotations

import base64
import mimetypes
import re
import time
from pathlib import Path

import requests

from .utils import human_size


class LLMError(RuntimeError):
    """模型调用失败。"""


class EmptyOutputError(LLMError):
    """模型把输出额度用完了，content 为空。"""

    def __init__(self, message: str, max_tokens: int = 0, reasoning_length: int = 0):
        super().__init__(message)
        self.max_tokens = max_tokens
        self.reasoning_length = reasoning_length


MAX_OUTPUT_TOKENS = 32768


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 180,
        max_retries: int = 3,
    ):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise LLMError("LLM_BASE_URL 为空。")
        self.endpoint = (
            base if base.endswith("/chat/completions") else base + "/chat/completions"
        )
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.last_usage: dict = {}
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "bili-analyzer/1.0",
            }
        )
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def chat(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.3,
    ) -> str:
        tokens = max(1, int(max_tokens))
        for attempt in range(4):
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": tokens,
            }
            try:
                return self._post(payload)
            except EmptyOutputError as exc:
                if attempt >= 3 or tokens >= MAX_OUTPUT_TOKENS:
                    raise LLMError(
                        f"{exc}\n"
                        "      该模型可能把输出额度都用于 reasoning。"
                        "请提高 --segment-max-tokens / --final-max-tokens，"
                        "或改用非推理模型。"
                    ) from exc
                new_tokens = min(tokens * 2, MAX_OUTPUT_TOKENS)
                print(
                    f"      模型思考占满 {tokens} tokens，"
                    f"自动提高到 {new_tokens} 重试..."
                )
                tokens = new_tokens

        raise LLMError("模型调用失败：输出额度调整后仍为空。")

    def chat_vision(
        self,
        system: str,
        user_text: str,
        image_paths: list[Path],
        max_tokens: int,
        temperature: float = 0.3,
    ) -> str:
        content: list[dict] = [{"type": "text", "text": user_text}]
        for path in image_paths:
            content.append(self._image_part(path))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        return self.chat(messages, max_tokens=max_tokens, temperature=temperature)

    def _post(self, payload: dict) -> str:
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(
                    self.endpoint,
                    json=payload,
                    timeout=(15, self.timeout),
                )
            except requests.RequestException as exc:
                last_error = f"网络请求失败：{exc}"
                if attempt >= self.max_retries:
                    break
                wait = 2 ** attempt
                print(f"      模型请求失败，{wait}s 后重试（{attempt}/{self.max_retries}）...")
                time.sleep(wait)
                continue

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise LLMError(
                        f"模型返回的不是 JSON：{response.text[:500]}"
                    ) from exc

                self.last_usage = data.get("usage") or {}
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"模型返回中没有 choices：{str(data)[:500]}")
                choice = choices[0]
                message = choice.get("message") or {}
                content = message.get("content")
                text = self._normalize_content(content)
                finish_reason = choice.get("finish_reason")
                reasoning = self._normalize_content(message.get("reasoning_content"))
                if not text.strip():
                    if finish_reason == "length":
                        raise EmptyOutputError(
                            "模型返回了空内容（输出额度被 reasoning 占满）。",
                            max_tokens=int(payload.get("max_tokens") or 0),
                            reasoning_length=len(reasoning),
                        )
                    detail = ""
                    if reasoning:
                        detail += f"\n      reasoning 片段：{reasoning[-300:]}"
                    refusal = message.get("refusal")
                    if refusal:
                        detail += f"\n      refusal：{refusal}"
                    raise LLMError(
                        "模型返回了空内容。"
                        f"\n      finish_reason：{finish_reason or '未知'}"
                        f"{detail}"
                    )
                if finish_reason == "length":
                    print("      [提示] 模型输出达到 max_tokens，内容可能被截断")
                return text.strip()

            detail = self._error_detail(response)
            last_error = f"HTTP {response.status_code}: {detail}"
            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                wait = 2 ** attempt
                print(
                    f"      模型接口返回 {response.status_code}，"
                    f"{wait}s 后重试（{attempt}/{self.max_retries}）..."
                )
                time.sleep(wait)
                continue
            break

        raise LLMError(
            "模型调用失败。\n"
            f"      地址：{self.endpoint}\n"
            f"      模型：{self.model}\n"
            f"      原因：{last_error}"
        )

    @staticmethod
    def _normalize_content(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return str(content or "")

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return (response.text or "").strip()[:800]
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:800]
        return str(payload)[:800]

    @staticmethod
    def _image_part(path: Path) -> dict:
        if not path.is_file():
            raise LLMError(f"图片不存在：{path}")
        raw = path.read_bytes()
        if len(raw) > 12 * 1024 * 1024:
            raise LLMError(
                f"图片过大（{human_size(len(raw))}）：{path.name}，请降低截图分辨率。"
            )
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        }


class MockLLM:
    """只用于验证流程，不调用任何模型。"""

    def __init__(self):
        self.last_usage: dict = {}

    def chat_vision(
        self,
        system: str,
        user_text: str,
        image_paths: list[Path],
        max_tokens: int,
        temperature: float = 0.3,
    ) -> str:
        transcript = self._section(user_text, "【带时间戳的转写】", "【随消息附上的关键画面】")
        if not transcript:
            transcript = self._section(user_text, "【带时间戳的转写】", "【画面】")
        lines = [line for line in transcript.splitlines() if line.strip()][:5]
        first = lines[0] if lines else "（无转写）"
        bullets = "\n".join(f"- {line}" for line in lines) or "- 无"
        shots = len(image_paths)
        return "\n".join(
            [
                "### 本段主题",
                f"（Mock 模式）本段从 {first} 开始，主要围绕转写中的内容展开。",
                "",
                "### 关键要点",
                bullets,
                "",
                "### 画面信息",
                f"- 本段附了 {shots} 张关键画面（Mock 模式不做真实视觉分析）。",
                "",
                "### 重要细节",
                "- 这是离线流程测试生成的内容，不代表真实模型结论。",
                "",
                "### 存疑/听不清",
                "- 无",
            ]
        )

    def chat(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.3,
    ) -> str:
        text = ""
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                text += content + "\n"
        time_matches = re.findall(r"\[(\d{2}:\d{2}:\d{2})\]", text)
        sample_times = "、".join(dict.fromkeys(time_matches[:6])) or "无"
        return "\n".join(
            [
                "## 一句话总结",
                "（Mock 模式）这是一份用于验证端到端流程的占位报告。",
                "",
                "## 核心内容",
                "程序已经完成视频下载、语音识别、关键帧提取、分段分析和报告导出；"
                "由于启用了 `--mock`，这里没有调用真实多模态模型。",
                "",
                "## 章节脉络",
                f"- 材料中出现的关键时间点包括：{sample_times}。",
                "",
                "## 关键要点与结论",
                "- 在 `.env` 中配置 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 后，"
                "去掉 `--mock` 即可生成真实分析。",
                "",
                "## 画面与演示",
                "- 关键截图已保存在缓存目录的 `frames/` 中，并用于分段多模态分析。",
                "",
                "## 值得回看的片段",
                f"- 可参考这些时间点：{sample_times}。",
                "",
                "## 一句话结论",
                "流程可用；接入真实模型后即可得到正式报告。",
            ]
        )

    @staticmethod
    def _section(text: str, start_marker: str, end_marker: str) -> str:
        start = text.find(start_marker)
        if start < 0:
            return ""
        start += len(start_marker)
        end = text.find(end_marker, start)
        if end < 0:
            end = len(text)
        return text[start:end].strip()
