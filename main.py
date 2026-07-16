"""
L-Coup Direct - 가전 트렌드 분석 백엔드
FastAPI + sqlite3 + 네이버 데이터랩 API(통합검색어 트렌드 / 쇼핑인사이트) 연동

실행:
    pip install -r requirements.txt
    cp .env.example .env   # NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 채우기
    uvicorn main:app --reload
"""

import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import date, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "lcoup_direct.db")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")

NAVER_SEARCH_TREND_URL = "https://openapi.naver.com/v1/datalab/search"
NAVER_SHOPPING_KEYWORDS_URL = "https://openapi.naver.com/v1/datalab/shopping/category/keywords"

# 가전(전자/컴퓨터) 대분류 쇼핑인사이트 카테고리 코드. 세부 카테고리는
# 네이버 데이터랩 문서(https://developers.naver.com/docs/serviceapi/datalab/shopping/shopping.md)의
# 카테고리 코드표를 참고해 keywords 테이블의 category 값에 맞게 조정한다.
DEFAULT_CATEGORY_CODE = os.getenv("NAVER_SHOPPING_CATEGORY_CODE", "50000003")

SEED_KEYWORDS = [
    ("가전", "냉장고"),
    ("가전", "세탁기"),
    ("가전", "에어컨"),
    ("가전", "공기청정기"),
    ("가전", "TV"),
]


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trends (
                date TEXT NOT NULL,
                group_name TEXT NOT NULL,
                ratio REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                keyword TEXT NOT NULL
            )
            """
        )
        existing = conn.execute("SELECT COUNT(*) AS c FROM keywords").fetchone()["c"]
        if existing == 0:
            conn.executemany(
                "INSERT INTO keywords (category, keyword) VALUES (?, ?)",
                SEED_KEYWORDS,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="L-Coup Direct API", lifespan=lifespan)


class TrendRow(BaseModel):
    date: str
    group_name: str
    ratio: float


def naver_headers() -> dict:
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="NAVER_CLIENT_ID / NAVER_CLIENT_SECRET가 설정되어 있지 않습니다. .env를 확인하세요.",
        )
    return {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "Content-Type": "application/json",
    }


async def fetch_naver_trend(keywords: list, days: int = 30) -> list[dict]:
    """
    keywords: [{"category": "가전", "keyword": "냉장고"}, ...] 형태의 리스트.
    네이버 통합 검색어 트렌드 API와 쇼핑인사이트(카테고리 내 키워드) API를
    동시에 호출하여 결과를 하나의 리스트로 합쳐 반환한다.

    반환 형식: [{"date": "YYYY-MM-DD", "group_name": str, "ratio": float}, ...]
    """
    if not keywords:
        return []

    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    headers = naver_headers()

    # 키워드를 카테고리별로 그룹핑 (group_name = "카테고리:키워드")
    keyword_groups = [
        {"groupName": f"{item['category']}:{item['keyword']}", "keywords": [item["keyword"]]}
        for item in keywords
    ]

    search_body = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "timeUnit": "date",
        "keywordGroups": keyword_groups,
    }

    shopping_body = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "timeUnit": "date",
        "category": DEFAULT_CATEGORY_CODE,
        "keyword": [
            {"name": f"{item['category']}:{item['keyword']}", "param": [item["keyword"]]}
            for item in keywords
        ],
    }

    results: list[dict] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1) 통합 검색어 트렌드
        try:
            resp = await client.post(NAVER_SEARCH_TREND_URL, headers=headers, json=search_body)
            resp.raise_for_status()
            for group in resp.json().get("results", []):
                name = f"search:{group['title']}"
                for point in group.get("data", []):
                    results.append(
                        {"date": point["period"], "group_name": name, "ratio": point["ratio"]}
                    )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"네이버 검색어 트렌드 API 오류: {exc.response.status_code} {exc.response.text}",
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"네이버 검색어 트렌드 API 호출 실패: {exc}")

        # 2) 쇼핑인사이트 (카테고리 내 키워드 트렌드)
        try:
            resp = await client.post(
                NAVER_SHOPPING_KEYWORDS_URL, headers=headers, json=shopping_body
            )
            resp.raise_for_status()
            for group in resp.json().get("results", []):
                name = f"shopping:{group['title']}"
                for point in group.get("data", []):
                    results.append(
                        {"date": point["period"], "group_name": name, "ratio": point["ratio"]}
                    )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"네이버 쇼핑인사이트 API 오류: {exc.response.status_code} {exc.response.text}",
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"네이버 쇼핑인사이트 API 호출 실패: {exc}")

    return results


@app.post("/api/sync")
async def sync_trends():
    """등록된 키워드 기준으로 최근 30일 네이버 트렌드 데이터를 가져와 DB에 적재한다."""
    with get_conn() as conn:
        rows = conn.execute("SELECT category, keyword FROM keywords").fetchall()
    keywords = [{"category": r["category"], "keyword": r["keyword"]} for r in rows]

    if not keywords:
        raise HTTPException(status_code=400, detail="등록된 키워드가 없습니다.")

    trend_data = await fetch_naver_trend(keywords, days=30)

    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO trends (date, group_name, ratio) VALUES (:date, :group_name, :ratio)",
            trend_data,
        )

    return {"synced": len(trend_data), "keywords": len(keywords)}


@app.get("/api/trends", response_model=list[TrendRow])
def get_trends(
    category: Optional[str] = Query(None, description="키워드 카테고리 필터"),
    keyword: Optional[str] = Query(None, description="키워드 필터"),
):
    """DB에 적재된 트렌드 데이터를 카테고리/키워드별로 조회한다."""
    query = "SELECT date, group_name, ratio FROM trends WHERE 1=1"
    params: list[str] = []

    if category:
        query += " AND group_name LIKE ?"
        params.append(f"%{category}:%")
    if keyword:
        query += " AND group_name LIKE ?"
        params.append(f"%:{keyword}")

    query += " ORDER BY date ASC"

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    return [TrendRow(date=r["date"], group_name=r["group_name"], ratio=r["ratio"]) for r in rows]


@app.get("/api/keywords")
def get_keywords():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, category, keyword FROM keywords").fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
