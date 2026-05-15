"""수동 다운로드 워커 — 작품 URL 하나에 대해 보유한 회차 전체 직렬 다운로드."""
import os
import re
import threading
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List

from .client import (KakaotoonClient, KakaotoonError,
                     AuthRequiredError, NotReadableError)
from .model import ModelKakaotoonItem
from .setup import *  # P, db, logger


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    'status': 'idle',
    'message': '',
    'content_id': None,
    'content_title': '',
    'started_at': None,
    'finished_at': None,
    'episodes': [],
    'current_index': -1,
    'total_to_download': 0,
    'completed': 0,
    'skipped': 0,
    'failed': 0,
}
_cancel_flag = threading.Event()
_thread: Optional[threading.Thread] = None


def get_state() -> Dict[str, Any]:
    with _state_lock:
        snap = {k: v for k, v in _state.items() if k != 'episodes'}
        snap['episodes'] = [dict(e) for e in _state['episodes']]
        return snap


def _set(**kw):
    with _state_lock:
        _state.update(kw)


def _reset_state():
    with _state_lock:
        _state.update({
            'status': 'idle', 'message': '',
            'content_id': None, 'content_title': '',
            'started_at': None, 'finished_at': None,
            'episodes': [], 'current_index': -1,
            'total_to_download': 0,
            'completed': 0, 'skipped': 0, 'failed': 0,
        })


def is_running() -> bool:
    with _state_lock:
        return _state['status'] in ('analyzing', 'running')


def cancel():
    _cancel_flag.set()
    _set(message='취소 요청됨')


def analyze(url_or_id: str) -> Dict[str, Any]:
    """URL → content 메타 + 회차 목록. 다운로드는 안 함."""
    P.logger.info('[manual] analyze BEGIN url_or_id=%r', url_or_id)
    content_id = KakaotoonClient.extract_content_id(url_or_id)
    if not content_id:
        return {'ret': 'fail', 'msg': f'URL에서 content_id 추출 실패: {url_or_id!r}'}

    cookies_json = (P.ModelSetting.get('cookies_json') or '').strip()
    if not cookies_json:
        return {'ret': 'fail', 'msg': '쿠키 미설정 — 설정 페이지에서 쿠키 주입 후 다시 시도'}

    try:
        cli = KakaotoonClient(cookies_json, logger=P.logger)
    except AuthRequiredError as e:
        return {'ret': 'fail', 'msg': f'쿠키 인증 실패: {e}'}
    except Exception as e:
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'클라이언트 생성 실패: {e}'}

    try:
        meta = cli.get_content(content_id)
    except AuthRequiredError as e:
        return {'ret': 'fail', 'msg': f'권한 만료 — 쿠키 재주입 필요: {e}'}
    except Exception as e:
        return {'ret': 'fail', 'msg': f'content meta 실패: {e}'}

    content_title = (meta.get('title') or f'content_{content_id}').strip()

    try:
        eps = cli.get_episodes_all(content_id)
    except Exception as e:
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'회차 목록 조회 실패: {e}'}

    if not eps:
        return {'ret': 'fail', 'msg': '회차 없음'}

    all_eps = []
    for ep in eps:
        avail = KakaotoonClient.episode_availability(ep)
        all_eps.append({
            'episode_id': ep.get('id'),
            'episode_no': KakaotoonClient.episode_no(ep),
            'title': ep.get('title', ''),
            'availability': avail,
            'state': 'pending',
            'pages_done': 0,
            'pages_total': 0,
            'save_dir': '',
            'error': '',
        })
    # 다운로드 가능 후보: 무료/소장
    episodes = [e for e in all_eps if e['availability'] in ('free', 'unlocked')]
    episodes.sort(key=lambda e: (e['episode_no'], e['episode_id'] or 0))
    will_download = len(episodes)

    _reset_state()
    _set(status='idle',
         message=f'분석 완료 — 전체 {len(all_eps)}개 중 다운로드 가능 {will_download}개',
         content_id=content_id, content_title=content_title,
         episodes=episodes, total_to_download=will_download)
    P.logger.info('[manual] analyze END content=%r total=%d will_download=%d',
                  content_title, len(all_eps), will_download)
    return {
        'ret': 'success',
        'content_id': content_id,
        'content_title': content_title,
        'episodes': episodes,
        'will_download': will_download,
        'total': len(all_eps),
    }


