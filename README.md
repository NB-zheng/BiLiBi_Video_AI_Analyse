# B站视频多模态分析器

把一条 B 站视频自动整理成一份带时间戳、内容梳理的 Markdown / PDF 报告；关键截图用于分段多模态分析，保存在缓存目录。

流程：

```
B站链接
  → 下载视频流 + 音频流
  → faster-whisper 语音识别（带时间戳）
  → 按语义分段，每段挑 1 张关键画面
  → 分段发给多模态模型（文字 + 截图）
  → 最终模型整合
  → Markdown + PDF
```

没有前端，全部在命令行完成；关键步骤和耗时都会打印出来。

---

## 1. 目录结构

```
.
├── main.py                     # 入口
├── requirements.txt
├── README.md
├── .env.example                # 配置模板
├── .gitignore
├── bili_analyzer/
│   ├── cli.py                  # 命令行参数与交互
│   ├── config.py               # 运行配置
│   ├── bilibili.py             # 链接解析、页面/API、音视频下载
│   ├── asr.py                  # faster-whisper 转写
│   ├── chunking.py             # 按句子边界分段
│   ├── frames.py               # 选帧策略 + ffmpeg 截图
│   ├── prompts.py              # 分段/整合提示词
│   ├── llm.py                  # OpenAI 兼容多模态接口
│   ├── pipeline.py             # 主流程编排
│   ├── pdf_export.py           # Markdown → PDF（PyMuPDF / Edge）
│   └── utils.py
├── model/                      # 本地 whisper 模型
└── output/
    ├── <标题>.md               # 最终 Markdown
    ├── <标题>.pdf              # 最终 PDF
    └── cache/<标题>/           # 所有中间产物
```

---

## 2. 安装

需要 Python 3.10+（测试于 3.12）。首次使用先创建虚拟环境并安装依赖（如果已有 `venv`，跳过创建）：

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

如果 PowerShell 禁止运行激活脚本，可以不激活，直接使用 `.\venv\Scripts\python.exe main.py`。

faster-whisper 模型不会放进仓库；首次运行会自动下载到 `model/`（`small` 约 500MB）。网络慢时可以先用 `--asr-model tiny` 试跑，或手动准备模型目录。

国内网络如果 pip 慢，可以用镜像：

```powershell
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

还需要 **ffmpeg**（截图用）：

```powershell
ffmpeg -version
```

如果命令不存在，安装后把 `bin` 目录加入 PATH，或在 `.env` 里写：

```dotenv
FFMPEG_BIN=C:\Program Files\ffmpeg\bin\ffmpeg.exe
```

---

## 3. 配置模型

复制配置模板：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少填这三项。模板默认使用 DeepSeek：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_API_KEY=sk-...
```

接口按 **OpenAI 兼容的 `/chat/completions`** 调用，图片以 base64 data URL 传入。常见服务商：

| 服务商 | LLM_BASE_URL 示例 | 多模态模型示例 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | `deepseek-flash`（支持图片，推理模型） |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-vl-max` |
| 智谱 AI | `https://open.bigmodel.cn/api/paas/v4` | `glm-4v-plus` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | 以控制台模型名为准 |
| 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` | 以控制台模型名为准 |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k-vision-preview` |
| OpenRouter | `https://openrouter.ai/api/v1` | 以控制台模型名为准 |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5vl` 等 |

注意：

- `LLM_BASE_URL` 按服务商要求填写；OpenAI 兼容服务通常带 `/v1`，DeepSeek 用 `https://api.deepseek.com`。
- 模型必须支持图片输入。
- 本地 Ollama 可以不填 `LLM_API_KEY`。

### B站 Cookie（可选）

不填也能跑，但匿名访问通常最高只有 480P。想要 1080P，建议提供 Cookie：

- 方式一：浏览器登录 B站，开发者工具 → Network → 任意请求 → 复制完整 `Cookie`，写进 `.env` 的 `BILI_COOKIE`。
- 方式二：把完整 Cookie 保存到项目根目录的 `cookie.txt`。
- 方式三：命令行 `--cookie "SESSDATA=...; bili_jct=..."`。

