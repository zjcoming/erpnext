"""Real MariaDB regression checks; uses ONLY the disposable ps_txn_patch_probe schema.

Run with the patched Frappe package on PYTHONPATH inside an isolated test runtime.
Requires PS_TXN_DB_PASSWORD and an explicit PS_TXN_DISPOSABLE=1 guard. This is a
low-level database/transaction test, not a substitute for Sales Order HTTP UAT.
"""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import unittest
from unittest.mock import patch

import MySQLdb
from werkzeug.wrappers import Request

import frappe
from frappe.auth import lock_login_credentials
from frappe.database import get_db
from frappe.model.naming import _series_current_read, getseries, revert_series_if_last
from frappe.sessions import Session
from frappe.utils.password import update_password


SCHEMA = "ps_txn_patch_probe"
HOST = os.environ.get("PS_TXN_DB_HOST", "perf-fix-db")
PASSWORD = os.environ["PS_TXN_DB_PASSWORD"]
SITES = os.environ.get("PS_TXN_SITES", "/home/frappe/frappe-bench/sites")
SITE = os.environ.get("PS_TXN_SITE", "perf.localhost")


def connection(schema=SCHEMA):
    return MySQLdb.connect(host=HOST, user="root", passwd=PASSWORD, database=schema)


def init_context():
    frappe.init(SITE, sites_path=SITES, force=True)
    frappe.local.db = get_db(host=HOST, user="root", password=PASSWORD, cur_db_name=SCHEMA)


