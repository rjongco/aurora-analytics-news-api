# Aurora Analytics News Ingestion Service

A production-ready, resilient news ingestion service built with Python and FastAPI. This service continuously polls NewsAPI for articles, deduplicates them using Redis, and streams validated data to AWS Kinesis Data Streams.

## 🏗️ Architecture Overview

```
┌─────────────┐         ┌──────────────────┐         ┌─────────┐         ┌─────────────┐
│  NewsAPI    │────────▶│  FastAPI Worker  │────────▶│  Redis  │         │   AWS       │
│  (Source)   │         │  (Polling Loop)  │         │  Cache  │         │   Kinesis   │
└─────────────┘         └──────────────────┘         └─────────┘         └─────────────┘
                                │                          │                      ▲
                                │                          │                      │
                                └──────────────────────────┴──────────────────────┘
                                     Deduplicate & Transform & Send
```

### Key Components

- **FastAPI Application**: REST API with health checks and manual trigger endpoints
- **Background Polling Worker**: Asynchronous loop that fetches news at configurable intervals
- **Redis**: Stores article hashes for deduplication (7-day TTL by default)
- **AWS Kinesis**: Receives validated, transformed articles for downstream processing
- **Retry Logic**: Exponential backoff for rate limits (429) and server errors (5xx)

## 🔑 Features

- ✅ **Resilient Polling**: Continues operation even if external services are temporarily unavailable
- ✅ **Deduplication**: Redis-backed hash storage prevents duplicate articles from being sent to Kinesis
- ✅ **Retry Logic**: Automatic retry with exponential backoff using Tenacity
- ✅ **Data Validation**: Pydantic models ensure schema correctness at every stage
- ✅ **Health Checks**: Detailed endpoint verifying connectivity to Redis and Kinesis
- ✅ **Production-Ready**: Multi-stage Docker build, non-root user, comprehensive logging 

## 📋 Prerequisites

