"""_episode_save_dir / _safe_filename 경로 로직 단위 테스트 (네트워크·DB 없이).

저장 경로와 '이미 받았는지' 디스크 확인이 같은 규칙을 써야 스킵이 정확하다.
worker.py 는 상대 import 가 있어 standalone 로드가 안 되므로, 의존 모듈을
sys.modules 에 스텁으로 넣고 'kt.worker' 로 로드한다.
실행: python -X utf8 test_save_dir.py
(플러그인 로더가 import 해도 부작용 없도록 모든 실행은 __main__ 가드 아래.)
"""
import importlib.util
import os
import sys
import types

_fail = 0


def check(name, got, want):
    global _fail
    ok = got == want
    if not ok:
        _fail += 1
    print(('OK  ' if ok else 'FAIL') + f' {name}: got={got!r} want={want!r}')


class _Dummy:
    """아무 속성 접근/호출이나 받아주는 더미 (import 시점 바인딩만 충족)."""
    def __getattr__(self, _):
        return self

    def __call__(self, *a, **k):
        return self


def _load_worker():
    here = os.path.dirname(os.path.abspath(__file__))
    pkg = types.ModuleType('kt')
    pkg.__path__ = [here]
    sys.modules['kt'] = pkg

    def stub(name, **attrs):
        m = types.ModuleType('kt.' + name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules['kt.' + name] = m

    stub('client', KakaotoonClient=_Dummy, KakaotoonError=Exception,
         AuthRequiredError=Exception, NotReadableError=Exception)
    stub('model', ModelKakaotoonItem=_Dummy)
    stub('notify', send_webhook=_Dummy(), build_download_summary=_Dummy(),
         build_cookie_expired_message=_Dummy(),
         build_notice_failure_message=_Dummy())
    stub('meta')
    stub('setup', P=_Dummy(), db=_Dummy(), logger=_Dummy())

    spec = importlib.util.spec_from_file_location(
        'kt.worker', os.path.join(here, 'worker.py'))
    m = importlib.util.module_from_spec(spec)
    sys.modules['kt.worker'] = m
    spec.loader.exec_module(m)
    return m


def main():
    W = _load_worker()
    esd = W._episode_save_dir
    sf = W._safe_filename

    check('safe: 금지문자 치환', sf('a/b:c?'), 'a_b_c_')

    # main 그룹: download_root/작품/0001_제목
    check('main 경로',
          esd('/dl', '완결', '작품A', 1, '1화', 'main'),
          os.path.join('/dl', '작품A', '0001_1화'))

    # complete 그룹: download_root/완결(notice_subdir)/작품/회차
    check('complete 경로 (notice_subdir 삽입)',
          esd('/dl', '완결', '작품A', 12, '열두번째', 'complete'),
          os.path.join('/dl', '완결', '작품A', '0012_열두번째'))

    # ep_no 4자리 zero-pad
    check('ep_no 4자리 패딩',
          esd('/dl', '완결', 'X', 7, 'a', 'main'),
          os.path.join('/dl', 'X', '0007_a'))

    # 작품/제목 금지문자 → 폴더명 안전화
    check('금지문자 안전화',
          esd('/dl', '완결', '작품/B', 3, '3화:특별', 'main'),
          os.path.join('/dl', '작품_B', '0003_3화_특별'))

    # 디스크 확인이 보는 zip 경로 = save_dir + '.zip'
    sd = esd('/dl', '완결', '작품A', 1, '1화', 'complete')
    check('zip 경로 규칙', sd + '.zip',
          os.path.join('/dl', '완결', '작품A', '0001_1화') + '.zip')

    print('\n' + ('ALL PASS' if _fail == 0 else f'{_fail} FAILED'))
    return 1 if _fail else 0


if __name__ == '__main__':
    sys.exit(main())
