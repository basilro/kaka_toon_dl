"""웹훅 알림 발송 유틸 — Discord/Slack/일반 자동 분기."""
from typing import List, Dict, Optional

import requests


def send_webhook(url: str, message: str, username: str = 'kakao_toon_dl',
                 timeout: int = 10) -> bool:
    """웹훅 URL 로 메시지 발송. URL 비어있으면 False 반환 (no-op).

    Discord / Slack / 기타 자동 분기:
      - discord.com/api/webhooks → {"content": msg, "username": ...}
      - hooks.slack.com         → {"text": msg}
      - 기타                     → {"content": msg, "text": msg}
    """
    if not url or not message:
        return False
    u = url.strip()
    try:
        if 'discord.com/api/webhooks' in u or 'discordapp.com/api/webhooks' in u:
            payload = {'content': message, 'username': username}
        elif 'hooks.slack.com' in u:
            payload = {'text': message}
        else:
            payload = {'content': message, 'text': message}
        r = requests.post(u, json=payload, timeout=timeout)
        return 200 <= r.status_code < 300
    except Exception:
        return False


_KIND_LABEL = {'waitfree': '기다무', 'ticket': '대여권'}


def _ticket_tag(items: List[Dict]) -> str:
    """회차 목록에서 티켓 사용 종류별 카운트 → '[기다무 ×2, 대여권 ×1]' 같은 태그.

    무료(kind='free' 또는 누락)만 있으면 빈 문자열.
    """
    counts: Dict[str, int] = {}
    for it in items:
        k = it.get('kind') or 'free'
        if k in _KIND_LABEL:
            counts[k] = counts.get(k, 0) + 1
    if not counts:
        return ''
    parts = [f'{_KIND_LABEL[k]} ×{counts[k]}'
             for k in ('waitfree', 'ticket') if k in counts]
    return f'  [{", ".join(parts)}]'


def build_download_summary(completed_items: List[Dict]) -> str:
    """완료된 다운로드 항목 list → 발송용 텍스트.

    completed_items: [{'group': 'main'|'complete', 'content_title': str,
                       'episode_title': str, 'episode_no': int,
                       'kind': 'free'|'waitfree'|'ticket'}, ...]
    """
    if not completed_items:
        return ''
    # group → content → list[episode]
    grouped: Dict[str, Dict[str, List[Dict]]] = {}
    for it in completed_items:
        g = it.get('group') or 'main'
        c = it.get('content_title') or '(unknown)'
        grouped.setdefault(g, {}).setdefault(c, []).append(it)

    total = len(completed_items)
    # 전체 티켓 사용 합계 (헤더용)
    header_tag = _ticket_tag(completed_items)
    header = f'[카카오웹툰] 다운로드 완료 — 총 {total}회차'
    if header_tag:
        # 헤더는 '[...]' 대신 '(티켓 사용: ...)' 로 풀어 적기
        body = header_tag.strip().lstrip('[').rstrip(']')
        header += f' (티켓 사용: {body})'
    lines: List[str] = [header]

    group_label = {'main': '작품', 'complete': '완결'}
    for g in ('main', 'complete'):
        if g not in grouped:
            continue
        lines.append('')
        lines.append(f'■ {group_label[g]}')
        for content_title, eps in sorted(grouped[g].items()):
            # ep_no 로 정렬
            eps_sorted = sorted(eps, key=lambda x: x.get('episode_no') or 0)
            cnt = len(eps_sorted)
            if cnt <= 5:
                titles = ', '.join((e.get('episode_title') or '?')
                                   for e in eps_sorted)
            else:
                first = eps_sorted[0].get('episode_title') or '?'
                last = eps_sorted[-1].get('episode_title') or '?'
                titles = f'{first} ~ {last}'
            lines.append(f'- {content_title} ({cnt}): {titles}{_ticket_tag(eps_sorted)}')
    return '\n'.join(lines)


def build_cookie_expired_message() -> str:
    return ('[카카오웹툰] 쿠키 만료 감지\n'
            '설정 페이지에서 쿠키를 재주입해주세요.\n'
            '(자동 다운로드가 중단됩니다)')


def build_notice_failure_message(notice_title: str, failed: List[Dict]) -> str:
    """유료화/종료 공지 작품 중 자동 다운에 실패한 항목 알림.

    failed: [{'title': str, 'reason': str}, ...]
    반환: 발송용 텍스트 (failed 비어있으면 빈 문자열).
    """
    if not failed:
        return ''
    lines = [f'[카카오웹툰] 유료화 공지 다운 실패 — {len(failed)}건']
    if notice_title:
        lines.append(f'(공지: {notice_title})')
    lines.append('')
    lines.append('아래 작품은 자동 다운에 실패했습니다. 수동 확인이 필요합니다:')
    for it in failed:
        title = it.get('title') or '(제목 미상)'
        reason = it.get('reason') or '실패'
        lines.append(f'- {title} — {reason}')
    return '\n'.join(lines)
