from __future__ import annotations

import asyncio
import json
import sys


class PDFExtractor:
    def __init__(self, concurrency: int = 2):
        self.semaphore = asyncio.Semaphore(concurrency)

    async def extract(self, data: bytes, pages: int) -> dict:
        async with self.semaphore:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "publicmail.pdf_worker",
                str(pages),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(data), timeout=15)
                if process.returncode != 0:
                    return {
                        "text": "",
                        "truncated": False,
                        "error": "PDF parser exceeded resources or exited unexpectedly",
                    }
                return json.loads(stdout)
            except TimeoutError:
                return {"text": "", "truncated": False, "error": "PDF parsing timed out"}
            except (ValueError, OSError) as exc:
                return {"text": "", "truncated": False, "error": f"PDF parser error: {str(exc)[:250]}"}
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
