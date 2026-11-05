"""Isolated, time-bounded PDF extraction subprocess; no network or shell operations."""

import io
import json
import sys

from pypdf import PdfReader


def main():
    if sys.platform != "darwin":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    reader = PdfReader(io.BytesIO(sys.stdin.buffer.read(5000001)))
    parts = []
    remaining = 50000
    for page in reader.pages[:25]:
        text = page.extract_text() or ""
        parts.append(text[:remaining])
        remaining -= len(text)
        if remaining <= 0:
            break
    print(
        json.dumps(
            {
                "text": "\n".join(parts),
                "pages": len(reader.pages),
                "truncated": len(reader.pages) > 25 or remaining <= 0,
            }
        )
    )


if __name__ == "__main__":
    main()
