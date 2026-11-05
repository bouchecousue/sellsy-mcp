from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit

import httpx

from .errors import ToolError


def approved_url(url: str, extra_hosts: list[str]) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in {None, 443}
        and (host == "sellsy.com" or host.endswith(".sellsy.com") or host in extra_hosts)
    )


async def read_file(client, params) -> dict:
    metadata = await client.get(f"/files/{params.id}")
    if metadata.get("id") != params.id:
        raise ToolError("invalid_response", "Identité du fichier non vérifiée.")
    url = metadata.get("public_link") or ""
    if not url:
        raise ToolError("file_unavailable", "Le fichier ne fournit pas de lien de lecture.")
    if metadata.get("size", 0) > params.max_bytes:
        raise ToolError("file_too_large", "Le fichier dépasse la limite de lecture.")
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as http:
        for _ in range(5):
            if not approved_url(url, client.settings.file_download_hosts):
                raise ToolError("untrusted_file_host", "Hôte du fichier non autorisé pour lecture serveur.")
            async with http.stream("GET", url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers.get("location", "")))
                    continue
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > params.max_bytes:
                        raise ToolError("file_too_large", "Le fichier dépasse la limite de lecture.")
                break
        else:
            raise ToolError("file_redirect_loop", "Trop de redirections du fichier.")
    content = bytes(chunks)
    if content.startswith(b"%PDF-"):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "sellsy_mcp.pdf_text",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(content), timeout=15)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ToolError("pdf_unreadable", "PDF chiffré, invalide ou trop complexe à extraire.")
        import json

        extraction = json.loads(stdout)
    elif str(metadata.get("extension", "")).lower() in {"txt", "csv", "md"}:
        text = content.decode("utf-8-sig")
        extraction = {"text": text[:50000], "truncated": len(text) > 50000}
    else:
        raise ToolError("unsupported_file_type", "Lecture texte disponible pour PDF, TXT, CSV et Markdown.")
    # Do not surface signed URL query strings to the model.
    return {
        "id": params.id,
        "name": metadata.get("name"),
        "bytes_read": len(content),
        **extraction,
        "ocr_required": not bool(extraction["text"].strip()),
    }
