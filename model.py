from datetime import datetime

from sqlalchemy import desc, or_

from .setup import *


class ModelKakaotoonItem(ModelBase):
    P = P
    __tablename__ = 'kaka_toon_dl_item'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    updated_time = db.Column(db.DateTime)

    # 작품/회차
    content_id = db.Column(db.Integer, index=True)
    content_title = db.Column(db.String)
    episode_id = db.Column(db.Integer, index=True, unique=True)
    episode_no = db.Column(db.Integer)
    episode_title = db.Column(db.String)
    page_count = db.Column(db.Integer)

    # 처리 상태: pending / using_ticket / downloading / completed / failed / partial / skipped_no_ticket / skipped_locked
    status = db.Column(db.String, index=True)
    error_msg = db.Column(db.String)

    # 사용한 티켓 종류 (free / waitfree / ticket)
    ticket_kind = db.Column(db.String)

    # 그룹: 'main' (사용자 지정 작품) | 'complete' (공지 기반 완결/유료화 예정)
    # 컬럼명은 SQL 예약어 회피를 위해 dl_group 사용
    dl_group = db.Column(db.String, index=True)

    # 파일 저장
    save_dir = db.Column(db.String)
    downloaded_count = db.Column(db.Integer)
    total_bytes = db.Column(db.BigInteger)
    downloaded_at = db.Column(db.DateTime)

    def __init__(self):
        self.created_time = datetime.now()
        self.updated_time = self.created_time
        self.status = 'pending'
        self.downloaded_count = 0
        self.total_bytes = 0
        self.dl_group = 'main'

    @classmethod
    def make_query(cls, req, order='desc', search='', option1='all', option2='all'):
        # 템플릿이 보내는 필드명은 option / search_word 인데 base.web_list 는
        # option1 / keyword 로 읽어서 search/option1 인자는 항상 'all'/'' 로 들어옴.
        # → req.form 에서 직접 읽어서 적용.
        query = db.session.query(cls)
        opt = (req.form.get('option') or option1 or 'all').strip()
        kw = (req.form.get('search_word') or req.form.get('keyword') or search or '').strip()
        if opt and opt != 'all':
            query = query.filter(cls.status == opt)
        if kw:
            pat = f'%{kw}%'
            query = query.filter(or_(cls.content_title.like(pat),
                                     cls.episode_title.like(pat)))
        if order == 'desc':
            query = query.order_by(desc(cls.id))
        else:
            query = query.order_by(cls.id)
        return query
