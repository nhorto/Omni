import contextlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import assistant
import browser
import hypr
import mail
import voice_bridge


def plan(mode, *actions, reply=""):
    return {"mode": mode, "reply": reply, "actions": list(actions)}


class OmiCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.patches = [
            patch.object(assistant, "DATA", root / "data"),
            patch.object(assistant, "CONFIG", root / "config"),
            patch.object(assistant, "RUNTIME", root / "runtime"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_denied_move_leaves_file_and_records_cancellation(self):
        source = Path(self.temp.name) / "original.txt"
        target = Path(self.temp.name) / "moved.txt"
        source.write_text("keep me")
        action = {"type": "move_file", "target": str(source), "destination": str(target), "content": "", "argv": []}
        with assistant.database() as db, patch("builtins.input", return_value="n"):
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Move the file", "codex", db, plan=plan("action", action))
            status = db.execute("SELECT status FROM actions").fetchone()[0]
        self.assertEqual(status, "cancelled")
        self.assertEqual(source.read_text(), "keep me")
        self.assertFalse(target.exists())

    def test_actions_stay_silent_questions_speak(self):
        with assistant.database() as db, patch.object(assistant, "speak_text") as speak:
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Open it", "codex", db, plan=plan("action", reply="I cannot open it"))
            speak.assert_not_called()
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("What is Omi?", "codex", db, plan=plan("conversation", reply="Omi is your assistant."))
            speak.assert_called_once_with("Omi is your assistant.", "codex")

    def test_followup_context_is_stored_locally(self):
        with assistant.database() as db, patch.object(assistant, "speak_text"):
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Who are you?", "codex", db, plan=plan("conversation", reply="I'm Omi."))
            self.assertEqual(assistant.recent_conversation(db), [{"request": "Who are you?", "mode": "conversation", "response": "I'm Omi."}])

    def test_memory_index_tracks_edits_and_deletions(self):
        with assistant.database() as db:
            assistant.execute({"type": "remember", "content": "I prefer concise answers"}, db)
            self.assertEqual(assistant.relevant_memories(db, "concise replies"), ["I prefer concise answers"])
            db.execute("UPDATE memory SET content=? WHERE id=1", ("I prefer detailed answers",))
            db.commit()
            self.assertEqual(assistant.relevant_memories(db, "concise replies"), [])
            self.assertEqual(assistant.relevant_memories(db, "detailed replies"), ["I prefer detailed answers"])
            db.execute("DELETE FROM memory WHERE id=1")
            db.commit()
            self.assertEqual(assistant.relevant_memories(db, "detailed replies"), [])

    def test_local_command_is_logged_without_agent_or_conversation_context(self):
        with assistant.database() as db, patch.object(assistant, "plan_with_agent") as agent:
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Remember that I prefer short replies", "codex", db)
            agent.assert_not_called()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_calls").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT mode,status FROM task_runs").fetchone(), ("action", "complete"))
            self.assertEqual(db.execute("SELECT content FROM memory").fetchone()[0], "I prefer short replies")

    def test_conversation_threads_are_separate(self):
        with assistant.database() as db, patch.object(assistant, "speak_text"):
            first = assistant.new_session(db)
            second = assistant.new_session(db)
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("First topic", "codex", db, plan=plan("conversation", reply="First answer"), session_id=first)
                assistant.run_request("Second topic", "codex", db, plan=plan("conversation", reply="Second answer"), session_id=second)
            self.assertEqual(len(assistant.recent_conversation(db, first)), 1)
            self.assertEqual(assistant.recent_conversation(db, second)[0]["request"], "Second topic")

    def test_auto_memory_is_verbatim_and_sensitive_facts_are_rejected(self):
        with assistant.database() as db, patch.object(assistant, "speak_text"):
            request = "I prefer morning meetings. My password is swordfish."
            proposal = {**plan("conversation", reply="Noted"), "memories": ["I prefer morning meetings", "My password is swordfish", "I live in Paris"]}
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request(request, "codex", db, plan=proposal)
            self.assertEqual(db.execute("SELECT content FROM memory").fetchall(), [("I prefer morning meetings",)])

    def test_knowledge_notes_are_retrieved_and_removed_from_index(self):
        with assistant.database() as db:
            timestamp = assistant.now()
            db.execute("INSERT INTO knowledge(title,body,source,created_at,updated_at) VALUES(?,?,?,?,?)", ("Orion project", "The Orion project serves the warehouse team.", "user app", timestamp, timestamp))
            db.commit()
            self.assertIn("warehouse team", assistant.relevant_knowledge(db, "Tell me about Orion")[0])
            db.execute("DELETE FROM knowledge")
            db.commit()
            self.assertEqual(assistant.relevant_knowledge(db, "Tell me about Orion"), [])

    def test_workspace_request_uses_local_route_and_validates_target(self):
        with assistant.database() as db, patch.object(hypr, "switch_workspace", return_value="Switched to workspace 3") as switch, patch.object(assistant, "plan_with_agent") as agent:
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Go to workspace 3", "codex", db)
            switch.assert_called_once_with("3")
            agent.assert_not_called()
            self.assertEqual(db.execute("SELECT status FROM actions").fetchone()[0], "executed")
        with self.assertRaises(ValueError):
            hypr.workspace_number("11")

    def test_long_knowledge_note_returns_matching_passage(self):
        with assistant.database() as db:
            timestamp = assistant.now()
            body = 'Unrelated introduction. ' * 200 + 'Orion delivery is scheduled for the western warehouse.'
            db.execute("INSERT INTO knowledge(title,body,source,created_at,updated_at) VALUES(?,?,?,?,?)", ('Project notes', body, 'synthetic', timestamp, timestamp))
            db.commit()
            self.assertIn('western warehouse', assistant.relevant_knowledge(db, 'Orion delivery')[0])

    def test_relative_reminder_runs_locally_and_stays_out_of_conversation(self):
        with assistant.database() as db, patch.object(assistant, 'plan_with_agent') as agent:
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request('Remind me in 15 minutes to take a break', 'codex', db)
            agent.assert_not_called()
            self.assertEqual(db.execute('SELECT content,status FROM reminders').fetchone(), ('take a break', 'scheduled'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0], 0)

    def test_training_export_preserves_observed_window_context(self):
        before = [{'address': '0x1234', 'workspace': 1, 'title': 'Synthetic terminal'}]
        after = [{'address': '0x1234', 'workspace': 3, 'title': 'Synthetic terminal'}]
        with assistant.database() as db, patch.object(hypr, 'available_windows', side_effect=[before, after]), patch.object(hypr, 'switch_workspace', return_value='Switched'):
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request('Go to workspace 3', 'codex', db)
            assistant.set_task_feedback(db, 1, 'correct')
            output = Path(self.temp.name) / 'training.jsonl'
            assistant.export_training(db, output)
            payload = __import__('json').loads(output.read_text())
            self.assertEqual(payload['schema_version'], 1)
            self.assertEqual(payload['desktop_before'], before)
            self.assertEqual(payload['desktop_after'], after)

    def test_window_placement_does_not_guess_after_ambiguous_launch(self):
        terminal = {"type": "terminal_run", "target": "", "destination": "", "content": "", "argv": ["printf", "hello"]}
        placement = {"type": "window_place", "target": "last_opened", "destination": "right", "content": "", "argv": []}
        with assistant.database() as db, patch.object(assistant, "execute", return_value="Terminal launch requested"), patch.object(hypr, "available_windows", return_value=[]), patch.object(hypr, "detect_new_window", return_value=None), patch.object(hypr, "place_window") as place:
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Open a terminal on the right", "codex", db, plan=plan("action", terminal, placement), approve=lambda _: True)
            place.assert_not_called()
            self.assertEqual([row[0] for row in db.execute("SELECT status FROM actions ORDER BY id")], ["pending", "failed"])
            self.assertEqual(db.execute("SELECT status FROM task_runs").fetchone()[0], "partial")

    def test_declined_terminal_stops_following_desktop_actions(self):
        terminal = {"type": "terminal_run", "target": "", "destination": "", "content": "", "argv": ["printf", "hello"]}
        placement = {"type": "window_place", "target": "last_opened", "destination": "right", "content": "", "argv": []}
        with assistant.database() as db, patch.object(assistant, "execute") as execute, patch.object(hypr, "place_window") as place:
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Open a terminal on the right", "codex", db, plan=plan("action", terminal, placement), approve=lambda _: False)
            execute.assert_not_called()
            place.assert_not_called()
            self.assertEqual([row[0] for row in db.execute("SELECT status FROM actions ORDER BY id")], ["cancelled"])
            self.assertEqual(db.execute("SELECT status FROM task_runs").fetchone()[0], "cancelled")

    def test_failed_command_stops_plan_and_is_not_training_eligible(self):
        command = {"type": "run_command", "target": "", "destination": "", "content": "", "argv": ["python3", "-c", "import sys; sys.exit(7)"]}
        with assistant.database() as db, contextlib.redirect_stdout(io.StringIO()):
            assistant.run_request("Run failing command", "codex", db, plan=plan("action", command), approve=lambda _: True)
            self.assertEqual(db.execute("SELECT status FROM actions").fetchone()[0], "failed")
            self.assertEqual(db.execute("SELECT status FROM task_runs").fetchone()[0], "partial")
            with self.assertRaisesRegex(ValueError, "Only completed"):
                assistant.set_task_feedback(db, 1, "correct")

    def test_terminal_result_reconciles_after_worker_finishes(self):
        from terminal_job import run
        import json
        for code, expected in ((0, "complete"), (6, "partial")):
            with self.subTest(code=code), assistant.database() as db:
                run_id = db.execute("INSERT INTO task_runs(source,request,agent,model,mode,plan,status,created_at) VALUES(?,?,?,?,?,?,?,?)", ("test", "terminal", "codex", "test", "action", "{}", "awaiting_commands", assistant.now())).lastrowid
                job_id = f"{run_id:032x}"
                action = {"type": "terminal_run", "argv": ["python3", "-c", f"import sys; sys.exit({code})"], "_job_id": job_id}
                assistant.record_action(db, "codex", "terminal", action, "pending", "started", run_id)
                jobs = assistant.DATA / "terminal-jobs"
                assistant.private_dir(jobs)
                with contextlib.redirect_stdout(io.StringIO()):
                    run(jobs / f"{job_id}.json", action["argv"])
                self.assertEqual(assistant.reconcile_terminal_jobs(db), 1)
                self.assertEqual(db.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()[0], expected)
                self.assertFalse((jobs / f"{job_id}.json").exists())

    def test_concurrent_request_cannot_execute_while_approval_is_pending(self):
        command = {"type": "run_command", "target": "", "destination": "", "content": "", "argv": ["printf", "test"]}
        with assistant.database() as db, patch.object(assistant, "execute") as execute:
            def approval(_description):
                with self.assertRaisesRegex(RuntimeError, "already handling"):
                    assistant.run_request("Second request", "codex", db, plan=plan("action", command))
                return False
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("First request", "codex", db, plan=plan("action", command), approve=approval)
            execute.assert_not_called()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 1)

    def test_malformed_action_stops_the_plan_and_records_failure(self):
        with assistant.database() as db, patch.object(assistant, "execute") as execute:
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Malformed plan", "codex", db, plan=plan("action", "invalid"))
            execute.assert_not_called()
            self.assertEqual(db.execute("SELECT status FROM actions").fetchone()[0], "failed")

    def test_training_export_requires_correct_feedback(self):
        with assistant.database() as db, patch.object(assistant, "speak_text"):
            action = {"type": "remember", "target": "", "destination": "", "content": "test fact", "argv": []}
            with contextlib.redirect_stdout(io.StringIO()):
                assistant.run_request("Remember test fact", "codex", db, plan=plan("action", action))
            task_id = db.execute("SELECT id FROM task_runs").fetchone()[0]
            output = Path(self.temp.name) / "training.jsonl"
            self.assertEqual(assistant.export_training(db, output), 0)
            assistant.set_task_feedback(db, task_id, "correct")
            output2 = Path(self.temp.name) / "reviewed.jsonl"
            self.assertEqual(assistant.export_training(db, output2), 1)
            self.assertEqual(__import__("json").loads(output2.read_text())["request"], "Remember test fact")
            assistant.set_task_feedback(db, task_id, "incorrect", "It should have used different wording")
            output3 = Path(self.temp.name) / "excluded.jsonl"
            self.assertEqual(assistant.export_training(db, output3), 0)
            output4 = Path(self.temp.name) / "all.jsonl"
            self.assertEqual(assistant.export_tasks(db, output4), 1)
            self.assertIn("It should have used different wording", output4.read_text())

    def test_elevenlabs_voice_uses_selected_id_and_transient_audio(self):
        class Audio:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b"synthetic-audio"

        assistant.save_settings({"agent": "codex", "speech_provider": "elevenlabs", "elevenlabs_voice_id": "voice123456789", "speech_enabled": True})
        with patch.object(assistant, "elevenlabs_key", return_value="test-key"), patch.object(assistant.urllib.request, "urlopen", return_value=Audio()) as request, patch.object(assistant.subprocess, "run") as player:
            assistant.speak_text("Hello there", "codex")
            self.assertIn("voice123456789", request.call_args.args[0].full_url)
            self.assertIn(b"Hello there", request.call_args.args[0].data)
            self.assertEqual(player.call_args.args[0][0], "mpv")
            self.assertFalse((assistant.RUNTIME / f"speech-{__import__('os').getpid()}.mp3").exists())

    def test_browser_target_must_match_visible_control(self):
        page = '- link "Learn more" [ref=f2e6] [cursor=pointer]:\n- heading "Example" [ref=f2e7]'
        with patch.object(browser, "snapshot", return_value=page):
            self.assertEqual(browser.resolve_target("Learn more rep?"), ("f2e6", "Learn more"))
            with self.assertRaises(ValueError):
                browser.resolve_target("Example")

    def test_failed_transcript_is_preserved(self):
        inbox = Path(self.temp.name) / "inbox"
        inbox.mkdir()
        path = inbox / "utterance.txt"
        path.write_text("A request")
        with patch.object(voice_bridge, "process_request", side_effect=RuntimeError("agent unavailable")), patch.object(voice_bridge, "notify"):
            voice_bridge.handle_transcript(path)
        self.assertFalse(path.exists())
        self.assertEqual((inbox / "utterance.failed").read_text(), "A request")

    def test_continuous_gate_stops_after_speech_and_resets_idle(self):
        loud = struct.pack("<100h", *([10000] * 100))
        quiet = struct.pack("<100h", *([0] * 100))
        gate = voice_bridge.UtteranceGate(0.0)
        self.assertIsNone(gate.observe(loud, 1.0))
        self.assertIsNone(gate.observe(quiet, 2.0))
        self.assertEqual(gate.observe(quiet, 2.5), "stop")
        idle = voice_bridge.UtteranceGate(0.0)
        self.assertEqual(idle.observe(quiet, 60.0), "cancel")

    def test_email_draft_requires_final_approval(self):
        with assistant.database() as db, patch.object(browser, "open_url", return_value="opened"):
            mail.prepare(db, "outlook", "person@example.com", "Hello", "A short note.")
            action = {"type": "email_send", "target": "latest", "destination": "", "content": "", "argv": []}
            with patch("builtins.input", return_value="n"), patch.object(browser, "click") as click:
                with contextlib.redirect_stdout(io.StringIO()):
                    assistant.run_request("Send the draft", "codex", db, plan=plan("action", action))
            click.assert_not_called()
            self.assertEqual(mail.draft(db, "latest")["status"], "prepared")
            self.assertEqual(db.execute("SELECT status FROM actions ORDER BY id DESC LIMIT 1").fetchone()[0], "cancelled")

    def test_email_send_checks_compose_and_marks_submitted(self):
        with assistant.database() as db, patch.object(browser, "open_url", return_value="opened"):
            mail.prepare(db, "outlook", "person@example.com", "Hello", "A short note.")
            snapshot = '- 0: (current) [Outlook](https://outlook.office.com/mail/)\nperson@example.com\nHello\nA short note.'
            with patch.object(browser, "snapshot", return_value=snapshot), patch.object(browser, "resolve_target", return_value=("e10", "Send")), patch.object(browser, "click", return_value="Sent") as click:
                result = mail.send(db, "latest")
            click.assert_called_once_with("Send")
            self.assertIn("submitted", result)
            self.assertEqual(mail.draft(db, "latest")["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
