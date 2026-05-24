"""Tests for the long-session DB layer (sessions table CRUD)."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Allow importing from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class SessionsDBTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._patcher = patch("db._db_path", self._tmp.name)
        self._patcher.start()

        import db
        self.db = db
        self.db.init_sessions_table()

    def tearDown(self) -> None:
        self._patcher.stop()
        os.unlink(self._tmp.name)

    def test_create_and_get(self) -> None:
        sid = self.db.session_create("/tmp/test.wav")
        self.assertIsInstance(sid, int)
        row = self.db.session_get(sid)
        self.assertEqual(row["audio_path"], "/tmp/test.wav")
        self.assertEqual(row["status"], "recording")
        self.assertIsNone(row["text"])
        self.assertEqual(row["chunks_done"], 0)

    def test_finalize_moves_to_transcribing(self) -> None:
        sid = self.db.session_create("/tmp/a.wav")
        self.db.session_finalize(sid, duration_sec=125.5)
        row = self.db.session_get(sid)
        self.assertEqual(row["status"], "transcribing")
        self.assertAlmostEqual(row["duration_sec"], 125.5)
        self.assertIsNotNone(row["ended_at"])

    def test_progress_and_text(self) -> None:
        sid = self.db.session_create("/tmp/b.wav")
        self.db.session_finalize(sid, 60.0)
        self.db.session_set_progress(sid, done=3, total=10)
        row = self.db.session_get(sid)
        self.assertEqual(row["chunks_done"], 3)
        self.assertEqual(row["chunk_count"], 10)

        self.db.session_set_text(sid, "hello world")
        row = self.db.session_get(sid)
        self.assertEqual(row["text"], "hello world")
        self.assertEqual(row["status"], "done")

    def test_set_status_with_error(self) -> None:
        sid = self.db.session_create("/tmp/c.wav")
        self.db.session_set_status(sid, "error", error="network timeout")
        row = self.db.session_get(sid)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], "network timeout")

    def test_list_orders_newest_first(self) -> None:
        s1 = self.db.session_create("/tmp/1.wav")
        s2 = self.db.session_create("/tmp/2.wav")
        s3 = self.db.session_create("/tmp/3.wav")
        rows = self.db.session_list()
        self.assertEqual([r["id"] for r in rows], [s3, s2, s1])

    def test_search_matches_text(self) -> None:
        sid = self.db.session_create("/tmp/d.wav")
        self.db.session_set_text(sid, "meeting about project alpha and beta")
        hits = self.db.session_search("alpha")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], sid)

        misses = self.db.session_search("nothing-here")
        self.assertEqual(misses, [])

    def test_delete(self) -> None:
        sid = self.db.session_create("/tmp/e.wav")
        self.db.session_delete(sid)
        self.assertIsNone(self.db.session_get(sid))

    def test_find_stale_returns_recording_rows(self) -> None:
        s1 = self.db.session_create("/tmp/stale1.wav")
        s2 = self.db.session_create("/tmp/stale2.wav")
        # s3 finalized — not stale
        s3 = self.db.session_create("/tmp/ok.wav")
        self.db.session_finalize(s3, 10.0)

        stale = self.db.sessions_find_stale()
        stale_ids = {r["id"] for r in stale}
        self.assertEqual(stale_ids, {s1, s2})
        self.assertNotIn(s3, stale_ids)


if __name__ == "__main__":
    unittest.main()
