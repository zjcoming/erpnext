"""Explicit disposable-site real HTTP regression. See production patches README."""
import os
import argparse, contextlib, contextvars, hashlib, io, json, re, secrets, sys, threading, traceback, uuid
from pathlib import Path

SITE = os.environ.get('PS_SESSION_TEST_SITE', '')
SITES = os.environ.get('PS_SESSION_SITES_PATH', '/home/frappe/frappe-bench/sites')

def frames(exc):
    return [{'file':f.filename,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    args.variant="candidate"
    out=Path(args.out)
    assert not out.exists()
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

    result={'site':SITE,'variant':args.variant,'source_sha256':expected_source_sha,
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'manifest_sha256':manifest_sha,'native_manifest_all_matches':True,'mocks':[],'transport':'Werkzeug Client -> frappe.app.application',
            'cases':[],'status':'running'}
    suppressed=io.StringIO()

    def connect():
        frappe.init(site=SITE,sites_path=SITES)
        frappe.connect()
        assert frappe.conf.db_host in ('db','localhost','127.0.0.1','::1') and not frappe.in_test and not frappe.conf.ignore_csrf
    def close():
        if getattr(frappe.local,'db',None): frappe.db.rollback()
        frappe.destroy()

    with contextlib.redirect_stdout(suppressed),contextlib.redirect_stderr(suppressed):
      for mode in ('A_activity_after_snapshot','B1_revoke_after_snapshot','B2_revoke_before_snapshot_cached','B3_revoke_after_commit_before_cache','C_password_reset_after_snapshot','E_after_response_revocation'):
        user='http-race-'+uuid.uuid4().hex+'@example.invalid'
        password=secrets.token_urlsafe(40)
        new_password=secrets.token_urlsafe(40)
        request_ip='127.78.'+str(secrets.randbelow(254)+1)+'.'+str(secrets.randbelow(254)+1)
        sid=None
        created=False
        info={'case':mode,'mechanism_checks':[],'safety_checks':[],'requests':[],
              'interleaving':{},'database_exceptions':[],'session_sql_results':[],'status':'running'}
        result['cases'].append(info)
        armed=False
        fired=False
        prior_reads=[]
        prior_writes=[]
        prior_session_updates=0
        def check(name,value,safety=False):
            info['safety_checks' if safety else 'mechanism_checks'].append({'name':name,'passed':bool(value)})
            if not value and not safety: raise AssertionError(name)
        secondary={}
        ready=threading.Event();go=threading.Event();cancel=threading.Event()
        worker=None
        def prepare_secondary():
            nonlocal worker
            def work():
                try:
                    secondary['stage']='connecting'
                    connect()
                    from frappe.sessions import delete_session
                    secondary['connection_id']=frappe.db.sql('SELECT CONNECTION_ID()')[0][0]
                    frappe.db.sql('SET SESSION innodb_lock_wait_timeout=4')
                    secondary['stage']='ready'
                    ready.set()
                    if not go.wait(timeout=45) or cancel.is_set(): return
                    secondary['stage']='mutation'
                    if mode.startswith('A_'):
                        frappe.db.sql('UPDATE tabUser SET last_active=DATE_ADD(COALESCE(last_active,NOW()),INTERVAL 1 SECOND) WHERE name=%s',(user,))
                        frappe.db.commit()
                    elif mode.startswith('C_'):
                        from frappe.utils.password import update_password
                        update_password(user,new_password,logout_all_sessions=True)
                        frappe.db.commit()
                        secondary['db_session_absent']=not bool(frappe.db.sql('SELECT 1 FROM tabSessions WHERE sid=%s',(sid,)))
                        secondary['cache_session_absent']=not bool(frappe.cache.hget('session',sid))
                        secondary['password_updated_using_native_helper']=True
                    else:
                        delete_session(sid,user=user,reason='Local HTTP interleaving probe')
                        secondary['db_session_absent']=not bool(frappe.db.sql('SELECT 1 FROM tabSessions WHERE sid=%s',(sid,)))
                        secondary['cache_session_absent']=not bool(frappe.cache.hget('session',sid))
                    secondary['committed']=True
                    secondary['stage']='completed'
                except Exception as exc:
                    secondary['error_type']=type(exc).__name__
                    secondary['errno']=exc.args[0] if exc.args and isinstance(exc.args[0],int) else None
                    secondary['error_frames']=frames(exc)
                finally:
                    ready.set();close()
            worker=threading.Thread(target=lambda:contextvars.Context().run(work),daemon=True)
            worker.start()
            check('secondary_preconnected_before_desk',ready.wait(timeout=12) and secondary.get('stage')=='ready')
        def independent(action):
            info['secondary_diagnostic']=secondary
            go.set()
            worker.join(timeout=12)
            if worker.is_alive():
                f=sys._current_frames().get(worker.ident)
                secondary['thread_stack']=[{'file':x.filename,'line':x.lineno,'function':x.name} for x in traceback.extract_stack(f)] if f else []
            check('independent_connection_completed_without_timeout',not worker.is_alive())
            check('independent_connection_committed',secondary.get('committed') is True)
            return dict(secondary)
        def inject(point):
            nonlocal fired
            fired=True
            d={'point':point,'prior_table_reads':list(prior_reads),'prior_writes_or_locks':list(prior_writes),
               'main_connection_id':frappe.db.sql('SELECT CONNECTION_ID()')[0][0],
               'in_test':bool(frappe.in_test),
               'after_response_stack':any(x.name=='run' and x.filename.endswith('/frappe/utils/__init__.py') for x in traceback.extract_stack())}
            info['interleaving']=d
            if mode=='B2_revoke_before_snapshot_cached':
                check('B2_no_table_read_before_cached_session_capture',not prior_reads)
            elif mode=='A_activity_after_snapshot':
                d['old_user_row_read']=bool(frappe.db.sql('SELECT last_active FROM tabUser WHERE name=%s',(user,)))
            else:
                d['old_session_row_read']=bool(frappe.db.sql('SELECT 1 FROM tabSessions WHERE sid=%s',(sid,)))
            d['secondary']=independent('activity' if mode.startswith('A_') else 'revoke')
            check('distinct_database_connections',d['main_connection_id']!=d['secondary']['connection_id'])
            check('http_context_unchanged_by_secondary',frappe.db.sql('SELECT CONNECTION_ID()')[0][0]==d['main_connection_id'])
        def profile(frame,event,arg):
            nonlocal prior_session_updates
            if not armed: return
            name=frame.f_code.co_qualname
            fn=frame.f_code.co_filename
            if event=='call' and name=='Session.update': prior_session_updates+=1
            if event=='return' and name=='Database.sql':
                query=str(frame.f_locals.get('query','')).replace('`','').upper()
                if re.search(r'^UPDATE\s+TABSESSIONS\b',query):
                    info['session_sql_results'].append({'kind':'update_sessions','rowcount':frame.f_locals['self']._cursor.rowcount})
            if not fired and event=='call' and name=='Database.sql' and '/apps/frappe/frappe/' in fn:
                query=str(frame.f_locals.get('query',''))
                normalized=query.replace('`','').upper()
                parent=frame.f_back
                in_update=False
                while parent:
                    if parent.f_code.co_qualname=='Session.update': in_update=True;break
                    parent=parent.f_back
                if mode in ('A_activity_after_snapshot','B1_revoke_after_snapshot','C_password_reset_after_snapshot','E_after_response_revocation') and in_update and 'SELECT' in normalized and 'TABUSER' in normalized and 'FOR UPDATE' in normalized:
                    inject('before_session_update_user_lock')
                elif re.search(r'\bSELECT\b.+\bFROM\b',normalized,re.S):
                    # Table identifiers only; never record parameter values or SQL text.
                    tables=re.findall(r'\bFROM\s+([A-Z0-9_]+)',normalized)
                    row={'tables':tables,'function':frame.f_back.f_code.co_qualname}
                    if row not in prior_reads: prior_reads.append(row)
                if not fired and (re.search(r'\b(UPDATE|INSERT|DELETE)\b',normalized) or 'FOR UPDATE' in normalized):
                    prior_writes.append({'verb':normalized.split()[0], 'tables':re.findall(r'\b(?:FROM|UPDATE|INTO)\s+([A-Z0-9_]+)',normalized),'columns':re.findall(r'(?:SET|,)\s*([A-Z0-9_]+)\s*=',normalized),'function':frame.f_back.f_code.co_qualname, 'stack':[{'file':x.filename,'line':x.lineno,'function':x.name} for x in traceback.extract_stack(frame.f_back)[-8:]]})
            if (not fired and mode=='B2_revoke_before_snapshot_cached' and event=='return'
                and name=='RedisWrapper.hget' and frame.f_locals.get('name')=='session'
                and frame.f_locals.get('key')==sid and arg):
                inject('cached_session_loaded_before_first_table_read')
            if (not fired and mode=='B3_revoke_after_commit_before_cache' and event=='call'
                and name=='RedisWrapper.hset' and frame.f_locals.get('name')=='session'
                and frame.f_locals.get('key')==sid and frame.f_back.f_code.co_qualname=='Session.update'):
                inject('session_commit_returned_before_cache_hset')
        def trace(frame,event,arg):
            if event=='call':
                if frame.f_code.co_qualname=='Database.sql': return trace
                return None
            if event=='exception':
                typ,exc,tb=arg
                errno=exc.args[0] if exc.args and isinstance(exc.args[0],int) else None
                if errno is not None:
                    row={'type':typ.__name__,'errno':errno,'file':frame.f_code.co_filename,'line':frame.f_lineno}
                    if row not in info['database_exceptions']: info['database_exceptions'].append(row)
            return trace
        def request(client,label,method,path,data=None,token=None,injecting=False,html=False):
            nonlocal armed
            headers={'Accept':'text/html' if html else 'application/json','User-Agent':'LocalHTTPRaceProbe/1'}
            if token: headers['X-Frappe-CSRF-Token']=token
            response=None
            entry=None
            armed=injecting
            if injecting: sys.setprofile(profile);sys.settrace(trace)
            try:
                response=client.open(path,method=method,data=data,headers=headers,base_url='http://'+SITE,
                                     environ_overrides={'REMOTE_ADDR':request_ip},buffered=False)
                body=response.get_json(silent=True) or {}
                text=response.get_data(as_text=True)
                entry={'label':label,'status':response.status_code,'exc_type':body.get('exc_type'),
                       'response_bytes':len(response.data),'identity_is_fixture':body.get('message')==user}
                info['requests'].append(entry)
                return response.status_code,body,text
            finally:
                if response is not None:
                    try:
                        response.close()
                        if entry is not None: entry['close_completed']=True
                    except Exception as exc:
                        if entry is not None: entry['close_error_type']=type(exc).__name__
                        raise
                    finally:
                        if entry is not None: entry['frappe_context_destroyed']=not bool(getattr(frappe.local,'initialised',False))
                sys.setprofile(None);sys.settrace(None);armed=False
        try:
            connect()
            try:
                check('fixture_unique',not frappe.db.exists('User',user))
                frappe.in_test=True
                frappe.flags.in_test=True
                doc=frappe.get_doc({'doctype':'User','email':user,'first_name':'HTTP Race Probe','enabled':1,
                    'send_welcome_email':0,'new_password':password,'roles':[{'role':'Desk User'}]})
                doc.insert();created=True
                check("fixture_contact_created_synchronously",frappe.db.count("Contact",{"user":user})==1)
                check('fixture_system_user',doc.user_type=='System User')
                frappe.db.commit()
            finally:
                frappe.in_test=False;frappe.flags.in_test=False;close()
            warm=Client(application,Response,use_cookies=True)
            warm_code,warm_body,_=request(warm,'fixture_initial_login','POST','/api/method/login',{'cmd':'login','usr':user,'pwd':password})
            check('fixture_initial_login_200',warm_code==200 and warm_body.get('message')=='Logged In')
            warm_code,_,warm_html=request(warm,'fixture_initial_desk','GET','/desk',html=True)
            warm_match=re.search(r'frappe\.csrf_token\s*=\s*"([^"\r\n]+)"',warm_html)
            check('fixture_initial_desk_200',warm_code==200 and bool(warm_match))
            warm_code,_,_=request(warm,'fixture_native_logout','POST','/api/method/logout',token=warm_match.group(1))
            check('fixture_native_logout_200',warm_code==200)
            connect()
            try:
                check('fixture_initial_versions_recorded',bool(frappe.db.get_value('User',user,'last_known_versions')))
                check('fixture_no_old_sessions',frappe.db.count('Sessions',{'user':user})==0)
            finally: close()
            client=Client(application,Response,use_cookies=True)
            code,body,_=request(client,'fresh_login','POST','/api/method/login',{'cmd':'login','usr':user,'pwd':password})
            check('first_login_200',code==200 and body.get('message')=='Logged In')
            cookie=client.get_cookie('sid',domain=SITE)
            check('new_session_cookie_exists',bool(cookie and cookie.value!='Guest'))
            sid=cookie.value
            follow_token=None
            if mode.startswith('E_'):
                code,_,first_html=request(client,'prepare_token_before_expiry_refresh','GET','/desk',html=True)
                token_match=re.search(r'frappe\.csrf_token\s*=\s*"([^"\r\n]+)"',first_html)
                check('E_initial_desk_200',code==200 and bool(token_match))
                follow_token=token_match.group(1)
                connect()
                try:
                    from frappe.sessions import get_expiry_in_seconds
                    from datetime import timedelta
                    lifetime=get_expiry_in_seconds()
                    threshold=min(lifetime/2,600) or 600
                    age=threshold+1
                    check('E_fixture_age_within_lifetime',age<lifetime)
                    stored=json.loads(frappe.db.sql('SELECT sessiondata FROM tabSessions WHERE sid=%s',(sid,))[0][0])
                    cached=frappe.cache.hget('session',sid)
                    old=str(frappe.utils.now_datetime()-timedelta(seconds=age))
                    stored['last_updated']=old;cached['data']['last_updated']=old
                    frappe.db.sql('UPDATE tabSessions SET sessiondata=%s,lastupdate=%s WHERE sid=%s',(json.dumps(stored),old,sid))
                    frappe.db.commit();frappe.cache.hset('session',sid,cached)
                    info['fixture_refresh_age']={'seconds':age,'refresh_threshold':threshold,'session_lifetime':lifetime,'purpose':'trigger genuine after_response Session.update(force=False)'}
                finally:close()
            prepare_secondary()
            if mode.startswith('E_'):
                code,body,html=request(client,'response_then_revoked_refresh','POST','/api/method/frappe.auth.get_logged_user',token=follow_token,injecting=True)
                check('E_response_was_already_authenticated',code==200 and body.get('message')==user)
                check('E_barrier_is_after_response',info['interleaving']['after_response_stack'])
                check('E_response_close_and_destroy_completed',info['requests'][-1].get('close_completed') and info['requests'][-1].get('frappe_context_destroyed'),safety=True)
            else:
                code,body,html=request(client,'first_desk_race','GET','/desk',injecting=True,html=True)
            info['session_update_calls_in_desk']=prior_session_updates
            check('interleaving_fired_exactly_once',fired)
            check('real_http_not_test_mode',info['interleaving'].get('in_test') is False)
            match=re.search(r'frappe\.csrf_token\s*=\s*"([^"\r\n]+)"',html)
            token=match.group(1) if match else follow_token
            connect()
            try:
                rows=frappe.db.sql('SELECT sessiondata FROM tabSessions WHERE sid=%s AND user=%s',(sid,user))
                cached=frappe.cache.hget('session',sid)
                info['after_race']={'db_session_present':bool(rows),'cache_session_present':bool(cached),
                    'db_token_present':bool(rows and json.loads(rows[0][0]).get('csrf_token')),
                    'cache_token_present':bool(cached and cached.get('data',{}).get('csrf_token'))}
                if not token and cached: token=cached.get('data',{}).get('csrf_token')
            finally: close()
            if mode.startswith('A_'):
                conflict=any(e['errno']==1020 for e in info['database_exceptions'])
                info['observed_snapshot_conflict_1020']=conflict
                check('desk_succeeds_despite_activity_race',code==200 and bool(match),safety=True)
                check('session_not_lost',info['after_race']['db_session_present'],safety=True)
                check('activity_no_1020',not conflict and code==200)
            else:
                check('revocation_deleted_real_session',info['interleaving']['secondary'].get('db_session_absent') is True)
                check('revocation_deleted_cached_session',info['interleaving']['secondary'].get('cache_session_absent') is True)
                check('revoked_session_not_recreated_in_db',not info['after_race']['db_session_present'],safety=True)
                info['cache_republished_before_next_request']=info['after_race']['cache_session_present']
                if mode!='B3_revoke_after_commit_before_cache':
                    check('revoked_session_not_republished_to_cache',not info['after_race']['cache_session_present'],safety=True)
                follow_code,follow_body,_=request(client,'next_authenticated_read','POST','/api/method/frappe.auth.get_logged_user',token=token)
                check('revoked_sid_cannot_authenticate_next_request',not(follow_code==200 and follow_body.get('message')==user),safety=True)
                connect()
                try:
                    info['after_next_request']={'db_session_present':bool(frappe.db.sql('SELECT 1 FROM tabSessions WHERE sid=%s',(sid,))), 'cache_session_present':bool(frappe.cache.hget('session',sid))}
                finally:close()
                check('revoked_cache_absent_after_next_request',not info['after_next_request']['cache_session_present'],safety=True)
                if mode.startswith('C_'):
                    wrong=Client(application,Response,use_cookies=True)
                    old_code,old_body,_=request(wrong,'old_password_login','POST','/api/method/login',{'cmd':'login','usr':user,'pwd':password})
                    check('old_password_rejected_by_real_http',old_code==401 and old_body.get('exc_type')=='AuthenticationError',safety=True)
                    fresh=Client(application,Response,use_cookies=True)
                    new_code,new_body,_=request(fresh,'new_password_login','POST','/api/method/login',{'cmd':'login','usr':user,'pwd':new_password})
                    check('new_password_real_http_login_200',new_code==200 and new_body.get('message')=='Logged In',safety=True)
                    new_code,_,new_html=request(fresh,'new_password_desk','GET','/desk',html=True)
                    new_match=re.search(r'frappe\.csrf_token\s*=\s*"([^"\r\n]+)"',new_html)
                    check('new_password_desk_200',new_code==200 and bool(new_match),safety=True)
                    if new_match:
                        new_code,new_body,_=request(fresh,'new_password_token_post','POST','/api/method/frappe.auth.get_logged_user',token=new_match.group(1))
                        check('new_password_token_post_identity',new_code==200 and new_body.get('message')==user,safety=True)

            info['status']='observed'
            info['safety_all_passed']=all(x['passed'] for x in info['safety_checks'])
        except Exception as exc:
            info['status']='harness_failed'
            info['failure']={'type':type(exc).__name__,'check':str(exc) if isinstance(exc,AssertionError) else None,'frames':frames(exc)}
        finally:
            sys.setprofile(None);sys.settrace(None);armed=False;close()
            if worker and worker.is_alive():
                cancel.set();go.set();worker.join(timeout=12)
            try:
                connect()
                if created:
                    from frappe.sessions import delete_session
                    for current_sid in frappe.db.sql('SELECT sid FROM tabSessions WHERE user=%s',(user,),pluck=True):
                        delete_session(current_sid,user=user,reason='Local HTTP race fixture cleanup')
                    if sid: frappe.cache.hdel('session',sid)
                    for contact in frappe.get_all('Contact',filters={'user':user},pluck='name'):
                        frappe.delete_doc('Contact',contact,force=True,delete_permanently=True)
                    frappe.db.delete('Activity Log',{'user':user})
                    frappe.db.delete('Notification Log',{'for_user':user})
                    # Only this fixture's automatically captured HTTP error records.
                    frappe.db.delete('Error Log',{'owner':user})
                    frappe.delete_doc('User',user,force=True,delete_permanently=True)
                    frappe.db.delete('Deleted Document',{'deleted_doctype':'Notification Settings','deleted_name':user})
                    frappe.db.commit();frappe.clear_cache(user=user)
                    frappe.cache.hdel('login_failed_count',user)
                    frappe.cache.hdel('login_failed_count',request_ip)
                counts={name:frappe.db.count(dt,filters) for name,dt,filters in [
                    ('users','User',{'name':user}),('sessions','Sessions',{'user':user}),
                    ('roles','Has Role',{'parenttype':'User','parent':user}),('contacts','Contact',{'user':user}),
                    ('activity','Activity Log',{'user':user}),('errors','Error Log',{'owner':user}),
                    ('settings','Notification Settings',{'name':user}),('shares','DocShare',{'user':user})]}
                counts['passwords']=frappe.db.sql('SELECT COUNT(*) FROM __Auth WHERE doctype=%s AND name=%s',('User',user))[0][0]
                counts['cached_session']=int(bool(sid and frappe.cache.hget('session',sid)))
                info['cleanup']={'remaining':counts,'all_zero':not any(counts.values())}
                if any(counts.values()): info['status']='cleanup_failed'
            except Exception as exc:
                info['cleanup']={'error_type':type(exc).__name__,'frames':frames(exc)};info['status']='cleanup_failed'
            finally: close()
        if info['status']!='observed': break
    result['suppressed_framework_output_chars']=len(suppressed.getvalue())
    result['status']='observed' if all(c['status']=='observed' for c in result['cases']) and len(result['cases'])==6 else 'failed'
    result['safety_all_passed']=all(c.get('safety_all_passed',False) for c in result['cases'])
    result['mechanisms_status']=result['status']
    result['status']='passed' if result['status']=='observed' and result['safety_all_passed'] else 'failed'
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'safety_all_passed':result['safety_all_passed'],
                     'cases':[{'case':c['case'],'status':c['status'],'safety_all_passed':c.get('safety_all_passed'),'cleanup':c.get('cleanup')} for c in result['cases']]}))
    return 0 if result['status']=='passed' else 1

if __name__=='__main__': raise SystemExit(main())
