"""
Universal Extractor 使用示例。

用法：
    py demo.py <URL>
    py demo.py                                    # 使用默认 WPS kdocs 链接

示例：
    py demo.py https://www.kdocs.cn/l/chtgPO02obP9
    py demo.py https://www.zhipin.com/job_detail/xxx.html
"""

import sys
import os

# Ensure the parent directory is importable
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from universal_extractor import UniversalExtractor, ExtractionError

# 默认测试 URL（WPS 金山文档）
URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.kdocs.cn/l/chtgPO02obP9"

print(f"Target: {URL}")
print("=" * 60)

ue = UniversalExtractor(
    headless=False,        # True=后台运行，False=显示浏览器窗口
    timeout=120_000,       # 超时（毫秒）
    ocr_backend="deepseek",  # OCR 后端：deepseek | tesseract
)

try:
    text = ue.extract(URL)
    print("=" * 60)
    print(f"SUCCESS — {len(text)} characters")
    print("=" * 60)
    print(text[:5000])
    if len(text) > 5000:
        print(f"\n... (truncated, {len(text)} chars total)")
except ExtractionError as e:
    print(f"FAILED: {e}")
