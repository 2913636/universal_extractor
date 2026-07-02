"""
通用网页内容提取器 — 5 层降级链。

① DOM 提取 → ② API 拦截 → ③ Canvas Hook → ④ CDP 内存扫描 → ⑤ 截图 OCR

用法:
    from universal_extractor import UniversalExtractor

    ue = UniversalExtractor(headless=False)
    text = ue.extract("https://www.kdocs.cn/l/chtgPO02obP9")
    print(text)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# --- Scrapling imports ---
from scrapling import StealthyFetcher

# --- Encoding fallback chain ---
_ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "big5"]

# --- Completeness thresholds ---
MIN_TEXT_LENGTH = 100
MIN_PARAGRAPHS = 3
MIN_SENTENCE_MARKERS = 10
BOILERPLATE_KEYWORDS = [
    "登录", "login", "注册", "register", "copyright",
    "cookie", "隐私", "privacy", "条款", "Sign in",
]

logger = logging.getLogger(__name__)


# ============================================================
# Data classes
# ============================================================

@dataclass
class ExtractionResult:
    text: str
    source_layer: int       # 1–5
    method: str             # 'dom' | 'api' | 'canvas_hook' | 'cdp_heap' | 'ocr'
    confidence: float = 0.5  # 0.0–1.0 heuristic


class ExtractionError(Exception):
    """All layers returned empty or insufficient text."""


# ============================================================
# Canvas Hook JS（injected via init_script）
# ============================================================

CANVAS_HOOK_JS = """
// Universal Extractor — Canvas text interceptor
(function() {
    if (window.__ueCanvasTexts) return;  // already injected
    window.__ueCanvasTexts = [];

    function record(text) {
        if (typeof text === 'string' && text.trim().length > 0) {
            window.__ueCanvasTexts.push(text.trim());
            // Prevent memory blow-up on infinite renders
            if (window.__ueCanvasTexts.length > 50000) {
                window.__ueCanvasTexts = window.__ueCanvasTexts.slice(-25000);
            }
        }
    }

    // Hook CanvasRenderingContext2D.fillText
    var origFillText = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, maxWidth) {
        record(text);
        return origFillText.call(this, text, x, y, maxWidth);
    };

    // Hook CanvasRenderingContext2D.strokeText
    var origStrokeText = CanvasRenderingContext2D.prototype.strokeText;
    CanvasRenderingContext2D.prototype.strokeText = function(text, x, y, maxWidth) {
        record(text);
        return origStrokeText.call(this, text, x, y, maxWidth);
    };

    // Hook OffscreenCanvasRenderingContext2D (used in Web Workers)
    if (typeof OffscreenCanvasRenderingContext2D !== 'undefined') {
        var origOffFill = OffscreenCanvasRenderingContext2D.prototype.fillText;
        OffscreenCanvasRenderingContext2D.prototype.fillText = function(text, x, y, maxWidth) {
            record(text);
            return origOffFill.call(this, text, x, y, maxWidth);
        };
        var origOffStroke = OffscreenCanvasRenderingContext2D.prototype.strokeText;
        OffscreenCanvasRenderingContext2D.prototype.strokeText = function(text, x, y, maxWidth) {
            record(text);
            return origOffStroke.call(this, text, x, y, maxWidth);
        };
    }
})();
"""


# ============================================================
# Utility functions
# ============================================================

def _decode(body: bytes, hint: str | None = None) -> str:
    """Decode bytes to string with multi-encoding fallback."""
    encodings = [hint] if hint else []
    encodings += _ENCODINGS
    for enc in encodings:
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _is_complete(text: str, min_length: int = MIN_TEXT_LENGTH) -> bool:
    """Heuristic: is this text the full page content or just UI chrome / outline?"""
    if not text or len(text.strip()) < min_length:
        return False
    text = text.strip()

    # Detect outline/TOC: lines are mostly short (outline items) → need more evidence
    lines = [l for l in text.split("\n") if l.strip()]
    if lines:
        avg_len = sum(len(l) for l in lines) / len(lines)
        long_lines = sum(1 for l in lines if len(l) > 80)
        # Outline signal: average line < 60 chars AND few long lines
        is_outline_like = avg_len < 60 and long_lines < len(lines) * 0.2
        if is_outline_like and len(text) < 500:
            return False  # Looks like a TOC, not full content

    # Has multiple paragraphs
    para_count = len(re.findall(r"\n\s*\n", text))
    if para_count >= MIN_PARAGRAPHS:
        return True

    # Rich sentence structure
    sentences = len(re.findall(r"[。！？.!?\n]", text))
    if sentences >= MIN_SENTENCE_MARKERS:
        return True

    # Long enough
    if len(text) >= min_length * 5:
        return True

    # Check for boilerplate (login pages, cookie walls, etc.)
    boilerplate_hits = sum(1 for kw in BOILERPLATE_KEYWORDS if kw.lower() in text[:300].lower())
    if boilerplate_hits >= 3 and len(text) < 500:
        return False

    return False


def _dig_for_text(data: Any, depth: int = 0, max_depth: int = 5) -> str | None:
    """Recursively walk JSON looking for the longest text field."""
    if depth > max_depth or data is None:
        return None
    candidates = []
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and len(v) > 50 and not _looks_like_code(v):
                candidates.append(v)
            else:
                sub = _dig_for_text(v, depth + 1, max_depth)
                if sub:
                    candidates.append(sub)
    elif isinstance(data, list) and len(data) < 500:
        for item in data:
            sub = _dig_for_text(item, depth + 1, max_depth)
            if sub:
                candidates.append(sub)
    return max(candidates, key=len) if candidates else None


def _looks_like_code(text: str) -> bool:
    """Quick check: is this JS/JSON/CSS rather than human content?"""
    code_marks = ["function(", "=>", "=== ", "typeof", "import {", "export ",
                  "constructor(", "super(", "require("]
    return any(m in text for m in code_marks)


def _clean_ocr_text(text: str) -> str:
    """Post-process OCR output: remove common artifacts, merge broken lines."""
    # Remove repeated whitespace
    text = re.sub(r" {3,}", "  ", text)
    # Merge hyphenated line breaks that OCR splits
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Remove >3 consecutive newlines
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


# ============================================================
# Main class
# ============================================================

class UniversalExtractor:
    """Five-layer fallback web page text extractor.

    Parameters:
        headless: Run browser headless (True) or visible (False).
        timeout: Browser session timeout in milliseconds.
        ocr_backend: ``"deepseek"`` or ``"tesseract"``.
        deepseek_key: DeepSeek API key (reads ``DEEPSEEK_API_KEY`` env var if None).
        deepseek_model: Model name for DeepSeek vision OCR.
        tesseract_cmd: Path to tesseract.exe (only needed for tesseract backend).
    """

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 120_000,
        ocr_backend: str = "deepseek",
        deepseek_key: str | None = None,
        deepseek_model: str = "deepseek-chat",
        tesseract_cmd: str | None = None,
    ):
        self.headless = headless
        self.timeout = timeout
        self.ocr_backend = ocr_backend

        # Resolve DeepSeek API key: explicit arg > env var > job-hunter .env
        if deepseek_key:
            self.deepseek_key = deepseek_key
        elif os.getenv("DEEPSEEK_KEY") or os.getenv("DEEPSEEK_API_KEY"):
            self.deepseek_key = (os.getenv("DEEPSEEK_KEY") or os.getenv("DEEPSEEK_API_KEY") or "")
        else:
            # Try loading from job-hunter .env
            try:
                from dotenv import load_dotenv
                for env_path in ["D:/job-hunter/.env", ".env"]:
                    if os.path.exists(env_path):
                        load_dotenv(env_path)
                        break
            except ImportError:
                pass
            self.deepseek_key = (os.getenv("DEEPSEEK_KEY") or os.getenv("DEEPSEEK_API_KEY") or "")

        self.deepseek_model = deepseek_model
        self.tesseract_cmd = tesseract_cmd

        # Temp files are written here and cleaned on each extract() call
        self._temp_root: Path | None = None

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    def extract(self, url: str) -> str:
        """Run the 5-layer fallback chain and return the best text."""
        self._temp_root = Path(tempfile.mkdtemp(prefix="ue_"))
        profile_dir = Path(tempfile.mkdtemp(prefix="ue_profile_"))
        results: list[ExtractionResult] = []

        try:
            # ---- Phase 1: Quick DOM-only attempt (fast path) ----
            print("Layer ①: Quick DOM extraction...")
            dom_text = self._quick_dom(url, profile_dir)
            if dom_text:
                results.append(ExtractionResult(dom_text, 1, "dom",
                                confidence=0.9 if _is_complete(dom_text) else 0.3))
            if _is_complete(dom_text):
                print("Layer ① complete — returning early.")
                return dom_text.strip()

            # ---- Phase 2: Full extraction with all hooks ----
            print("Layers ②–⑤: Full extraction...")
            self._full_extraction(url, profile_dir, results)

        except Exception as exc:
            logger.error(f"Extraction error: {exc}")
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
            shutil.rmtree(str(self._temp_root), ignore_errors=True)

        return self._pick_best(results)

    # --------------------------------------------------------
    # Layer ① — Quick DOM
    # --------------------------------------------------------

    def _quick_dom(self, url: str, profile_dir: Path) -> str:
        """Lightweight fetch: run a single page_action, return DOM text."""
        collected: list[str] = []

        def action(page):
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            text = self._dom_extract(page)
            if text:
                collected.append(text)

        try:
            StealthyFetcher.fetch(
                url,
                headless=self.headless,
                timeout=30000,
                user_data_dir=str(profile_dir),
                page_action=action,
                network_idle=True,
            )
        except Exception:
            pass

        return collected[0] if collected else ""

    def _dom_extract(self, page) -> str:
        """Extract text from DOM with multi-selector fallback, then full body as backup."""
        return page.evaluate("""() => {
            const selectors = [
                'article', 'main', '.content', '.article-content',
                '#content', '.post-content', '[class*="doc"]',
                '[class*="editor"]', '[class*="page-content"]'
            ];
            let best = '';
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.innerText && el.innerText.trim().length > 100) {
                    const t = el.innerText.trim();
                    if (t.length > best.length) best = t;
                }
            }
            if (best.length > 200) return best;
            // Fallback: full body text
            return (document.body && document.body.innerText) ? document.body.innerText.trim() : '';
        }""")

    # --------------------------------------------------------
    # Phase 2 — Full extraction orchestration
    # --------------------------------------------------------

    def _full_extraction(
        self, url: str, profile_dir: Path, results: list[ExtractionResult]
    ) -> None:
        """Set up all hooks via page_setup, then run extraction via page_action."""
        captured_api_texts: list[str] = []
        hook_script_path: str = ""

        # Write the Canvas hook JS to a temp file for injection via page_setup
        hook_script_path = str(self._temp_root / "canvas_hook.js")
        Path(hook_script_path).write_text(CANVAS_HOOK_JS, encoding="utf-8")

        def setup(page):
            """page_setup: register interceptors before navigation."""
            # Layer ② — intercept API responses
            page.on("response", self._make_api_handler(captured_api_texts))

            # Layer ③ — inject Canvas hook via CDP (most reliable, bypasses all Playwright limits)
            try:
                cdp = page.context.new_cdp_session(page)
                cdp.send("Page.addScriptToEvaluateOnNewDocument",
                          {"source": CANVAS_HOOK_JS})
                cdp.detach()
            except Exception:
                # Fallback A: page.add_init_script
                try:
                    page.add_init_script(path=hook_script_path)
                except Exception:
                    # Fallback B: JS file interception
                    try:
                        def inject_into_js(route):
                            response = route.fetch()
                            body = response.body()
                            modified = CANVAS_HOOK_JS.encode() + b"\n" + body
                            route.fulfill(body=modified, headers={
                                **response.headers,
                                "content-length": str(len(modified)),
                            })
                        page.route("**/*.js", inject_into_js)
                    except Exception:
                        pass

        def action(page):
            """page_action: run all layers after page loads."""
            # Wait for full render
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(5000)

            # Dismiss login modals
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)

            # Layer ① — DOM (retry after full render)
            dom_text = self._dom_extract(page)
            if dom_text:
                results.append(ExtractionResult(dom_text, 1, "dom",
                                                confidence=0.9 if _is_complete(dom_text) else 0.3))

            # Layer ② — Check captured API responses
            for api_text in captured_api_texts:
                if api_text and len(api_text) > 50:
                    results.append(ExtractionResult(api_text, 2, "api",
                                                    confidence=0.7 if _is_complete(api_text) else 0.4))

            # Layer ③ — Collect Canvas hook texts
            canvas_text = self._canvas_collect(page)
            if canvas_text:
                results.append(ExtractionResult(canvas_text, 3, "canvas_hook",
                                                confidence=0.8))

            # Layer ④ — CDP heap scan
            cdp_text = self._cdp_scan(page)
            if cdp_text:
                results.append(ExtractionResult(cdp_text, 4, "cdp_heap",
                                                confidence=0.5))

            # Layer ⑤ — Screenshot + OCR
            ocr_text = self._ocr_extract(page)
            if ocr_text:
                results.append(ExtractionResult(ocr_text, 5, "ocr",
                                                confidence=0.6))

        try:
            StealthyFetcher.fetch(
                url,
                headless=self.headless,
                timeout=self.timeout,
                user_data_dir=str(profile_dir),
                page_setup=setup,
                page_action=action,
                network_idle=True,
                disable_resources=False,
            )
        except Exception as exc:
            logger.warning(f"Full extraction fetch error: {exc}")

    # --------------------------------------------------------
    # Layer ② — API Response Interception
    # --------------------------------------------------------

    def _make_api_handler(self, captured: list[str]) -> Callable:
        """Return a response handler that captures text from JSON/HTML APIs."""
        def on_response(response):
            try:
                url = response.url
                ctype = response.headers.get("content-type", "")

                # JSON APIs
                if "json" in ctype:
                    body = response.body()
                    text = body.decode("utf-8", errors="replace")
                    try:
                        data = json.loads(text)
                        extracted = _dig_for_text(data)
                        if extracted:
                            captured.append(extracted)
                    except (json.JSONDecodeError, ValueError):
                        pass

                # HTML APIs (might return content fragments)
                if "/api/" in url and "html" in ctype:
                    body = response.body()
                    decoded = _decode(body)
                    if len(decoded) > 200:
                        captured.append(decoded)
            except Exception:
                pass

        return on_response

    # --------------------------------------------------------
    # Layer ③ — Canvas Hook Collection
    # --------------------------------------------------------

    def _canvas_collect(self, page) -> str:
        """Collect text captured by the injected Canvas hook."""
        try:
            texts = page.evaluate("""() => {
                var ts = window.__ueCanvasTexts;
                if (!ts || ts.length === 0) return '';
                // Filter: keep meaningful text, remove single chars and numbers-only
                var valid = ts.filter(function(t) {
                    return t.length > 3 || /[一-鿿]/.test(t);
                });
                // Deduplicate consecutive repeats (Canvas often re-renders same text)
                var deduped = [];
                var last = '';
                for (var i = 0; i < valid.length; i++) {
                    if (valid[i] !== last) {
                        deduped.push(valid[i]);
                        last = valid[i];
                    }
                }
                return deduped.join('\\n');
            }""")
            return texts.strip() if texts else ""
        except Exception as exc:
            logger.warning(f"Layer ③ error: {exc}")
            return ""

    # --------------------------------------------------------
    # Layer ④ — CDP Memory Scan
    # --------------------------------------------------------

    def _cdp_scan(self, page) -> str:
        """Scan JS heap via Chrome DevTools Protocol for text objects."""
        try:
            cdp = page.context.new_cdp_session(page)

            result = cdp.send("Runtime.evaluate", {
                "expression": """
                    (function() {
                        var found = [];
                        // Walk window sub-objects (depth 3)
                        function walk(obj, depth) {
                            if (depth > 3 || !obj || typeof obj !== 'object') return;
                            try {
                                var keys = Object.getOwnPropertyNames(obj).slice(0, 50);
                                for (var i = 0; i < keys.length; i++) {
                                    try {
                                        var val = obj[keys[i]];
                                        if (typeof val === 'string' && val.length > 100 && val.length < 50000) {
                                            found.push(val);
                                        } else if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
                                            walk(val, depth + 1);
                                        }
                                    } catch(e) {}
                                }
                            } catch(e) {}
                        }
                        walk(window, 0);

                        // Check localStorage for cached text
                        try {
                            for (var j = 0; j < localStorage.length; j++) {
                                var v = localStorage.getItem(localStorage.key(j));
                                if (v && v.length > 100) found.push(v);
                            }
                        } catch(e) {}

                        // Check common SSR state globals
                        var globals = ['__NEXT_DATA__', '__NUXT__', '__INITIAL_STATE__'];
                        for (var k = 0; k < globals.length; k++) {
                            try {
                                var gv = window[globals[k]];
                                if (gv) found.push(JSON.stringify(gv).slice(0, 10000));
                            } catch(e) {}
                        }

                        return found.sort(function(a,b) { return b.length - a.length; }).slice(0, 5);
                    })()
                """,
                "returnByValue": True,
                "timeout": 10000,
            })

            cdp.detach()

            strings = result.get("result", {}).get("value", [])
            if strings and isinstance(strings, list):
                for s in strings:
                    if not isinstance(s, str) or len(s) < 200:
                        continue
                    # Reject JSON/config payloads (analytics event tracking config)
                    if s.strip().startswith("{") and ('"version"' in s[:200] or '"disable"' in s[:500]):
                        continue
                    if '"encryptAttrs"' in s[:500] or '"events"' in s[:500]:
                        continue
                    # Reject base64-encoded HTML (watermarks, templates)
                    if s.strip().startswith("PG") and len(s) > 500:
                        # Looks like base64 HTML content
                        continue
                    # Reject pure HTML/XML strings
                    if s.strip().startswith("<") and ("</" in s or "/>" in s):
                        continue
                    return s
        except Exception as exc:
            logger.warning(f"Layer ④ error: {exc}")
        return ""

    # --------------------------------------------------------
    # Layer ⑤ — Screenshot + OCR
    # --------------------------------------------------------

    def _ocr_extract(self, page) -> str:
        """Scroll through the page, take screenshots, and OCR each one."""
        if self.ocr_backend == "deepseek":
            return self._ocr_via_deepseek(page)
        return self._ocr_via_tesseract(page)

    def _ocr_via_deepseek(self, page) -> str:
        """Take screenshots and send to DeepSeek for text extraction."""
        if not self.deepseek_key:
            logger.warning("Layer ⑤ skipped: no DeepSeek API key.")
            return ""

        screenshot_paths = self._capture_views(page, max_views=6)
        if not screenshot_paths:
            return ""

        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("Layer ⑤ skipped: openai package not installed.")
            return ""

        client = OpenAI(
            api_key=self.deepseek_key,
            base_url="https://api.deepseek.com",
        )

        all_text: list[str] = []
        for path in screenshot_paths:
            try:
                import base64 as b64mod
                b64 = b64mod.b64encode(Path(path).read_bytes()).decode()

                # DeepSeek Chat may not support image_url natively.
                # Try image_url first; if it fails, fall back to text-only prompt
                # that simply asks to acknowledge (image input not supported).
                try:
                    response = client.chat.completions.create(
                        model=self.deepseek_model,
                        messages=[{
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "请提取这张截图中的所有文字内容，包括中文和英文。"
                                        "按照原文顺序输出，保留标题层级和段落结构。"
                                        "不要添加任何解释，只输出图片中的文字。"
                                    ),
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                                },
                            ],
                        }],
                        temperature=0.1,
                        max_tokens=2048,
                    )
                except Exception:
                    # Image not supported by this model; skip this screenshot
                    continue

                content = response.choices[0].message.content or ""
                if content.strip():
                    all_text.append(content.strip())
            except Exception as exc:
                logger.warning(f"DeepSeek OCR error for {path}: {exc}")

        return _clean_ocr_text("\n\n".join(all_text))

    def _ocr_via_tesseract(self, page) -> str:
        """Take screenshots and run local Tesseract OCR."""
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            logger.warning("Layer ⑤ skipped: pytesseract/Pillow not installed.")
            return ""

        screenshot_paths = self._capture_views(page, max_views=6)
        if not screenshot_paths:
            return ""

        all_text: list[str] = []
        for path in screenshot_paths:
            try:
                from PIL import Image
                import pytesseract

                if self.tesseract_cmd:
                    pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd

                img = Image.open(path).convert("L")
                config = "--oem 3 --psm 6 -l chi_sim+eng"
                text = pytesseract.image_to_string(img, config=config)
                if text.strip():
                    all_text.append(text.strip())
            except Exception as exc:
                logger.warning(f"Tesseract OCR error for {path}: {exc}")

        return _clean_ocr_text("\n\n".join(all_text))

    def _capture_views(self, page, max_views: int = 10) -> list[str]:
        """Scroll through the page, taking screenshots. Returns list of file paths."""
        paths: list[str] = []
        try:
            dims = page.evaluate("""() => ({
                h: Math.max(document.body.scrollHeight, 5000),
                vh: window.innerHeight || 900
            })""")
            total_h = min(dims.get("h", 5000), 20000)
            vh = dims.get("vh", 900)

            # Scroll with mouse wheel (more reliable than keyboard on Canvas pages)
            for step_num in range(max_views):
                y_offset = step_num * vh
                if y_offset >= total_h:
                    break

                # Scroll using JS (works even on Canvas-rendered pages)
                page.evaluate(f"window.scrollTo(0, {y_offset})")
                page.wait_for_timeout(800)

                filepath = str(self._temp_root / f"ocr_{step_num:03d}.png")
                page.screenshot(path=filepath, full_page=False)
                paths.append(filepath)

            # Also take one full-page screenshot as backup
            fullpath = str(self._temp_root / "ocr_full.png")
            page.screenshot(path=fullpath, full_page=True)
            paths.append(fullpath)

        except Exception as exc:
            logger.warning(f"View capture error: {exc}")

        return paths

    # --------------------------------------------------------
    # Result Selection
    # --------------------------------------------------------

    def _pick_best(self, results: list[ExtractionResult]) -> str:
        """Pick the best extraction result by layer priority and completeness."""
        # Diagnostic: print all results
        for r in results:
            print(f"  [Layer {r.source_layer}] {r.method}: {len(r.text)} chars, "
                  f"complete={_is_complete(r.text)}, conf={r.confidence}")

        # Priority: DOM > Canvas Hook > API > CDP > OCR
        for layer in [1, 3, 2, 4, 5]:
            for r in results:
                if r.source_layer == layer and _is_complete(r.text):
                    print(f"  => Selected layer {r.source_layer} ({r.method}) — {len(r.text)} chars")
                    return r.text.strip()

        # Fallback: prefer DOM/Canvas text > API (which is often JSON config) > CDP > OCR
        # Skip API results that look like JSON/config (not human-readable content)
        dom_or_hook = [r for r in results
                       if r.text and len(r.text.strip()) > 100
                       and r.source_layer in (1, 3)]
        if dom_or_hook:
            best = max(dom_or_hook, key=lambda r: len(r.text))
            print(f"  => Fallback DOM/hook layer {best.source_layer} — {len(best.text)} chars")
            return best.text.strip()

        # Next: valid API results that look like actual text (not JSON)
        text_api = [r for r in results
                    if r.text and len(r.text.strip()) > 100
                    and r.source_layer == 2
                    and not r.text.strip().startswith("{")]
        if text_api:
            best = max(text_api, key=lambda r: len(r.text))
            print(f"  => Fallback API text — {len(best.text)} chars")
            return best.text.strip()

        # Last resort: longest non-empty text > 50 chars
        valid = [r for r in results if r.text and len(r.text.strip()) > 50]
        if valid:
            best = max(valid, key=lambda r: len(r.text))
            print(f"  => Last resort layer {best.source_layer} — {len(best.text)} chars")
            return best.text.strip()

        raise ExtractionError(
            f"All 5 layers failed to extract meaningful text."
            f" (got {len(results)} results: "
            f"{[f'L{r.source_layer}={len(r.text)}c' for r in results]})"
        )
