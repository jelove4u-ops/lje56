"""
L-Coup Direct - 가전 트렌드 분석 백엔드
FastAPI + sqlite3 + 네이버 데이터랩 API(통합검색어 트렌드 / 쇼핑인사이트) 연동

실행:
    pip install -r requirements.txt
    cp .env.example .env   # NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 채우기
    uvicorn main:app --reload

=====================================================================
Claude 연동 가이드 - 카카오 MCP 알림 브릿지 (사람이 읽는 운영 문서)
=====================================================================
이 백엔드는 카카오톡 메시지를 직접 보내지 않는다. 이상 징후를 감지해
"대기 중인 알림(Pending Alerts)" 큐에 적재하고, GET /api/alerts/pending
으로 그 목록을 노출할 뿐이다. 실제 발송은 카카오 MCP가 연결된 Claude
세션(예: 사용자가 명시적으로 구성한 Claude Code 반복 실행/트리거)이
아래 절차를 수행할 때만 일어난다. 이 주석은 문서일 뿐이며 어떤 Claude
세션의 동작도 자동으로 트리거하지 않는다 - 실제 폴링/발송 자동화는
사용자가 별도로 스케줄러(예: Claude Code Routine)를 만들어야 동작한다.

카카오 MCP가 연결된 Claude 세션에서 알림을 처리하려면:
  1. GET /api/alerts/pending 을 호출해 대기 중인 알림 목록을 확인한다.
  2. 응답의 "alerts" 배열이 비어있지 않다면, 각 alert마다 카카오 MCP의
     메시지 발송 도구(예: send_kakaotalk_message)를 호출해 아래 포맷으로
     사용자 본인에게 카톡을 보낸다.

         🚨 [L-Coup Direct 트렌드 경보]
         네이버에서 '{category}' 검색량이 전일 대비 {increase_rate}% 급증!
         쿠팡 기획전 제안 및 광고 입찰가 조정을 검토하세요.

     (각 alert 객체의 message 필드에 이미 이 포맷으로 렌더링된 문자열이
     들어있으므로 그대로 사용해도 된다.)
  3. 발송에 성공한 alert는 POST /api/alerts/{id}/ack 를 호출해 상태를
     'sent'로 표시한다. ack하지 않으면 다음 폴링에서도 계속 pending으로
     남아있어 중복 발송으로 이어질 수 있다.
=====================================================================
"""

import asyncio
import os
import sqlite3
import statistics
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta
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
    ("가전", "LG 냉장고"),
    ("가전", "삼성 냉장고"),
]

# 급상승 키워드 판정 기준
SPIKE_RECENT_DAYS = 3
SPIKE_BASELINE_DAYS = 14
SPIKE_MIN_BASELINE_DAYS = 5  # 이 값보다 데이터가 적으면 판정에서 제외
SPIKE_STD_THRESHOLD = 2.0

# 전일 대비 카테고리 검색 지수 폭증 알림 기준 (%)
ALERT_INCREASE_THRESHOLD_PCT = 50.0


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                alert_date TEXT NOT NULL,
                today_avg REAL NOT NULL,
                yesterday_avg REAL NOT NULL,
                increase_rate REAL NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                UNIQUE(category, alert_date)
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


async def detect_spike_keywords(
    recent_days: int = SPIKE_RECENT_DAYS,
    baseline_days: int = SPIKE_BASELINE_DAYS,
    min_baseline_days: int = SPIKE_MIN_BASELINE_DAYS,
    threshold: float = SPIKE_STD_THRESHOLD,
) -> list[dict]:
    """
    최근 N일 검색량 평균이 그 이전 baseline_days일 평균 대비
    '표준편차 threshold배' 이상 급증한 키워드 그룹을 찾아낸다.

    스코어: spike_score = (recent_avg - baseline_mean) / baseline_std
    baseline_std가 0인데 recent_avg가 baseline_mean보다 크면(완전히 새로 뜬 키워드)
    표준편차로 나눌 수 없으므로 스코어를 상한값(SPIKE_STD_THRESHOLD의 큰 배수)으로 고정한다.
    """

    def _analyze() -> list[dict]:
        with get_conn() as conn:
            groups = [
                r["group_name"]
                for r in conn.execute("SELECT DISTINCT group_name FROM trends").fetchall()
            ]
            spikes: list[dict] = []
            for group_name in groups:
                rows = conn.execute(
                    """
                    SELECT date, AVG(ratio) AS ratio FROM trends
                    WHERE group_name = ?
                    GROUP BY date
                    ORDER BY date ASC
                    """,
                    (group_name,),
                ).fetchall()

                if len(rows) < recent_days + min_baseline_days:
                    continue

                recent = rows[-recent_days:]
                baseline = rows[-(recent_days + baseline_days):-recent_days]
                if len(baseline) < min_baseline_days:
                    continue

                recent_avg = statistics.fmean(r["ratio"] for r in recent)
                baseline_values = [r["ratio"] for r in baseline]
                baseline_mean = statistics.fmean(baseline_values)
                baseline_std = (
                    statistics.pstdev(baseline_values) if len(baseline_values) > 1 else 0.0
                )

                if baseline_std > 0:
                    spike_score = (recent_avg - baseline_mean) / baseline_std
                elif recent_avg > baseline_mean:
                    spike_score = 999.0  # baseline 변동이 없던 키워드가 새로 급등한 경우
                else:
                    spike_score = 0.0

                if spike_score >= threshold:
                    spikes.append(
                        {
                            "group_name": group_name,
                            "recent_avg": round(recent_avg, 2),
                            "baseline_mean": round(baseline_mean, 2),
                            "baseline_std": round(baseline_std, 2),
                            "spike_score": round(spike_score, 2),
                        }
                    )

            spikes.sort(key=lambda x: x["spike_score"], reverse=True)
            return spikes

    return await asyncio.get_event_loop().run_in_executor(None, _analyze)


