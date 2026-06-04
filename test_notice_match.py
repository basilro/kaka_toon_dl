"""find_content 매칭 로직 단위 테스트 (네트워크 없이 순수 함수만 검증).

client.py 를 standalone 모듈로 로드 — 상대 import 없으므로 가능.
대상: KakaotoonClient._normalize_title, _pick_match
실행: python -X utf8 test_notice_match.py
(플러그인 로더가 import 해도 부작용 없도록 모든 실행은 __main__ 가드 아래에 둠.)
"""
import importlib.util
import os
import sys

_fail = 0


def check(name, got, want):
    global _fail
    ok = got == want
    if not ok:
        _fail += 1
    print(('OK  ' if ok else 'FAIL') + f' {name}: got={got!r} want={want!r}')


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        'kt_client', os.path.join(here, 'client.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    KC = m.KakaotoonClient

    # ---- _normalize_title ----
    check('norm strips [19세 완전판]',
          KC._normalize_title('나쁜 X, 나쁜 X [19세 완전판]'), '나쁜x,나쁜x')
    check('norm strips (완전판)',
          KC._normalize_title('어떤작품 (완전판)'), '어떤작품')
    check('norm plain lower+nospace',
          KC._normalize_title('소녀 신선'), '소녀신선')

    # ---- _pick_match: 핵심 시나리오 = 19세 완전판 ----
    # 검색 키워드를 '나쁜 X, 나쁜 X' 로 떼서 검색했을 때 카탈로그 결과:
    adult_results = [
        {'title': '나쁜 X, 나쁜 X', 'id': 555, 'adult': True},
        {'title': '나쁜 X, 나쁜 X 외전', 'id': 556, 'adult': True},
    ]
    hit = KC._pick_match('나쁜 X, 나쁜 X [19세 완전판]', adult_results)
    check('pick: 19세 완전판 → 괄호 제거 정규화 매칭', hit and hit['id'], 555)

    # adult 힌트 → adult=True 우선
    mixed = [
        {'title': '나쁜 X, 나쁜 X', 'id': 100, 'adult': False},
        {'title': '나쁜 X, 나쁜 X', 'id': 101, 'adult': True},
    ]
    hit = KC._pick_match('나쁜 X, 나쁜 X [19세 완전판]', mixed)
    check('pick: adult 힌트 있으면 adult=True 우선', hit and hit['id'], 101)

    # 정확 일치 최우선
    exact = [
        {'title': '나를 죽인 황제의 딸로 살아남는 방법', 'id': 200, 'adult': False},
        {'title': '나를 죽인 황제의 딸', 'id': 201, 'adult': False},
    ]
    hit = KC._pick_match('나를 죽인 황제의 딸로 살아남는 방법', exact)
    check('pick: 정확 일치 우선', hit and hit['id'], 200)

    # 매칭 전혀 없으면 None (오매칭 방지)
    none_match = [{'title': '전혀다른작품', 'id': 9, 'adult': False}]
    hit = KC._pick_match('나쁜 X, 나쁜 X [19세 완전판]', none_match)
    check('pick: 무관한 결과 → None', hit, None)

    # 빈 결과 → None
    check('pick: 빈 결과 → None', KC._pick_match('아무거나', []), None)

    print('\n' + ('ALL PASS' if _fail == 0 else f'{_fail} FAILED'))
    return 1 if _fail else 0


if __name__ == '__main__':
    sys.exit(main())
