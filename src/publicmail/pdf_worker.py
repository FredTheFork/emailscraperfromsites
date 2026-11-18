"""Isolated, resource-bounded PDF text extraction. Invoked by pdf.py, not users."""

from __future__ import annotations

import io
import json
import logging
import sys


def read_pdf(data: bytes, page_limit: int) -> dict:
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        return {"text": "", "error": "Encrypted PDF; not opened", "truncated": False}
    parts, length = [], 0
    total = len(reader.pages)
    truncated = total > page_limit
    for page in list(range(min(total, page_limit))):
        text = (reader.pages[page].extract_text() or "")[: 200_000 - length]
        parts.append(text)
        length += len(text)
        if length >= 200_000:
            truncated = True
            break
    return {"text": "\n".join(parts), "truncated": truncated, "error": ""}


def main() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass  # Windows still gets the parent's wall-clock timeout.
    try:
        limit = max(1, min(100, int(sys.argv[1])))
        data = sys.stdin.buffer.read(25_000_001)
        if len(data) > 25_000_000:
            raise ValueError("PDF exceeds input limit")
        result = read_pdf(data, limit)
    except Exception as exc:
        result = {
            "text": "",
            "truncated": False,
            "error": f"PDF parsing failed: {type(exc).__name__}: {str(exc)[:250]}",
        }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
