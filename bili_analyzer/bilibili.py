from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from .config import QUALITY_IDS, QUALITY_LABELS
from .utils import warn

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.I)
BILI_VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?bilibili\.com/video/[A-Za-z0-9?=&%._~:/+#-]+",
    re.I,
)
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AV_RE = re.compile(r"(?<![0-9A-Za-z])av(\d+)", re.I)

CODEC_NAMES = {7: "H.264", 12: "HEVC", 13: "AV1"}


class BiliError(RuntimeError):
    """B 站解析/下载失败。"""


class MediaExpired(BiliError):
    """CDN 地址过期，需要刷新播放地址。"""


@dataclass
class VideoRef:
    bvid: str | None = None
    aid: int | None = None
    page: int = 1
    page_url: str = ""


@dataclass
class Stream:
    url: str
    quality_id: int = 0
    codecid: int = 0
    width: int = 0
    height: int = 0
    bandwidth: int = 0
    backup_urls: list[str] = field(default_factory=list)

    @property
    def codec(self) -> str:
        return CODEC_NAMES.get(self.codecid, f"codec-{self.codecid}")

    @property
    def quality_label(self) -> str:
        return QUALITY_LABELS.get(self.quality_id, str(self.quality_id or "未知"))


@dataclass
class VideoInfo:
    bvid: str
    aid: int
    cid: int
    title: str
    description: str
    duration: float
    uploader: str
    pubdate: str
    page_url: str
    video: Stream
    audio: Stream | None
    page_index: int = 1
    requested_quality: str = "1080"
    stream_source: str = ""

    @property
    def quality_text(self) -> str:
        stream = self.video
        size = f"{stream.width}x{stream.height}" if stream.width else "原始尺寸"
        return f"{stream.quality_label} ({size}, {stream.codec})"


def extract_video_url(text: str) -> str:
    """从一段夹杂描述的文字里提取第一个 B 站视频链接。"""
    text = (text or "").strip()
    if not text:
        raise BiliError("没有输入视频链接。")

    match = B23_RE.search(text) or BILI_VIDEO_URL_RE.search(text)
    if match:
        return match.group(0).rstrip("，。！？、；：,.!?;:）】》)\"'")

    match = BV_RE.search(text)
    if match:
        return f"https://www.bilibili.com/video/{match.group(0)}"

    match = AV_RE.search(text)
    if match:
        return f"https://www.bilibili.com/video/av{match.group(1)}"

    raise BiliError(
        "没有识别到 B 站视频链接。支持：\n"
        "  https://www.bilibili.com/video/BVxxxxxxxxxx\n"
        "  https://b23.tv/xxxxxxx\n"
        "  BVxxxxxxxxxx 或 av123456"
    )


def parse_video_ref(url: str) -> VideoRef:
    parsed = urlparse(url)
    path = parsed.path
    ref = VideoRef()

    match = re.search(r"/video/(BV[0-9A-Za-z]{10})", path, re.I)
    if match:
        ref.bvid = match.group(0).split("/")[-1]
    else:
        match = re.search(r"/video/av(\d+)", path, re.I)
        if match:
            ref.aid = int(match.group(1))

    if not ref.bvid and not ref.aid:
        match = BV_RE.search(url)
        if match:
            ref.bvid = match.group(0)
        else:
            match = AV_RE.search(url)
            if match:
                ref.aid = int(match.group(1))

    if not ref.bvid and not ref.aid:
        raise BiliError(f"无法从链接中解析 BV/av 号：{url}")

    page = parse_qs(parsed.query).get("p", ["1"])[0]
    try:
        ref.page = max(1, int(page))
    except ValueError:
        ref.page = 1

    if ref.bvid:
        ref.page_url = f"https://www.bilibili.com/video/{ref.bvid}/"
    else:
        ref.page_url = f"https://www.bilibili.com/video/av{ref.aid}/"
    if ref.page > 1:
        ref.page_url += f"?p={ref.page}"
    return ref


