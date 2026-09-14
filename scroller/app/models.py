from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    userId: str = Field(..., min_length=1, description="Stable id; maps to browser profile + SharePoint folder")
    maxPosts: int = Field(50, ge=5, le=200)
    maxScrolls: int = Field(15, ge=1, le=80)
    headed: Optional[bool] = Field(
        None,
        description="Show browser window. Default from env. Use true for first LinkedIn login.",
    )
    loginWaitSeconds: int = Field(
        300,
        ge=30,
        le=900,
        description="How long to wait for LinkedIn login when headed and a login wall is shown.",
    )
    feedUrl: str = Field("https://www.linkedin.com/feed/", description="Home feed URL")


class FeedPost(BaseModel):
    author: Optional[str] = None
    headline: Optional[str] = None
    text: Optional[str] = None
    url: Optional[str] = None
    socialProof: Optional[str] = None
    rank: int


class ScanResponse(BaseModel):
    userId: str
    scannedAt: datetime
    postCount: int
    loginRequired: bool = False
    message: Optional[str] = None
    posts: list[FeedPost]


class HealthResponse(BaseModel):
    status: str
    service: str = "linkedin-feed-scroller"
