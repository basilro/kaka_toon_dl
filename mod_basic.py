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
            'auto_start': 'False',
        }
        self.web_list_model = ModelKakaotoonItem

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
                    cli = KakaotoonClient(P.ModelSetting.get('cookies_json'), logger=P.logger)
                    ok = cli.verify()
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '쿠키 유효 (로그인 상태 확인됨)' if ok else '쿠키 만료/무효 — 재주입 필요'}
                except AuthRequiredError as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'run_now':
                ret = self.do_action()
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
                w = Worker()
                ret = w.run()
                P.logger.info('[basic] do_action END ret=%s', ret)
                return ret
        except Exception as e:
            P.logger.error('[basic] do_action Exception: %s', e)
            P.logger.error(traceback.format_exc())
            return {'ret': 'fail', 'msg': str(e)}
