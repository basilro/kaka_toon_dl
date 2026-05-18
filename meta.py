"""작품 폴더에 info.xml(ComicInfo) + cover.jpg 생성.

reading_info(SJVA 메타 도구) 의 ComicInfo.xml 스키마를 따른다.
다운로드 시 작품 폴더가 처음 만들어질 때 1회만 생성하면 됨 — 이미 있으면 스킵.
"""
import io
import os
import re
from typing import Optional, Dict, Any

import requests


XML_TEMPLATE = '''<?xml version="1.0"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{title}</Title>
  <Series>{title}</Series>
  <Summary>{summary}</Summary>
  <Writer>{writer}</Writer>
  <Publisher>{publisher}</Publisher>
  <Genre>{genre}</Genre>
  <Tags>{tags}</Tags>
  <LanguageISO>ko</LanguageISO>
  <Notes>{notes}</Notes>
  <CoverArtist></CoverArtist>
  <Penciller></Penciller>
  <Inker></Inker>
  <Colorist></Colorist>
  <Letterer></Letterer>
  <Editor></Editor>
  <Characters></Characters>
  <Web>{web}</Web>
</ComicInfo>'''


def _xml_escape(s: str) -> str:
    s = s or ''
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&apos;')).strip()


def _normalize(content_data: Dict[str, Any]) -> Dict[str, Any]:
    """search/decorator 응답을 ComicInfo 빌드용 균일 dict 로 변환."""
    cd = content_data or {}

    title = (cd.get('title') or '').strip()

    authors, illustrators, publisher = [], [], ''
    for a in cd.get('authors') or []:
        name = (a.get('name') or '').strip()
        if not name:
            continue
        t = (a.get('type') or '').upper()
        if t == 'AUTHOR':
            authors.append(name)
        elif t == 'ILLUSTRATOR':
            illustrators.append(name)
        elif t == 'PUBLISHER':
            publisher = name
    writer_names = authors + [x for x in illustrators if x not in authors]
    writer = ', '.join(writer_names)

    # kakao 는 "액션/무협" 형태의 슬래시 구분 → ComicInfo 쉼표
    genre = (cd.get('genre') or '').strip()
    genre = re.sub(r'\s*/\s*', ',', genre)

    summary = (cd.get('catchphraseThreeLines')
               or cd.get('catchphraseTwoLines')
               or cd.get('synopsis')
               or '').replace('\\n', '\n').strip()

    is_completed = bool(cd.get('isStopContent') or cd.get('completed'))
    notes = '완결' if is_completed else '연재'

    # 표지 — 캐릭터컷 우선 (사용자 선택)
    cover_url = (cd.get('featuredCharacterImageA')
                 or cd.get('backgroundImage')
                 or cd.get('titleImageA')
                 or cd.get('shareImg')
                 or cd.get('poster')
                 or '')

    seo = (cd.get('seoId') or '').strip()
    cid = cd.get('id')
    if seo and cid:
        web = f'https://webtoon.kakao.com/content/{seo}/{cid}'
    elif cid:
        web = f'https://webtoon.kakao.com/content/{cid}'
    else:
        web = ''

    return {
        'title': title,
        'writer': writer,
        'publisher': publisher,
        'genre': genre,
        'tags': '카카오웹툰',
        'summary': summary,
        'notes': notes,
        'cover_url': cover_url,
        'web': web,
    }


def build_info_xml(content_data: Dict[str, Any]) -> str:
    nd = _normalize(content_data)
    return XML_TEMPLATE.format(
        title=_xml_escape(nd['title']),
        summary=_xml_escape(nd['summary']),
        writer=_xml_escape(nd['writer']),
        publisher=_xml_escape(nd['publisher']),
        genre=_xml_escape(nd['genre']),
        tags=_xml_escape(nd['tags']),
        notes=_xml_escape(nd['notes']),
        web=_xml_escape(nd['web']),
    )


def _download_cover(url: str, dest_path: str,
                    session: Optional[requests.Session] = None,
                    logger=None) -> bool:
    if not url:
        return False
    try:
        sess = session or requests
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            if logger:
                logger.warning('cover 다운로드 실패 HTTP=%d url=%s',
                               r.status_code, url[:120])
            return False
        data = r.content
        # 이미 JPG → 그대로 저장
        if data[:3] == b'\xff\xd8\xff':
            with open(dest_path, 'wb') as fp:
                fp.write(data)
            return True
        # WebP/PNG → JPG 변환
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
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
            img.save(dest_path, format='JPEG', quality=92, optimize=False)
            return True
        except Exception as e:
            if logger:
                logger.warning('cover JPG 변환 실패 — 원본 저장: %s', e)
            with open(dest_path, 'wb') as fp:
                fp.write(data)
            return True
    except Exception as e:
        if logger:
            logger.warning('cover 다운로드 예외: %s url=%s', e, url[:120])
        return False


def is_meta_complete(content_folder: str) -> bool:
    """info.xml + cover.jpg 둘 다 있으면 True."""
    if not content_folder:
        return False
    return (os.path.isfile(os.path.join(content_folder, 'info.xml')) and
            os.path.isfile(os.path.join(content_folder, 'cover.jpg')))


def ensure_meta(content_folder: str,
                content_data: Dict[str, Any],
                session: Optional[requests.Session] = None,
                logger=None,
                is_completed: Optional[bool] = None) -> Dict[str, bool]:
    """info.xml + cover.jpg 가 없으면 생성. 둘 다 있으면 no-op.

    `content_data` 는 kakao search/decorator 응답의 content dict.
    `is_completed` 가 명시되면 content_data 값을 덮어씀 (공지 기반 다운에서 유용).
    멱등 — 여러 번 호출해도 안전.
    """
    result = {'info': False, 'cover': False}
    if not content_folder:
        return result

    info_path = os.path.join(content_folder, 'info.xml')
    cover_path = os.path.join(content_folder, 'cover.jpg')

    need_info = not os.path.isfile(info_path)
    need_cover = not os.path.isfile(cover_path)
    if not need_info and not need_cover:
        return result

    if not content_data:
        if logger:
            logger.info('content_data 없음 — info/cover 생성 스킵 (folder=%s)',
                        content_folder)
        return result

    try:
        os.makedirs(content_folder, exist_ok=True)
    except Exception as e:
        if logger:
            logger.warning('content folder 생성 실패: %s — %s',
                           content_folder, e)
        return result

    if is_completed is not None:
        content_data = dict(content_data)
        content_data['isStopContent'] = bool(is_completed)

    if need_info:
        try:
            xml = build_info_xml(content_data)
            with open(info_path, 'w', encoding='utf-8') as fp:
                fp.write(xml)
            result['info'] = True
            if logger:
                logger.info('info.xml 생성: %s', info_path)
        except Exception as e:
            if logger:
                logger.warning('info.xml 생성 실패: %s', e)

    if need_cover:
        nd = _normalize(content_data)
        if _download_cover(nd['cover_url'], cover_path,
                           session=session, logger=logger):
            result['cover'] = True
            if logger:
                logger.info('cover.jpg 생성: %s', cover_path)
    return result
