"""압축 생성/검증/삭제 분리 로직 단위 테스트 (네트워크·DB 없이).

새 구조: _zip_episode_folder(생성, 원본 유지) → _verify_episode_zip(원본 대조
검증) → 통과 시에만 원본 삭제. compress_episode_folder 는 이 셋을 묶은 편의 함수.
worker.py 는 상대 import 가 있어 standalone 로드가 안 되므로 의존 모듈을
sys.modules 스텁으로 넣고 'kt.worker' 로 로드한다.
실행: python -X utf8 test_compress.py
"""
import importlib.util
import os
import sys
import tempfile
import types
import zipfile

_fail = 0


def check(name, got, want=True):
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


def _make_ep(parent, name, files):
    """files: {filename: bytes} 로 회차 폴더 생성."""
    d = os.path.join(parent, name)
    os.makedirs(d, exist_ok=True)
    for fn, data in files.items():
        with open(os.path.join(d, fn), 'wb') as f:
            f.write(data)
    return d


def main():
    W = _load_worker()
    zip_fn = W._zip_episode_folder
    verify = W._verify_episode_zip
    compress = W.compress_episode_folder

    tmp = tempfile.mkdtemp(prefix='cmp_test_')
    imgs = {'001.webp': b'\x00img-one-data',
            '002.webp': b'img-two-longer-data-xyz',
            '_keys.json': b'{debug}'}  # 비이미지 — zip 에 빠져야 함

    # 1) _zip_episode_folder: zip 생성 + 원본 폴더 유지(삭제 안 함)
    ep = _make_ep(tmp, '0001_1화', imgs)
    zp = zip_fn(ep)
    check('zip 생성됨', zp is not None and os.path.isfile(zp))
    check('생성 단계는 원본 폴더 유지', os.path.isdir(ep))
    with zipfile.ZipFile(zp) as zf:
        names = set(zf.namelist())
    check('zip 에 이미지만 포함(_keys.json 제외)',
          names, {'001.webp', '002.webp'})

    # 2) 검증: 원본과 일치 → True
    check('검증 통과(원본 일치)', verify(ep, zp))

    # 3) 멱등: 다시 호출해도 같은 zip 재사용, 원본 유지
    zp2 = zip_fn(ep)
    check('멱등 — 같은 zip 경로', zp2 == zp)
    check('멱등 — 원본 폴더 여전히 유지', os.path.isdir(ep))

    # 4) 검증이 손상 zip 을 잡아냄 (데이터 바이트 1개 뒤집기 → CRC 깨짐)
    raw = bytearray(open(zp, 'rb').read())
    raw[40] ^= 0xFF  # 첫 멤버 데이터 영역 근처 한 바이트 손상
    with open(zp, 'wb') as f:
        f.write(raw)
    check('검증 실패(손상 zip)', verify(ep, zp), False)

    # 5) 검증이 누락/불일치를 잡아냄: 원본에 zip 에 없는 이미지 추가
    ep_b = _make_ep(tmp, '0002_2화', {'001.webp': b'aaa', '002.webp': b'bbb'})
    zb = zip_fn(ep_b)
    check('두번째 zip 생성', zb is not None)
    with open(os.path.join(ep_b, '003.webp'), 'wb') as f:
        f.write(b'new-page-not-in-zip')
    check('검증 실패(원본에 zip 없는 이미지 추가)', verify(ep_b, zb), False)

    # 6) compress_episode_folder: 신선한 폴더 → 생성+검증+삭제
    ep_c = _make_ep(tmp, '0003_3화', {'001.webp': b'p1', '002.webp': b'p2'})
    zc = compress(ep_c)
    check('compress: zip 생성', zc is not None and os.path.isfile(zc))
    check('compress: 검증 통과 후 원본 삭제됨', not os.path.isdir(ep_c))

    # 7) 작품 폴더 신호(cover.jpg) → 압축 거부, 폴더 보존
    work = _make_ep(tmp, '작품폴더', {'cover.jpg': b'jpg', '0001_1화.zip': b'z'})
    check('작품 폴더(cover.jpg) 압축 거부', zip_fn(work), None)
    check('작품 폴더 보존됨', os.path.isdir(work))

    # 8) 이미지 없는 폴더 → None
    empty = _make_ep(tmp, '0004_빈', {'note.txt': b'x'})
    check('이미지 없으면 None', zip_fn(empty), None)

    print('\n' + ('ALL PASS' if _fail == 0 else f'{_fail} FAILED'))
    return 1 if _fail else 0


if __name__ == '__main__':
    sys.exit(main())
