# kaka_toon_dl

카카오웹툰 기다무 무료 티켓 자동 사용 + 회차 이미지 다운로드 SJVA 플러그인.

> kakaopage_dl 의 형제 플러그인. 카카오페이지가 아니라 **카카오웹툰**(webtoon.kakao.com)을 대상으로 한다.

## 동작

스케줄러가 돌 때마다 설정에 적힌 작품들을 순회:

1. 작품 검색 (`gateway-kw.kakao.com/search/v2/content`) → contentId 매칭
2. 회차 목록 (`/episode/v2/views/content-home/contents/{id}/episodes`) 페이징 수집
3. 회차 분류
   - `readable=true` + `useType=FREE` → 무료 / 그 외 readable → 보유
   - `useType=WAIT_FOR_FREE` + readable=false → 기다무 대기
   - `useType=PAY|EARLY_ACCESS` → 유료
4. 무료/보유 회차는 바로 다운, 기다무 대기 회차는:
   - `POST /gift/v1/check-and-receive-free-tickets` → 충전 시도
   - `GET /ticket/v2/.../available-tickets` → `ticketCount` 확인
   - `POST /episode/v3/episodes/{id}/pass` (body `{}`) → 차감 + 열람 등록
5. `POST /episode/v1/views/viewer/episodes/{id}/media-resources` → `media.files[].url` (`.webp.cef`) + AES key/iv (`aid`, `zid`)
6. `.webp.cef` 다운로드 → AES-CBC 복호화 → `.webp` 저장
7. DB(`kakao_toon_dl_item`)에 회차별 이력 기록 — 같은 회차 재다운로드 안 함

## 이미지 복호화 (AES_CBC_WEBP)

카카오웹툰은 회차 이미지를 **AES-CBC 암호화된 WebP** (`.webp.cef`) 로 내려준다. 복호화 키는 `media-resources` 응답에 같이 옴.

```json
{
  "media": {
    "aid": "<base64 32B>",   // AES-256 key
    "zid": "<base64 32B>",   // 첫 16B = IV
    "container": "WEBP",
    "codec": "VP8",
    "files": [{ "url": "https://kr-a.kakaopagecdn.com/...webp.cef?__token__=..." }]
  }
}
```

`client.KakaotoonClient.decrypt_cef()` 가 여러 키 조합을 시도하며, 복호화된 데이터가 RIFF/WEBP 매직바이트로 시작하면 성공으로 본다. 실패하면 원본 `.webp.cef` + `_keys.json` 을 그대로 남겨두니 별도 도구로 후처리 가능.

## 설정 (`설정` 메뉴)

| 항목 | 설명 |
|---|---|
| 체크할 웹툰 | 한 줄당 하나. 작품 제목 / URL / contentId(숫자) 모두 가능 |
| 카카오 쿠키 JSON | Cookie-Editor 로 `webtoon.kakao.com` 도메인 쿠키 export 한 JSON |
| 다운로드 경로 | `{경로}/{작품}/{NNNN_회차}/{001.webp ...}` |
| 1회 실행 최대 사용 장수 | 기다무 무료 티켓 사용 한도 (보통 1) |
| 기다무 무료 티켓 사용 | On 권장 |
| 보유 일반 티켓 사용 | Off 권장. On이면 보유 일반 이용권 잔량 전부 사용 |

### 쿠키 주입

1. Chrome 에 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) 설치
2. `webtoon.kakao.com` 카카오 로그인
3. Cookie-Editor → Export → JSON → 복사
4. 설정의 "카카오 쿠키" 텍스트박스에 붙여넣기 + 저장
5. "쿠키 검증" 으로 유효 확인

## API 메모

| 엔드포인트 | 용도 |
|---|---|
| `GET /search/v2/content?word=...` | 작품 검색 |
| `GET /decorator/v2/decorator/contents/{contentId}` | 컨텐츠 메타 |
| `GET /episode/v2/views/content-home/contents/{contentId}/episodes?sort=-NO&offset=N&limit=30` | 회차 페이징 |
| `GET /ticket/v2/views/content-home/available-tickets?contentId=...` | 보유 티켓 수 |
| `POST /gift/v1/check-and-receive-free-tickets` `{contentId}` | 기다무 무료 티켓 충전 |
| `POST /episode/v1/views/continue` `{contentId, type:AES_CBC_WEBP, timestamp, nonce}` | 이어보기 (현재/다음 회차) |
| `POST /episode/v3/episodes/{episodeId}/pass` `{}` | 회차 열람 등록 (티켓 차감) |
| `GET /episode/v2/episodes/{episodeId}` | 회차 메타 |
| `POST /episode/v1/views/viewer/episodes/{id}/media-resources` `{id, type:AES_CBC_WEBP, nonce, timestamp, download:false, webAppId}` | 이미지 URL + aid/zid |
| `GET /auth/v1/auth/token/refresh?access_token=...` | access_token 갱신 (선택) |
