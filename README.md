# Universal Extractor — 通用网页内容提取器

5 层降级链，对任意网页自动选择最佳策略提取正文。

```
extract(url)
  ├── ① DOM 提取          → 直接从页面拿文字
  ├── ② API 拦截           → 从 XHR/Fetch 响应中提取
  ├── ③ Canvas Hook        → 拦截 Canvas fillText 获取绑图文字
  ├── ④ CDP 内存扫描       → 扫 JS 堆内存找文本对象
  └── ⑤ 截图 + OCR         → 截图后用 AI/OCR 识别文字
```

## 安装

```bash
pip install scrapling[all] pillow pytesseract openai python-dotenv
```

OCR 层需要以下之一：
- **DeepSeek**（推荐）：在 `.env` 中设置 `DEEPSEEK_KEY=sk-xxx`
- **Tesseract**（本地）：[下载安装](https://github.com/UB-Mannheim/tesseract/wiki) + 中文语言包

## 使用

```python
from universal_extractor import UniversalExtractor

ue = UniversalExtractor(headless=True)
text = ue.extract("https://example.com/article")
print(text)
```

## 适用场景

| 网站类型 | 生效层 | 提取率 |
|---------|-------|-------|
| 博客、新闻站 | ① DOM | ~100% |
| SPA（React/Vue） | ① DOM | ~95% |
| BOSS直聘等招聘站 | ①+② API | ~95% |
| 普通 Canvas 页面 | ③ Hook | ~90% |
| WPS/飞书/腾讯文档 | ①（降级） | ~20%（仅大纲） |

## 架构

每层返回文字或 None，链式降级：

- **① DOM**：`document.body.innerText` + 多选择器降级
- **② API**：`page.on("response")` 拦截 JSON/HTML 类型的 XHR
- **③ Canvas Hook**：CDP `Page.addScriptToEvaluateOnNewDocument` 注入 fillText 钩子
- **④ CDP**：`Runtime.evaluate` 遍历 window 属性 + localStorage
- **⑤ OCR**：逐屏截图 → DeepSeek Vision / Tesseract 识别

完整性检测自动判断文字是否足够（段落数、句子密度、大纲识别、JSON 误判过滤）。

## 技术栈

- **Scrapling 0.4.9**：浏览器自动化（Playwright Chromium）
- **CDP**（Chrome DevTools Protocol）：底层注入与内存扫描
- **DeepSeek API**：云端 OCR（可选）
- **Tesseract**：本地 OCR（可选）

## 项目结构

```
universal_extractor/
  __init__.py      # 公开 API
  extractor.py     # 主类，5 层逻辑（~400 行）
  demo.py          # 使用示例
  README.md        # 本文件
```

## License

MIT

🔗 https://github.com/2913636/universal_extractor
