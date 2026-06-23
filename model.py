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

    content_id = db.Column(db.Integer, index=True)
    content_title = db.Column(db.String)
    episode_id = db.Column(db.Integer, index=True, unique=True)
    episode_no = db.Column(db.Integer)
    episode_title = db.Column(db.String)
    page_count = db.Column(db.Integer)

    status = db.Column(db.String, index=True)
    error_msg = db.Column(db.String)

    ticket_kind = db.Column(db.String)

    dl_group = db.Column(db.String, index=True)

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
