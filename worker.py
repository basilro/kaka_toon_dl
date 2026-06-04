"""스케줄 1회 실행 단위 — 작품 리스트를 돌면서 다운로드 시도."""
import os
import re
import threading
from datetime import datetime, date
from typing import List, Optional, Dict, Any, Tuple

from .client import (KakaotoonClient, KakaotoonError,
                     AuthRequiredError, NotReadableError)
from .model import ModelKakaotoonItem
from .notify import (send_webhook, build_download_summary,
                     build_cookie_expired_message,
                     build_notice_failure_message)
from . import meta as meta_mod
from .setup import *  # P, db, logger


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


_IMAGE_EXTS = ('.webp', '.jpg', '.jpeg', '.png', '.gif', '.bmp')


def compress_episode_folder(ep_folder: str) -> Optional[str]:
    """회차 폴더 → 같은 위치에 .zip 생성. 성공 시 원본 폴더 삭제. 멱등.

    이미 .zip 이 있으면 그대로 둠 (덮어쓰지 않음). 이미지 파일만 포함 —
    .webp.cef 원본 / _keys.json 같은 디버그 파일은 제외.

    반환: 생성/기존 zip 경로 또는 None (실패).

    안전장치: 폴더 안에 서브디렉토리가 있으면 회차 폴더가 아닌 작품/그룹 폴더로
    판단하여 압축 거부 (실수로 작품 전체를 날리는 사고 방지).
    """
    import shutil
    import zipfile
    if not os.path.isdir(ep_folder):
        return None

    try:
        entries = os.listdir(ep_folder)
    except Exception:
        return None
    for entry in entries:
        if os.path.isdir(os.path.join(ep_folder, entry)):
            P.logger.warning(
                '압축 거부 (서브디렉토리 존재 → 회차 폴더 아님): %s', ep_folder)
            return None

    parent = os.path.dirname(ep_folder)
    name = os.path.basename(ep_folder)
    zip_path = os.path.join(parent, name + '.zip')
    if os.path.exists(zip_path):
        # 이미 압축됨 — 원본 폴더 남아있으면 삭제만
        try:
            shutil.rmtree(ep_folder)
        except Exception:
            pass
        return zip_path

    files_to_zip = []
    for f in sorted(entries):
        path = os.path.join(ep_folder, f)
        if os.path.isfile(path) and f.lower().endswith(_IMAGE_EXTS):
            files_to_zip.append((f, path))
    if not files_to_zip:
        return None

    tmp_zip = zip_path + '.tmp'
    try:
        with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_STORED) as zf:
            for arcname, path in files_to_zip:
                zf.write(path, arcname=arcname)
        os.replace(tmp_zip, zip_path)
    except Exception as e:
        if os.path.exists(tmp_zip):
            try:
                os.remove(tmp_zip)
            except Exception:
                pass
        P.logger.warning('압축 실패 %s: %s', ep_folder, e)
        return None

    try:
        shutil.rmtree(ep_folder)
    except Exception as e:
        P.logger.warning('압축 후 폴더 삭제 실패 %s: %s', ep_folder, e)
    return zip_path


# ---- 자동 다운로드 진행 상태 (싱글톤) ----
_auto_state_lock = threading.Lock()
_auto_state: Dict[str, Any] = {
    'status': 'idle',
    'started_at': None,
    'finished_at': None,
    'message': '',
    'titles_total': 0,
    'titles_done': 0,
    'current_title': '',
    'current_phase': '',
    'current_episode': '',
    'current_pages_done': 0,
    'current_pages_total': 0,
    'summary': {'downloaded': 0, 'skipped': 0, 'failed': 0},
}


def get_auto_state() -> Dict[str, Any]:
    with _auto_state_lock:
        snap = dict(_auto_state)
        snap['summary'] = dict(_auto_state['summary'])
        return snap


def _auto_set(**kw):
    with _auto_state_lock:
        _auto_state.update(kw)


def _auto_reset():
    with _auto_state_lock:
        _auto_state.update({
            'status': 'idle', 'started_at': None, 'finished_at': None,
            'message': '', 'titles_total': 0, 'titles_done': 0,
            'current_title': '', 'current_phase': '',
            'current_episode': '', 'current_pages_done': 0, 'current_pages_total': 0,
            'summary': {'downloaded': 0, 'skipped': 0, 'failed': 0},
        })


def _auto_summary_inc(key: str, delta: int = 1):
    with _auto_state_lock:
        _auto_state['summary'][key] = _auto_state['summary'].get(key, 0) + delta


