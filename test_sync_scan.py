"""discover_title_folders 디스크 스캔 로직 단위 테스트 (네트워크·DB 없이).

메타 동기화가 체크 목록이 아니라 다운로드 폴더를 스캔해 작품 폴더를 찾는다.
main 그룹(download_root 직속) + 완결 그룹(notice_subdir 아래) 을 모두 수집하는지
검증. worker.py 는 상대 import 가 있어 standalone 로드가 안 되므로 의존 모듈을
sys.modules 스텁으로 넣고 'kt.worker' 로 로드한다.
실행: python -X utf8 test_sync_scan.py
"""
import importlib.util
import os
import sys
import tempfile
import types

_fail = 0


def check(name, got, want):
    global _fail
    ok = got == want
    if not ok:
        _fail += 1
    print(('OK  ' if ok else 'FAIL') + f' {name}: got={got!r} want={want!r}')


class _Dummy:
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
    disc = W.discover_title_folders

    root = tempfile.mkdtemp(prefix='scan_test_')
    # main 그룹 작품들 (회차 폴더 유무와 무관하게 작품 폴더로 잡힘)
    os.makedirs(os.path.join(root, '작품A', '0001_1화'))
    os.makedirs(os.path.join(root, '작품B'))
    # 파일은 무시되어야 함
    open(os.path.join(root, 'readme.txt'), 'w').close()
    # 완결 그룹 (notice_subdir 아래)
    os.makedirs(os.path.join(root, '완결', '완결작1', '0001_1화'))
    os.makedirs(os.path.join(root, '완결', '완결작2'))

    got = disc(root, '완결')
    check('main+complete 스캔', sorted(got),
          sorted([('main', '작품A'), ('main', '작품B'),
                  ('complete', '완결작1'), ('complete', '완결작2')]))
    check('완결 컨테이너 자체는 작품으로 안 잡힘',
          ('main', '완결') in got, False)
    check('파일은 작품으로 안 잡힘',
          any(n == 'readme.txt' for _g, n in got), False)
    check('없는 root → 빈 목록', disc(os.path.join(root, 'nope'), '완결'), [])

    # custom notice_subdir 도 정상 동작
    root2 = tempfile.mkdtemp(prefix='scan_test2_')
    os.makedirs(os.path.join(root2, 'COMPLETE', '끝난작'))
    os.makedirs(os.path.join(root2, '연재중작'))
    check('custom notice_subdir', sorted(disc(root2, 'COMPLETE')),
          sorted([('main', '연재중작'), ('complete', '끝난작')]))

    print('\n' + ('ALL PASS' if _fail == 0 else f'{_fail} FAILED'))
    return 1 if _fail else 0


if __name__ == '__main__':
    sys.exit(main())
