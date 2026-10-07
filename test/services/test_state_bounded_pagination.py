import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models import const
from app.services.state import INDEX_KEY, MIGRATED_KEY, RedisState


class _CommandTrackingRedisProxy:
    """Proxy around redis client that tracks command call counts."""

    def __init__(self, target):
        self._target = target
        self.commands = []

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if callable(attr):

            def wrapper(*args, **kwargs):
                self.commands.append(name)
                return attr(*args, **kwargs)

            return wrapper
        return attr


class TestStateBoundedPagination(unittest.TestCase):
    def setUp(self):
        self.host = os.getenv("MPT_TEST_REDIS_HOST", "127.0.0.1")
        self.port = int(os.getenv("MPT_TEST_REDIS_PORT", "6389"))
        self.db = int(os.getenv("MPT_TEST_REDIS_DB", "15"))
        self.prefix = f"t07-test-{uuid.uuid4()}"

    def _get_state(self):
        return RedisState(host=self.host, port=self.port, db=self.db)

    def _make_key(self, name):
        return f"{self.prefix}-{name}"

    def tearDown(self):
        # Clean up any test keys created during tests
        try:
            state = self._get_state()
            client = state._redis
            keys = client.keys(f"{self.prefix}*")
            if keys:
                client.delete(*keys)
        except Exception:
            pass

    @unittest.skipUnless(
        os.getenv("MPT_TEST_REDIS_HOST") or os.getenv("MPT_RUN_INTEGRATION_TESTS") == "1",
        "Redis host not configured",
    )
    def test_bounded_pagination_operation_count_on_large_history(self):
        """
        Acceptance criterion 1:
        Fetching a 10-item page from 1,000 tasks and 1,000 unrelated keys must NOT
        perform a full database scan or scan all Redis keys.
        """
        state = self._get_state()
        client = state._redis

        # Populate 1,000 valid tasks
        task_ids = [self._make_key(f"task-{i:04d}") for i in range(1000)]
        for task_id in task_ids:
            state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100)

        # Populate 1,000 unrelated Redis keys (hashes, lists, strings)
        unrelated_keys = []
        for i in range(1000):
            unrelated_hash = self._make_key(f"unrelated-hash-{i:04d}")
            unrelated_list = self._make_key(f"unrelated-list-{i:04d}")
            client.hset(unrelated_hash, mapping={"secret": "value", "other": "123"})
            client.rpush(unrelated_list, "item1", "item2")
            unrelated_keys.extend([unrelated_hash, unrelated_list])

        try:
            # Ensure index migration completes first
            state.get_all_tasks(page=1, page_size=10)

            # Wrap client with command tracker
            tracker = _CommandTrackingRedisProxy(client)
            state._redis = tracker
            tracker.commands.clear()

            # Retrieve page 1 of size 10
            page_tasks, total = state.get_all_tasks(page=1, page_size=10)

            # Assert scan command was NOT executed
            scan_calls = [c for c in tracker.commands if c == "scan"]
            self.assertEqual(
                len(scan_calls),
                0,
                "Normal page retrieval must not trigger Redis SCAN commands!",
            )

            # Total returned count and page size must be correct
            self.assertEqual(len(page_tasks), 10)
            self.assertGreaterEqual(total, 1000)

            # Ordering must match sorted task IDs
            expected_page = sorted(task_ids)[:10]
            returned_ids = [t["task_id"] for t in page_tasks]
            self.assertEqual(returned_ids, expected_page)
        finally:
            client.delete(*task_ids, *unrelated_keys)

    @unittest.skipUnless(
        os.getenv("MPT_TEST_REDIS_HOST") or os.getenv("MPT_RUN_INTEGRATION_TESTS") == "1",
        "Redis host not configured",
    )
    def test_ordering_and_totals_across_pages(self):
        """
        Acceptance criterion 2:
        Page slice semantics, lexicographical ordering, and totals remain correct.
        """
        state = self._get_state()
        client = state._redis

        task_ids = [self._make_key(f"order-{i:02d}") for i in range(25)]
        for task_id in task_ids:
            state.update_task(task_id, state=const.TASK_STATE_COMPLETE)

        try:
            all_returned = []
            page_size = 7
            for page in range(1, 5):
                tasks, total = state.get_all_tasks(page=page, page_size=page_size)
                self.assertGreaterEqual(total, 25)
                all_returned.extend(tasks)

            returned_ids = [t["task_id"] for t in all_returned if t["task_id"].startswith(self.prefix)]
            expected_sorted = sorted(task_ids)
            self.assertEqual(returned_ids[:25], expected_sorted)
        finally:
            client.delete(*task_ids)

    @unittest.skipUnless(
        os.getenv("MPT_TEST_REDIS_HOST") or os.getenv("MPT_RUN_INTEGRATION_TESTS") == "1",
        "Redis host not configured",
    )
    def test_namespace_filtering_and_malformed_records(self):
        """
        Acceptance criterion 2:
        Queue lists and unrelated hashes are filtered out and ignored.
        """
        state = self._get_state()
        client = state._redis

        valid_id = self._make_key("valid-task")
        unrelated_hash = self._make_key("unrelated-hash")
        mismatched_id_hash = self._make_key("mismatched-id")
        queue_key = self._make_key("queue")

        try:
            state.update_task(valid_id, state=const.TASK_STATE_COMPLETE)
            client.hset(unrelated_hash, mapping={"foo": "bar"})
            client.hset(mismatched_id_hash, mapping={"task_id": "different-id"})
            client.rpush(queue_key, "item1", "item2")

            tasks, _ = state.get_all_tasks(page=1, page_size=100)
            returned_ids = [t["task_id"] for t in tasks if "task_id" in t]

            self.assertIn(valid_id, returned_ids)
            self.assertNotIn(unrelated_hash, returned_ids)
            self.assertNotIn(mismatched_id_hash, returned_ids)
            self.assertNotIn(queue_key, returned_ids)
        finally:
            client.delete(valid_id, unrelated_hash, mismatched_id_hash, queue_key)

    @unittest.skipUnless(
        os.getenv("MPT_TEST_REDIS_HOST") or os.getenv("MPT_RUN_INTEGRATION_TESTS") == "1",
        "Redis host not configured",
    )
    def test_deletion_and_expiry_lazy_cleanup(self):
        """
        Acceptance criterion 2:
        Deletion removes task from index; external deletion / expiry is lazily cleaned up.
        """
        state = self._get_state()
        client = state._redis

        t1 = self._make_key("expire-1")
        t2 = self._make_key("expire-2")
        t3 = self._make_key("expire-3")

        try:
            state.update_task(t1)
            state.update_task(t2)
            state.update_task(t3)

            # Explicit delete
            state.delete_task(t2)

            # External delete / simulate TTL expiry
            client.delete(t3)

            # get_all_tasks should detect t3 is missing, lazily remove it from index
            tasks, _ = state.get_all_tasks(page=1, page_size=10)
            returned_ids = [t["task_id"] for t in tasks if t["task_id"].startswith(self.prefix)]

            self.assertEqual(returned_ids, [t1])
        finally:
            client.delete(t1, t2, t3)

    @unittest.skipUnless(
        os.getenv("MPT_TEST_REDIS_HOST") or os.getenv("MPT_RUN_INTEGRATION_TESTS") == "1",
        "Redis host not configured",
    )
    def test_legacy_data_discoverability_and_idempotent_migration(self):
        """
        Acceptance criterion 3:
        Legacy unindexed tasks are discovered by migration, which is idempotent/resumable.
        """
        state = self._get_state()
        client = state._redis

        legacy_1 = self._make_key("legacy-1")
        legacy_2 = self._make_key("legacy-2")

        try:
            # Create legacy task hashes directly in Redis without update_task (unindexed)
            client.hset(
                legacy_1,
                mapping={
                    "task_id": legacy_1,
                    "state": "1",
                    "progress": "50",
                },
            )
            client.hset(
                legacy_2,
                mapping={
                    "task_id": legacy_2,
                    "state": "2",
                    "progress": "100",
                },
            )

            # Clear migration flag and index to simulate legacy database startup
            client.delete(MIGRATED_KEY, INDEX_KEY)
            state._migrated = False

            # Run migration idempotently
            state.migrate_index()

            tasks, _ = state.get_all_tasks(page=1, page_size=100)
            returned_ids = [t["task_id"] for t in tasks if t["task_id"].startswith(self.prefix)]

            self.assertIn(legacy_1, returned_ids)
            self.assertIn(legacy_2, returned_ids)

            # Run migration again (idempotent test)
            state.migrate_index()
            tasks_after, _ = state.get_all_tasks(page=1, page_size=100)
            returned_ids_after = [t["task_id"] for t in tasks_after if t["task_id"].startswith(self.prefix)]

            self.assertEqual(sorted(returned_ids), sorted(returned_ids_after))
        finally:
            client.delete(legacy_1, legacy_2)


if __name__ == "__main__":
    unittest.main()
