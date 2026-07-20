"""Document text extraction — PDFs, Word/RTF/HTML, and on-device OCR.

Everything runs local and free:
  .pdf                    → pdftotext (poppler) if installed, else pypdf
  .docx/.doc/.rtf/.odt/
  .html/.htm/.webarchive  → /usr/bin/textutil (ships with macOS)
  images                  → Apple Vision framework OCR (pyobjc, on-device)
  everything else         → plain text read

extract_text() never raises — it returns (ok, text_or_error).
"""

import asyncio
import os
from pathlib import Path

from services.system_control import run_shell, which_tool, _q

_TEXTUTIL_EXTS = {".docx", ".doc", ".rtf", ".rtfd", ".odt", ".html", ".htm",
                  ".webarchive", ".wordml"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".tiff", ".tif", ".gif",
               ".bmp", ".webp"}
# Binary formats read_file must never cat raw.
BINARY_DOC_EXTS = {".pdf"} | _TEXTUTIL_EXTS | _IMAGE_EXTS


def _clamp(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) > max_chars:
        return text[:max_chars] + f"\n…[truncated: {len(text)} chars total]"
    return text or "(no extractable text)"


async def _pdf_text(path: str, max_chars: int) -> tuple[bool, str]:
    # 1. pdftotext (poppler) — best layout fidelity.
    if which_tool("pdftotext"):
        try:
            ok, out = await run_shell(f"pdftotext -layout {_q(path)} -", timeout=30,
                                      max_chars=max_chars + 500)
            if ok and out.strip():
                return True, _clamp(out, max_chars)
        except asyncio.TimeoutError:
            pass
    # 2. pypdf — pure-Python fallback, no brew needed.
    try:
        from pypdf import PdfReader

        def _read() -> str:
            reader = PdfReader(path)
            parts = []
            for page in reader.pages[:100]:
                parts.append(page.extract_text() or "")
                if sum(len(p) for p in parts) > max_chars * 2:
                    break
            return "\n".join(parts)

        text = await asyncio.to_thread(_read)
        if text.strip():
            return True, _clamp(text, max_chars)
        return False, ("PDF contains no extractable text layer (likely a scan) — "
                       "OCR the pages instead: it may be an image-only PDF.")
    except ImportError:
        return False, ("No PDF extractor available — install poppler "
                       "(`brew install poppler`) or pypdf (`pip install pypdf`).")
    except Exception as e:
        return False, f"PDF extraction failed: {e}"


def _ocr_sync(path: str) -> str:
    """On-device OCR via Apple's Vision framework (pyobjc). Raises on failure —
    the async wrapper translates."""
    import Foundation
    import Vision

    url = Foundation.NSURL.fileURLWithPath_(path)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    ok = handler.performRequests_error_([request], None)
    # pyobjc returns either bool or (bool, error) depending on version.
    if isinstance(ok, tuple):
        ok = ok[0]
    if not ok:
        raise RuntimeError("Vision OCR request failed")
    lines = []
    for obs in (request.results() or []):
        cands = obs.topCandidates_(1)
        if cands and len(cands):
            lines.append(str(cands[0].string()))
    return "\n".join(lines)


async def ocr_image(path: str, max_chars: int = 8000) -> tuple[bool, str]:
    try:
        text = await asyncio.to_thread(_ocr_sync, path)
    except ImportError:
        return False, ("OCR needs pyobjc Vision bindings — "
                       "`pip install pyobjc-framework-Vision`.")
    except Exception as e:
        return False, f"OCR failed: {e}"
    if not text.strip():
        return True, "(no text detected in the image)"
    return True, _clamp(text, max_chars)


async def extract_text(path: str, max_chars: int = 8000) -> tuple[bool, str]:
    """Extract readable text from any document. Never raises."""
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return False, f"No such file: {p}"
    if p.is_dir():
        return False, f"{p} is a directory — use read_file for listings."
    max_chars = max(500, min(int(max_chars or 8000), 30_000))
    ext = p.suffix.lower()

    if ext == ".pdf":
        return await _pdf_text(str(p), max_chars)

    if ext in _TEXTUTIL_EXTS:
        try:
            ok, out = await run_shell(
                f"textutil -convert txt -stdout {_q(str(p))}", timeout=30,
                max_chars=max_chars + 500)
        except asyncio.TimeoutError:
            return False, "textutil timed out."
        return (True, _clamp(out, max_chars)) if ok else (False, out[:300])

    if ext in _IMAGE_EXTS:
        return await ocr_image(str(p), max_chars)

    # Plain text-ish — read directly, replacing anything undecodable.
    try:
        return True, _clamp(p.read_text(errors="replace"), max_chars)
    except Exception as e:
        return False, f"Could not read {p}: {e}"