class TestMariaDBContention(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("PS_TXN_DISPOSABLE") != "1":
            raise RuntimeError("Explicit disposable database guard is required")
        cls.admin = connection(schema="mysql")
        with cls.admin.cursor() as cursor:
            cursor.execute("SELECT VERSION(), @@innodb_snapshot_isolation, @@tx_isolation")
            cls.version = cursor.fetchone()
            assert cls.version[0].startswith("11.8.8"), cls.version
            assert cls.version[1:] == (1, "REPEATABLE-READ"), cls.version
            cursor.execute(f"CREATE DATABASE `{SCHEMA}`")
            cursor.execute(f"USE `{SCHEMA}`")
            cursor.execute("CREATE TABLE tabSeries (name VARCHAR(140) PRIMARY KEY, current BIGINT NULL) ENGINE=InnoDB")
            cursor.execute("CREATE TABLE tabProbe (name VARCHAR(140) PRIMARY KEY, qty INT) ENGINE=InnoDB")
            cursor.execute("CREATE TABLE tabProbeDoc (name VARCHAR(140) PRIMARY KEY) ENGINE=InnoDB")
            cursor.execute("CREATE TABLE tabUser (name VARCHAR(140) PRIMARY KEY, username VARCHAR(140) UNIQUE, mobile_no VARCHAR(140) UNIQUE, enabled INT, last_active VARCHAR(40), last_ip VARCHAR(40)) ENGINE=InnoDB")
            cursor.execute("CREATE TABLE __Auth (doctype VARCHAR(40), name VARCHAR(140), fieldname VARCHAR(40), password TEXT, encrypted INT, PRIMARY KEY(doctype,name,fieldname)) ENGINE=InnoDB")
            cursor.execute("CREATE TABLE tabSessions (sid VARCHAR(140) PRIMARY KEY, user VARCHAR(140), sessiondata TEXT, lastupdate VARCHAR(40)) ENGINE=InnoDB")
        print("Database under test:", cls.version, flush=True)

    @classmethod
    def tearDownClass(cls):
        with cls.admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{SCHEMA}`")
        cls.admin.close()

    def setUp(self):
        with self.admin.cursor() as cursor:
            for table in ("tabSeries", "tabProbe", "tabProbeDoc", "tabUser", "__Auth", "tabSessions"):
                cursor.execute(f"DELETE FROM `{table}`")
            cursor.execute("INSERT INTO tabProbe VALUES ('stock', 1)")
            cursor.execute("INSERT INTO tabUser VALUES ('worker@example.test','worker','13900000000',1,NULL,NULL)")
            cursor.execute("INSERT INTO __Auth VALUES ('User','worker@example.test','password','hash-before',0)")
        self.admin.commit()
        init_context()

    def wait_for_lock(self, thread_id, result):
        deadline = time.monotonic() + 10
        state = None
        while time.monotonic() < deadline:
            if result.done():
                result.result()
                self.fail("Worker finished without reaching the intended lock wait")
            with self.admin.cursor() as cursor:
                cursor.execute("SELECT trx_state FROM information_schema.innodb_trx WHERE trx_mysql_thread_id=%s", (thread_id,))
                state = cursor.fetchone()
                if state in (("LOCK WAIT",), (b"LOCK WAIT",)):
                    return
                # MariaDB can wait for a unique-key lock during optimization
                # (PROCESSLIST state Statistics), before INNODB_TRX lists it.
                cursor.execute("SELECT INFO FROM information_schema.PROCESSLIST WHERE ID=%s AND COMMAND='Query'", (thread_id,))
                process = cursor.fetchone()
                if process and "tabUser" in (process[0] or ""):
                    return
            time.sleep(0.05)
        with self.admin.cursor() as cursor:
            cursor.execute("SELECT ID,DB,STATE,INFO FROM information_schema.PROCESSLIST WHERE DB=%s", (SCHEMA,))
            processes = cursor.fetchall()
            cursor.execute("SELECT trx_mysql_thread_id,trx_state,trx_query FROM information_schema.innodb_trx")
            transactions = cursor.fetchall()
        self.fail(f"Worker {thread_id} did not reach intended wait: {state!r}; processes={processes!r}; transactions={transactions!r}")

    def tearDown(self):
        frappe.db.rollback()
        frappe.destroy()

    def test_counter_current_read_preserves_business_snapshot_and_rollback(self):
        self.assertEqual(frappe.db.sql("SELECT qty FROM tabProbe"), ((1,),))
        other = connection()
        with other.cursor() as cursor:
            cursor.execute("INSERT INTO tabSeries VALUES ('PS-', 7)")
            cursor.execute("UPDATE tabProbe SET qty=2")
        other.commit()
        self.assertEqual(getseries("PS-", 5), "00008")
        self.assertEqual(frappe.db.transaction_writes, 1)
        self.assertEqual(frappe.db.sql("SELECT @@innodb_snapshot_isolation"), ((1,),))
        self.assertEqual(frappe.db.sql("SELECT qty FROM tabProbe"), ((1,),))
        frappe.db.rollback()
        with other.cursor() as cursor:
            cursor.execute("SELECT current FROM tabSeries WHERE name='PS-'")
            self.assertEqual(cursor.fetchone(), (7,))
        other.close()

    def test_new_series_concurrent_allocations_are_unique(self):
        barrier = threading.Barrier(12)

        def allocate(_):
            init_context()
            try:
                frappe.db.sql("SELECT qty FROM tabProbe")
                barrier.wait(timeout=15)
                value = getseries("NEW-", 5)
                frappe.db.sql("INSERT INTO tabProbeDoc VALUES (%s)", (value,))
                frappe.db.commit()
                return value
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=12) as pool:
            values = list(pool.map(allocate, range(12)))
        self.assertEqual(set(values), {f"{value:05}" for value in range(1, 13)})
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM tabProbeDoc"), ((12,),))

    def test_last_delete_conditional_decrement_and_rollback(self):
        self.assertEqual(getseries("PS-", 5), "00001")
        self.assertEqual(getseries("PS-", 5), "00002")
        frappe.db.commit()
        revert_series_if_last("PS-.#####", "PS-00001")
        self.assertEqual(frappe.db.sql("SELECT current FROM tabSeries"), ((2,),))
        revert_series_if_last("PS-.#####", "PS-00002")
        self.assertEqual(frappe.db.sql("SELECT current FROM tabSeries"), ((1,),))
        frappe.db.rollback()
        self.assertEqual(frappe.db.sql("SELECT current FROM tabSeries"), ((2,),))
        self.assertEqual(frappe.db.sql("SELECT @@innodb_snapshot_isolation"), ((1,),))

    def test_counter_update_does_not_mask_business_write_conflict(self):
        frappe.db.sql("SELECT qty FROM tabProbe")
        other = connection()
        with other.cursor() as cursor:
            cursor.execute("UPDATE tabProbe SET qty=2")
        other.commit()
        self.assertEqual(getseries("FAIL-", 5), "00001")
        with self.assertRaises(frappe.QueryDeadlockError):
            frappe.db.sql("UPDATE tabProbe SET qty=3")
        frappe.db.rollback()
        with other.cursor() as cursor:
            cursor.execute("SELECT current FROM tabSeries WHERE name='FAIL-'")
            self.assertIsNone(cursor.fetchone())
            cursor.execute("SELECT qty FROM tabProbe")
            self.assertEqual(cursor.fetchone(), (2,))
        other.close()

    def test_null_counter_and_statement_failure_keep_isolation(self):
        frappe.db.sql("INSERT INTO tabSeries VALUES ('NULL-', NULL)")
        frappe.db.commit()
        with self.assertRaises(MySQLdb.IntegrityError) as raised:
            getseries("NULL-", 5)
        self.assertEqual(raised.exception.args[0], 1062)
        self.assertEqual(frappe.db.sql("SELECT @@innodb_snapshot_isolation"), ((1,),))
        with self.assertRaises(MySQLdb.ProgrammingError):
            _series_current_read("UPDATE missing_probe_table SET x=1", ())
        self.assertEqual(frappe.db.sql("SELECT @@innodb_snapshot_isolation"), ((1,),))
        self.assertEqual(frappe.db.sql("SELECT current FROM tabSeries"), ((None,),))

    def test_concurrent_login_locks_precede_snapshot_and_resolve_all_aliases(self):
        barrier = threading.Barrier(12)

        def login(index):
            init_context()
            try:
                frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
                frappe.form_dict.usr = ("worker@example.test", "worker", "13900000000")[index % 3]
                self.assertIsNone(frappe.db._conn)
                barrier.wait(timeout=15)
                lock_login_credentials()
                rows = frappe.db.sql("SELECT enabled FROM tabUser WHERE name='worker@example.test'")
                self.assertEqual(rows, ((1,),))
                frappe.db.sql("UPDATE tabUser SET last_active=%s WHERE name='worker@example.test'", (str(index),))
                frappe.db.commit()
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(login, range(12)))

    def test_unknown_identifier_locks_without_changing_credentials(self):
        frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
        frappe.form_dict.usr = "does-not-exist@example.test"
        lock_login_credentials()
        self.assertEqual(frappe.db.sql("SELECT password FROM __Auth"), (("hash-before",),))
        self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM tabUser"), ((1,),))

    def test_unrelated_user_login_is_not_blocked(self):
        with self.admin.cursor() as cursor:
            cursor.execute("INSERT INTO tabUser VALUES ('other@example.test','other','13900000001',1,NULL,NULL)")
            cursor.execute("INSERT INTO __Auth VALUES ('User','other@example.test','password','other-hash',0)")
            cursor.execute("EXPLAIN SELECT name FROM tabUser WHERE name=%s FOR UPDATE", ("worker@example.test",))
            print("Credential lock query plan:", cursor.fetchall(), flush=True)
        self.admin.commit()
        frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
        frappe.form_dict.usr = "worker@example.test"
        lock_login_credentials()

        def other_login():
            init_context()
            try:
                frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
                frappe.form_dict.usr = "other@example.test"
                lock_login_credentials()
                frappe.db.commit()
                return True
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(other_login)
            try:
                self.assertTrue(result.result(timeout=2))
            finally:
                frappe.db.rollback()

    def test_password_reset_uses_same_lock_order_as_login(self):
        # Hold User ONLY. Old reset writes Auth first and then waits for User;
        # new reset must wait for User before locking Auth. NOWAIT distinguishes them.
        frappe.db.sql("SELECT name FROM tabUser WHERE name='worker@example.test' FOR UPDATE")
        started = threading.Event()
        worker = {}

        def reset():
            init_context()
            try:
                worker["id"] = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
                started.set()
                update_password("worker@example.test", "isolated-test-password-2026")
                frappe.db.sql("UPDATE tabUser SET last_active='reset' WHERE name='worker@example.test'")
                frappe.db.commit()
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(reset)
            try:
                self.assertTrue(started.wait(5))
                self.wait_for_lock(worker["id"], result)
                self.assertEqual(frappe.db.sql("SELECT password FROM __Auth WHERE name='worker@example.test' FOR UPDATE NOWAIT"), (("hash-before",),))
                frappe.db.sql("UPDATE tabUser SET last_active='login' WHERE name='worker@example.test'")
                frappe.db.commit()
                result.result(timeout=10)
            finally:
                frappe.db.rollback()
        self.assertNotEqual(frappe.db.sql("SELECT password FROM __Auth"), (("hash-before",),))

    def test_session_refresh_waits_for_user_before_locking_session(self):
        sid = "ps_txn_patch_probe_refresh"
        with self.admin.cursor() as cursor:
            cursor.execute("INSERT INTO tabSessions VALUES (%s,'worker@example.test','{}','2026-01-01')", (sid,))
        self.admin.commit()
        frappe.db.sql("SELECT name FROM tabUser WHERE name='worker@example.test' FOR UPDATE")
        started = threading.Event()
        worker = {}

        def refresh():
            init_context()
            try:
                frappe.local.system_settings = {"session_expiry": "240:00:00", "time_zone": "UTC"}
                frappe.local.request_ip = "127.0.0.1"
                session = Session.__new__(Session)
                session.sid = sid
                session._update_in_cache = False
                session.data = frappe._dict(user="worker@example.test", sid=sid, data=frappe._dict(last_updated="2026-01-01 00:00:00"))
                frappe.local.session = session.data
                worker["id"] = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
                started.set()
                return session.update(force=True)
            finally:
                frappe.cache.hdel("session", sid)
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(refresh)
            try:
                self.assertTrue(started.wait(5))
                self.wait_for_lock(worker["id"], result)
                self.assertEqual(frappe.db.sql("SELECT sid FROM tabSessions WHERE sid=%s FOR UPDATE NOWAIT", (sid,)), ((sid,),))
                frappe.db.commit()
                self.assertTrue(result.result(timeout=10))
            finally:
                frappe.db.rollback()

    def test_prelogin_guard_preserves_existing_callbacks_and_writes(self):
        frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
        frappe.form_dict.usr = "worker@example.test"
        for name in ("before_commit", "after_commit", "before_rollback", "after_rollback"):
            manager = getattr(frappe.db, name)
            manager.add(lambda: self.fail("Prelogin must not execute existing callbacks"))
            lock_login_credentials()
            self.assertIsNone(frappe.db._conn)
            self.assertEqual(len(manager._functions), 1)
            manager.reset()
        frappe.db.sql("INSERT INTO tabProbeDoc VALUES ('uncommitted')")
        lock_login_credentials()
        self.assertEqual(frappe.db.transaction_writes, 1)
        self.assertEqual(frappe.db.sql("SELECT name FROM tabProbeDoc"), (("uncommitted",),))

    def test_nonlogin_and_invalid_identifiers_do_not_start_db(self):
        frappe.local.request = Request.from_values(path="/api/resource/Sales Order", method="POST")
        frappe.form_dict.usr = "worker@example.test"
        lock_login_credentials()
        self.assertIsNone(frappe.db._conn)
        frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
        for user in (None, [], "", "x" * 141):
            frappe.form_dict.usr = user
            lock_login_credentials()
            self.assertIsNone(frappe.db._conn)

    def test_alias_change_between_lookup_and_locks_fails_before_authentication(self):
        frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
        frappe.form_dict.usr = "worker"
        database = frappe.local.db
        rollback = database.rollback

        def move_alias(*args, **kwargs):
            rollback(*args, **kwargs)
            with self.admin.cursor() as cursor:
                cursor.execute("UPDATE tabUser SET username='renamed' WHERE name='worker@example.test'")
            self.admin.commit()

        with patch.object(database, "rollback", side_effect=move_alias):
            with self.assertRaises(frappe.AuthenticationError) as raised:
                lock_login_credentials()
        self.assertEqual(str(raised.exception), "Invalid login credentials. Please try again.")

    def test_login_lock_timeout_preserves_original_error(self):
        other = connection()
        with other.cursor() as cursor:
            cursor.execute("SELECT name FROM tabUser WHERE name='worker@example.test' FOR UPDATE")
        database = frappe.local.db
        create_connection = database.get_connection

        def short_timeout_connection():
            conn = create_connection()
            with conn.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout=1")
            return conn

        frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
        frappe.form_dict.usr = "worker@example.test"
        try:
            with patch.object(database, "get_connection", side_effect=short_timeout_connection):
                with self.assertRaises(frappe.QueryTimeoutError) as raised:
                    lock_login_credentials()
            self.assertEqual(raised.exception.__cause__.args[0], 1205)
        finally:
            other.rollback()
            other.close()

    def test_login_waits_for_disable_and_observes_latest_credentials(self):
        other = connection()
        with other.cursor() as cursor:
            cursor.execute("UPDATE tabUser SET enabled=0 WHERE name='worker@example.test'")
            cursor.execute("UPDATE __Auth SET password='hash-after' WHERE name='worker@example.test'")
        started = threading.Event()

        def login():
            init_context()
            try:
                frappe.local.request = Request.from_values(path="/api/method/login", method="POST")
                frappe.form_dict.usr = "worker@example.test"
                started.set()
                lock_login_credentials()
                return (frappe.db.sql("SELECT enabled FROM tabUser"), frappe.db.sql("SELECT password FROM __Auth"))
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(login)
            try:
                self.assertTrue(started.wait(5))
                time.sleep(0.1)
                self.assertFalse(result.done())
                other.commit()
                self.assertEqual(result.result(timeout=10), (((0,),), (("hash-after",),)))
            finally:
                other.rollback()
                other.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
