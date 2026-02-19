"""
Aurora Analytics News Ingestion Service
FastAPI application with background polling worker for NewsAPI integration.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

import boto3
import httpx
import redis.asyncio as redis
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from pydantic_settings import BaseSettings
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.schemas import NewsAPIResponse, KinesisArticle

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application configuration settings."""
    news_api_key: str
    news_api_url: str = "https://newsapi.org/v2/everything"
    search_query: str = "technology"
    search_language: str = "en"
    sort_by: str = "publishedAt"
    poll_interval_seconds: int = 300  # 5 minutes
    page_size: int = 99
    
    redis_url: str = "redis://localhost:6379"
    redis_ttl_seconds: int = 86400 * 7  # 7 days
    
    aws_region: str = "us-east-1"
    kinesis_stream_name: str
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    localstack_endpoint: Optional[str] = None
    env: str = "local" #production
    
    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()

# Global state
redis_client: Optional[redis.Redis] = None
kinesis_client = None
polling_task: Optional[asyncio.Task] = None


async def get_redis_client() -> redis.Redis:
    """Get or create Redis client connection."""
    global redis_client
    if redis_client is None:
        redis_client = await redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True
        )
    return redis_client
 
def get_kinesis_client():
    global kinesis_client
    if kinesis_client is None:
        session_kwargs = {"region_name": settings.aws_region}
          
        if settings.env.lower() == 'production':
            logger.info(f"ENVIRONMENT: PRODUCTION - Routing to AWS Cloud")
            session_kwargs.update({
                "aws_access_key_id": settings.aws_access_key_id,
                "aws_secret_access_key": settings.aws_secret_access_key,
            })
        
        # If we provide a LOCALSTACK_ENDPOINT in our .env, use it!
        elif settings.env.lower() == 'local' and settings.localstack_endpoint and settings.localstack_endpoint.strip():
            logger.info("ENVIRONMENT: LOCAL - Routing to LocalStack")
            session_kwargs["endpoint_url"] = settings.localstack_endpoint
            session_kwargs["aws_access_key_id"] = settings.aws_access_key_id
            session_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
            
        kinesis_client = boto3.client("kinesis", **session_kwargs)
    return kinesis_client

class RateLimitError(Exception):
    """Custom exception for rate limit errors."""
    pass


class ServerError(Exception):
    """Custom exception for server errors (5xx)."""
    pass


