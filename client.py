"""카카오웹툰 BFF API 클라이언트.

HAR 트래픽 (검색/목록/뷰어) 분석 결과를 기반으로 작성.

쿠키는 Cookie-Editor 등으로 export한 webtoon.kakao.com 도메인 쿠키 JSON 그대로 받음.
필수 쿠키: 카카오 로그인 세션 (_kau, _kawlt, _kahai, _karmt 등) — 미로그인 시
        무료 회차만 일부 접근 가능, 기다무/구매 회차는 401.
"""
import base64
import hashlib
import io
import json
import os
import random
import re
import string
import struct
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

import requests

try:
    from Crypto.Cipher import AES
except Exception:  # 첫 로드 시점에 자동설치되지만 fallback
    AES = None


GW = 'https://gateway-kw.kakao.com'
WEB = 'https://webtoon.kakao.com'

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36')


class KakaotoonError(Exception):
    pass


class AuthRequiredError(KakaotoonError):
    """쿠키 없음/만료 — 사용자에게 재주입 요청 신호."""


class NotReadableError(KakaotoonError):
    """회차가 잠금 상태 (티켓 없음/대기중)."""


def _rand_nonce(n: int = 11) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _now_ms() -> str:
    return str(int(time.time() * 1000))


class KakaotoonClient:

    def __init__(self, cookies_json: str, logger=None):
        self.logger = logger
        self._parse_cookies(cookies_json)
        # webAppId는 카카오웹툰 SPA에서 device 식별자로 쓰임 — _karmt 쿠키 일부 + 타임스탬프 패턴.
        # 정확한 생성 로직은 모르지만 임의 문자열로도 미디어 리소스 응답을 받는 경우가 많음.
        self._web_app_id = self._gen_web_app_id()
        # access_token: 카카오웹툰은 별도 access_token을 cookie 또는 localStorage로 관리.
        # 쿠키 중 KW_AT, kw_at, access_token 등 흔한 이름을 찾아본다 (없으면 None).
        self._access_token: Optional[str] = None
        for c in self.cookies:
            n = (c['name'] or '').lower()
            if n in ('kw_at', 'kw-at', 'access_token', 'accesstoken'):
                self._access_token = c['value']
                break

    # ---- 내부 ----
    def _log(self, level: str, msg: str, *args):
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg, *args)
        else:
            print(f'[{level.upper()}] ' + (msg % args if args else msg))

    def _parse_cookies(self, cookies_json):
        if isinstance(cookies_json, list):
            data = cookies_json
        else:
            s = (cookies_json or '').strip()
            if not s:
                raise AuthRequiredError('cookies_json 비어있음')
            data = json.loads(s)
        self.cookies = []
        for c in data:
            if not c.get('name'):
                continue
            self.cookies.append({
                'name': c['name'], 'value': c.get('value', ''),
                'domain': c.get('domain', '.kakao.com'),
                'path': c.get('path', '/'),
            })
        if not any(c['name'] == '_kau' for c in self.cookies):
            # 카카오 통합 로그인 쿠키
            raise AuthRequiredError('필수 쿠키 _kau 없음 — webtoon.kakao.com 로그인 후 재주입 필요')

    @staticmethod
    def _gen_web_app_id() -> str:
        # HAR 예시: "KP.2200438603.1775312939209"
        # KP. + 10자리 숫자 + . + 13자리 timestamp (ms)
        rand = ''.join(random.choices(string.digits, k=10))
        return f'KP.{rand}.{_now_ms()}'

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            'User-Agent': DEFAULT_UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ko',
            'Origin': WEB,
            'Referer': WEB + '/',
        })
        for c in self.cookies:
            try:
                s.cookies.set(c['name'], c['value'],
                              domain=c['domain'].lstrip('.'),
                              path=c['path'])
            except Exception:
                pass
        if self._access_token:
            s.headers['Authorization'] = f'Bearer {self._access_token}'
        return s

    def _json(self, r: requests.Response) -> Dict[str, Any]:
        try:
            return r.json()
        except Exception:
            raise KakaotoonError(f'invalid JSON ({r.status_code}): {r.text[:200]}')

    def _check(self, body: Dict[str, Any], r: Optional[requests.Response] = None) -> Dict[str, Any]:
        # 카카오웹툰 BFF 응답 패턴:
        #   성공: {"data": ..., "meta": {...}}
        #   실패: {"errorCode": "...", "errorMessage": "..."} or HTTP 4xx/5xx
        if r is not None and r.status_code in (401, 403):
            raise AuthRequiredError(f'HTTP {r.status_code}: 인증 필요 — 쿠키 재주입')
        if isinstance(body, dict) and body.get('errorCode'):
            ec = body.get('errorCode')
            em = body.get('errorMessage') or ''
            if 'AUTH' in str(ec).upper() or 'LOGIN' in em.upper():
                raise AuthRequiredError(f'{ec}: {em}')
            if 'NOT_READABLE' in str(ec).upper() or 'NO_TICKET' in str(ec).upper():
                raise NotReadableError(f'{ec}: {em}')
            raise KakaotoonError(f'{ec}: {em}')
        return body

    # ---- 로그인 확인 ----
    def verify(self) -> bool:
        """쿠키 유효성 빠른 체크. /gift/v1/check-and-receive-free-tickets 같은
        로그인 필수 엔드포인트로 검증해도 되지만, 회차 목록(공개) 호출이 더 가벼움.
        실제 검증은 인기 컨텐츠(id=1) 등으로.
        """
        try:
            # 공개 검색이라도 200 떨어지면 게이트웨이 동작 OK. 로그인까지 검증은
            # tickets API로.
            s = self._session()
            r = s.get(f'{GW}/ticket/v2/views/content-home/available-tickets',
                      params={'contentId': 1}, timeout=10)
            if r.status_code in (401, 403):
                return False
            return r.status_code == 200
        except Exception:
            return False

    # ---- 검색 / 메타 ----
    def search_content(self, keyword: str, limit: int = 30, offset: int = 0) -> List[Dict]:
        s = self._session()
        r = s.get(f'{GW}/search/v2/content',
                  params={'word': keyword, 'limit': limit, 'offset': offset},
                  timeout=15)
        body = self._check(self._json(r), r)
        return (body.get('data') or {}).get('content', []) or []

    def find_content(self, title: str) -> Optional[Dict]:
        items = self.search_content(title)
        for it in items:
            if it.get('title') == title:
                return it
        return items[0] if items else None

    def get_content(self, content_id: int) -> Dict:
        s = self._session()
        r = s.get(f'{GW}/decorator/v2/decorator/contents/{content_id}', timeout=15)
        body = self._check(self._json(r), r)
        return body.get('data', {}) or {}

    def get_episodes(self, content_id: int, offset: int = 0,
                     limit: int = 30, sort: str = '-NO') -> Tuple[List[Dict], bool]:
        """회차 한 페이지. (episodes, has_more) 반환."""
        s = self._session()
        r = s.get(f'{GW}/episode/v2/views/content-home/contents/{content_id}/episodes',
                  params={'sort': sort, 'offset': offset, 'limit': limit}, timeout=15)
        body = self._check(self._json(r), r)
        data = body.get('data', {}) or {}
        eps = data.get('episodes') or []
        # meta가 없을 때도 안전하게 — limit 미만이면 끝
        more = len(eps) >= limit
        return eps, more

    def get_episodes_all(self, content_id: int, on_progress=None,
                         max_pages: int = 100) -> List[Dict]:
        out: List[Dict] = []
        seen = set()
        for page in range(max_pages):
            offset = page * 30
            try:
                eps, more = self.get_episodes(content_id, offset=offset)
            except KakaotoonError as e:
                self._log('warning', '회차 페이지 %d 실패: %s', page, e)
                break
            new = 0
            for e in eps:
                eid = e.get('id')
                if eid is None or eid in seen:
                    continue
                seen.add(eid)
                out.append(e)
                new += 1
            if on_progress:
                try:
                    on_progress(len(out))
                except Exception:
                    pass
            if new == 0 or not more:
                break
            time.sleep(0.2)
        return out

    # ---- 이용권 / 자동 충전 ----
    def get_available_tickets(self, content_id: int) -> Dict:
        s = self._session()
        r = s.get(f'{GW}/ticket/v2/views/content-home/available-tickets',
                  params={'contentId': content_id}, timeout=15)
        body = self._check(self._json(r), r)
        return body.get('data', {}) or {}

    def check_and_receive_free_tickets(self, content_id: int) -> Dict:
        """기다무 무료 티켓 충전 (수령). 응답: {freeTickets, received, totalTicketCount}."""
        s = self._session()
        s.headers['Content-Type'] = 'application/json'
        r = s.post(f'{GW}/gift/v1/check-and-receive-free-tickets',
                   data=json.dumps({'contentId': content_id}), timeout=15)
        body = self._check(self._json(r), r)
        return body.get('data', {}) or {}

    def continue_episode(self, content_id: int) -> Dict:
        """이어보기 — 현재 읽고 있는/다음 회차 정보."""
        s = self._session()
        s.headers['Content-Type'] = 'application/json'
        payload = {
            'contentId': content_id,
            'type': 'AES_CBC_WEBP',
            'timestamp': _now_ms(),
            'nonce': _rand_nonce(),
        }
        r = s.post(f'{GW}/episode/v1/views/continue',
                   data=json.dumps(payload), timeout=15)
        body = self._check(self._json(r), r)
        return body.get('data', {}) or {}

    # ---- 회차 열기 (티켓 사용) ----
    def pass_episode(self, episode_id: int) -> Dict:
        """회차 열람 등록 — 무료 회차면 그냥 통과, 기다무/대여권 회차면 차감.
        응답: {pass, alreadyRented, canNotUseWaitForFree, messageType, ...}
        """
        s = self._session()
        s.headers['Content-Type'] = 'application/json'
        r = s.post(f'{GW}/episode/v3/episodes/{episode_id}/pass',
                   data='{}', timeout=15)
        body = self._check(self._json(r), r)
        data = body.get('data', {}) or {}
        if not data.get('pass'):
            # 통과 못함 — 사유 확인
            mt = data.get('messageType', '')
            msg = data.get('message', '')
            if data.get('canNotUseWaitForFree'):
                raise NotReadableError(f'기다무 사용 불가: {msg}')
            raise NotReadableError(f'pass 실패 ({mt}): {msg}')
        return data

    def get_episode(self, episode_id: int) -> Dict:
        s = self._session()
        r = s.get(f'{GW}/episode/v2/episodes/{episode_id}', timeout=15)
        body = self._check(self._json(r), r)
        return body.get('data', {}) or {}

    # ---- 이미지 리소스 ----
    def get_media_resources(self, episode_id: int) -> Dict:
        """이미지 URL 목록 + AES key/iv (aid, zid) 받기."""
        s = self._session()
        s.headers['Content-Type'] = 'application/json'
        payload = {
            'id': episode_id,
            'type': 'AES_CBC_WEBP',
            'nonce': _rand_nonce(),
            'timestamp': _now_ms(),
            'download': False,
            'webAppId': self._web_app_id,
        }
        r = s.post(f'{GW}/episode/v1/views/viewer/episodes/{episode_id}/media-resources',
                   data=json.dumps(payload), timeout=20)
        body = self._check(self._json(r), r)
        return body.get('data', {}) or {}

    # ---- 다운로드 + 복호화 ----
    def download_image(self, url: str) -> bytes:
        """`.webp.cef` 암호화 이미지 raw bytes 다운로드."""
        s = self._session()
        s.headers.update({'Accept': 'image/avif,image/webp,*/*'})
        r = s.get(url, timeout=30)
        if r.status_code != 200:
            raise KakaotoonError(f'image fetch {r.status_code} {url[:120]}')
        return r.content

    @staticmethod
    def decrypt_cef(encrypted: bytes, aid_b64: str, zid_b64: str) -> bytes:
        """AES-CBC 복호화 후 PKCS7 패딩 제거 → WebP bytes 반환.

        키 추정:
          aid (base64, 32B) → AES-256 key
          zid (base64, 32B) → first 16B = IV (AES block size)
        실패 시 fallback 조합도 시도.
        """
        if AES is None:
            raise KakaotoonError('pycryptodome 미설치 — pip install pycryptodome')
        aid = base64.b64decode(aid_b64)
        zid = base64.b64decode(zid_b64)

        attempts = []
        # 1. 가장 가능성 높음: AES-256, key=aid(32B), iv=zid[:16]
        if len(aid) >= 32 and len(zid) >= 16:
            attempts.append(('aes256-key:aid32-iv:zid[:16]', aid[:32], zid[:16]))
        # 2. AES-256, key=aid, iv=zid[16:32]
        if len(aid) >= 32 and len(zid) >= 32:
            attempts.append(('aes256-key:aid32-iv:zid[16:32]', aid[:32], zid[16:32]))
        # 3. AES-128, key=aid[:16], iv=zid[:16]
        if len(aid) >= 16 and len(zid) >= 16:
            attempts.append(('aes128-key:aid[:16]-iv:zid[:16]', aid[:16], zid[:16]))
        # 4. AES-128, key=zid[:16], iv=aid[:16]   (swap 시도)
        if len(aid) >= 16 and len(zid) >= 16:
            attempts.append(('aes128-key:zid[:16]-iv:aid[:16]', zid[:16], aid[:16]))

        last_err = None
        for name, key, iv in attempts:
            try:
                cipher = AES.new(key, AES.MODE_CBC, iv)
                dec = cipher.decrypt(encrypted)
                # PKCS7 패딩 제거
                if dec:
                    pad = dec[-1]
                    if 1 <= pad <= 16 and dec.endswith(bytes([pad]) * pad):
                        dec = dec[:-pad]
                # WebP magic check: RIFF....WEBP
                if len(dec) >= 12 and dec[:4] == b'RIFF' and dec[8:12] == b'WEBP':
                    return dec
                # JPEG/PNG 도 일단 수용
                if dec[:3] == b'\xff\xd8\xff' or dec[:8] == b'\x89PNG\r\n\x1a\n':
                    return dec
            except Exception as e:
                last_err = e
                continue
        raise KakaotoonError(f'복호화 실패: 모든 조합 시도. last={last_err}')

    # ---- 이미지 포맷 변환 ----
    @staticmethod
    def to_image_format(image_bytes: bytes, fmt: str,
                        jpg_quality: int = 92) -> Tuple[bytes, str]:
        """복호화된 이미지 bytes를 jpg/png로 변환. fmt='webp'면 원본 그대로.
        반환: (bytes, ext) — ext는 '.webp'/'.jpg'/'.png'.

        이미 원본이 JPG/PNG였으면 (드물지만 fallback) 그 형식 유지.
        """
        # 원본 매직바이트로 이미 JPG/PNG면 그대로
        if image_bytes[:3] == b'\xff\xd8\xff':
            return image_bytes, '.jpg'
        if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            return image_bytes, '.png'

        fmt = (fmt or 'webp').lower().strip()
        if fmt in ('webp', ''):
            return image_bytes, '.webp'

        try:
            from PIL import Image
        except Exception:
            return image_bytes, '.webp'

        try:
            img = Image.open(io.BytesIO(image_bytes))
            if fmt in ('jpg', 'jpeg'):
                # JPG는 알파 채널 없음 — 흰 배경 합성
                if img.mode in ('RGBA', 'LA'):
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                elif img.mode == 'P':
                    img = img.convert('RGBA')
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                out = io.BytesIO()
                img.save(out, format='JPEG', quality=jpg_quality, optimize=False)
                return out.getvalue(), '.jpg'
            if fmt == 'png':
                if img.mode == 'P':
                    img = img.convert('RGBA')
                out = io.BytesIO()
                img.save(out, format='PNG', optimize=False)
                return out.getvalue(), '.png'
        except Exception:
            pass
        return image_bytes, '.webp'

    # ---- URL 파싱 ----
    @staticmethod
    def extract_content_id(url_or_id: str) -> Optional[int]:
        """카카오웹툰 URL/숫자에서 content_id 추출.

        지원:
          - https://webtoon.kakao.com/content/{seoId}/{contentId}
          - https://webtoon.kakao.com/content/{contentId}
          - https://webtoon.kakao.com/viewer/{contentId}/{episodeId}
          - kakaowebtoon://content/{contentId}
          - 숫자
        """
        s = (url_or_id or '').strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        # /content/.../숫자
        m = re.search(r'/content/[^/]+/(\d+)', s)
        if m:
            return int(m.group(1))
        # /content/숫자
        m = re.search(r'/content/(\d+)(?:/|$|\?)', s)
        if m:
            return int(m.group(1))
        # /viewer/숫자/숫자  (앞이 contentId)
        m = re.search(r'/viewer/(\d+)/\d+', s)
        if m:
            return int(m.group(1))
        # ?contentId=숫자
        m = re.search(r'[?&]contentId=(\d+)', s, re.I)
        if m:
            return int(m.group(1))
        # 마지막 6자리 이상 숫자
        m = re.search(r'(\d{2,})', s)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def extract_episode_id(url: str) -> Optional[int]:
        """뷰어 URL에서 episode_id 추출 (선택)."""
        m = re.search(r'/viewer/\d+/(\d+)', url or '')
        return int(m.group(1)) if m else None

    @staticmethod
    def episode_availability(ep: Dict) -> str:
        """회차 메타에서 무료/대여/잠금 추정.

        useType:
          FREE: 무료
          WAIT_FOR_FREE: 기다무 (충전 안되면 잠금)
          PAY: 유료 (대여/소장)
          EARLY_ACCESS: 미리보기 (유료)
        readable: 실제로 볼 수 있는지
        """
        if ep.get('readable'):
            if (ep.get('useType') or '') == 'FREE':
                return 'free'
            return 'unlocked'
        # readable=False
        ut = (ep.get('useType') or '').upper()
        if ut == 'WAIT_FOR_FREE':
            return 'waitfree'  # 기다무 대기 중
        if ut in ('PAY', 'EARLY_ACCESS'):
            return 'pay'
        return 'locked'

    @staticmethod
    def episode_no(ep: Dict) -> int:
        return int(ep.get('no') or 0)
