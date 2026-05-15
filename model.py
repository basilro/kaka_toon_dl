from datetime import datetime

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