def _period_avg(conn: sqlite3.Connection, group_name: str, start: date, end: date) -> Optional[float]:
    row = conn.execute(
        "SELECT AVG(ratio) AS avg_ratio FROM trends WHERE group_name = ? AND date >= ? AND date <= ?",
        (group_name, start.isoformat(), end.isoformat()),
    ).fetchone()
    return row["avg_ratio"]


def calculate_period_change(
    conn: sqlite3.Connection, group_name: str, period_days: int = 30, offset_days: int = 30
) -> dict:
    """
    group_name의 최근 period_days일 평균 검색 지수를, offset_days일 이전의
    동일 길이 구간과 비교해 증감률(%)을 계산한다.
    offset_days=30 -> MoM(전월 대비), offset_days=365 -> YoY(전년 대비)
    """
    today = date.today()
    current_start = today - timedelta(days=period_days - 1)
    current_end = today
    prev_end = current_start - timedelta(days=offset_days - period_days)
    prev_start = prev_end - timedelta(days=period_days - 1)

    current_avg = _period_avg(conn, group_name, current_start, current_end)
    previous_avg = _period_avg(conn, group_name, prev_start, prev_end)

    change_pct = None
    if current_avg is not None and previous_avg:
        change_pct = round((current_avg - previous_avg) / previous_avg * 100, 2)

    return {
        "current_avg": round(current_avg, 2) if current_avg is not None else None,
        "previous_avg": round(previous_avg, 2) if previous_avg is not None else None,
        "change_pct": change_pct,
    }


def calculate_mom(conn: sqlite3.Connection, group_name: str, period_days: int = 30) -> dict:
    """MoM(전월 대비): 최근 30일(M) vs 그 이전 30일(M-1) 평균 검색 지수 비교."""
    result = calculate_period_change(conn, group_name, period_days=period_days, offset_days=period_days)
    return {
        "group_name": group_name,
        "current_avg": result["current_avg"],
        "previous_avg": result["previous_avg"],
        "mom_pct": result["change_pct"],
    }


def calculate_yoy(conn: sqlite3.Connection, group_name: str, period_days: int = 30) -> dict:
    """YoY(전년 대비): 최근 30일 vs 1년 전 동일 구간 평균 검색 지수 비교."""
    result = calculate_period_change(conn, group_name, period_days=period_days, offset_days=365)
    return {
        "group_name": group_name,
        "current_avg": result["current_avg"],
        "yoy_avg": result["previous_avg"],
        "yoy_pct": result["change_pct"],
    }


def _category_from_group(group_name: str) -> str:
    """group_name은 'source:category:keyword' 형태이므로 가운데 항목을 추출한다."""
    parts = group_name.split(":", 2)
    return parts[1] if len(parts) >= 2 else group_name


def detect_daily_category_spikes(threshold_pct: float = ALERT_INCREASE_THRESHOLD_PCT) -> list[dict]:
    """
    카테고리별로 가장 최근 두 날짜(전일 vs 당일)의 평균 검색 지수를 비교해
    전일 대비 threshold_pct% 이상 폭증한 카테고리를 찾아낸다.
    """
    with get_conn() as conn:
        rows = conn.execute("SELECT date, group_name, ratio FROM trends").fetchall()

    by_category_date: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        category = _category_from_group(r["group_name"])
        by_category_date.setdefault(category, {}).setdefault(r["date"], []).append(r["ratio"])

    spikes = []
    for category, date_map in by_category_date.items():
        dates = sorted(date_map.keys())
        if len(dates) < 2:
            continue
        latest_date, prev_date = dates[-1], dates[-2]
        today_avg = statistics.fmean(date_map[latest_date])
        yesterday_avg = statistics.fmean(date_map[prev_date])
        if yesterday_avg <= 0:
            continue
        increase_rate = (today_avg - yesterday_avg) / yesterday_avg * 100
        if increase_rate >= threshold_pct:
            spikes.append(
                {
                    "category": category,
                    "alert_date": latest_date,
                    "today_avg": round(today_avg, 2),
                    "yesterday_avg": round(yesterday_avg, 2),
                    "increase_rate": round(increase_rate, 2),
                }
            )
    return spikes


