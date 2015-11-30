from .fileutil import dumps, loads
from .log import threadlog, thread_push_log, thread_pop_log
from .readonly import ReadonlyView
from .readonly import ensure_deeply_readonly, get_mutable_deepcopy
from repoze.lru import LRUCache
import contextlib
import os
import py
import sqlite3
import time


class BaseConnection:
    def __init__(self, sqlconn, basedir, storage):
        self._sqlconn = sqlconn
        self._basedir = basedir
        self.dirty_files = {}
        self.storage = storage
        self._changelog_cache = storage._changelog_cache

    def close(self):
        self._sqlconn.close()

    def db_read_typedkey(self, relpath):
        q = "SELECT keyname, serial FROM kv WHERE key = ?"
        c = self._sqlconn.cursor()
        row = c.execute(q, (relpath,)).fetchone()
        if row is None:
            raise KeyError(relpath)
        return tuple(row[:2])

    def db_write_typedkey(self, relpath, name, next_serial):
        q = "INSERT OR REPLACE INTO kv (key, keyname, serial) VALUES (?, ?, ?)"
        self._sqlconn.execute(q, (relpath, name, next_serial))

    def write_changelog_entry(self, serial, entry):
        threadlog.debug("writing changelog for serial %s", serial)
        data = dumps(entry)
        self._sqlconn.execute(
            "INSERT INTO changelog (serial, data) VALUES (?, ?)",
            (serial, sqlite3.Binary(data)))
        self._sqlconn.commit()

    def get_raw_changelog_entry(self, serial):
        q = "SELECT data FROM changelog WHERE serial = ?"
        row = self._sqlconn.execute(q, (serial,)).fetchone()
        if row is not None:
            return bytes(row[0])
        return None

    def get_changes(self, serial):
        changes = self._changelog_cache.get(serial)
        if changes is None:
            data = self.get_raw_changelog_entry(serial)
            changes, rel_renames = loads(data)
            # make values in changes read only so no calling site accidentally
            # modifies data
            changes = ensure_deeply_readonly(changes)
            assert isinstance(changes, ReadonlyView)
            self._changelog_cache.put(serial, changes)
        return changes


class Connection(BaseConnection):
    def io_file_os_path(self, path):
        return None

    def io_file_exists(self, path):
        assert not os.path.isabs(path)
        c = self._sqlconn.cursor()
        q = "SELECT path FROM files WHERE path = ?"
        c.execute(q, (path,))
        result = c.fetchone()
        c.close()
        return result is not None

    def io_file_set(self, path, content):
        assert not os.path.isabs(path)
        assert not path.endswith("-tmp")
        c = self._sqlconn.cursor()
        q = "INSERT OR REPLACE INTO files (path, size, data) VALUES (?, ?, ?)"
        c.execute(q, (path, len(content), sqlite3.Binary(content)))
        c.close()

    def io_file_open(self, path):
        return py.io.BytesIO(self.io_file_get(path))

    def io_file_get(self, path):
        assert not os.path.isabs(path)
        c = self._sqlconn.cursor()
        q = "SELECT data FROM files WHERE path = ?"
        c.execute(q, (path,))
        content = c.fetchone()
        c.close()
        if content is None:
            raise IOError()
        return bytes(content[0])

    def io_file_size(self, path):
        assert not os.path.isabs(path)
        c = self._sqlconn.cursor()
        q = "SELECT size FROM files WHERE path = ?"
        c.execute(q, (path,))
        result = c.fetchone()
        c.close()
        if result is not None:
            return result[0]

    def io_file_delete(self, path):
        assert not os.path.isabs(path)
        c = self._sqlconn.cursor()
        q = "DELETE FROM files WHERE path = ?"
        c.execute(q, (path,))
        c.close()

    def write_transaction(self):
        return Writer(self.storage, self)


class BaseStorage:
    def __init__(self, basedir, notify_on_commit, cache_size):
        self.basedir = basedir
        self.sqlpath = self.basedir.join(".sqlite")
        self._notify_on_commit = notify_on_commit
        self._changelog_cache = LRUCache(cache_size)  # is thread safe
        self.last_commit_timestamp = time.time()
        self.ensure_tables_exist()
        with self.get_connection() as conn:
            row = conn._sqlconn.execute("select max(serial) from changelog").fetchone()
            serial = row[0]
            if serial is None:
                self.next_serial = 0
            else:
                self.next_serial = serial + 1

    def get_connection(self, closing=True):
        sqlconn = sqlite3.connect(str(self.sqlpath), timeout=60)
        conn = self.Connection(sqlconn, self.basedir, self)
        conn.text_factory = bytes
        if closing:
            return contextlib.closing(conn)
        return conn


class Storage(BaseStorage):
    Connection = Connection

    def perform_crash_recovery(self):
        pass

    def ensure_tables_exist(self):
        if self.sqlpath.exists():
            return
        with self.get_connection() as conn:
            threadlog.info("DB: Creating schema")
            c = conn._sqlconn.cursor()
            c.execute("""
                CREATE TABLE kv (
                    key TEXT NOT NULL PRIMARY KEY,
                    keyname TEXT,
                    serial INTEGER
                )
            """)
            c.execute("""
                CREATE TABLE changelog (
                    serial INTEGER PRIMARY KEY,
                    data BLOB NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE files (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    data BLOB NOT NULL
                )
            """)


def devpiserver_storage_backend():
    return dict(
        storage=Storage,
        name="sqlite_db_files",
        description="SQLite backend with files in DB for testing only")


class Writer:
    def __init__(self, storage, conn):
        self.conn = conn
        self.storage = storage
        self.changes = {}

    def record_set(self, typedkey, value=None):
        """ record setting typedkey to value (None means it's deleted) """
        assert not isinstance(value, ReadonlyView), value
        try:
            _, back_serial = self.conn.db_read_typedkey(typedkey.relpath)
        except KeyError:
            back_serial = -1
        self.conn.db_write_typedkey(typedkey.relpath, typedkey.name,
                                    self.storage.next_serial)
        # at __exit__ time we write out changes to the _changelog_cache
        # so we protect here against the caller modifying the value later
        value = get_mutable_deepcopy(value)
        self.changes[typedkey.relpath] = (typedkey.name, back_serial, value)

    def __enter__(self):
        self.log = thread_push_log("fswriter%s:" % self.storage.next_serial)
        return self

    def __exit__(self, cls, val, tb):
        thread_pop_log("fswriter%s:" % self.storage.next_serial)
        if cls is None:
            entry = self.changes, []
            self.conn.write_changelog_entry(self.storage.next_serial, entry)
            commit_serial = self.storage.next_serial
            self.storage.next_serial += 1
            message = "committed: keys: %s"
            args = [",".join(map(repr, list(self.changes)))]
            self.log.info(message, *args)

            self.storage._notify_on_commit(commit_serial)
        else:
            self.log.info("roll back at %s" %(self.storage.next_serial))