def run_with_url(url_or_id: str) -> Dict[str, Any]:
    P.logger.info('[manual] run_with_url BEGIN url=%r', url_or_id)
    if is_running():
        return {'ret': 'fail', 'msg': '이미 실행 중'}
    ar = analyze(url_or_id)
    if ar.get('ret') != 'success':
        return ar
    sr = start()
    return {
        'ret': sr.get('ret', 'fail'),
        'msg': sr.get('msg', ''),
        'content_id': ar.get('content_id'),
        'content_title': ar.get('content_title'),
        'will_download': ar.get('will_download'),
        'total': ar.get('total'),
    }


def start() -> Dict[str, Any]:
    global _thread
    if is_running():
        return {'ret': 'fail', 'msg': '이미 실행 중'}
    with _state_lock:
        if not _state['content_id'] or not _state['episodes']:
            return {'ret': 'fail', 'msg': '먼저 작품을 분석하세요'}
    download_root = (P.ModelSetting.get('download_path') or '').strip()
    if not download_root:
        return {'ret': 'fail', 'msg': 'download_path 미설정'}

    _cancel_flag.clear()
    _set(status='running', message='다운로드 시작', started_at=datetime.now().isoformat(),
         finished_at=None, current_index=-1, completed=0, skipped=0, failed=0)
    _thread = threading.Thread(target=_run, args=(download_root,), daemon=True)
    _thread.start()
    return {'ret': 'success', 'msg': '시작됨'}


def _run(download_root: str):
    with F.app.app_context():
        try:
            cookies_json = (P.ModelSetting.get('cookies_json') or '').strip()
            cli = KakaotoonClient(cookies_json, logger=P.logger)
            with _state_lock:
                content_id = _state['content_id']
                content_title = _state['content_title']
                episodes = list(_state['episodes'])

            for idx, ep in enumerate(episodes):
                if _cancel_flag.is_set():
                    _set(status='canceled',
                         finished_at=datetime.now().isoformat(),
                         message='취소됨')
                    return
                _set(current_index=idx)
                P.logger.info('[manual] _run [%d/%d] %s avail=%s eid=%s',
                              idx + 1, len(episodes), ep.get('title'),
                              ep.get('availability'), ep.get('episode_id'))
                ok = _download_episode(cli, content_id, content_title,
                                       idx, ep, download_root)
                with _state_lock:
                    if ok == 'completed':
                        _state['completed'] += 1
                    elif ok == 'skipped':
                        _state['skipped'] += 1
                    else:
                        _state['failed'] += 1

            _set(status='done', finished_at=datetime.now().isoformat(),
                 current_index=-1, message='완료')
        except AuthRequiredError as e:
            _set(status='error', finished_at=datetime.now().isoformat(),
                 message=f'쿠키 만료/무효: {e}')
        except Exception as e:
            P.logger.error('[manual] _run exception: %s', e)
            P.logger.error(traceback.format_exc())
            _set(status='error', finished_at=datetime.now().isoformat(),
                 message=f'에러: {e}')


def _ep_update(idx: int, **kw):
    with _state_lock:
        _state['episodes'][idx].update(kw)