- Python 3.11+
- Docker & Docker Compose
- NewsAPI API Key ([Get one here](https://newsapi.org/register))
- AWS Account with Kinesis Data Stream created
- AWS credentials with `kinesis:PutRecord` permission

## 🚀 Quick Start

### 1. Clone and Configure

```bash
git clone https://github.com/rjongco/aurora-analytics-news-api.git
cd newsapi
cp .env.example .env
```

### 2. Set Environment Variables

Edit `.env` with your credentials:

```bash
# NewsAPI Configuration
NEWS_API_KEY=your_newsapi_key_here
SEARCH_QUERY=technology
SEARCH_LANGUAGE=en
POLL_INTERVAL_SECONDS=300

# AWS Configuration
AWS_REGION=us-east-1
KINESIS_STREAM_NAME=aurora-news-stream
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key

# Redis Configuration
REDIS_URL=redis://redis:6379
REDIS_TTL_SECONDS=604800
```

### 3. Run with Docker Compose
#### To run locally (without aws):

```bash
docker-compose -f compose.local.yml up --build
```
#### To run production (with aws):

```bash
docker-compose -f compose.prod.yml up --build
```

The service will be available at `http://localhost:8000`

### 4. Verify Operation

```bash
# Health check
curl http://localhost:8000/health

# Manually trigger a poll (for testing)
curl -X POST http://localhost:8000/trigger-poll
```

## 🔧 Configuration

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `NEWS_API_KEY` | Your NewsAPI API key | - | ✅ |
| `NEWS_API_URL` | NewsAPI endpoint | `https://newsapi.org/v2/everything` | ❌ |
| `SEARCH_QUERY` | Keywords to search for | `technology` | ❌ |
| `SEARCH_LANGUAGE` | Language filter | `en` | ❌ |
| `SORT_BY` | Sort order | `publishedAt` | ❌ |
| `POLL_INTERVAL_SECONDS` | Seconds between polls | `300` (5 min) | ❌ |
| `PAGE_SIZE` | NewsAPI max limit | `99` | ❌ |
| `AWS_REGION` | AWS region | `us-east-1` | ❌ |
| `KINESIS_STREAM_NAME` | Kinesis stream name | - | ✅ |
| `AWS_ACCESS_KEY_ID` | AWS access key | - | ✅ |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | - | ✅ |

## 📊 Data Flow & Deduplication Logic

### Input: NewsAPI Article Schema

```json
{
  "source": {"id": "techcrunch", "name": "TechCrunch"},
  "author": "John Doe",
  "title": "Breaking Tech News",
  "description": "Article description...",
  "url": "https://example.com/article",
  "urlToImage": "https://example.com/image.jpg",
  "publishedAt": "2026-02-19T10:30:00Z",
  "content": "Full article content..."
}
```

### Output: Kinesis Article Schema

```json
{
  "article_id": "a3f5e9b2c8d1...",
  "source_name": "TechCrunch",
  "title": "Breaking Tech News",
  "content": "Full article content...",
  "url": "https://example.com/article",
  "author": "John Doe",
  "published_at": "2026-02-19T10:30:00Z",
  "ingested_at": "2026-02-19T11:00:00Z"
}
```

### Deduplication Process

1. **Hash Generation**: Each article's URL (or title if URL is missing) is hashed using SHA-256
2. **Redis Lookup**: Check if `article:<hash>` exists in Redis
3. **Skip or Process**: 
   - If exists → Skip (already processed)
   - If not exists → Transform, send to Kinesis, then store hash in Redis with 7-day TTL
4. **TTL Management**: Redis automatically expires old hashes after 7 days

**Why 7 days?** NewsAPI's free tier typically returns articles from the past month. A 7-day window balances deduplication effectiveness with memory usage.

## 🏃 Running Locally (Without Docker)

### Install Dependencies

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Start Redis

```bash
docker run -d -p 6379:6379 redis:7-alpine
```

### Run Application

```bash
uvicorn app.main:app --reload
```

## 🧪 API Endpoints

### `GET /`
Basic health check
```bash
curl http://localhost:8000/
```

### `GET /health`
Detailed health check with Redis and Kinesis connectivity
```bash
curl http://localhost:8000/health
```

### `POST /trigger-poll`
Manually trigger a single poll cycle (useful for testing)
```bash
curl -X POST http://localhost:8000/trigger-poll
```

## 🛡️ Error Handling & Resilience

### Retry Logic (Tenacity)
- **Rate Limits (429)**: Exponential backoff starting at 4s, max 60s, up to 5 attempts
- **Server Errors (5xx)**: Same retry strategy
- **Backoff Formula**: `min(60, 4 * (2 ^ attempt_number))`

### Graceful Degradation
- **Redis Unavailable**: Service continues, logs warnings, assumes articles are new (no data loss)
- **Kinesis Unavailable**: Logs error, continues polling (subsequent successful sends will catch up)
- **NewsAPI Errors**: Logs and waits for next poll cycle

### Logging
All operations are logged with appropriate levels:
- `INFO`: Normal operations, successful sends
- `WARNING`: Rate limits, retries
- `ERROR`: Failed connections, unexpected errors

## 🔒 Security & Best Practices

- ✅ Non-root Docker user (`appuser`)
- ✅ Multi-stage Docker build (minimal attack surface)
- ✅ Environment variables for secrets (never hardcoded)
- ✅ CI/CD with security scanning (Trivy, Bandit, Safety)
- ✅ Health check endpoints for orchestration (Kubernetes-ready)
- ✅ Connection pooling and async I/O for efficiency


## 📈 Performance Tuning

### Adjust Poll Interval
```bash
POLL_INTERVAL_SECONDS=180  # Poll every 3 minutes
```

### Increase Batch Size
Modify `pageSize` in `fetch_news_articles()` (max 100 for NewsAPI)

### Optimize Redis TTL
```bash
REDIS_TTL_SECONDS=259200  # 3 days for faster expiry
```

## 🐛 Troubleshooting

### Service won't start
```bash
# Check logs
docker-compose logs news-api

# Verify environment variables
docker-compose config
```

### Redis connection errors
```bash
# Test Redis connectivity
docker exec -it aurora-redis redis-cli ping
```

### Kinesis permission errors
Ensure your IAM user/role has:
```json
{
  "Effect": "Allow",
  "Action": [
    "kinesis:PutRecord",
    "kinesis:DescribeStream"
  ],
  "Resource": "arn:aws:kinesis:REGION:ACCOUNT:stream/STREAM_NAME"
}
```
---

**Built with ❤️ by Rafael Jongco**