class BilibiliClient:
    def __init__(self, cookie: str = "", timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.bilibili.com/",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        if cookie:
            self.session.headers["Cookie"] = cookie

    # ------------------------------------------------------------------
    # 视频信息
    # ------------------------------------------------------------------

    def fetch(self, url: str, quality: str = "1080") -> VideoInfo:
        source = extract_video_url(url)
        if "b23.tv" in source.lower():
            source = self._resolve_short_url(source)

        ref = parse_video_ref(source)
        page_url = ref.page_url
        html = self._get_html(page_url)

        meta_title, meta_desc = self._parse_meta(html)
        playinfo = self._parse_playinfo(html)

        try:
            view = self._get_view(ref)
        except BiliError as exc:
            warn(f"视频信息接口失败，尝试只根据页面解析：{exc}")
            view = {}

        pages = view.get("pages") or []
        page_data: dict[str, Any] = {}
        if pages:
            if ref.page > len(pages):
                raise BiliError(f"视频只有 {len(pages)} 个分P，请求的是 P{ref.page}")
            page_data = pages[ref.page - 1]

        bvid_from_api = str(view.get("bvid") or ref.bvid or "")
        aid = int(view.get("aid") or ref.aid or 0)
        bvid = bvid_from_api or (f"av{aid}" if aid else "unknown")
        cid = int(page_data.get("cid") or view.get("cid") or 0)
        if not cid:
            cid = self._parse_cid_from_html(html)
        if not cid:
            raise BiliError("没有拿到视频 cid，无法获取播放地址。")

        title = meta_title or str(view.get("title") or bvid or "未命名视频")
        part_title = str(page_data.get("part") or "").strip()
        if len(pages) > 1 and part_title and part_title not in title:
            title = f"{title} · P{ref.page} {part_title}"

        description = self._clean_description(meta_desc)
        api_description = self._clean_description(str(view.get("desc") or ""))
        if not description:
            description = api_description
        duration = float(
            page_data.get("duration")
            or view.get("duration")
            or self._parse_duration_from_html(html)
            or 0
        )

        uploader = str((view.get("owner") or {}).get("name") or "未知UP主")
        pubdate = ""
        raw_pubdate = view.get("pubdate")
        if raw_pubdate:
            try:
                pubdate = datetime.fromtimestamp(int(raw_pubdate)).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                pubdate = ""

        stream_source = "页面 __playinfo__"
        streams = self._streams_from_playinfo(playinfo, quality)
        if streams is None:
            stream_source = "播放地址接口"
            playdata = self._get_playurl(bvid_from_api, aid, cid, quality)
            streams = self._streams_from_playinfo(playdata, quality)
        if streams is None:
            raise BiliError("没有找到可下载的视频流（可能是付费/地区限制视频）。")

        video_stream, audio_stream = streams
        return VideoInfo(
            bvid=bvid,
            aid=aid,
            cid=cid,
            title=title,
            description=description,
            duration=duration,
            uploader=uploader,
            pubdate=pubdate,
            page_url=page_url,
            video=video_stream,
            audio=audio_stream,
            page_index=ref.page,
            requested_quality=quality,
            stream_source=stream_source,
        )

    def _resolve_short_url(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        response.raise_for_status()
        final_url = response.url or ""
        if "bilibili.com/video" in final_url:
            return final_url
        match = BILI_VIDEO_URL_RE.search(response.text) or BV_RE.search(response.text)
        if match:
            value = match.group(0)
            if value.startswith("BV"):
                return f"https://www.bilibili.com/video/{value}"
            return value
        raise BiliError(f"短链接跳转失败：{url}")

    def _get_view(self, ref: VideoRef) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ref.bvid:
            params["bvid"] = ref.bvid
        else:
            params["aid"] = ref.aid
        return self._api("https://api.bilibili.com/x/web-interface/view", params)

    def _get_playurl(
        self,
        bvid: str,
        aid: int,
        cid: int,
        quality: str,
    ) -> dict[str, Any]:
        attempts = [("4048", 120), ("16", 80), ("1", 64)]
        errors: list[str] = []
        for fnval, qn in attempts:
            params: dict[str, Any] = {
                "cid": cid,
                "qn": qn,
                "fnval": fnval,
                "fourk": 1,
            }
            if bvid:
                params["bvid"] = bvid
            else:
                params["avid"] = aid
            try:
                data = self._api(
                    "https://api.bilibili.com/x/player/playurl",
                    params,
                )
                if data.get("dash") or data.get("durl"):
                    return data
            except BiliError as exc:
                errors.append(str(exc))
        detail = "；".join(errors[-2:]) if errors else "接口没有返回 dash/durl"
        raise BiliError(
            "获取播放地址失败。该视频可能需要登录 Cookie、会员或存在地区限制。\n"
            f"      接口信息：{detail}"
        )

    # ------------------------------------------------------------------
    # HTTP / 解析
    # ------------------------------------------------------------------

    def _get_html(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 412:
            raise BiliError(
                "B站返回 412（风控）。请稍后重试，或提供有效 Cookie 后再试。"
            )
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text

    def _api(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise BiliError(f"接口返回的不是 JSON：{url}") from exc

        code = payload.get("code")
        if code not in (0, None):
            message = payload.get("message") or payload.get("msg") or "未知错误"
            hint = ""
            if code in (-352, -412, -799):
                hint = "；可能需要提供有效 Cookie，或稍后重试"
            elif code in (-10403, -404):
                hint = "；该视频可能需要登录/会员，或存在地区限制"
            raise BiliError(f"B站接口错误 {code}: {message}{hint}")
        return payload.get("data") or {}

    def _parse_meta(self, html: str) -> tuple[str, str]:
        title = ""
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if match:
            title = html_lib.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
            title = re.sub(
                r"[\s_\-|·]*哔哩哔哩[\s_\-|·]*bilibili\s*$",
                "",
                title,
                flags=re.I,
            ).strip()

        description = ""
        for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
            if not re.search(r'name\s*=\s*["\']description["\']', tag, re.I):
                continue
            match = re.search(r'content\s*=\s*["\'](.*?)["\']', tag, re.S | re.I)
            if match:
                description = html_lib.unescape(match.group(1))
                break

        return title, description

    @staticmethod
    def _parse_playinfo(html: str) -> dict[str, Any] | None:
        marker = "window.__playinfo__"
        index = html.find(marker)
        if index < 0:
            return None
        equals = html.find("=", index)
        if equals < 0:
            return None
        raw = html[equals + 1 :].lstrip()
        if not raw.startswith("{"):
            return None
        try:
            payload, _ = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _parse_cid_from_html(html: str) -> int:
        match = re.search(r'"cid"\s*:\s*(\d+)', html)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _parse_duration_from_html(html: str) -> int:
        for pattern in (r'"duration"\s*:\s*(\d+)', r'"timelength"\s*:\s*(\d+)'):
            match = re.search(pattern, html)
            if match:
                value = int(match.group(1))
                return value // 1000 if value > 100000 else value
        return 0

    @staticmethod
    def _clean_html_text(text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", text or "")
        text = html_lib.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _clean_description(cls, text: str) -> str:
        """去掉 B 站 meta description 里的播放量、相关视频等 SEO 尾巴。"""
        text = cls._clean_html_text(text)
        text = re.sub(r"[,，]\s*视频播放量.*$", "", text, flags=re.S)
        text = re.sub(r"\s*视频作者.*$", "", text, flags=re.S)
        text = re.sub(r"\s*作者简介.*$", "", text, flags=re.S)
        return text.strip(" -,，。;；")

    # ------------------------------------------------------------------
    # 流选择
    # ------------------------------------------------------------------

    def _streams_from_playinfo(
        self,
        payload: dict[str, Any] | None,
        quality: str,
    ) -> tuple[Stream, Stream | None] | None:
        if not payload:
            return None
        data = payload.get("data") if "data" in payload else payload
        if not isinstance(data, dict):
            return None

        dash = data.get("dash")
        if isinstance(dash, dict):
            videos = dash.get("video") or []
            audios = dash.get("audio") or []
            if videos:
                video = self._pick_video(videos, quality)
                audio = self._pick_audio(audios)
                return video, audio

        durl = data.get("durl")
        if isinstance(durl, list) and durl:
            if len(durl) > 1:
                raise BiliError("该视频返回了分段 durl，当前版本暂不支持。")
            item = durl[0]
            url = item.get("url")
            if not url:
                return None
            return (
                Stream(
                    url=url,
                    quality_id=int(data.get("quality") or 0),
                    codecid=7,
                    width=0,
                    height=0,
                ),
                None,
            )
        return None

    @staticmethod
    def _pick_video(videos: list[dict[str, Any]], quality: str) -> Stream:
        usable = [
            video
            for video in videos
            if video.get("baseUrl") or video.get("base_url")
        ]
        if not usable:
            raise BiliError("视频流列表为空。")

        limit = QUALITY_IDS.get(str(quality), 80)
        if quality == "best":
            limit = 127

        under = [video for video in usable if int(video.get("id") or 0) <= limit]
        if under:
            best_quality = max(int(video.get("id") or 0) for video in under)
            candidates = [
                video
                for video in under
                if int(video.get("id") or 0) == best_quality
            ]
            # 同一清晰度下优先 H.264，兼容性最好；没有则用 HEVC/AV1。
            avc = [
                video
                for video in candidates
                if int(video.get("codecid") or 0) == 7
            ]
            chosen = max(
                avc or candidates,
                key=lambda video: int(video.get("bandwidth") or 0),
            )
        else:
            chosen = min(usable, key=lambda video: int(video.get("id") or 0))

        base_url = chosen.get("baseUrl") or chosen.get("base_url")
        backups = chosen.get("backupUrl") or chosen.get("backup_url") or []
        return Stream(
            url=str(base_url),
            quality_id=int(chosen.get("id") or 0),
            codecid=int(chosen.get("codecid") or 0),
            width=int(chosen.get("width") or 0),
            height=int(chosen.get("height") or 0),
            bandwidth=int(chosen.get("bandwidth") or 0),
            backup_urls=[str(url) for url in backups if url],
        )

    @staticmethod
    def _pick_audio(audios: list[dict[str, Any]]) -> Stream | None:
        usable = [
            audio
            for audio in audios
            if audio.get("baseUrl") or audio.get("base_url")
        ]
        if not usable:
            return None
        chosen = max(
            usable,
            key=lambda audio: (
                int(audio.get("bandwidth") or 0),
                int(audio.get("id") or 0),
            ),
        )
        base_url = chosen.get("baseUrl") or chosen.get("base_url")
        backups = chosen.get("backupUrl") or chosen.get("backup_url") or []
        return Stream(
            url=str(base_url),
            quality_id=int(chosen.get("id") or 0),
            codecid=int(chosen.get("codecid") or 0),
            bandwidth=int(chosen.get("bandwidth") or 0),
            backup_urls=[str(url) for url in backups if url],
        )

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def download(
        self,
        info: VideoInfo,
        cache_dir: Path,
        fresh: bool = False,
    ) -> tuple[Path, Path | None]:
        cache_dir.mkdir(parents=True, exist_ok=True)
        video_path = cache_dir / "video.mp4"
        audio_path = cache_dir / "audio.m4a"

        self._download_with_refresh(
            info,
            info.video,
            video_path,
            "视频",
            fresh,
        )

        audio_result: Path | None = None
        if info.audio is not None:
            try:
                self._download_with_refresh(
                    info,
                    info.audio,
                    audio_path,
                    "音频",
                    fresh,
                )
                audio_result = audio_path
            except BiliError as exc:
                warn(f"音频下载失败：{exc}")
                warn("将尝试直接从视频文件识别（仅当视频本身带音轨时可行）。")
                audio_result = None
        elif not fresh and audio_path.is_file() and audio_path.stat().st_size > 1024:
            audio_result = audio_path

        return video_path, audio_result

    def _download_with_refresh(
        self,
        info: VideoInfo,
        stream: Stream,
        path: Path,
        label: str,
        fresh: bool,
    ) -> None:
        if path.is_file() and path.stat().st_size > 1024 and not fresh:
            return

        try:
            self._download_stream(stream, path, info.page_url, label)
        except MediaExpired:
            warn(f"{label}的 CDN 链接已过期，重新获取播放地址...")
            new_info = self.fetch(info.page_url, quality=info.requested_quality)
            info.video = new_info.video
            info.audio = new_info.audio
            stream = info.video if label == "视频" else info.audio
            if stream is None:
                raise BiliError(f"{label}流不可用")
            self._download_stream(stream, path, info.page_url, label)

    def _download_stream(
        self,
        stream: Stream,
        path: Path,
        page_url: str,
        label: str,
    ) -> None:
        urls = [stream.url, *stream.backup_urls]
        errors: list[str] = []
        for index, url in enumerate(urls, start=1):
            tmp = path.with_name(path.name + ".part")
            tmp.unlink(missing_ok=True)
            try:
                headers = {
                    "Referer": page_url,
                    "Accept-Encoding": "identity",
                }
                with self.session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(15, 120),
                ) as response:
                    if response.status_code in (401, 403, 404, 410):
                        raise MediaExpired(
                            f"{label}下载返回 HTTP {response.status_code}"
                        )
                    response.raise_for_status()
                    total = int(response.headers.get("Content-Length") or 0)
                    done = 0
                    with open(tmp, "wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            done += len(chunk)

                if total and done != total:
                    raise BiliError(
                        f"{label}下载不完整：{done}/{total} 字节"
                    )
                if done < 1024:
                    raise BiliError(f"{label}文件过小，可能下载失败。")
                tmp.replace(path)
                return
            except MediaExpired:
                tmp.unlink(missing_ok=True)
                raise
            except requests.RequestException as exc:
                tmp.unlink(missing_ok=True)
                errors.append(f"线路{index}: {exc}")
                if index < len(urls):
                    warn(f"{label}线路{index}失败，尝试备用地址...")
                    continue
            except Exception:
                tmp.unlink(missing_ok=True)
                raise

        raise BiliError(f"{label}下载失败：{'；'.join(errors[-2:])}")