Cookie 只保存在本地，程序不会上传。请勿把 `.env` 或 `cookie.txt` 提交到公开仓库。

---

## 4. 使用

### 直接传链接

```powershell
python main.py "https://www.bilibili.com/video/BV1xxxxxxxxx/"
```

链接可以夹在一段文字里，程序会自动提取第一个 B 站链接；也支持 `b23.tv` 短链、`BV号`、`av号`。

### 交互式运行

```powershell
python main.py
```

会依次提示输入视频链接和 Cookie（Cookie 输入不回显，直接回车可匿名）。

### 先验证流程，不调用模型

```powershell
python main.py "https://www.bilibili.com/video/BV1xxxxxxxxx/" --mock
```

`--mock` 会走完下载、识别、截图、分段、报告、PDF，只是用占位内容代替模型输出。适合先检查环境，或没有 API Key 时试跑。

> Mock 生成的分段笔记也会写入 `segments/` 缓存。之后接入真实模型时请加 `--force-llm`，避免复用占位笔记。

### 常用参数

```powershell
# 不截图，只做文字分析
python main.py "<链接>" --no-shots

# 每段最多 2 张图，全片最多 20 张
python main.py "<链接>" --shots-per-segment 2 --max-shots 20

# 使用 tiny 模型（更快、精度低）
python main.py "<链接>" --asr-model tiny

# 英文视频 / 自动检测语言
python main.py "<链接>" --language en
python main.py "<链接>" --language auto

# 忽略音视频和转写缓存，重新识别
python main.py "<链接>" --fresh

# 忽略已生成的分段笔记，重新调用模型
python main.py "<链接>" --force-llm

# 只生成 Markdown，不生成 PDF
python main.py "<链接>" --pdf-engine none
```

完整参数：`python main.py --help`。

---

## 5. 输出

最终只保留两份文件，其余都在 `output/cache/<标题>/`：

```
output/
├── <标题>.md                  # 最终整合报告（不含关键画面附录）
├── <标题>.pdf                 # 最终 PDF
└── cache/
    └── <标题>/
        ├── media/
        │   ├── video.mp4      # 视频流（无声）
        │   └── audio.m4a      # 音频流
        ├── transcript.json    # 转写缓存（带时间戳）
        ├── transcript.md      # 可读的完整转写
        ├── meta.json          # 视频信息、模型、分段/截图数量
        ├── shots.json         # 每张截图的时间、原因、对应台词
        ├── frames/            # 关键截图
        │   └── shot_001_00-01-41.jpg
        └── segments/          # 每个分段的模型笔记
            └── segment_01.md
```

标题会做 Windows 文件名清洗（去掉 `<>:"/\|?*` 等字符，过长截断到 80 字符），其余保持不变。

视频流和音频流分开保存，不额外合并：截图用视频流，语音识别用音频流，少一次转码、也省空间。

重新运行时会复用 `output/cache/<标题>/` 里的媒体、转写和分段笔记，除非加 `--fresh` 或 `--force-llm`。

> 直接传链接时，如果既没有 `--cookie`、环境变量，也没有 `cookie.txt`，程序会按匿名方式继续，不会停下来提问。需要交互输入 Cookie 时，不带链接运行 `python main.py` 即可。

---

## 6. 关键设计

### 分段

- 按**句子边界**切分，不会从一句话中间截断。
- 默认每段目标 5 分钟、最多 1800 字；两者任一先到就切。
- 长视频会切成多段，每段单独发给多模态模型，避免上下文超限。

### 截图（尽量少）

没有按固定间隔、每句话或场景变化抽帧。默认策略是：

- **每个分段只截 1 张**，全片最多 12 张。
- 在该段内找“最像在展示画面”的句子：包含「画面、屏幕、图表、代码、左边、右边、这里、看」等关键词的句子优先。
- 如果整段都没有画面关键词，就取段落中点附近的一句。
- 截图时间取该句开始后 0.25～0.8 秒，尽量避开转场。

这样 10 分钟视频通常只有 1～2 张图，30 分钟视频约 5～8 张。需要更多画面时：

```powershell
--shots-per-segment 2 --max-shots 20
```

### 模型调用