def _download_episode(cli: KakaotoonClient, content_id: int, content_title: str,
                      idx: int, ep: Dict[str, Any], download_root: str) -> str:
    episode_id = ep['episode_id']
    episode_title = ep['title']
    ep_no = ep['episode_no']

    rec = db.session.query(ModelKakaotoonItem).filter_by(episode_id=episode_id).first()
    if rec and rec.status == 'completed':
        _ep_update(idx, state='completed', save_dir=rec.save_dir or '',
                   pages_done=rec.downloaded_count or 0,
                   pages_total=rec.page_count or 0)
        return 'completed'
    if rec is None:
        rec = ModelKakaotoonItem()
        rec.content_id = content_id
        rec.content_title = content_title
        rec.episode_id = episode_id
        rec.episode_no = ep_no
        rec.episode_title = episode_title
        db.session.add(rec)
        db.session.commit()

    _ep_update(idx, state='downloading', error='')
    rec.status = 'downloading'; rec.updated_time = datetime.now(); db.session.commit()
    rec.ticket_kind = 'free' if ep.get('availability') == 'free' else 'unlocked'

    # pass (무료/언락된 회차도 호출해 두면 카카오 측 마킹)
    try:
        cli.pass_episode(episode_id)
    except NotReadableError as e:
        _ep_update(idx, state='skipped', error=f'미열람: {e}')
        rec.status = 'skipped_no_ticket'; db.session.commit()
        return 'skipped'
    except Exception as e:
        P.logger.info('[manual] %s pass 실패 (계속): %s', episode_title, e)

    try:
        mr = cli.get_media_resources(episode_id)
    except KakaotoonError as e:
        _ep_update(idx, state='failed', error=f'media-resources: {e}')
        rec.status = 'failed'; rec.error_msg = f'media-resources: {e}'; db.session.commit()
        return 'failed'
    media = mr.get('media') or {}
    files = media.get('files') or []
    aid = media.get('aid'); zid = media.get('zid')
    if not files:
        _ep_update(idx, state='failed', error='no media files')
        rec.status = 'failed'; rec.error_msg = 'no media files'; db.session.commit()
        return 'failed'

    c_folder = _safe_filename(content_title)
    e_folder = f'{ep_no:04d}_{_safe_filename(episode_title)}'
    save_dir = os.path.join(download_root, c_folder, e_folder)
    os.makedirs(save_dir, exist_ok=True)
    rec.save_dir = save_dir
    _ep_update(idx, save_dir=save_dir, pages_total=len(files), pages_done=0)
    rec.page_count = len(files)
    db.session.commit()

    downloaded = 0; total_bytes = 0; failed = 0
    for i, f in enumerate(files, start=1):
        if _cancel_flag.is_set():
            break
        url = f.get('url')
        try:
            enc = cli.download_image(url)
            try:
                dec = KakaotoonClient.decrypt_cef(enc, aid, zid) if (aid and zid) else None
            except KakaotoonError as e:
                dec = None
                P.logger.warning('[%s] %s page %d 복호화 실패: %s', content_title, episode_title, i, e)
            if dec is not None:
                ext = '.webp'
                if dec[:3] == b'\xff\xd8\xff':
                    ext = '.jpg'
                elif dec[:8] == b'\x89PNG\r\n\x1a\n':
                    ext = '.png'
                local = os.path.join(save_dir, f'{i:03d}{ext}')
                with open(local, 'wb') as fp:
                    fp.write(dec)
                total_bytes += len(dec)
            else:
                local = os.path.join(save_dir, f'{i:03d}.webp.cef')
                with open(local, 'wb') as fp:
                    fp.write(enc)
                total_bytes += len(enc)
            downloaded += 1
            _ep_update(idx, pages_done=downloaded)
        except Exception as e:
            failed += 1
            P.logger.warning('manual %s p%s 실패: %s', episode_title, i, e)

    if aid and zid:
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
        _ep_update(idx, state='completed')
        db.session.commit()
        return 'completed'
    elif downloaded > 0:
        rec.status = 'partial'
        rec.error_msg = f'failed {failed}/{len(files)}'
        _ep_update(idx, state='failed', error=f'부분실패 {failed}/{len(files)}')
        db.session.commit()
        return 'failed'
    else:
        rec.status = 'failed'
        rec.error_msg = f'all failed ({len(files)})'
        _ep_update(idx, state='failed', error='전부 실패')
        db.session.commit()
        return 'failed'
