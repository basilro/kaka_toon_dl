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
        """쿠키가 로그인 세션으로 유효한지 확인.

        `/popularity/v1/my-review?episodeId=X` 응답의 userId 필드 존재 여부로
        판정. 안정적으로 존재하는 free 회차 후보 여러 개 순차 시도 — 하나라도
        userId 가 잡히면 OK.
        """
        s = self._session()
        candidates = (189606, 26737, 26638)  # 인기작 안정 회차 ID
        for eid in candidates:
            try:
                r = s.get(f'{GW}/popularity/v1/my-review',
                          params={'episodeId': eid}, timeout=10)
                if r.status_code in (401, 403):
                    self._log('info', 'verify ep=%s → %d (auth fail)',
                              eid, r.status_code)
                    return False
                if r.status_code != 200:
                    self._log('info', 'verify ep=%s → %d, 다음 후보 시도',
                              eid, r.status_code)
                    continue
                body = r.json()
                uid = (body.get('data') or {}).get('userId')
                if uid:
                    self._log('info', 'verify OK (ep=%s, userId=%s)', eid, uid)
                    return True
            except Exception as e:
                self._log('info', 'verify ep=%s 예외: %s', eid, e)
                continue
        return False

    # ---- 공지 ----
    def get_notice_list(self, offset: int = 0, limit: int = 30) -> List[Dict]:
        """공지 목록 (more/notice 페이지의 그것).

        반환: [{id, platform, title, content(HTML)}, ...]
        title 패턴: 'YYYY년 M월 유료화 & 종료 작품 관련 안내'
        """
        s = self._session()
        s.headers['Referer'] = WEB + '/more/notice'
        r = s.get(f'{GW}/notification/v1/notice',
                  params={'offset': offset, 'limit': limit}, timeout=15)
        body = self._check(self._json(r), r)
        return body.get('data') or []

    @staticmethod
    def parse_paid_notice(content_html: str) -> List[Dict[str, str]]:
        """유료화/종료 공지 본문(HTML)에서 작품 목록 추출.

        패턴 예시 (HTML 태그 제거 후):
          <유료화 작품>
          1. 무적자 / 노경찬, 휘, 임준욱 (5월 12일)
          - 무료회차 범위 : 1화 ~ 10화 / 후기
          ...
          <종료 작품>
          1. 작품명 / 작가 (날짜)

        반환: [{'title': '무적자', 'author': '노경찬, 휘, 임준욱',
                'date': '5월 12일', 'kind': 'paid'|'ended'}]
        """
        import html as _html
        # <p> <br> 등 → 개행. 나머지 태그는 공백
        plain = re.sub(r'<\s*(br|p|/p|/div|/li|li)[^>]*>', '\n', content_html or '',
                       flags=re.IGNORECASE)
        plain = re.sub(r'<[^>]+>', '', plain)
        plain = _html.unescape(plain)

        results: List[Dict[str, str]] = []
        kind = None  # paid / ended
        # 아이템 패턴: 줄 시작에 "숫자." 다음 "제목 / 작가 (날짜)"
        item_re = re.compile(
            r'^\s*\d+\s*[\.．、]\s*(.+?)\s*[/／]\s*(.+?)\s*\(([^)]+?)\)\s*$')
        for raw_line in plain.split('\n'):
            line = raw_line.strip()
            if not line:
                continue
            # 섹션 마커
            if '유료화' in line and ('작품' in line) and len(line) < 30:
                kind = 'paid'
                continue
            if '종료' in line and ('작품' in line) and len(line) < 30:
                kind = 'ended'
                continue
            if kind is None:
                continue
            m = item_re.match(line)
            if not m:
                continue
            title = m.group(1).strip()
            author = m.group(2).strip()
            date = m.group(3).strip()
            # 너무 짧거나 부적합한 항목 거름
            if len(title) < 1 or len(title) > 80:
                continue
            results.append({'title': title, 'author': author,
                            'date': date, 'kind': kind})
        return results

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

    # ---- 사용자 ID (복호화 키 도출에 필요) ----
    def get_user_id(self, any_episode_id: int) -> Optional[str]:
        """로그인된 사용자의 kakao webtoon userId (예: 'koru00000020f7d0b7').

        /popularity/v1/my-review?episodeId=... 응답의 userId 필드.
        decrypt_cef 의 키 도출에 사용됨.
        """
        s = self._session()
        try:
            r = s.get(f'{GW}/popularity/v1/my-review',
                      params={'episodeId': any_episode_id}, timeout=10)
            body = self._check(self._json(r), r)
            return (body.get('data') or {}).get('userId')
        except Exception as e:
            self._log('warning', 'get_user_id 실패: %s', e)
            return None

    # ---- 이미지 리소스 ----
    def get_media_resources(self, episode_id: int) -> Dict:
        """이미지 URL 목록 + 암호화 키 (aid, zid) + 키 도출 메타 받기.

        반환 dict 에는 응답 data 외에 `_req` 필드로 우리가 보낸 nonce/timestamp
        도 포함 — 이 값들이 복호화 키 도출에 사용됨.
        """
        s = self._session()
        s.headers['Content-Type'] = 'application/json'
        nonce = _rand_nonce()
        timestamp = _now_ms()
        payload = {
            'id': episode_id,
            'type': 'AES_CBC_WEBP',
            'nonce': nonce,
            'timestamp': timestamp,
            'download': False,
            'webAppId': self._web_app_id,
        }
        r = s.post(f'{GW}/episode/v1/views/viewer/episodes/{episode_id}/media-resources',
                   data=json.dumps(payload), timeout=20)
        body = self._check(self._json(r), r)
        data = body.get('data', {}) or {}
        data['_req'] = {'nonce': nonce, 'timestamp': timestamp,
                        'episode_id': episode_id}
        return data

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
    def _pkcs7_unpad(b: bytes) -> bytes:
        if not b:
            return b
        p = b[-1]
        if 1 <= p <= 16 and b.endswith(bytes([p]) * p):
            return b[:-p]
        return b

    @staticmethod
    def derive_image_key_iv(aid_b64: str, zid_b64: str,
                            user_id: Optional[str], episode_id: int,
                            timestamp: str, nonce: str) -> Tuple[bytes, bytes]:
        """카카오웹툰 viewer JS 분석으로 확보한 키 도출 알고리즘.

        u = (userId || episodeId) + episodeId + timestamp
        c = nonce + timestamp
        keyMat = SHA-256(u)                            # 32B AES-256 unwrap key
        ivMat  = SHA-256(c)[:16]                       # 16B unwrap IV
        actualKey = unpad(AES-256-CBC-decrypt(base64decode(aid), keyMat, ivMat))  # 16B
        actualIv  = unpad(AES-256-CBC-decrypt(base64decode(zid), keyMat, ivMat))  # 16B
        """
        if AES is None:
            raise KakaotoonError('pycryptodome 미설치 — pip install pycryptodome')
        uid_part = str(user_id) if user_id else str(episode_id)
        u_str = uid_part + str(episode_id) + str(timestamp)
        c_str = str(nonce) + str(timestamp)
        # JS의 charCodeAt(0) 동일: 각 char를 1 byte로 (latin-1).
        key_mat = hashlib.sha256(u_str.encode('latin-1')).digest()
        iv_mat = hashlib.sha256(c_str.encode('latin-1')).digest()[:16]

        wrapped_key = base64.b64decode(aid_b64)
        wrapped_iv = base64.b64decode(zid_b64)
        actual_key = KakaotoonClient._pkcs7_unpad(
            AES.new(key_mat, AES.MODE_CBC, iv_mat).decrypt(wrapped_key))
        actual_iv = KakaotoonClient._pkcs7_unpad(
            AES.new(key_mat, AES.MODE_CBC, iv_mat).decrypt(wrapped_iv))
        # 실제 이미지 cipher 는 AES-128 (16B key, 16B IV)
        if len(actual_key) not in (16, 24, 32):
            raise KakaotoonError(f'actual_key 길이 비정상: {len(actual_key)}B')
        if len(actual_iv) != 16:
            # IV 가 16B 아니면 첫 16B 만 사용 (자기방어)
            actual_iv = actual_iv[:16] if len(actual_iv) >= 16 else actual_iv.ljust(16, b'\0')
        return actual_key, actual_iv

    @staticmethod
    def decrypt_cef(encrypted: bytes, aid_b64: str, zid_b64: str,
                    user_id: Optional[str], episode_id: int,
                    timestamp: str, nonce: str) -> bytes:
        """`.webp.cef` 암호화 이미지 복호화.

        viewer JS 의 키 도출 + AES-CBC decrypt + PKCS7 unpad.
        결과: WebP bytes (RIFF...WEBP).
        """
        if AES is None:
            raise KakaotoonError('pycryptodome 미설치 — pip install pycryptodome')
        key, iv = KakaotoonClient.derive_image_key_iv(
            aid_b64, zid_b64, user_id, episode_id, timestamp, nonce)
        dec = AES.new(key, AES.MODE_CBC, iv).decrypt(encrypted)
        dec = KakaotoonClient._pkcs7_unpad(dec)
        if not (len(dec) >= 12 and dec[:4] == b'RIFF' and dec[8:12] == b'WEBP'):
            # 매직 검증 실패 — JPG/PNG fallback (드물지만 fallback 응답)
            if dec[:3] != b'\xff\xd8\xff' and dec[:8] != b'\x89PNG\r\n\x1a\n':
                raise KakaotoonError(
                    f'복호화 결과가 WebP/JPG/PNG 아님 (first8={dec[:8].hex()})')
        return dec

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