def enqueue_pending_alerts() -> None:
    """감지된 폭증 카테고리를 alerts 큐에 적재한다 (같은 카테고리/날짜는 중복 적재하지 않음)."""
    spikes = detect_daily_category_spikes()
    if not spikes:
        return

    with get_conn() as conn:
        for s in spikes:
            message = (
                "🚨 [L-Coup Direct 트렌드 경보]\n"
                f"네이버에서 '{s['category']}' 검색량이 전일 대비 {s['increase_rate']}% 급증!\n"
                "쿠팡 기획전 제안 및 광고 입찰가 조정을 검토하세요."
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO alerts
                    (category, alert_date, today_avg, yesterday_avg, increase_rate, message, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    s["category"],
                    s["alert_date"],
                    s["today_avg"],
                    s["yesterday_avg"],
                    s["increase_rate"],
                    message,
                    datetime.utcnow().isoformat(),
                ),
            )


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


@app.get("/api/analysis/spikes")
async def get_spike_keywords():
    """
    최근 3일 평균이 이전 14일 평균 대비 표준편차 2배 이상 급증한 키워드 그룹을
    급증 지수(spike_score) 높은 순으로 반환한다.
    """
    spikes = await detect_spike_keywords()
    return {"count": len(spikes), "spikes": spikes}


@app.get("/api/analysis/compare")
def compare_brands(
    category: str = Query("가전", description="비교할 키워드의 카테고리"),
    keyword_a: str = Query("LG 냉장고", description="비교 대상 A 키워드"),
    keyword_b: str = Query("삼성 냉장고", description="비교 대상 B 키워드"),
    source: str = Query("search", pattern="^(search|shopping)$", description="search 또는 shopping"),
):
    """
    두 키워드 그룹(예: 'LG 냉장고' vs '삼성 냉장고')의 최근 30일 점유율 스코어와
    MoM/YoY 변동률을 함께 반환한다.
    """
    group_a = f"{source}:{category}:{keyword_a}"
    group_b = f"{source}:{category}:{keyword_b}"

    with get_conn() as conn:
        mom_a, yoy_a = calculate_mom(conn, group_a), calculate_yoy(conn, group_a)
        mom_b, yoy_b = calculate_mom(conn, group_b), calculate_yoy(conn, group_b)

    avg_a = mom_a["current_avg"] or 0.0
    avg_b = mom_b["current_avg"] or 0.0
    total = avg_a + avg_b
    share_a = round(avg_a / total * 100, 2) if total > 0 else None
    share_b = round(avg_b / total * 100, 2) if total > 0 else None

    return {
        "category": category,
        "source": source,
        "comparison": [
            {
                "keyword": keyword_a,
                "group_name": group_a,
                "current_avg": mom_a["current_avg"],
                "share_pct": share_a,
                "mom_pct": mom_a["mom_pct"],
                "yoy_pct": yoy_a["yoy_pct"],
            },
            {
                "keyword": keyword_b,
                "group_name": group_b,
                "current_avg": mom_b["current_avg"],
                "share_pct": share_b,
                "mom_pct": mom_b["mom_pct"],
                "yoy_pct": yoy_b["yoy_pct"],
            },
        ],
    }


@app.get("/api/alerts/pending")
def get_pending_alerts():
    """
    전일 대비 검색 지수가 ALERT_INCREASE_THRESHOLD_PCT% 이상 폭증한 카테고리를
    감지해 alerts 큐에 적재하고, 아직 발송되지 않은(pending) 알림을 반환한다.
    카카오 MCP 연동 Claude 세션이 이 엔드포인트를 폴링해 알림을 발송한다
    (파일 상단의 "Claude 연동 가이드" 참고).
    """
    enqueue_pending_alerts()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, category, alert_date, today_avg, yesterday_avg,
                   increase_rate, message, status, created_at
            FROM alerts
            WHERE status = 'pending'
            ORDER BY created_at DESC
            """
        ).fetchall()
    return {"count": len(rows), "alerts": [dict(r) for r in rows]}


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    """카카오톡 발송에 성공한 알림을 'sent'로 표시해 중복 발송을 방지한다."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE alerts SET status = 'sent' WHERE id = ? AND status = 'pending'",
            (alert_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="대기 중인 알림을 찾을 수 없습니다.")
    return {"id": alert_id, "status": "sent"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
