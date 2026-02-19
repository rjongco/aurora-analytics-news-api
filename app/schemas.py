"""
Pydantic models for NewsAPI data validation and Kinesis transformation.
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator
import hashlib


class NewsAPISource(BaseModel):
    """Source object from NewsAPI response."""
    id: Optional[str] = None
    name: str


class NewsAPIArticle(BaseModel):
    """Article object from NewsAPI response."""
    source: NewsAPISource
    author: Optional[str] = None
    title: str
    description: Optional[str] = None
    url: str
    urlToImage: Optional[str] = None
    publishedAt: str
    content: Optional[str] = None


class NewsAPIResponse(BaseModel):
    """Complete NewsAPI response structure."""
    status: str
    totalResults: int
    articles: list[NewsAPIArticle]


class KinesisArticle(BaseModel):
    """
    Transformed article schema for AWS Kinesis.
    This is the flat structure that will be sent to the stream.
    """
    article_id: str = Field(..., description="Unique hash derived from URL or title")
    source_name: str = Field(..., description="Name of the news source")
    title: str
    content: Optional[str] = None
    url: str
    author: Optional[str] = None
    published_at: str = Field(..., description="Original publication timestamp")
    ingested_at: str = Field(..., description="UTC ISO timestamp of ingestion")

    @classmethod
    def from_newsapi_article(cls, article: NewsAPIArticle) -> "KinesisArticle":
        """
        Transform a NewsAPI article into the Kinesis output format.
        
        Args:
            article: NewsAPI article object
            
        Returns:
            KinesisArticle ready for Kinesis ingestion
        """
        # Generate unique article_id using URL (primary) or title as fallback
        hash_source = article.url or article.title
        article_id = hashlib.sha256(hash_source.encode('utf-8')).hexdigest()
        
        return cls(
            article_id=article_id,
            source_name=article.source.name,
            title=article.title,
            content=article.content,
            url=article.url,
            author=article.author,
            published_at=article.publishedAt,
            ingested_at=datetime.utcnow().isoformat() + "Z"
        )
