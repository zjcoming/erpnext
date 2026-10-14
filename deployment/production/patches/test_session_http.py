"""Explicit disposable-site real HTTP regression. See production patches README."""
import os
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import sys
import traceback
import uuid

SITE = os.environ.get('PS_SESSION_TEST_SITE', '')
SITES = os.environ.get('PS_SESSION_SITES_PATH', '/home/frappe/frappe-bench/sites')
TRACE_NAMES = {
    'HTTPRequest.__init__', 'HTTPRequest.validate_csrf_token',
    'LoginManager.login', 'LoginManager.post_login', 'LoginManager.make_session',
    'Session.start', 'Session.insert_session_record', 'Session.update',
    'get_csrf_token', 'generate_csrf_token', 'lock_login_credentials',
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    args.variant = "candidate"
    out = Path(args.out)
    if out.exists():
        raise SystemExit('Refusing to overwrite evidence')
    if os.environ.get('PS_SESSION_DISPOSABLE') != '1':
        parser.error('Set PS_SESSION_DISPOSABLE=1 only for a disposable local test site')
    if not (SITE == 'hs-demo-release-20261008.localhost' or re.fullmatch(r'test[-_.][a-z0-9_.-]+\.localhost', SITE)) or any(word in SITE.lower() for word in ('demoerp','production')):
        parser.error('PS_SESSION_TEST_SITE must identify an explicit allowed local test site')
    import frappe
    manifest_bytes = Path(args.manifest).read_bytes()
    manifest = json.loads(manifest_bytes)
    base = Path(frappe.__file__).resolve().parent.parent
    for relative, hashes in manifest['files'].items():
        target = (base / relative).resolve()
        assert base in target.parents, 'Manifest path must stay inside Frappe source'
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashes['patched'], 'Native source differs from manifest: ' + relative
    expected_source_sha = manifest['files']['frappe/sessions.py']['patched']
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    import frappe
    from frappe.app import application
    from werkzeug.test import Client
    from werkzeug.wrappers import Response

    user = 'http-chain-' + uuid.uuid4().hex + '@example.invalid'
    password = secrets.token_urlsafe(40)
    result = {'variant': args.variant, 'site': SITE, 'sessions_sha256': expected_source_sha,
              'test_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'manifest_sha256': manifest_sha, 'native_manifest_all_matches': True, 'transport': 'Werkzeug Client -> frappe.app.application', 'network_requests': 0,
              'mocks': [], 'requests': [], 'checks': [], 'cleanup': {}, 'status': 'running'}
    fixture_created = False
    owned_sids = set()
    suppressed = io.StringIO()
    ip = '127.77.' + str(secrets.randbelow(254)+1) + '.' + str(secrets.randbelow(254)+1)

    def connect():
        frappe.init(site=SITE, sites_path=SITES)
        frappe.connect()
        assert frappe.local.site == SITE and frappe.conf.db_host in ('db', 'localhost', '127.0.0.1', '::1')

    def close():
        if getattr(frappe.local, 'db', None):
            frappe.db.rollback()
        frappe.destroy()

    def check(name, value):
        result['checks'].append({'name': name, 'passed': bool(value)})
        if not value:
            raise AssertionError(name)

    def read_session(sid):
        connect()
        try:
            rows = frappe.db.sql('SELECT user, sessiondata FROM tabSessions WHERE sid=%s', (sid,))
            check('exact_session_' + str(len(result['requests'])), len(rows) == 1 and rows[0][0] == user)
            data = json.loads(rows[0][1])
            return data
        finally:
            close()

    def request(client, label, method, path, data=None, token=None, html=False):
        trace = []
        def observe(frame, event, arg):
            if event=='return' and frame.f_code.co_qualname=='Database.sql':
                query=str(frame.f_locals.get('query','')).replace('`','').upper()
                if re.search(r'^UPDATE\s+TABSESSIONS\b',query):
                    trace.append({'function':'Sessions UPDATE result','rowcount':frame.f_locals['self']._cursor.rowcount,'in_test':bool(frappe.in_test)})
            if event != 'call':
                return
            qn = frame.f_code.co_qualname
            fn = frame.f_code.co_filename
            if qn in TRACE_NAMES and '/apps/frappe/frappe/' in fn:
                trace.append({'function': qn, 'line': frame.f_code.co_firstlineno,
                              'file': fn.split('/apps/frappe/', 1)[1],
                              'in_test': bool(frappe.in_test)})
        headers = {'Accept': 'text/html' if html else 'application/json', 'User-Agent': 'LocalHTTPChainProbe/1'}
        if token is not None:
            headers['X-Frappe-CSRF-Token'] = token
        response = None
        sys.setprofile(observe)
        try:
            response = client.open(path, method=method, data=data, headers=headers,
                base_url='http://' + SITE, environ_overrides={'REMOTE_ADDR': ip}, buffered=True)
            text = response.get_data(as_text=True)
            body = response.get_json(silent=True) or {}
            entry = {'label': label, 'method': method, 'path': path, 'http_status': response.status_code,
                     'exc_type': body.get('exc_type'), 'trace': trace,
                     'request_csrf_present': token is not None,
                     'response_bytes': len(response.data)}
            result['requests'].append(entry)
            return response.status_code, body, text
        finally:
            if response is not None:
                response.close()
            sys.setprofile(None)

    with contextlib.redirect_stdout(suppressed), contextlib.redirect_stderr(suppressed):
        try:
            connect()
            try:
                check('real_request_test_mode_off', not bool(frappe.in_test))
                check('csrf_enabled', not frappe.conf.ignore_csrf)
                check('no_referrer_bypass', not frappe.cache.get_value('allowed_referrers', generator=lambda: frappe.conf.get('allowed_referrers', [])))
                check('password_login_enabled', not frappe.db.get_single_value('System Settings', 'disable_user_pass_login'))
                check('fixture_unique', not frappe.db.exists('User', user))
                # Fixture setup only: run native Contact creation synchronously rather than
                # leave a delayed worker job referring to a user deleted at cleanup.
                frappe.in_test = True
                frappe.flags.in_test = True
                doc = frappe.get_doc({'doctype': 'User', 'email': user, 'first_name': 'HTTP Chain Probe',
                    'enabled': 1, 'send_welcome_email': 0, 'new_password': password,
                    'roles': [{'role': 'Desk User'}], 'user_type': 'System User'})
                doc.insert()
                fixture_created = True
                check("fixture_contact_created_synchronously", frappe.db.count("Contact", {"user": user}) == 1)
                check('fixture_is_system_user', doc.user_type == 'System User')
                frappe.db.commit()
            finally:
                frappe.in_test = False
                frappe.flags.in_test = False
                close()

            client = Client(application, Response, use_cookies=True)
            code, body, _ = request(client, 'first_login', 'POST', '/api/method/login',
                                    {'cmd': 'login', 'usr': user, 'pwd': password})
            cookie = client.get_cookie('sid', domain=SITE)
            check('fresh_sid_returned', bool(cookie and cookie.value and cookie.value != 'Guest'))
            sid = cookie.value
            owned_sids.add(sid)
            persisted = read_session(sid)
            result['pre_desk'] = {'sid_persisted_for_fixture': True,
                                  'csrf_token_present': bool(persisted.get('csrf_token'))}
            check('first_login_succeeds', code == 200 and body.get('message') == 'Logged In')
            check('baseline_does_not_precreate_token', not persisted.get('csrf_token'))
            code, body, html = request(client, 'first_desk', 'GET', '/desk', html=True)
            check('first_desk_200', code == 200 and 'frappe.boot =' in html)
            match = re.search(r'frappe\.csrf_token\s*=\s*"([^"\r\n]+)"', html)
            check('desk_has_csrf_token', bool(match))
            token = match.group(1)
            persisted = read_session(sid)
            check('desk_token_matches_persisted', persisted.get('csrf_token') == token)
            code, body, _ = request(client, 'valid_token_post', 'POST', '/api/method/frappe.auth.get_logged_user', token=token)
            check('valid_token_returns_fixture_user', code == 200 and body.get('message') == user)
            code, body, _ = request(client, 'wrong_token_post', 'POST', '/api/method/frappe.auth.get_logged_user', token=secrets.token_hex(32))
            check('wrong_token_rejected', code == 400 and body.get('exc_type') == 'CSRFTokenError')
            code, body, _ = request(client, 'missing_token_post', 'POST', '/api/method/frappe.auth.get_logged_user')
            check('missing_token_rejected', code == 400 and body.get('exc_type') == 'CSRFTokenError')
            code, body, _ = request(client, 'valid_after_rejections', 'POST', '/api/method/frappe.auth.get_logged_user', token=token)
            check('session_survives_rejected_posts', code == 200 and body.get('message') == user)
            # Boundary 1: real forced HTTP refreshes at the same frozen instant.
            # Time is a deterministic fixture; Session, CSRF and DB remain real.
            from datetime import datetime, timezone, timedelta
            from freezegun import freeze_time
            fixed=datetime.now(timezone.utc).replace(microsecond=0)
            with freeze_time(fixed):
                code,_,_=request(client,'same_time_refresh_1','POST','/api/method/frappe.sessions.clear',token=token)
                check('same_time_first_force_200',code==200)
                first=read_session(sid)
                code,_,_=request(client,'same_time_refresh_2','POST','/api/method/frappe.sessions.clear',token=token)
                check('same_time_second_force_200',code==200)
                second=read_session(sid)
                check('same_time_session_values_unchanged',first==second)
            result['same_value_boundary']={'sessiondata_equal':first==second,'fixed_clock':fixed.isoformat(),
                'second_update_sql_results':[x for x in result['requests'][-1]['trace'] if x['function']=='Sessions UPDATE result']}
            # Boundary 2: non-HTTP caller without request IP. Use the real Session.
            connect()
            try:
                from werkzeug.test import EnvironBuilder
                from werkzeug.wrappers import Request
                from frappe.sessions import Session
                frappe.local.request=Request(EnvironBuilder(path='/desk',headers={'Cookie':'sid='+sid}).get_environ())
                frappe.local.request_ip=None
                obj=Session(user=user,resume=True)
                frappe.local.session_obj=obj
                frappe.local.session=obj.data
                before_ip=frappe.db.get_value('User',user,'last_ip')
                check('none_ip_has_previous_value',bool(before_ip))
                updated=obj.update(force=True)
                after_ip=frappe.db.get_value('User',user,'last_ip')
                result['none_ip_boundary']={'updated':bool(updated),'ip_preserved':before_ip==after_ip,
                    'non_http_caller':True,'in_test':bool(frappe.in_test)}
                check('none_ip_preserves_existing_user_ip',updated and before_ip==after_ip)
            finally:close()
            # Boundary 3: misleading cache identity must defer to real DB SID owner.
            connect()
            try:
                cached=frappe.cache.hget('session',sid)
                cached['user']='Guest';cached['data']['user']='Guest'
                frappe.cache.hset('session',sid,cached)
            finally:close()
            code,body,_=request(client,'cache_owner_mismatch','POST','/api/method/frappe.auth.get_logged_user',token=token)
            check('cache_owner_mismatch_uses_database_identity',code==200 and body.get('message')==user)
            connect()
            try:
                cached=frappe.cache.hget('session',sid)
                check('cache_owner_repaired',cached and cached.get('user')==user and cached.get('data',{}).get('user')==user)
            finally:close()
            # Boundary 4: DB expiry wins even when the cached record is still fresh.
            connect()
            try:
                from frappe.sessions import get_expired_threshold
                expiry=frappe.utils.get_datetime(get_expired_threshold())-timedelta(seconds=1)
                frappe.db.sql('UPDATE tabSessions SET lastupdate=%s WHERE sid=%s',(expiry,sid))
                frappe.db.commit()
                result['db_expiry_fixture']={'db_lastupdate':str(expiry),'cached_session_left_fresh':True}
            finally:close()
            code,body,_=request(client,'expired_db_fresh_cache','POST','/api/method/frappe.auth.get_logged_user',token=token)
            check('expired_database_session_cannot_authenticate',not(code==200 and body.get('message')==user))
            connect()
            try:
                check('expired_cache_removed',not frappe.cache.hget('session',sid))
                check('expired_database_session_removed',not frappe.db.sql('SELECT 1 FROM tabSessions WHERE sid=%s',(sid,)))
            finally:close()
            check('all_http_auth_calls_have_test_mode_off', all(not event['in_test'] for row in result['requests'] for event in row['trace']))
            check('all_requests_use_real_httprequest', all(any(t['function']=='HTTPRequest.__init__' for t in row['trace']) for row in result['requests']))
            result['status'] = 'passed'
        except Exception as exc:
            result['status'] = 'failed'
            result['failure'] = {'type': type(exc).__name__,
                'check': str(exc) if isinstance(exc, AssertionError) else None,
                'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name} for f in traceback.extract_tb(exc.__traceback__)]}
        finally:
            sys.setprofile(None)
            close()
            try:
                connect()
                if fixture_created:
                    from frappe.sessions import delete_session
                    sids = frappe.db.sql('SELECT sid FROM tabSessions WHERE user=%s', (user,), pluck=True)
                    owned_sids.update(sids)
                    for sid in sids:
                        delete_session(sid, user=user, reason='Local HTTP regression fixture cleanup')
                    for contact in frappe.get_all('Contact', filters={'user': user}, pluck='name'):
                        frappe.delete_doc('Contact', contact, force=True, delete_permanently=True)
                    frappe.db.delete('Activity Log', {'user': user})
                    frappe.db.delete('Notification Log', {'for_user': user})
                    frappe.delete_doc('User', user, force=True, delete_permanently=True)
                    frappe.db.delete('Deleted Document', {'deleted_doctype': 'Notification Settings', 'deleted_name': user})
                    frappe.db.commit()
                    frappe.clear_cache(user=user)
                    for sid in owned_sids:
                        frappe.cache.hdel('session', sid)
                counts = {
                    'users': frappe.db.count('User', {'name': user}),
                    'sessions': frappe.db.count('Sessions', {'user': user}),
                    'passwords': frappe.db.sql('SELECT COUNT(*) FROM __Auth WHERE doctype=%s AND name=%s', ('User', user))[0][0],
                    'roles': frappe.db.count('Has Role', {'parenttype': 'User', 'parent': user}),
                    'contacts': frappe.db.count('Contact', {'user': user}),
                    'activity_logs': frappe.db.count('Activity Log', {'user': user}),
                    'notification_settings': frappe.db.count('Notification Settings', {'name': user}),
                    'shares': frappe.db.count('DocShare', {'user': user}),
                }
                result['cleanup'] = {'remaining': counts, 'all_zero': not any(counts.values()),
                    'cached_test_sessions_remaining': sum(bool(frappe.cache.hget('session', sid)) for sid in owned_sids)}
                if not result['cleanup']['all_zero'] or result['cleanup']['cached_test_sessions_remaining']:
                    result['status'] = 'failed'
            except Exception as exc:
                result['cleanup'] = {'error_type': type(exc).__name__,
                    'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name} for f in traceback.extract_tb(exc.__traceback__)]}
                result['status'] = 'failed'
            finally:
                close()
    result['suppressed_framework_output_chars'] = len(suppressed.getvalue())
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'variant': args.variant, 'requests': len(result['requests']),
                      'checks': len(result['checks']), 'cleanup': result['cleanup']}))
    return 0 if result['status'] == 'passed' else 1

if __name__ == '__main__':
    raise SystemExit(main())