class Worker:

    def __init__(self):
        self.cfg = P.ModelSetting.to_dict()
        self.download_root = (self.cfg.get('download_path') or '').strip()
        self.cookies_json = (self.cfg.get('cookies_json') or '').strip()
        self.items: List[str] = self._split_items(self.cfg.get('titles') or '')
        self.max_per_run = int(self.cfg.get('max_per_run') or '1')
        self.use_waitfree = (self.cfg.get('use_waitfree') or 'True') == 'True'
        self.use_owned_ticket = (self.cfg.get('use_owned_ticket') or 'False') == 'True'
        self.output_format = (self.cfg.get('output_format') or 'webp').lower().strip()
        self.notice_auto_dl = (self.cfg.get('notice_auto_dl') or 'False') == 'True'
        self.notice_subdir = (self.cfg.get('notice_subdir') or '완결').strip() or '완결'
        self.notify_cookie_url = (self.cfg.get('notify_webhook_cookie') or '').strip()
        self.notify_download_url = (self.cfg.get('notify_webhook_download') or '').strip()
        self.proxy_url = KakaotoonClient.resolve_proxy(
            self.cfg.get('use_proxy'), self.cfg.get('proxy_url'))
        self.use_compress = (self.cfg.get('use_compress') or 'False') == 'True'
        self.client: Optional[KakaotoonClient] = None
        self.user_id: Optional[str] = None  # 첫 회차 처리 시 lazy 로드
        self.completed_items: List[Dict[str, Any]] = []  # 알림용 누적

    @staticmethod
    def _split_items(raw: str) -> List[str]:
        out = []
        for chunk in (raw or '').replace('\r', '').replace('|', '\n').split('\n'):
            s = chunk.strip()
            if s:
                out.append(s)
        return out

    # ---- public ----
    def run(self) -> dict:
        P.logger.info('[basic] Worker.run BEGIN items=%s use_waitfree=%s use_owned_ticket=%s max_per_run=%s',
                      self.items, self.use_waitfree, self.use_owned_ticket, self.max_per_run)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='시작', titles_total=len(self.items))
        if not self.download_root:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.cookies_json:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='cookies_json 미설정')
            return {'ret': 'fail', 'reason': 'no_cookies'}
        if not self.items:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='체크할 작품 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = KakaotoonClient(self.cookies_json, logger=P.logger,
                                          proxy_url=self.proxy_url)
        except AuthRequiredError as e:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'쿠키 인증 실패: {e}')
            return {'ret': 'fail', 'reason': 'auth', 'msg': str(e)}

        if not self.client.verify():
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='쿠키 만료 — 재주입 필요')
            # 만료 알림 — 1회만 발송 (스팸 방지)
            try:
                already = (P.ModelSetting.get('cookie_expired_notified') or 'False') == 'True'
                if not already and self.notify_cookie_url:
                    if send_webhook(self.notify_cookie_url,
                                    build_cookie_expired_message()):
                        P.ModelSetting.set('cookie_expired_notified', 'True')
            except Exception as e:
                P.logger.warning('쿠키 만료 알림 발송 실패: %s', e)
            return {'ret': 'fail', 'reason': 'cookie_expired'}

        # 정상 verify → 만료 플래그 리셋 (다음 만료 때 다시 1회 알림 가능)
        try:
            if (P.ModelSetting.get('cookie_expired_notified') or 'False') == 'True':
                P.ModelSetting.set('cookie_expired_notified', 'False')
        except Exception:
            pass

        summary = {'titles': len(self.items), 'downloaded': 0, 'skipped': 0, 'failed': 0}
        for raw in self.items:
            _auto_set(current_title=raw, current_phase='searching',
                      current_episode='', current_pages_done=0, current_pages_total=0)
            try:
                got = self._process_item(raw)
                if got == 'downloaded':
                    summary['downloaded'] += 1; _auto_summary_inc('downloaded')
                elif got == 'skipped':
                    summary['skipped'] += 1; _auto_summary_inc('skipped')
                else:
                    summary['failed'] += 1; _auto_summary_inc('failed')
            except Exception as e:
                import traceback
                P.logger.error('process item %r exception: %s', raw, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1; _auto_summary_inc('failed')
            _auto_set(titles_done=summary['downloaded'] + summary['skipped'] + summary['failed'])

        # ---- 공지 기반 자동 다운로드 (유료화/종료 작품) ----
        if self.notice_auto_dl:
            try:
                ncount = self._process_notices(summary)
                P.logger.info('[basic] notice 처리: %d 작품 추가 다운', ncount)
            except Exception as e:
                import traceback
                P.logger.error('notice 처리 예외: %s', e)
                P.logger.error(traceback.format_exc())

        # ---- 다운로드 완료 요약 알림 (받은 게 있을 때만) ----
        if self.completed_items and self.notify_download_url:
            try:
                msg = build_download_summary(self.completed_items)
                if msg:
                    ok = send_webhook(self.notify_download_url, msg)
                    P.logger.info('다운로드 요약 알림 발송: %s (%d건)',
                                  'OK' if ok else 'FAIL', len(self.completed_items))
            except Exception as e:
                P.logger.warning('다운로드 요약 알림 예외: %s', e)

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"완료 — 다운 {summary['downloaded']}, 스킵 {summary['skipped']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ---- 공지 처리 ----
    @staticmethod
    def _extract_notice_year(title: str) -> int:
        m = re.search(r'(\d{4})\s*년', title or '')
        if m:
            return int(m.group(1))
        return date.today().year

    @staticmethod
    def _parse_item_date(year: int, date_str: str) -> Optional[date]:
        if not date_str:
            return None
        m = re.search(r'(\d+)\s*월\s*(\d+)\s*일', date_str)
        if not m:
            return None
        try:
            return date(year, int(m.group(1)), int(m.group(2)))
        except Exception:
            return None

    def _process_notices(self, summary: Dict[str, int]) -> int:
        """유료화/종료 공지 → 작품 목록 추출 → 무료/보유 회차 다운로드.

        작품별 (M월 N일) 일자가 오늘보다 과거면 자동 스킵 (이미 유료화돼서
        무료로 받을 수 없음).
        """
        _auto_set(current_phase='fetch_notice', current_title='[공지 확인]',
                  current_episode='', current_pages_done=0, current_pages_total=0)
        try:
            notices = self.client.get_notice_list(limit=30)
        except KakaotoonError as e:
            P.logger.warning('공지 목록 조회 실패: %s', e)
            return 0

        # 매월 유료화/종료 공지는 하나 — 최근 매칭된 1개만 사용
        paid_notice = None
        for n in notices:
            title = n.get('title') or ''
            if '유료화' in title or '종료' in title:
                paid_notice = n
                break
        if paid_notice is None:
            P.logger.info('유료화/종료 공지 없음 (공지 응답 %d개 중)', len(notices))
            return 0

        year = self._extract_notice_year(paid_notice.get('title') or '')
        content = paid_notice.get('content') or ''
        items = KakaotoonClient.parse_paid_notice(content)
        P.logger.info('대상 공지: id=%s title=%r (본문 %dB, 파싱 %d개)',
                      paid_notice.get('id'), paid_notice.get('title'),
                      len(content), len(items))
        if not items and content:
            # 파싱 0개일 때 진단용 본문 미리보기
            preview = re.sub(r'<[^>]+>', ' ', content[:600])
            preview = re.sub(r'\s+', ' ', preview).strip()
            P.logger.info('  [파싱실패 진단] 본문 미리보기: %s', preview[:300])

        today = date.today()
        seen_titles: set = set()
        targets: List[Dict[str, str]] = []
        skipped_past = 0
        for it in items:
            t = it['title']
            if t in seen_titles:
                continue
            paid_dt = self._parse_item_date(year, it.get('date', ''))
            if paid_dt is not None and paid_dt < today:
                skipped_past += 1
                continue
            seen_titles.add(t)
            it['_paid_date'] = paid_dt.isoformat() if paid_dt else ''
            it['_year'] = year
            targets.append(it)
        P.logger.info('공지 추출 작품: %d개 (paid=%d ended=%d, 과거날짜 스킵=%d)',
                      len(targets),
                      sum(1 for x in targets if x['kind'] == 'paid'),
                      sum(1 for x in targets if x['kind'] == 'ended'),
                      skipped_past)
        if not targets:
            return 0

        processed = 0
        failed_works: List[Dict[str, str]] = []   # 알림용 — 검색/처리 실패분
        for it in targets:
            title = it['title']
            _auto_set(current_title=f'[공지] {title}',
                      current_phase='searching',
                      current_episode='', current_pages_done=0, current_pages_total=0)
            try:
                got = self._process_notice_content(title)
                if got == 'downloaded':
                    summary['downloaded'] += 1; _auto_summary_inc('downloaded')
                    processed += 1
                elif got == 'skipped':
                    summary['skipped'] += 1; _auto_summary_inc('skipped')
                elif got == 'notfound':
                    summary['failed'] += 1; _auto_summary_inc('failed')
                    failed_works.append({'title': title, 'reason': '검색 매칭 실패'})
                else:  # 'failed'
                    summary['failed'] += 1; _auto_summary_inc('failed')
                    failed_works.append({'title': title, 'reason': '회차 조회/처리 실패'})
            except Exception as e:
                P.logger.warning('공지 작품 %r 처리 실패: %s', title, e)
                summary['failed'] += 1; _auto_summary_inc('failed')
                failed_works.append({'title': title, 'reason': f'예외: {e}'})

        # 실패 작품 웹훅 알림 (신규 실패분만 — 매 실행 스팸 방지)
        self._notify_notice_failures(paid_notice.get('id'),
                                     paid_notice.get('title'), failed_works)
        return processed

    def _notify_notice_failures(self, notice_id, notice_title,
                                failed_works: List[Dict[str, str]]) -> None:
        """공지 작품 다운 실패분을 웹훅으로 1회 알림.

        같은 공지(id) 내에서 이미 알린 작품은 다시 알리지 않는다 — 매 스케줄
        실행마다 검색실패가 반복돼도 알림은 신규분만 발송 (스팸 방지).
        """
        if not failed_works or not self.notify_download_url:
            return
        import json as _json
        nid = str(notice_id)
        try:
            raw = P.ModelSetting.get('notice_fail_notified') or ''
            state = _json.loads(raw) if raw else {}
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        already = set(state.get(nid) or [])
        fresh = [w for w in failed_works if w.get('title') not in already]
        if not fresh:
            return
        try:
            msg = build_notice_failure_message(notice_title or '', fresh)
            if msg and send_webhook(self.notify_download_url, msg):
                merged = sorted(already | {w['title'] for w in fresh})
                P.ModelSetting.set(
                    'notice_fail_notified',
                    _json.dumps({nid: merged}, ensure_ascii=False))
                P.logger.info('[공지] 실패 작품 알림 발송: %d건', len(fresh))
        except Exception as e:
            P.logger.warning('[공지] 실패 작품 알림 예외: %s', e)

    def _process_notice_content(self, title: str) -> str:
        """공지 작품 1개 — 검색 → 무료/보유 회차 + 보유 티켓 유료회차를 완결 폴더에 다운.

        반환: 'downloaded' | 'skipped' | 'notfound' | 'failed'
          - notfound: 검색 매칭 실패 (작품 자체를 못 찾음 → 알림 대상)
          - failed:   회차 조회 등 처리 실패

        기다무(waitfree) 회차는 의도적으로 제외 — 기다무 티켓을 소진하지 않는다.
        유료(pay) 회차는 '보유 티켓'(대여권/소장권)이 있을 때만 받는다.
        """
        item = self.client.find_content(title)
        if not item:
            P.logger.warning('[공지] %s 검색 매칭 실패', title)
            return 'notfound'
        content_id = item['id']
        display = item.get('title') or title

        try:
            meta = self.client.get_content(content_id)
            if meta.get('title'):
                display = meta['title']
        except KakaotoonError:
            pass

        _auto_set(current_phase='fetch_episodes', current_title=f'[공지] {display}')

        # 공지 기반 다운은 유료화/완결 → is_completed=True 로 info.xml 기록
        self._ensure_meta(display, content_id, group='complete', is_completed=True)

        try:
            eps = self.client.get_episodes_all(content_id)
        except KakaotoonError as e:
            P.logger.warning('[공지] %s 회차 조회 실패: %s', display, e)
            return 'failed'
        if not eps:
            return 'failed'

        # 분류: 무료/보유(free,unlocked) + 유료(pay). 기다무는 제외.
        unlocked: List[Dict] = []
        pay_eps: List[Dict] = []
        for ep in eps:
            eid = ep.get('id')
            if not eid:
                continue
            rec = (db.session.query(ModelKakaotoonItem)
                   .filter_by(episode_id=eid).first())
            if rec and rec.status == 'completed':
                continue
            avail = KakaotoonClient.episode_availability(ep)
            if avail in ('free', 'unlocked'):
                unlocked.append(ep)
            elif avail == 'pay':
                pay_eps.append(ep)
        unlocked.sort(key=KakaotoonClient.episode_no)
        pay_eps.sort(key=KakaotoonClient.episode_no)
        if not unlocked and not pay_eps:
            P.logger.info('[공지] %s 새로 받을 무료/보유 회차 없음', display)
            return 'skipped'
        P.logger.info('[공지] %s 미수신 — 무료/보유 %d개, 유료(보유티켓) %d개',
                      display, len(unlocked), len(pay_eps))

        downloaded = 0
        _auto_set(current_phase='downloading')

        # 1) 무료/보유 회차 — 제한 없이
        for ep in unlocked:
            _auto_set(current_episode=ep.get('title', ''),
                      current_pages_done=0, current_pages_total=0)
            if self._download_one(display, content_id, ep,
                                  kind='free', group='complete') == 'downloaded':
                downloaded += 1

        # 2) 유료 회차 — 보유(대여/소장) 티켓이 있을 때만. 기다무 티켓 충전은 안 함.
        for ep in pay_eps:
            try:
                av = self.client.get_available_tickets(content_id)
            except KakaotoonError as e:
                P.logger.warning('[공지] %s available-tickets 실패: %s', display, e)
                break
            if int(av.get('ticketCount') or 0) <= 0:
                P.logger.info('[공지] %s 보유 티켓 0 — 유료 회차 다운 종료', display)
                break
            _auto_set(current_phase='downloading',
                      current_episode=ep.get('title', ''),
                      current_pages_done=0, current_pages_total=0)
            result = self._download_one(display, content_id, ep,
                                        kind='ticket', group='complete')
            if result == 'downloaded':
                downloaded += 1
            else:
                P.logger.info('[공지] %s 유료 회차 다운 실패(%s) — 종료', display, result)
                break

        return 'downloaded' if downloaded else 'skipped'

    # ---- per item ----
    def _process_item(self, raw: str) -> str:
        cid = KakaotoonClient.extract_content_id(raw)
        if cid:
            content_id = cid
            display = raw
            P.logger.info('[%s] content_id 직접: %s', raw, content_id)
        else:
            it = self.client.find_content(raw)
            if not it:
                P.logger.warning('[%s] 검색 실패', raw)
                return 'failed'
            content_id = it['id']
            display = it.get('title') or raw
            P.logger.info('[%s] 검색→ content_id=%s title=%r', raw, content_id, display)

        return self._process_content(display, content_id)

    def _process_content(self, title: str, content_id: int) -> str:
        _auto_set(current_phase='fetch_episodes')
        try:
            meta = self.client.get_content(content_id)
            if meta.get('title'):
                title = meta['title']
                _auto_set(current_title=title)
        except KakaotoonError as e:
            P.logger.warning('[%s] content meta 실패 (계속): %s', title, e)

        # info.xml / cover.jpg — 작품 폴더에 한 번만
        self._ensure_meta(title, content_id, group='main')

        try:
            eps = self.client.get_episodes_all(content_id)
        except KakaotoonError as e:
            P.logger.error('[%s] 회차 조회 실패: %s', title, e)
            return 'failed'
        if not eps:
            P.logger.warning('[%s] 회차 없음', title)
            return 'failed'

        # 분류
        unlocked: List[Dict] = []        # 무료/소장 (그냥 pass)
        waitfree_eps: List[Dict] = []    # 기다무 대기 (티켓 충전되면)
        pay_eps: List[Dict] = []         # 유료 회차

        for ep in eps:
            eid = ep.get('id')
            if not eid:
                continue
            rec = db.session.query(ModelKakaotoonItem).filter_by(episode_id=eid).first()
            if rec and rec.status == 'completed':
                continue
            avail = KakaotoonClient.episode_availability(ep)
            if avail in ('free', 'unlocked'):
                unlocked.append(ep)
            elif avail == 'waitfree':
                waitfree_eps.append(ep)
            elif avail == 'pay':
                pay_eps.append(ep)
        unlocked.sort(key=KakaotoonClient.episode_no)
        waitfree_eps.sort(key=KakaotoonClient.episode_no)
        pay_eps.sort(key=KakaotoonClient.episode_no)
        P.logger.info('[%s] 미수신 — 무료/소장 %d개, 기다무대기 %d개, 유료 %d개',
                      title, len(unlocked), len(waitfree_eps), len(pay_eps))

        downloaded_count = 0
        _auto_set(current_phase='downloading')

        # 1) 무료/소장 — 제한 없이
        for ep in unlocked:
            _auto_set(current_episode=ep.get('title', ''),
                      current_pages_done=0, current_pages_total=0)
            if self._download_one(title, content_id, ep, kind='free') == 'downloaded':
                downloaded_count += 1

        # 2) 기다무 대기 회차 — 티켓 자동 충전 후 사용
        wf_used = 0
        if waitfree_eps and self.use_waitfree:
            for ep in waitfree_eps:
                if wf_used >= self.max_per_run:
                    P.logger.info('[%s] 기다무 max_per_run(%d) 도달 — 중단',
                                  title, self.max_per_run)
                    break
                _auto_set(current_phase='check_ticket',
                          current_episode=ep.get('title', ''))
                # 충전 시도 → 사용 가능한 무료 티켓 있으면 totalTicketCount 증가
                try:
                    self.client.check_and_receive_free_tickets(content_id)
                except KakaotoonError as e:
                    P.logger.info('[%s] free ticket 수령 실패 (계속): %s', title, e)
                # 잔량 확인
                try:
                    av = self.client.get_available_tickets(content_id)
                except KakaotoonError as e:
                    P.logger.warning('[%s] available-tickets 실패: %s', title, e)
                    break
                tc = int(av.get('ticketCount') or 0)
                if tc <= 0:
                    P.logger.info('[%s] 사용 가능 티켓 0 — 기다무 다운 종료', title)
                    break

                _auto_set(current_phase='downloading')
                result = self._download_one(title, content_id, ep, kind='waitfree')
                if result == 'downloaded':
                    downloaded_count += 1
                    wf_used += 1
                else:
                    P.logger.info('[%s] 기다무 회차 다운 실패(%s) — 종료', title, result)
                    break
        elif waitfree_eps:
            P.logger.info('[%s] 기다무 사용 Off — %d개 스킵', title, len(waitfree_eps))

        # 3) 유료 회차 — 보유 티켓으로만 (옵션 On일 때)
        if pay_eps and self.use_owned_ticket:
            for ep in pay_eps:
                try:
                    av = self.client.get_available_tickets(content_id)
                except KakaotoonError as e:
                    P.logger.warning('[%s] available-tickets 실패: %s', title, e)
                    break
                if int(av.get('ticketCount') or 0) <= 0:
                    P.logger.info('[%s] 일반 티켓 잔량 0 — 유료 회차 다운 종료', title)
                    break
                _auto_set(current_phase='downloading',
                          current_episode=ep.get('title', ''))
                result = self._download_one(title, content_id, ep, kind='ticket')
                if result == 'downloaded':
                    downloaded_count += 1
                else:
                    P.logger.info('[%s] 유료 회차 다운 실패(%s) — 종료', title, result)
                    break

        return 'downloaded' if downloaded_count else 'skipped'

    # ---- 회차 폴더 일괄 압축 (UI 버튼) ----
    def compress_all(self) -> dict:
        """download_path 아래의 모든 회차 폴더를 ZIP 으로 압축.

        '회차 폴더' = 이미지 파일을 가진 폴더. info.xml/cover.jpg 만 있는
        작품 폴더는 자동으로 건너뜀 (이미지 없음).
        이미 .zip 으로 압축된 회차는 건너뜀 (멱등).
        """
        P.logger.info('[basic] compress_all BEGIN root=%s', self.download_root)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='압축 시작')
        if not self.download_root or not os.path.isdir(self.download_root):
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정/없음')
            return {'ret': 'fail', 'reason': 'no_download_path'}

        # 폴더 스캔 — 압축 중 폴더가 삭제되므로 미리 목록 수집.
        # '회차 폴더' = 서브디렉토리 없는 leaf + 이미지 보유 폴더만 후보로 선정
        # (작품 폴더가 cover.jpg 만으로 잘못 후보에 들어가 통째로 zip+rmtree
        #  되는 사고 방지).
        candidates: List[str] = []
        for root, dirs, files in os.walk(self.download_root):
            if dirs:
                continue
            if any(f.lower().endswith(_IMAGE_EXTS) for f in files):
                candidates.append(root)

        _auto_set(titles_total=len(candidates))
        compressed = 0
        skipped = 0
        failed = 0
        for idx, ep in enumerate(candidates, start=1):
            rel = os.path.relpath(ep, self.download_root)
            _auto_set(current_title=rel, current_phase='compressing',
                      titles_done=idx - 1)
            try:
                zip_path = compress_episode_folder(ep)
                if zip_path:
                    compressed += 1
                else:
                    skipped += 1
            except Exception as e:
                P.logger.warning('압축 예외 %s: %s', ep, e)
                failed += 1
            _auto_set(titles_done=idx)

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='',
                  message=(f'압축 완료 — 처리 {compressed}개, '
                           f'스킵 {skipped}개, 실패 {failed}개'))
        P.logger.info('[basic] compress_all END processed=%d skipped=%d failed=%d',
                      compressed, skipped, failed)
        return {'ret': 'success', 'processed': compressed,
                'skipped': skipped, 'failed': failed}

    # ---- 기존 _keys.json 일괄 삭제 (UI 버튼) ----
    def cleanup_keys_json(self) -> dict:
        """download_path 아래의 모든 _keys.json 파일을 찾아 삭제.

        과거 버전에서 정상 다운로드된 회차에도 _keys.json 이 같이 남던 버그
        잔재 청소용. 향후 다운로드는 복호화 실패 시에만 _keys.json 생성됨.
        """
        P.logger.info('[basic] cleanup_keys_json BEGIN root=%s', self.download_root)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='_keys.json 청소 시작')
        if not self.download_root or not os.path.isdir(self.download_root):
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정/없음')
            return {'ret': 'fail', 'reason': 'no_download_path'}

        removed = 0
        failed = 0
        for root, _dirs, files in os.walk(self.download_root):
            if '_keys.json' in files:
                target = os.path.join(root, '_keys.json')
                try:
                    os.remove(target)
                    removed += 1
                    _auto_set(message=f'삭제 {removed}개…',
                              current_title=os.path.relpath(root, self.download_root))
                except Exception as e:
                    failed += 1
                    P.logger.warning('_keys.json 삭제 실패 %s: %s', target, e)

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='',
                  message=f'_keys.json 청소 완료 — 삭제 {removed}개, 실패 {failed}개')
        P.logger.info('[basic] cleanup_keys_json END removed=%d failed=%d',
                      removed, failed)
        return {'ret': 'success', 'removed': removed, 'failed': failed}

    # ---- 전 작품 메타 일괄 동기화 (UI 버튼) ----
    def sync_metadata_all(self) -> dict:
        """titles 리스트의 모든 작품에 대해 info.xml/cover.jpg 누락분 생성.

        다운로드 폴더에 작품 폴더가 이미 있는 항목만 처리 (없는 작품은
        만들지 않고 스킵 — 다운로드 전에 굳이 빈 폴더 만들 필요 없음).
        완결 그룹(notice_subdir 아래)도 동시에 점검.
        """
        P.logger.info('[basic] sync_metadata_all BEGIN items=%s', self.items)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='메타 동기화 시작', titles_total=len(self.items))
        if not self.download_root:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.cookies_json:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='cookies_json 미설정')
            return {'ret': 'fail', 'reason': 'no_cookies'}
        if not self.items:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='체크할 작품 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = KakaotoonClient(self.cookies_json, logger=P.logger,
                                          proxy_url=self.proxy_url)
        except AuthRequiredError as e:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'쿠키 인증 실패: {e}')
            return {'ret': 'fail', 'reason': 'auth', 'msg': str(e)}

        summary = {'titles': len(self.items), 'info': 0, 'cover': 0,
                   'skipped_no_folder': 0, 'failed': 0}
        for raw in self.items:
            _auto_set(current_title=raw, current_phase='sync_metadata',
                      current_episode='', current_pages_done=0,
                      current_pages_total=0)
            try:
                cid = KakaotoonClient.extract_content_id(raw)
                if cid is None:
                    it = self.client.find_content(raw)
                    if not it:
                        summary['failed'] += 1
                        continue
                    cid = it['id']
                    title_guess = it.get('title') or raw
                else:
                    title_guess = raw
                _auto_set(current_title=title_guess)

                # main / complete 두 위치 모두 점검 — 폴더가 있는 쪽에만 메타 채움
                any_processed = False
                cd_cache = None
                for group, is_completed in (('main', None), ('complete', True)):
                    folder = self._content_folder(title_guess, group)
                    if not os.path.isdir(folder):
                        continue
                    any_processed = True
                    if meta_mod.is_meta_complete(folder):
                        continue
                    if cd_cache is None:
                        cd_cache = self.client.get_meta_for_info(
                            cid, title_hint=title_guess) or {}
                        # 메타에서 더 정확한 제목 발견 시 폴더 이름 재구성
                        new_title = (cd_cache.get('title') or '').strip()
                        if new_title and new_title != title_guess:
                            alt_folder = self._content_folder(new_title, group)
                            if os.path.isdir(alt_folder) and not os.path.isdir(folder):
                                folder = alt_folder
                            title_guess = new_title
                            _auto_set(current_title=new_title)
                    if not cd_cache:
                        summary['failed'] += 1
                        continue
                    r = meta_mod.ensure_meta(
                        folder, cd_cache,
                        session=self.client.make_session(),
                        logger=P.logger,
                        is_completed=is_completed)
                    if r.get('info'):
                        summary['info'] += 1
                    if r.get('cover'):
                        summary['cover'] += 1
                if not any_processed:
                    summary['skipped_no_folder'] += 1
            except Exception as e:
                import traceback
                P.logger.error('[sync_metadata] %r 예외: %s', raw, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1
            _auto_set(titles_done=(summary['info'] + summary['cover']
                                   + summary['skipped_no_folder']
                                   + summary['failed']))

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"메타 동기화 완료 — info {summary['info']}, "
                           f"cover {summary['cover']}, "
                           f"폴더없음 {summary['skipped_no_folder']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ---- info.xml / cover.jpg ----
    def _content_folder(self, content_title: str, group: str) -> str:
        c_folder = _safe_filename(content_title)
        if group == 'complete':
            return os.path.join(self.download_root,
                                _safe_filename(self.notice_subdir),
                                c_folder)
        return os.path.join(self.download_root, c_folder)

    def _ensure_meta(self, content_title: str, content_id: int,
                     group: str = 'main',
                     is_completed: Optional[bool] = None) -> None:
        """작품 폴더에 info.xml/cover.jpg 가 없으면 생성. 멱등."""
        try:
            folder = self._content_folder(content_title, group)
            if meta_mod.is_meta_complete(folder):
                return
            cd = self.client.get_meta_for_info(content_id,
                                               title_hint=content_title)
            if not cd:
                P.logger.info('[%s] meta dict 없음 — info/cover 스킵',
                              content_title)
                return
            meta_mod.ensure_meta(folder, cd,
                                 session=self.client.make_session(),
                                 logger=P.logger,
                                 is_completed=is_completed)
        except Exception as e:
            P.logger.warning('[%s] ensure_meta 예외(계속): %s',
                             content_title, e)

    # ---- one episode ----
    def _download_one(self, content_title: str, content_id: int,
                      ep: Dict[str, Any], kind: str = 'free',
                      group: str = 'main') -> str:
        episode_id = ep['id']
        ep_no = KakaotoonClient.episode_no(ep)
        episode_title = ep.get('title', '')

        rec = db.session.query(ModelKakaotoonItem).filter_by(episode_id=episode_id).first()
        if rec and rec.status == 'completed':
            return 'skipped'
        if rec is None:
            rec = ModelKakaotoonItem()
            rec.content_id = content_id
            rec.content_title = content_title
            rec.episode_id = episode_id
            rec.episode_no = ep_no
            rec.episode_title = episode_title
            rec.dl_group = group
            db.session.add(rec)
            db.session.commit()
        else:
            rec.dl_group = group
        rec.updated_time = datetime.now()
        rec.ticket_kind = kind

        # ---- pass (티켓 차감 / 열람 등록) ----
        if kind != 'free':
            rec.status = 'using_ticket'; db.session.commit()
            try:
                self.client.pass_episode(episode_id)
            except NotReadableError as e:
                rec.status = 'skipped_no_ticket'; rec.error_msg = str(e)
                db.session.commit()
                P.logger.warning('[%s] %s pass(%s) 거부: %s',
                                 content_title, episode_title, kind, e)
                return 'skipped'
            except KakaotoonError as e:
                rec.status = 'failed'; rec.error_msg = f'pass: {e}'
                db.session.commit()
                P.logger.warning('[%s] %s pass(%s) 실패: %s',
                                 content_title, episode_title, kind, e)
                return 'failed'
        else:
            # 무료도 pass 한 번 호출해두면 history 마킹됨 — 실패해도 무시
            try:
                self.client.pass_episode(episode_id)
            except Exception as e:
                P.logger.info('[%s] %s 무료 pass (무시): %s',
                              content_title, episode_title, e)

        # ---- media-resources ----
        try:
            mr = self.client.get_media_resources(episode_id)
        except KakaotoonError as e:
            rec.status = 'failed'; rec.error_msg = f'media-resources: {e}'
            db.session.commit()
            P.logger.warning('[%s] %s media-resources 실패: %s',
                             content_title, episode_title, e)
            return 'failed'
        media = mr.get('media') or {}
        files = media.get('files') or []
        aid = media.get('aid'); zid = media.get('zid')
        req_meta = mr.get('_req') or {}
        if not files:
            rec.status = 'failed'; rec.error_msg = 'no media files'
            db.session.commit()
            P.logger.warning('[%s] %s media-resources files 비어있음 (aid=%s zid=%s)',
                             content_title, episode_title,
                             'Y' if aid else 'N', 'Y' if zid else 'N')
            return 'failed'

        # 복호화에 필요한 userId — 첫 회차 진입 시 1회 조회
        if self.user_id is None:
            self.user_id = self.client.get_user_id(episode_id)
            P.logger.info('[%s] kakao userId 확보: %r', content_title, self.user_id)

        # ---- 저장 경로 ----
        c_folder = _safe_filename(content_title)
        e_folder = f'{ep_no:04d}_{_safe_filename(episode_title)}'
        if group == 'complete':
            save_dir = os.path.join(self.download_root,
                                    _safe_filename(self.notice_subdir),
                                    c_folder, e_folder)
        else:
            save_dir = os.path.join(self.download_root, c_folder, e_folder)
        os.makedirs(save_dir, exist_ok=True)
        rec.save_dir = save_dir
        rec.status = 'downloading'
        rec.page_count = len(files)
        db.session.commit()
        _auto_set(current_pages_total=len(files), current_pages_done=0)

        downloaded = 0; total_bytes = 0; failed: List[Tuple[int, str]] = []
        cef_saved = False  # 복호화 실패해서 .webp.cef 원본을 저장한 페이지가 있는지
        for i, f in enumerate(files, start=1):
            url = f.get('url')
            try:
                enc = self.client.download_image(url)
                try:
                    if aid and zid and req_meta:
                        dec = KakaotoonClient.decrypt_cef(
                            enc, aid, zid,
                            user_id=self.user_id,
                            episode_id=episode_id,
                            timestamp=req_meta.get('timestamp', ''),
                            nonce=req_meta.get('nonce', ''))
                    else:
                        dec = None
                except KakaotoonError as e:
                    dec = None
                    P.logger.warning('[%s] %s page %d 복호화 실패: %s — .cef 원본만 저장',
                                     content_title, episode_title, i, e)
                if dec is not None:
                    data, ext = KakaotoonClient.to_image_format(dec, self.output_format)
                    local = os.path.join(save_dir, f'{i:03d}{ext}')
                    with open(local, 'wb') as fp:
                        fp.write(data)
                    total_bytes += len(data)
                else:
                    local = os.path.join(save_dir, f'{i:03d}.webp.cef')
                    with open(local, 'wb') as fp:
                        fp.write(enc)
                    total_bytes += len(enc)
                    cef_saved = True
                downloaded += 1
                _auto_set(current_pages_done=downloaded)
            except Exception as e:
                failed.append((i, str(e)))
                P.logger.warning('[%s] %s page %d 실패: %s',
                                 content_title, episode_title, i, e)

        # 복호화 실패해서 .webp.cef 원본이 남은 페이지가 있을 때만 _keys.json 저장
        # (정상 다운로드된 회차에는 만들지 않음 — 후처리에 필요 없으므로)
        if cef_saved and aid and zid:
            try:
                with open(os.path.join(save_dir, '_keys.json'), 'w', encoding='utf-8') as fp:
                    fp.write(f'{{"aid": "{aid}", "zid": "{zid}"}}\n')
            except Exception:
                pass

        rec.downloaded_count = downloaded
        rec.total_bytes = total_bytes
        rec.downloaded_at = datetime.now()
        rec.updated_time = rec.downloaded_at
        if downloaded == len(files):
            rec.status = 'completed'
            P.logger.info('[%s] %s 다운로드 완료 (%d개, %.1fKB)',
                          content_title, episode_title, downloaded, total_bytes / 1024)
            self.completed_items.append({
                'group': group,
                'content_title': content_title,
                'episode_title': episode_title,
                'episode_no': ep_no,
                'kind': kind,  # 'free' | 'waitfree' | 'ticket'
            })
        elif downloaded > 0:
            rec.status = 'partial'
            rec.error_msg = f'failed {len(failed)}/{len(files)}'
        else:
            rec.status = 'failed'
            rec.error_msg = f'all failed ({len(files)})'
        db.session.commit()

        # 정상 완료 + 압축 옵션 On → 회차 폴더 ZIP 으로 묶고 원본 삭제
        if self.use_compress and rec.status == 'completed':
            _auto_set(current_phase='compressing')
            zip_path = compress_episode_folder(save_dir)
            if zip_path:
                rec.save_dir = zip_path
                db.session.commit()
                P.logger.info('[%s] %s 압축 완료 → %s',
                              content_title, episode_title, zip_path)
            _auto_set(current_phase='downloading')

        return 'downloaded' if rec.status in ('completed', 'partial') else 'failed'