@retry(
    retry=retry_if_exception_type((RateLimitError, ServerError)),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
async def fetch_news_articles(client: httpx.AsyncClient) -> NewsAPIResponse:
    """
    Fetch articles from NewsAPI with retry logic.
    
    Args:
        client: HTTPX async client
        
    Returns:
        Validated NewsAPI response
        
    Raises:
        RateLimitError: When rate limited (429)
        ServerError: When server error occurs (5xx)
        HTTPException: For other HTTP errors
    """
    headers = {"X-Api-Key": settings.news_api_key}
    
    params = {
        "q": settings.search_query,
        "language": settings.search_language,
        "sortBy": settings.sort_by,
        "pageSize": settings.page_size,  # Maximum allowed
    }
    
    try:
        response = await client.get(
            settings.news_api_url,
            headers=headers,
            params=params,
            timeout=30.0
        )
        
        # Handle rate limiting
        if response.status_code == 429:
            logger.warning("Rate limited by NewsAPI, will retry with backoff")
            raise RateLimitError("NewsAPI rate limit exceeded")
        
        # Handle server errors
        if 500 <= response.status_code < 600:
            logger.error(f"NewsAPI server error: {response.status_code}")
            raise ServerError(f"NewsAPI server error: {response.status_code}")
        
        # Raise for other HTTP errors
        response.raise_for_status()
        
        # Parse and validate response
        data = response.json()
        return NewsAPIResponse(**data)
        
    except httpx.HTTPError as e:
        logger.error(f"HTTP error fetching news: {e}")
        raise


async def is_article_processed(redis_client: redis.Redis, article_id: str) -> bool:
    """
    Check if an article has already been processed.
    
    Args:
        redis_client: Redis client connection
        article_id: Unique article identifier
        
    Returns:
        True if article was already processed, False otherwise
    """
    try:
        key = f"article:{article_id}"
        exists = await redis_client.exists(key)
        return exists == 1
    except Exception as e:
        logger.error(f"Redis error checking article {article_id}: {e}")
        # If Redis fails, assume article is not processed to avoid data loss
        return False


async def mark_article_processed(redis_client: redis.Redis, article_id: str) -> None:
    """
    Mark an article as processed in Redis.
    
    Args:
        redis_client: Redis client connection
        article_id: Unique article identifier
    """
    try:
        key = f"article:{article_id}"
        await redis_client.setex(key, settings.redis_ttl_seconds, "1")
    except Exception as e:
        logger.error(f"Redis error marking article {article_id}: {e}")


async def send_to_kinesis(article: KinesisArticle) -> bool:
    """
    Send article to AWS Kinesis Data Stream.
    
    Args:
        article: Transformed article ready for Kinesis
        
    Returns:
        True if successful, False otherwise
    """
    try: 
        kinesis = get_kinesis_client()
        response = kinesis.put_record(
            StreamName=settings.kinesis_stream_name,
            Data=article.model_dump_json(),
            PartitionKey=article.article_id
        )
        
        logger.info(
            f"Sent article {article.article_id} to Kinesis. "
            f"Shard: {response['ShardId']}, Sequence: {response['SequenceNumber']}"
        )
        return True
        
    except ClientError as e:
        logger.error(f"AWS Kinesis error sending article {article.article_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending to Kinesis: {e}")
        return False


async def process_articles(response: NewsAPIResponse) -> tuple[int, int]:
    """
    Process articles: deduplicate and send to Kinesis.
    
    Args:
        response: Validated NewsAPI response
        
    Returns:
        Tuple of (new_articles_count, sent_to_kinesis_count)
    """
    try:
        redis_conn = await get_redis_client()
        kinesis_conn = get_kinesis_client()
        kinesis_conn.describe_stream(StreamName=settings.kinesis_stream_name)
    except Exception as e:
        logger.error(f"Failed to connect. Please check env variables: {e}")
        redis_conn = None
        kinesis_conn = None
    
    new_articles = 0
    sent_to_kinesis = 0

    if kinesis_conn is None:
        logger.error(f"Failed to connect to AWS Kinesis Stream. Double-check connection variables")
        return new_articles, sent_to_kinesis 
    
    for article in response.articles:
        try:
            # Transform to Kinesis format
            kinesis_article = KinesisArticle.from_newsapi_article(article)
            
            # Check if already processed (deduplication)
            if redis_conn:
                if await is_article_processed(redis_conn, kinesis_article.article_id):
                    logger.debug(f"Skipping duplicate article: {kinesis_article.article_id}")
                    continue
            
            new_articles += 1
            
            # Send to Kinesis
            if kinesis_conn and await send_to_kinesis(kinesis_article):
                sent_to_kinesis += 1
                
                # Mark as processed in Redis
                if redis_conn:
                    await mark_article_processed(redis_conn, kinesis_article.article_id)
            
        except Exception as e:
            logger.error(f"Error processing article: {e}")
            continue
    
    return new_articles, sent_to_kinesis


async def polling_worker():
    """
    Background worker that polls NewsAPI at regular intervals.
    Implements resilient error handling to keep service running.
    """
    logger.info("Starting news polling worker")
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                logger.info("Fetching articles from NewsAPI...")
                response = await fetch_news_articles(client)
                
                total_articles = len(response.articles)
                logger.info(f"Fetched {total_articles} articles from NewsAPI")
                
                if total_articles > 0:
                    new_count, sent_count = await process_articles(response)
                    logger.info(
                        f"Processing complete: {new_count} new articles, "
                        f"{sent_count} sent to Kinesis"
                    )
                else:
                    logger.info("No articles returned from NewsAPI")
                
            except (RateLimitError, ServerError) as e:
                logger.error(f"Failed to fetch articles after retries: {e}")
            except Exception as e:
                logger.error(f"Unexpected error in polling worker: {e}")
            
            # Wait before next poll
            logger.info(f"Sleeping for {settings.poll_interval_seconds} seconds...")
            await asyncio.sleep(settings.poll_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Starts background worker on startup and cleans up on shutdown.
    """
    global polling_task, redis_client
    
    # Startup
    logger.info("Starting Aurora Analytics News Ingestion Service")
    # Start background polling worker
    polling_task = asyncio.create_task(polling_worker())
    
    yield
    
    # Shutdown
    logger.info("Shutting down service...")
    
    # Cancel polling task
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
    
    # Close Redis connection
    if redis_client:
        await redis_client.close()


# FastAPI application
app = FastAPI(
    title="Aurora Analytics News Ingestion Service",
    description="Production-ready news ingestion service polling NewsAPI and streaming to AWS Kinesis",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "service": "Aurora Analytics News Ingestion",
        "status": "running",
        "version": "1.0.0"
    }


@app.get("/health")
async def health_check():
    """
    Detailed health check endpoint.
    Verifies connectivity to Redis and AWS Kinesis.
    """
    health_status = {
        "service": "healthy",
        "redis": "unknown",
        "kinesis": "unknown",
    }
    
    # Check Redis
    try:
        redis_conn = await get_redis_client()
        await redis_conn.ping()
        health_status["redis"] = "connected"
    except Exception as e:
        health_status["redis"] = f"error: {str(e)}"
        health_status["service"] = "degraded"
    
    # Check Kinesis
    try:
        kinesis = get_kinesis_client()
        kinesis.describe_stream(StreamName=settings.kinesis_stream_name)
        health_status["kinesis"] = "connected"
    except Exception as e:
        health_status["kinesis"] = f"error: {str(e)}"
        health_status["service"] = "degraded"
    
    return health_status


@app.post("/trigger-poll")
async def trigger_manual_poll():
    """
    Manually trigger a single poll cycle (useful for testing).
    Does not affect the background polling schedule.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await fetch_news_articles(client)
            new_count, sent_count = await process_articles(response)
            
            return {
                "status": "success",
                "total_articles": len(response.articles),
                "new_articles": new_count,
                "sent_to_kinesis": sent_count
            }
    except Exception as e:
        logger.error(f"Manual poll failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
