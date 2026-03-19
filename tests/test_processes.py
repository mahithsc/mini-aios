import tempfile
import time
import unittest

from aios_core.tools.processes import (
    ProcessManager,
    _process_manager,
    process_kill,
    process_poll,
    process_send,
    process_spawn,
)


class ProcessManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = ProcessManager()

    def tearDown(self) -> None:
        self.manager.close_all()
        self.tempdir.cleanup()

    def _spawn_session(self) -> dict[str, object]:
        session = self.manager.spawn(cwd=self.tempdir.name)
        self.assertNotIn("error", session)
        self.assertEqual(session["status"], "idle")
        return session

    def _poll_until_command_finishes(
        self,
        process_id: str,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.time() + timeout
        cursor = 0
        last_poll = None
        while time.time() < deadline:
            last_poll = self.manager.poll(process_id, cursor=cursor)
            cursor = last_poll["next_cursor"]
            command = last_poll.get("command")
            if command and command.get("status") == "completed":
                return last_poll
            time.sleep(0.05)
        self.fail(f"command did not complete in {timeout}s: {last_poll}")

    def _poll_until_not_running(
        self,
        process_id: str,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.time() + timeout
        cursor = 0
        last_poll = None
        while time.time() < deadline:
            last_poll = self.manager.poll(process_id, cursor=cursor)
            cursor = last_poll["next_cursor"]
            if last_poll["status"] != "running":
                return last_poll
            time.sleep(0.05)
        self.fail(f"process stayed running for {timeout}s: {last_poll}")

    def test_spawn_creates_live_shell_session(self) -> None:
        session = self._spawn_session()
        listing = self.manager.list()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["process_id"], session["process_id"])
        self.assertTrue(listing[0]["shell_alive"])

    def test_send_command_completes_and_returns_output(self) -> None:
        session = self._spawn_session()
        process_id = session["process_id"]

        result = self.manager.send(process_id, command="pwd")
        self.assertNotIn("error", result)

        poll = self._poll_until_command_finishes(process_id)
        self.assertEqual(poll["command"]["exit_code"], 0)
        self.assertIn(self.tempdir.name, poll["output"])

    def test_poll_returns_incremental_output_from_cursor(self) -> None:
        session = self._spawn_session()
        process_id = session["process_id"]

        send_result = self.manager.send(process_id, command="printf 'alpha\\n'")
        self.assertNotIn("error", send_result)

        first = self._poll_until_command_finishes(process_id)
        second = self.manager.poll(process_id, cursor=first["next_cursor"])

        self.assertIn("alpha", first["output"])
        self.assertEqual(second["output"], "")
        self.assertEqual(second["next_cursor"], first["next_cursor"])

    def test_rejects_second_wrapped_command_while_one_is_active(self) -> None:
        session = self._spawn_session()
        process_id = session["process_id"]

        first = self.manager.send(process_id, command="sleep 1")
        self.assertNotIn("error", first)

        second = self.manager.send(process_id, command="pwd")
        self.assertIn("error", second)

        self._poll_until_command_finishes(process_id, timeout=3.0)

    def test_kill_sigint_interrupts_foreground_command(self) -> None:
        session = self._spawn_session()
        process_id = session["process_id"]

        send_result = self.manager.send(process_id, command="sleep 10")
        self.assertNotIn("error", send_result)

        time.sleep(0.2)
        kill_result = self.manager.kill(process_id, signal_name="SIGINT")
        self.assertNotIn("error", kill_result)

        poll = self._poll_until_not_running(process_id, timeout=3.0)
        self.assertIn(poll["status"], {"idle", "exited"})

    def test_invalid_process_id_returns_error(self) -> None:
        result = self.manager.poll("missing-process")
        self.assertEqual(result["error"], "unknown process_id: missing-process")

    def test_exit_command_transitions_session_to_exited(self) -> None:
        session = self._spawn_session()
        process_id = session["process_id"]

        send_result = self.manager.send(process_id, command="exit 7")
        self.assertNotIn("error", send_result)

        poll = self._poll_until_not_running(process_id, timeout=3.0)
        self.assertEqual(poll["status"], "exited")
        self.assertEqual(poll["command"]["exit_code"], 7)


class ProcessToolWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        _process_manager.close_all()

    def tearDown(self) -> None:
        _process_manager.close_all()
        self.tempdir.cleanup()

    def test_explicit_tool_wrappers_run_round_trip(self) -> None:
        session = process_spawn(cwd=self.tempdir.name)
        self.assertNotIn("error", session)

        send_result = process_send(session["process_id"], command="pwd")
        self.assertNotIn("error", send_result)

        cursor = 0
        deadline = time.time() + 5.0
        last_poll = None
        while time.time() < deadline:
            last_poll = process_poll(session["process_id"], cursor=cursor)
            cursor = last_poll["next_cursor"]
            command = last_poll.get("command")
            if command and command.get("status") == "completed":
                break
            time.sleep(0.05)

        self.assertIsNotNone(last_poll)
        self.assertEqual(last_poll["command"]["exit_code"], 0)
        self.assertIn(self.tempdir.name, last_poll["output"])

        kill_result = process_kill(session["process_id"], signal="SIGTERM")
        self.assertNotIn("error", kill_result)


if __name__ == "__main__":
    unittest.main()
