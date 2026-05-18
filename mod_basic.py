import traceback

from .model import ModelKakaotoonItem
from .setup import *
from .worker import Worker


class ModuleBasic(PluginModuleBase):

    def __init__(self, P):
        super(ModuleBasic, self).__init__(
            P, name='basic', first_menu='setting',
            scheduler_desc='카카오웹툰 자동 다운로드',
        )
        self.db_default = {
            f'db_version': '1',
            f'{self.name}_auto_start': 'False',
            f'{self.name}_interval': '0 */3 * * *',
            f'{self.name}_db_delete_day': '90',
            f'{self.name}_db_auto_delete': 'False',
            f'{P.package_name}_item_last_list_option': '',

            'titles': '',
            'cookies_json': '',
            'download_path': '',
            'max_per_run': '1',
            'use_waitfree': 'True',
            'use_owned_ticket': 'False',
            'output_format': 'webp',
            'notice_auto_dl': 'False',         # 매월 유료화/종료 공지 자동 다운
            'notice_subdir': '완결',            # 저장될 하위 폴더명
            'notify_webhook_cookie': '',       # 쿠키 만료 시 발송할 웹훅
            'notify_webhook_download': '',     # 다운로드 완료 요약 발송 웹훅
            'cookie_expired_notified': 'False',# 쿠키 만료 알림 1회 발송 플래그
            'use_proxy': 'False',              # 프록시 사용 여부
            'proxy_url': '',                   # warproxy 등. use_proxy=True + 값 있을 때만 사용
            'use_compress': 'False',           # 정상 다운 완료 시 회차 폴더 ZIP 으로 압축 + 원본 삭제
            'auto_start': 'False',
        }
        self.web_list_model = ModelKakaotoonItem
        # 기존 DB 에 새 컬럼 자동 추가 (SQLAlchemy 가 alter 안 함)
        self._migrate_db()

    @staticmethod
    def _migrate_db():
        """기존 sqlite DB 에 누락된 컬럼 추가. 신규 설치는 create_all 로 OK.

        SJVA 는 플러그인 별 bind sqlite 를 쓰므로 bind 된 engine 을 사용해야 함.
        또 __init__ 단계에서는 app context 가 없으므로 명시적으로 wrap.
        """
        try:
            from sqlalchemy import inspect as sa_inspect, text
            with F.app.app_context():
                # 플러그인 bind 된 engine 우선, 없으면 default
                bind = P.package_name
                engine = None
                try:
                    engine = db.engines.get(bind)  # Flask-SQLAlchemy 3.x
                except Exception:
                    pass
                if engine is None:
                    try:
                        engine = db.get_engine(bind=bind)  # 2.x
                    except Exception:
                        engine = db.engine

                insp = sa_inspect(engine)
                try:
                    cols = {c['name'] for c in insp.get_columns('kaka_toon_dl_item')}
                except Exception as e:
                    P.logger.warning('[basic] DB migration: 테이블 정보 조회 실패 — %s', e)
                    return
                need = []
                if 'dl_group' not in cols:
                    need.append(('dl_group', 'VARCHAR'))
                if not need:
                    return
                for col, typ in need:
                    stmt = f'ALTER TABLE kaka_toon_dl_item ADD COLUMN {col} {typ}'
                    with engine.begin() as conn:  # DDL — 자동 commit
                        conn.execute(text(stmt))
                    P.logger.info('[basic] DB migration: %s 추가됨', col)
        except Exception as e:
            P.logger.warning('[basic] DB migration 실패 (계속): %s', e)

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        if sub == 'setting':
            arg['is_include'] = F.scheduler.is_include(self.get_scheduler_name())
            arg['is_running'] = F.scheduler.is_running(self.get_scheduler_name())
        return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)

    def process_command(self, command, arg1=None, arg2=None, arg3=None, req=None):
        try:
            P.logger.info('[basic.process_command] cmd=%r arg1=%r arg2=%r arg3=%r',
                          command, arg1, arg2, arg3)
        except Exception:
            pass
        ret = {'ret': 'success'}
        try:
            if command == 'verify_cookies':
                from .client import KakaotoonClient, AuthRequiredError
                try:
                    proxy_url = KakaotoonClient.resolve_proxy(
                        P.ModelSetting.get('use_proxy'),
                        P.ModelSetting.get('proxy_url'))
                    cli = KakaotoonClient(P.ModelSetting.get('cookies_json'),
                                          logger=P.logger, proxy_url=proxy_url)
                    ok = cli.verify()
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '쿠키 유효 (로그인 상태 확인됨)' if ok else '쿠키 만료/무효 — 재주입 필요'}
                except AuthRequiredError as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'run_now':
                ret = self.do_action()
            elif command == 'sync_metadata':
                ret = self.do_action_sync_metadata()
            elif command == 'cleanup_keys_json':
                ret = self.do_action_cleanup_keys_json()
            elif command == 'compress_all':
                ret = self.do_action_compress_all()
            elif command == 'mrun':
                from . import manual_worker
                url = (arg1 or '').strip()
                if not url and req is not None:
                    try:
                        url = (req.form.get('url') or req.values.get('url')
                               or req.args.get('url') or '').strip()
                    except Exception:
                        pass
                ret = manual_worker.run_with_url(url)
            elif command == 'mcancel':
                from . import manual_worker
                manual_worker.cancel()
                ret = {'ret': 'success', 'msg': '취소 요청 보냄'}
            elif command == 'mprogress':
                from . import manual_worker
                ret = {'ret': 'success', 'state': manual_worker.get_state()}
            elif command == 'status_progress':
                from . import manual_worker, worker as auto_worker
                ret = {
                    'ret': 'success',
                    'auto': auto_worker.get_auto_state(),
                    'manual': manual_worker.get_state(),
                }
            elif command == 'notify_test':
                # arg1 = 'cookie' | 'download'
                from .notify import send_webhook
                kind = (arg1 or 'cookie').strip().lower()
                url_key = ('notify_webhook_cookie' if kind == 'cookie'
                           else 'notify_webhook_download')
                url = (P.ModelSetting.get(url_key) or '').strip()
                if not url:
                    ret = {'ret': 'fail', 'msg': f'{kind} URL 미설정'}
                else:
                    msg = f'[카카오웹툰] 테스트 알림 ({kind}) — 정상 수신 확인용'
                    ok = send_webhook(url, msg)
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '발송 성공' if ok else '발송 실패 (URL/형식 확인)'}
            elif command == 'db_delete_items':
                ids = []
                for x in (arg1 or '').split(','):
                    x = x.strip()
                    if x.isdigit():
                        ids.append(int(x))
                if not ids:
                    ret = {'ret': 'fail', 'msg': '삭제할 ID 없음', 'count': 0}
                else:
                    cnt = (db.session.query(ModelKakaotoonItem)
                           .filter(ModelKakaotoonItem.id.in_(ids))
                           .delete(synchronize_session=False))
                    db.session.commit()
                    ret = {'ret': 'success', 'count': cnt}
        except Exception as e:
            P.logger.error('[basic.process_command] inner Exception: %s', e)
            P.logger.error(traceback.format_exc())
            ret = {'ret': 'fail', 'msg': str(e)}
        try:
            return jsonify(ret)
        except Exception as e:
            P.logger.error('[basic.process_command] jsonify 실패: %s ret=%r', e, ret)
            return jsonify({'ret': 'fail', 'msg': f'jsonify 실패: {e}'})

    def scheduler_function(self):
        P.logger.info('[basic] scheduler_function CALLED')
        try:
            ret = self.do_action()
            P.logger.info('[basic] scheduler 종료: %s', ret)
        except Exception as e:
            P.logger.error('[basic] scheduler Exception: %s', e)
            P.logger.error(traceback.format_exc())

    def do_action(self):
        P.logger.info('[basic] do_action BEGIN')
        try:
            with F.app.app_context():
                # __init__ 시점에 migration 이 app context 부족으로 실패해도
                # 여기서 한번 더 시도 (idempotent — 이미 컬럼 있으면 no-op)
                self._migrate_db()
                w = Worker()
                ret = w.run()
                P.logger.info('[basic] do_action END ret=%s', ret)
                return ret
        except Exception as e:
            P.logger.error('[basic] do_action Exception: %s', e)
            P.logger.error(traceback.format_exc())
            return {'ret': 'fail', 'msg': str(e)}

    def do_action_sync_metadata(self):
        """체크할 작품 전체의 info.xml / cover.jpg 누락분 백그라운드 동기화."""
        import threading
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 자동 다운로드 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().sync_metadata_all()
            except Exception as e:
                P.logger.error('[basic] sync_metadata run Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '메타 동기화 시작됨 — "진행 상황" 메뉴에서 확인'}

    def do_action_compress_all(self):
        """download_path 아래 모든 회차 폴더 ZIP 압축 + 원본 폴더 삭제 (백그라운드)."""
        import threading
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 다른 작업 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().compress_all()
            except Exception as e:
                P.logger.error('[basic] compress_all Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '압축 시작됨 — "진행 상황" 메뉴에서 확인'}

    def do_action_cleanup_keys_json(self):
        """download_path 아래의 _keys.json 일괄 삭제 (과거 버그 잔재 청소)."""
        import threading
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 다른 작업 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().cleanup_keys_json()
            except Exception as e:
                P.logger.error('[basic] cleanup_keys_json Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '_keys.json 청소 시작됨 — "진행 상황" 메뉴에서 확인'}
