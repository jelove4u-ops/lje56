# L-Coup Direct

네이버 데이터랩(검색어 트렌드 / 쇼핑인사이트)을 수집·분석해 가전 트렌드를 감지하고,
급상승 키워드와 전일 대비 폭증 카테고리를 카카오 MCP 알림 브릿지로 넘겨주는
FastAPI 백엔드 + React 대시보드 + Docker 패키징.

- `main.py` — FastAPI 백엔드 (sqlite3, 네이버 데이터랩 연동, 급상승/MoM·YoY 분석, 알림 큐)
- `frontend/` — Vite + React + Tailwind 대시보드 (Nginx로 정적 서빙)
- `Dockerfile` / `frontend/Dockerfile` / `docker-compose.yml` — 통합 구동 패키징

## 1. 사전 준비

```bash
cp .env.example .env
```

`.env`를 열어 네이버 오픈 API 자격 증명을 채운다 (https://developers.naver.com/apps 에서 발급):

```
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
```

이 파일은 `.gitignore`에 등록되어 있어 커밋되지 않는다. `docker-compose.yml`이
`env_file: .env`로 백엔드 컨테이너에 주입한다.

## 2. 한 번에 실행

```bash
docker-compose up --build
```

- 백엔드: http://localhost:8000 (컨테이너 내부 sqlite DB는 `backend_data` 볼륨에 영속화됨)
- 프런트엔드(Nginx가 서빙하는 프로덕션 빌드): http://localhost:8080
  - 프런트는 상대경로(`/api/...`)로 호출하고, Nginx가 이를 `backend` 컨테이너로 프록시한다
    (`frontend/nginx.conf`). 브라우저 입장에서는 동일 오리진이라 CORS 설정이 필요 없다.
- 헬스체크: `GET /health` — 백엔드가 `service_healthy`가 되어야 프런트 컨테이너가 기동한다.

첫 실행 시 DB가 비어 있으므로 데이터를 한 번 적재한다:

```bash
curl -X POST http://localhost:8000/api/sync
```

이후 대시보드(http://localhost:8080)를 열면 지표/차트/추천 키워드가 채워진다.
프런트는 20초 간격으로 자동 새로고침되며, 헤더의 새로고침 버튼으로 즉시 갱신할 수도 있다.

로컬 개발(Docker 없이)은 이전 단계와 동일하게 `uvicorn main:app --reload` +
`cd frontend && npm run dev`로 진행하면 된다 (`frontend/.env.example` 참고).

## 3. 시스템 점검 — 네이버 API 장애/Rate Limit 대응

`main.py`의 `fetch_naver_trend()`는 다음을 보장한다:

- **429 (Rate Limit 초과)**: `WARNING` 레벨로 로깅하고, 클라이언트에도 429를 그대로 전달해
  "한도 초과, 잠시 후 재시도"임을 구분할 수 있게 한다. 서버 프로세스는 죽지 않는다.
- **그 외 네이버 API 오류(5xx 등)**: `ERROR` 레벨로 로깅하고 502로 변환해 반환한다.
- **네트워크 오류(타임아웃 등)**: `httpx.RequestError`를 잡아 502로 변환하고 로깅한다.
- **예상 밖의 예외(그 외 전부)**: 전역 `@app.exception_handler(Exception)`이 잡아
  `logger.exception(...)`으로 스택트레이스를 남기고 500 JSON을 반환한다 — 어떤 경로로도
  uvicorn 워커 프로세스 자체가 죽지 않는다.

로그는 `docker-compose logs -f backend`로 확인한다. 로그 레벨은 `.env`에
`LOG_LEVEL=DEBUG` 등으로 조정 가능하다 (기본 `INFO`).

`POST /api/sync`가 429/502를 반환하면 몇 분 후 다시 호출하면 된다 — 이미 적재된
`trends` 데이터는 그대로 유지되므로 대시보드/분석 엔드포인트는 이전 데이터로 계속 동작한다.

## 4. 카카오 MCP 연동 — 경보를 실제로 받아보는 전체 워크플로우

이 백엔드는 카카오톡을 직접 호출하지 않는다. "누가 폴링해서 보낼지"는 카카오 MCP가
연결된 Claude 세션의 몫이다 (`main.py` 상단 주석의 "Claude 연동 가이드" 참고).

1. **스택 기동 + 데이터 적재**
   ```bash
   docker-compose up --build -d
   curl -X POST http://localhost:8000/api/sync
   ```
2. **카카오 MCP가 연결된 Claude 세션 준비.** 이 리포지토리를 사용하는 Claude Code 세션에
   카카오 MCP 도구(예: `send_kakaotalk_message`)가 연결되어 있어야 한다.
3. **폴링 자동화 구성.** 그 세션에서 주기적으로(예: Claude Code Routine) 아래를 반복 실행하도록
   설정한다:
   - `GET http://localhost:8000/api/alerts/pending` 호출
   - `alerts` 배열이 비어있지 않으면, 각 항목의 `message` 필드(이미 아래 포맷으로 렌더링됨)를
     그대로 카카오 MCP 메시지 발송 도구에 넘겨 발송

     ```
     🚨 [L-Coup Direct 트렌드 경보]
     네이버에서 '{category}' 검색량이 전일 대비 {increase_rate}% 급증!
     쿠팡 기획전 제안 및 광고 입찰가 조정을 검토하세요.
     ```
   - 발송에 성공한 알림은 `POST http://localhost:8000/api/alerts/{id}/ack` 로 확인 처리
     (중복 발송 방지)
4. **경보가 실제로 뜨는지 확인.** `/api/alerts/pending`은 호출될 때마다 전일 대비 카테고리
   검색 지수가 50% 이상 오른 경우를 자동 감지해 큐에 적재한다 — 별도 트리거 없이 폴링만
   하면 새 경보가 쌓인다. 데이터가 하루치뿐이면 "전일 대비" 계산이 불가능하므로,
   `/api/sync`를 며칠에 걸쳐 반복 실행하거나 최소 2일 이상의 데이터가 쌓인 뒤 테스트한다.

> 주의: 이 프로젝트의 Claude 세션은 위 폴링 자동화를 스스로 구성해주지 않는다 — 사용자가
> 별도로 Routine/트리거를 만들어야 실제로 동작한다. `main.py` 상단 주석은 그 자동화를
> 만들 때 참고할 문서일 뿐, 어떤 세션의 동작도 자동으로 트리거하지 않는다.

## 5. 유용한 명령어

```bash
docker-compose up --build      # 빌드 + 기동 (포그라운드)
docker-compose up --build -d   # 백그라운드
docker-compose logs -f backend # 백엔드 로그(Rate Limit/에러 확인)
docker-compose down            # 중지 (볼륨은 유지되어 DB 데이터 보존)
docker-compose down -v         # 중지 + DB 볼륨까지 삭제
```
