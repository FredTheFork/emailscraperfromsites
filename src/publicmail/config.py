from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CrawlSettings(BaseModel):
    """Persisted settings. Robots and public-network restrictions are not optional."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    concurrency: int = Field(24, ge=1, le=100)
    delay: float = Field(1.5, ge=0.5, le=120)
    max_pages: int = Field(24, ge=1, le=500)
    max_depth: int = Field(3, ge=0, le=10)
    max_candidates: int = Field(250, ge=10, le=2000)
    request_timeout: float = Field(20, ge=2, le=120)
    site_timeout: float = Field(240, ge=10, le=3600)
    max_attempts: int = Field(3, ge=1, le=6)
    retry_base: float = Field(30, ge=1, le=3600)
    max_html_bytes: int = Field(3_000_000, ge=100_000, le=10_000_000)
    max_pdf_bytes: int = Field(8_000_000, ge=100_000, le=25_000_000)
    max_pdf_documents: int = Field(3, ge=0, le=20)
    pdf_pages: int = Field(20, ge=1, le=100)
    max_sitemaps: int = Field(5, ge=0, le=20)
    sitemap_urls: int = Field(2000, ge=10, le=20_000)
    http_fallback: bool = True
    browser: bool = False
    # Automatic Colab collection also renders important public contact pages
    # when a footer/third-party email has already been found by HTTP.
    browser_contact_pages: bool = False
    browser_pages: int = Field(2, ge=1, le=5)
    browser_concurrency: int = Field(2, ge=1, le=4)
    user_agent: str = Field("PublicMailBot/1.0", min_length=5, max_length=250)

    @field_validator("user_agent")
    @classmethod
    def validate_agent(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("User agent cannot contain control characters")
        return value

    @property
    def robot_name(self) -> str:
        return self.user_agent.split("/", 1)[0].split(" ", 1)[0]