- 分段消息：系统提示 + 本段带时间戳转写 + 关键截图 + 固定输出结构。
- 最终消息：视频元信息 + 所有分段笔记 + 完整转写（过长会截断）+ 整合要求。
- 最终整合只发文字，不重复发送截图，避免同一张图被计费两次；画面信息已经写进分段笔记。
- 每段结果会写入 `segments/`，失败或中断后重跑可复用。
- 最终整合失败时，程序会把分段笔记直接拼成报告，仍然生成 Markdown/PDF。

### PDF

- 优先用 **PyMuPDF Story**：把 Markdown 转 HTML，嵌入中文字体，分页渲染。
- PyMuPDF 失败时自动尝试 **Edge/Chrome 无头打印**。
- 最终报告故意不放“关键画面”附录；截图只参与分段分析，原图保留在 `output/cache/<标题>/frames/`。

---

## 7. 常见问题

**清晰度只有 480P**

匿名访问的限制。配置 B站 Cookie 后重新运行即可。

**下载 403 / CDN 链接过期**

程序会自动刷新一次播放地址。如果仍失败，重新运行；已下载完成的文件会跳过。

**提示找不到 ffmpeg**

安装 ffmpeg 并加入 PATH，或在 `.env` 设置 `FFMPEG_BIN`。截图依赖它，音频识别不依赖。

**PDF 中文变成方框**

Windows 默认使用 `C:\Windows\Fonts\simhei.ttf`。其他系统可在 `.env` 设置：

```dotenv
PDF_FONT_PATH=/path/to/your/chinese-font.ttf
```

也可以改用 `--pdf-engine edge`。

**模型报 400 / 图片格式错误**

确认模型支持视觉输入，并且 `LLM_BASE_URL` 是 OpenAI 兼容地址。部分服务商的图片字段或模型名与 OpenAI 不完全一致，需要按服务商文档调整 `LLM_MODEL`。

**模型返回空内容 / 报“输出额度被 reasoning 占满”**

`deepseek-flash` 这类推理模型会先输出 `reasoning_content`，它和最终 `content` 共用 `max_tokens`。如果思考过程用完额度，接口会返回 `content` 为空、`finish_reason=length`，看起来就是“模型返回了空内容”。

程序现已处理：

- 默认每段 8000 tokens、最终整合 16000 tokens；
- 检测到上述情况时，自动把输出上限翻倍重试，最高 32768；
- 仍失败时，可手动提高：

```powershell
--segment-max-tokens 16000 --final-max-tokens 32000
```

或者换用非推理的多模态模型。注意推理 token 也会计费，额度不是越高越好。

**模型报上下文超限**

减小分段：

```powershell
--chunk-seconds 180 --chunk-chars 1200
```

同时减少每段截图数 `--shots-per-segment 1`。

**转写有繁体字或错字**

`small` 模型在中文上偶尔会输出繁体或听错专有名词。可以换 `--asr-model medium`（更慢、更准），或在提示词/报告阶段人工校对。转写原文保存在 `transcript.md`。

**首次运行很慢**

第一次会加载/下载 faster-whisper 模型；CPU 识别速度约为音频时长的 1/3～1/2。之后会复用 `cache/`。

---

## 8. 成本与时间粗估

以 30 分钟、中文口播视频为例：

| 阶段 | 大概耗时 | 说明 |
| --- | --- | --- |
| 下载 | 10～60 秒 | 取决于清晰度和网络 |
| 语音识别 | 10～20 分钟 | CPU，small 模型 |
| 截图 | 几秒 | 5～8 张 |
| 分段模型 | 3～8 次请求 | 每段 1 张图 + 约 1500 字 |
| 最终整合 | 1 次请求 | 约 1～2 万 token 输入 |

模型费用取决于服务商单价；图片按 base64 计入输入 token。想省钱就减少 `--max-shots` 和 `--shots-per-segment`。

---

## 9. 说明

- 仅用于个人学习、研究和内容整理；请遵守 B站服务条款和视频版权，不要批量抓取或二次分发受版权保护的内容。
- 程序不会绕过付费/会员/地区限制；遇到这类视频会明确报错。
- 模型输出可能有事实错误，发布前请核对 `transcript.md` 和原视频。
