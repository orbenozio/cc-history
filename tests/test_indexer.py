"""Platform-independent tests for cc_history.

Run with:  python -m pytest tests/  (or  python tests/test_indexer.py)

These tests exercise the parser, project decoder, query builder and the
end-to-end index/search/show path against a temp DB and the bundled fixture.
They never touch the real ~/.claude or the user's app data dir.
"""

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cc_history as cc  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample-session.jsonl"


class ParseTests(unittest.TestCase):
    def test_skips_noise_types(self):
        ts, entries = cc.parse_line({"type": "queue-operation"})
        self.assertEqual(entries, [])
        ts, entries = cc.parse_line({"type": "summary", "summary": "x"})
        self.assertEqual(entries, [])

    def test_text_block(self):
        obj = {"type": "user", "timestamp": "t",
               "message": {"content": [{"type": "text", "text": "hello"}]}}
        ts, entries = cc.parse_line(obj)
        self.assertEqual(ts, "t")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "text")
        self.assertEqual(entries[0].role, "user")
        self.assertEqual(entries[0].text, "hello")

    def test_thinking_is_assistant(self):
        obj = {"type": "assistant",
               "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}}
        _, entries = cc.parse_line(obj)
        self.assertEqual(entries[0].kind, "thinking")
        self.assertEqual(entries[0].role, "assistant")

    def test_tool_use_carries_name(self):
        obj = {"type": "assistant",
               "message": {"content": [
                   {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}}
        _, entries = cc.parse_line(obj)
        self.assertEqual(entries[0].kind, "tool_use")
        self.assertEqual(entries[0].tool_name, "Read")
        self.assertIn("Read(", entries[0].text)
        self.assertIn("/x", entries[0].text)

    def test_tool_result_is_user(self):
        obj = {"type": "user",
               "message": {"content": [{"type": "tool_result", "content": "output"}]}}
        _, entries = cc.parse_line(obj)
        self.assertEqual(entries[0].kind, "tool_result")
        self.assertEqual(entries[0].role, "user")

    def test_image_skipped(self):
        obj = {"type": "assistant",
               "message": {"content": [{"type": "image", "source": {}}]}}
        _, entries = cc.parse_line(obj)
        self.assertEqual(entries, [])

    def test_stringified_message(self):
        obj = {"type": "user", "message": "raw old string"}
        _, entries = cc.parse_line(obj)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].text, "raw old string")
        self.assertEqual(entries[0].kind, "text")

    def test_tool_result_truncation(self):
        big = "x" * (cc.TOOL_RESULT_MAX + 5000)
        obj = {"type": "user",
               "message": {"content": [{"type": "tool_result", "content": big}]}}
        _, entries = cc.parse_line(obj)
        self.assertIn("truncated", entries[0].text)
        self.assertLess(len(entries[0].text.encode("utf-8")),
                        cc.TOOL_RESULT_MAX + 200)


class QueryTests(unittest.TestCase):
    def test_phrase_autoquote(self):
        self.assertEqual(cc.build_fts_query("auth flow"), '"auth flow"')

    def test_operator_passthrough(self):
        self.assertEqual(cc.build_fts_query("auth OR login"), "auth OR login")
        self.assertEqual(cc.build_fts_query("data*"), "data*")
        self.assertEqual(cc.build_fts_query('"x y"'), '"x y"')

    def test_duration_parsing(self):
        # Both branches must return a UTC ("…Z") timestamp so string-compares
        # against entries.ts (also UTC "Z") are correct across timezones.
        iso_z = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        self.assertRegex(cc.parse_duration_or_date("2d"), iso_z)
        out = cc.parse_duration_or_date("2026-01-15")
        self.assertRegex(out, iso_z)
        # A naive local date is interpreted as local midnight, then converted
        # to UTC — verify it matches that exact conversion (tz-independent).
        from datetime import datetime, timezone
        expected = (datetime(2026, 1, 15)
                    .astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(out, expected)

    def test_explicit_offset_honored(self):
        # An explicit offset is converted to UTC, not reinterpreted as local.
        self.assertEqual(
            cc.parse_duration_or_date("2026-01-15T12:00:00+02:00"),
            "2026-01-15T10:00:00Z",
        )


class NaiveDecodeTests(unittest.TestCase):
    def test_naive_windows(self):
        if sys.platform == "win32":
            self.assertEqual(
                cc._decode_naive("c--Users-orben-Projects-Diburit"),
                "c:\\Users\\orben\\Projects\\Diburit",
            )

    def test_naive_posix(self):
        if sys.platform != "win32":
            self.assertEqual(
                cc._decode_naive("-Users-orben-Projects-foo"),
                "/Users/orben/Projects/foo",
            )


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(cc.tempfile.mkdtemp(prefix="cc-history-test-"))
        self.db = self.tmp / "index.db"
        self.conn = cc.init_db(self.db)

    def tearDown(self):
        self.conn.close()
        cc.__dict__  # keep ref
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_fixture(self):
        n = cc.index_file(self.conn, FIXTURE, "/tmp/cc-history-fixture", 0, False)
        self.assertGreater(n, 0)

        kinds = dict(self.conn.execute(
            "SELECT kind, COUNT(*) FROM entries GROUP BY kind").fetchall())
        self.assertIn("text", kinds)
        self.assertIn("thinking", kinds)
        self.assertIn("tool_use", kinds)
        self.assertIn("tool_result", kinds)

        # image must NOT have produced an entry
        self.assertNotIn("image", kinds)

    def test_incremental_no_dupes(self):
        cc.index_file(self.conn, FIXTURE, "/tmp/p", 0, False)
        first = self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        # re-index from recorded offset = end of file -> 0 new rows
        row = self.conn.execute("SELECT last_offset FROM files WHERE path=?",
                                (str(FIXTURE),)).fetchone()
        n = cc.index_file(self.conn, FIXTURE, "/tmp/p", row["last_offset"], False)
        self.assertEqual(n, 0)
        second = self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        self.assertEqual(first, second)

    def test_fts_search_roundtrip(self):
        cc.index_file(self.conn, FIXTURE, "/tmp/p", 0, False)
        ro = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        rows = ro.execute(
            "SELECT entries.text FROM entries_fts "
            "JOIN entries ON entries.id = entries_fts.rowid "
            "WHERE entries_fts MATCH ?", ('"auth flow"',)).fetchall()
        self.assertTrue(any("auth flow" in r["text"] for r in rows))
        ro.close()

    def test_hebrew_search(self):
        cc.index_file(self.conn, FIXTURE, "/tmp/p", 0, False)
        ro = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        rows = ro.execute(
            "SELECT entries.text FROM entries_fts "
            "JOIN entries ON entries.id = entries_fts.rowid "
            "WHERE entries_fts MATCH ?", ("שלום",)).fetchall()
        self.assertTrue(len(rows) >= 1)
        ro.close()

    def test_crash_midfile_rolls_back(self):
        """Sanity test #10: a failure mid-file must roll back every row from
        that file and leave last_offset untouched, so a re-run retries cleanly
        without doubling entry counts."""
        # sqlite3.Connection.execute is read-only, so wrap it in a proxy that
        # raises partway through the inserts to simulate a crash mid-file.
        class _CrashingConn:
            def __init__(self, real, fail_on_nth_insert):
                self._real = real
                self._n = 0
                self._fail = fail_on_nth_insert

            def execute(self, sql, *a, **k):
                if sql.startswith("INSERT INTO entries "):
                    self._n += 1
                    if self._n == self._fail:
                        raise RuntimeError("simulated crash mid-file")
                return self._real.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._real, name)

        proxy = _CrashingConn(self.conn, fail_on_nth_insert=3)
        with self.assertRaises(RuntimeError):
            cc.index_file(proxy, FIXTURE, "/tmp/p", 0, False)

        # Rollback: no entries, no files row -> next run starts from offset 0.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

        # Clean re-run produces the full set exactly once (no doubling).
        n1 = cc.index_file(self.conn, FIXTURE, "/tmp/p", 0, False)
        total = self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        self.assertEqual(n1, total)
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
