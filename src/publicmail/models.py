from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EmailHit:
    email: str
    method: str
    context: str = ""
    confidence: str = "high"


@dataclass(frozen=True)
class Link:
    url: str
    priority: int
    kind: str = "html"


@dataclass
class Analysis:
    emails: list[EmailHit] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    title: str = ""
    javascript_hint: bool = False
    challenge: bool = False
    source_url: str = ""


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    content_type: str
    charset: str = "utf-8"
    redirect_chain: tuple[str, ...] = ()

    def text(self) -> str:
        try:
            return self.body.decode(self.charset or "utf-8", errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class FetchError(Exception):
    def __init__(
        self,
        message: str,
        *,
        category: str = "network",
        retryable: bool = False,
        retry_after: float = 0,
        status: int | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status
