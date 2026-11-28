"""Transactional state, durable tasks and local adapter contracts."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
import wave

from efir.assistant import Ollama, decision, preview_text, proposals, propose, validate
from efir.__main__ import main
from efir.jobs import ATTEMPTS, LEASE, Queue, run_one
from efir.project import EfirError, Project
from efir.store import MIGRATIONS, StudioProject, migrate, open_project
from efir.tts import Piper


def wav(path, payload=b"\0\0"):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", ""))
        output.writeframes(payload * 100)


class VoiceFixture:
    recipe = {"engine": "test"}

    def __init__(self, hook=lambda: None):
        self.hook = hook
        self.calls = []

    def synthesize(self, text, path, *, cancel):
        self.calls.append(text)
        self.hook()
        if cancel.is_set():
            raise EfirError("Cancelled fixture")
        wav(path, bytes((len(text) % 256, len(self.calls))))


class StudioFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Эфир SQLite ")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.project = StudioProject.create(self.path / "Проект", "Короткий эфир")
        self.first = self.project.add(title="Первый", frames=40, visual=None, speech="Вы в эфире.")
        self.second = self.project.add(title="Второй", frames=55, visual=None, speech="Звук в студии.")
        self.project.save()

    def data(self, *operations):
        return {"base_revision": self.project.revision, "operations": list(operations)}


class StoreTests(StudioFixture):
    def test_open_history_restore_and_optimistic_commit(self):
        stale = open_project(self.project.root)
        self.project.move(self.second.id, 1)
        self.project.save()
        with self.assertRaisesRegex(EfirError, "Проект изменился"):
            stale.save()
        restored = self.project.restore(stale.revision)
        self.assertGreater(restored.revision, self.project.revision)
        self.assertEqual(restored.scenes, stale.scenes)
        self.assertEqual([row["id"] for row in restored.history()], [4, 3, 2, 1])
        with self.assertRaisesRegex(EfirError, "Ревизия"):
            restored.restore(99)
        with self.assertRaisesRegex(EfirError, "существует"):
            StudioProject.create(stale.root, "Дубликат")
        with self.assertRaisesRegex(EfirError, "SQLite"):
            Project.create(stale.root, "Дубликат v0.1")
        with self.assertRaisesRegex(EfirError, "без клипа"):
            restored.update(self.first.id, source_audio=True)
        with self.assertRaisesRegex(EfirError, "Реплика"):
            restored.update(self.first.id, speech=None)

    def test_concurrent_open_copies_have_one_winner(self):
        def save(work):
            work.update(self.first.id, caption="edit")
            try:
                work.save()
            except EfirError as error:
                return str(error)
            return "saved"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, [StudioProject.open(self.project.root) for _ in range(2)]))
        self.assertEqual(results.count("saved"), 1)
        self.assertIn("Проект изменился", " ".join(results))

    def test_migrate_to_new_directory_and_verify_every_referenced_asset(self):
        legacy = Project.create(self.path / "v1", "Наследие", "1:1")
        image = self.path / "кадр.png"
        image.write_bytes(b"image")
        visual = legacy.import_media(image)
        one = legacy.add(title="Первый", visual=visual, frames=21, caption="Кадр", notes="Источник")
        two = legacy.add(title="Второй", visual=visual, frames=25)
        legacy.move(two.id, 1)
        legacy.save()
        manifest = legacy.inside("project.json").read_bytes()
        converted = migrate(legacy.root, self.path / "v2")
        self.assertEqual(converted.aspect, "1:1")
        self.assertEqual([scene.id for scene in converted.scenes], [two.id, one.id])
        self.assertEqual(converted.scenes[1].caption, "Кадр")
        self.assertEqual(converted.asset(visual).read_bytes(), b"image")
        self.assertEqual(legacy.inside("project.json").read_bytes(), manifest)
        self.assertFalse(converted.inside("project.json").exists())
        for target, reason in [(legacy.root / "nested", "вне"), (converted.root, "новый")]:
            with self.assertRaisesRegex(EfirError, reason):
                migrate(legacy.root, target)
        legacy.asset(visual).write_bytes(b"corrupt")
        with self.assertRaisesRegex(EfirError, "изменено"):
            migrate(legacy.root, self.path / "bad")
        self.assertFalse((self.path / "bad").exists())

    def test_portable_preserves_old_voice_and_recovers_copied_lease(self):
        queue = Queue(self.project)
        engine = VoiceFixture()
        first = queue.enqueue("voice", scene_id=self.first.id, recipe=engine.recipe)
        run_one(self.project, tts=engine)
        self.project = StudioProject.open(self.project.root)
        old_voice = self.project.scenes[0].voice
        self.project.update(self.first.id, voice=None)
        self.project.save()
        active = Queue(self.project)
        second = active.enqueue("voice", scene_id=self.second.id, recipe=engine.recipe)
        active.claim(200)
        destination = self.path / "Backup"
        self.assertEqual(self.project.portable(destination), destination.resolve())
        copied = StudioProject.open(destination)
        self.assertEqual(copied.scenes, self.project.scenes)
        self.assertTrue(copied.asset(old_voice).stat().st_size > 40)
        self.assertEqual(run_one(copied, tts=engine), second)
        self.assertEqual({item["id"]: item["state"] for item in Queue(copied).jobs()},
                         {first: "succeeded", second: "succeeded"})
        with self.assertRaisesRegex(EfirError, "вне"):
            copied.portable(destination / "nested")
        with self.assertRaisesRegex(EfirError, "новый"):
            self.project.portable(destination)

    def test_upgrade_and_reject_future_or_inaccessible_schema(self):
        target = self.path / "schema"
        target.mkdir()
        with closing(sqlite3.connect(target / "project.sqlite")) as db, db:
            for statement in MIGRATIONS[0]:
                db.execute(statement)
            db.execute("PRAGMA user_version = 1")
            db.execute("INSERT INTO revision(id, document, reason) VALUES (1, ?, 'fixture')",
                       (json.dumps(self.project.document()),))
            db.execute("INSERT INTO project VALUES ('legacy', 1)")
        upgraded = StudioProject.open(target)
        self.assertEqual(upgraded.scenes, self.project.scenes)
        self.assertEqual(Queue(upgraded).jobs(), [])
        with upgraded.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
            db.execute("PRAGMA user_version = 999")
        with self.assertRaisesRegex(EfirError, "не поддерживается"):
            StudioProject.open(target)
        with self.assertRaisesRegex(EfirError, "Ошибка базы"):
            StudioProject.open(self.path / "bad")


class QueueTests(StudioFixture):
    def test_recovers_an_abruptly_interrupted_process(self):
        now = 1000
        queue = Queue(self.project, clock=lambda: now)
        job = queue.enqueue("voice", scene_id=self.first.id, recipe=VoiceFixture.recipe)
        process = subprocess.run([sys.executable, "-c", """
import os
import sys
from efir.jobs import Queue
from efir.store import StudioProject
Queue(StudioProject.open(sys.argv[1]), clock=lambda: 1000).claim(90)
os._exit(7)
""", str(self.project.root)], timeout=15)
        self.assertEqual(process.returncode, 7)
        self.assertEqual(queue.jobs()[0]["state"], "running")
        now += LEASE + 1
        self.assertEqual(run_one(self.project, tts=VoiceFixture(), clock=lambda: now), job)
        self.assertEqual(queue.jobs()[0]["attempt"], 2)

    def test_corrupted_cached_voice_is_rebuilt(self):
        queue = Queue(self.project)
        engine = VoiceFixture()
        queue.enqueue("voice", scene_id=self.first.id, recipe=engine.recipe)
        run_one(self.project, tts=engine)
        project = StudioProject.open(self.project.root)
        with self.assertRaisesRegex(EfirError, "изменился"):
            queue.enqueue("export")
        asset = project.scenes[0].voice
        project.asset(asset).write_bytes(b"broken")
        Queue(project).enqueue("voice", scene_id=self.first.id, recipe=engine.recipe)
        run_one(project, tts=engine)
        updated = StudioProject.open(project.root)
        self.assertNotEqual(asset, updated.scenes[0].voice)
        self.assertEqual(len(engine.calls), 2)

    def test_selective_generation_caption_independence_and_explicit_regeneration(self):
        engine = VoiceFixture()
        queue = Queue(self.project)
        one = queue.enqueue("voice", scene_id=self.first.id, recipe=engine.recipe)
        self.assertEqual(one, queue.enqueue("voice", scene_id=self.first.id, recipe=engine.recipe))
        self.assertEqual(run_one(self.project, tts=engine), one)
        self.project = StudioProject.open(self.project.root)
        queue = Queue(self.project)
        two = queue.enqueue("voice", scene_id=self.second.id, recipe=engine.recipe)
        self.assertEqual(run_one(self.project, tts=engine), two)
        self.project = StudioProject.open(self.project.root)
        voices = [scene.voice for scene in self.project.scenes]
        self.project.update(self.first.id, caption="Только субтитр")
        self.project.save()
        queue = Queue(self.project)
        cached = queue.enqueue("voice", scene_id=self.first.id, recipe=engine.recipe)
        run_one(self.project, tts=engine)
        self.assertEqual(queue.enqueue("voice", scene_id=self.first.id, recipe=engine.recipe), cached)
        self.assertEqual(len(engine.calls), 2)
        self.project.update(self.second.id, speech="Новая реплика длиннее.")
        self.project.save()
        queue.enqueue("voice", scene_id=self.second.id, recipe=engine.recipe)
        run_one(self.project, tts=engine)
        updated = StudioProject.open(self.project.root)
        self.assertEqual(updated.scenes[0].voice, voices[0])
        self.assertNotEqual(updated.scenes[1].voice, voices[1])
        self.assertNotEqual(Queue(updated).enqueue("voice", scene_id=self.second.id, recipe=engine.recipe,
                                                  regenerate=True), two)
        run_one(updated, tts=engine)
        self.assertEqual(len(engine.calls), 4)
        self.assertIsNone(run_one(updated, tts=engine))

    def test_late_voice_result_is_kept_without_overwriting_current_scene(self):
        queue = Queue(self.project)
        queue.enqueue("voice", scene_id=self.first.id, recipe=VoiceFixture.recipe)

        def edit():
            edit_project = StudioProject.open(self.project.root)
            edit_project.update(self.first.id, speech="Другой текст.")
            edit_project.save()

        run_one(self.project, tts=VoiceFixture(edit))
        current = StudioProject.open(self.project.root)
        result = json.loads(queue.jobs()[0]["result"])
        self.assertFalse(result["selected"])
        self.assertIsNone(current.scenes[0].voice)
        self.assertTrue(current.asset(result["asset"]).is_file())

    def test_missing_engine_retry_cancel_and_recovery_limit(self):
        now = 1000
        queue = Queue(self.project, clock=lambda: now)
        with self.assertRaisesRegex(EfirError, "локальный голос"):
            queue.enqueue("voice", scene_id=self.first.id)
        with self.assertRaisesRegex(EfirError, "тип"):
            queue.enqueue("unknown")
        with self.assertRaisesRegex(EfirError, "Повтор"):
            queue.retry("unknown")
        job = queue.enqueue("voice", scene_id=self.first.id, recipe=VoiceFixture.recipe)
        run_one(self.project, clock=lambda: now)
        self.assertIn("исходными параметрами", queue.jobs()[0]["error"])
        queue.retry(job)
        for _ in range(ATTEMPTS - 1):
            claimed = queue.claim(120)
            self.assertEqual(claimed["id"], job)
            self.assertIsNone(queue.claim(120))
            now += LEASE + 1
        self.assertIsNone(queue.claim(120))
        self.assertEqual(queue.jobs()[0]["state"], "failed")
        with self.assertRaises(EfirError):
            queue.retry(job)
        cancelled = queue.enqueue("export")
        queue.cancel(cancelled)
        queue.cancel(cancelled)
        self.assertEqual(queue.jobs()[0]["state"], "cancelled")
        next_job = queue.enqueue("export")
        queue.claim(30)
        queue.cancel(next_job)
        now += LEASE + 1
        self.assertIsNone(queue.claim(30))
        self.assertEqual(queue.jobs()[0]["state"], "cancelled")

    def test_running_cancel_and_job_timeout_do_not_publish(self):
        now = 100
        queue = Queue(self.project, clock=lambda: now)
        job = queue.enqueue("voice", scene_id=self.first.id, recipe=VoiceFixture.recipe)

        def cancel():
            nonlocal now
            queue.cancel(job)
            now += 1

        run_one(self.project, tts=VoiceFixture(cancel), clock=lambda: now)
        self.assertEqual(queue.jobs()[0]["state"], "cancelled")
        queue.enqueue("voice", scene_id=self.first.id, recipe=VoiceFixture.recipe)

        def expire():
            nonlocal now
            now += 2

        run_one(self.project, tts=VoiceFixture(expire), timeout=1, clock=lambda: now)
        self.assertEqual(queue.jobs()[0]["state"], "failed")
        self.assertIn("Истекло", queue.jobs()[0]["error"])
        self.assertIsNone(StudioProject.open(self.project.root).scenes[0].voice)

    def test_expired_worker_cannot_commit_after_leasing_is_lost(self):
        now = 100
        queue = Queue(self.project, clock=lambda: now)
        queue.enqueue("export")
        previous = queue.claim(120)
        now += LEASE + 1
        self.assertFalse(queue.heartbeat(previous))
        current = queue.claim(120)
        with self.project.connect() as db:
            queue.finish(db, previous, error="stale")
        self.assertNotEqual(current["worker"], previous["worker"])
        self.assertEqual(queue.jobs()[0]["state"], "running")


class PlanTests(StudioFixture):
    def test_cli_confirmation_rejection_and_configured_voice(self):
        engine = Mock(model="local", **{"plan.return_value": self.data(
            {"op": "voice", "scene": self.first.id})})
        root = str(self.project.root)

        def cli(command, *arguments):
            with redirect_stdout(StringIO()) as stdout:
                self.assertEqual(main([command, root, *arguments]), 0)
                return stdout.getvalue().strip()

        with patch("efir.assistant.Ollama", return_value=engine), patch(
            "efir.tts.Piper.configured", return_value=VoiceFixture(),
        ):
            declined = cli("plan", "Озвучь первую сцену", "--model", "demo").splitlines()[0]
            cli("reject", declined)
            accepted = cli("plan", "Подготовь запись").splitlines()[0]
            revision, job = cli("apply", accepted).split()
            self.assertEqual(int(revision), self.project.revision)
            self.assertEqual(cli("work", "--piper"), job)
            self.assertNotEqual(cli("voice", self.first.id, "--regenerate"), job)
        legacy = Project.create(self.path / "Legacy", "Проверка")
        with self.assertRaisesRegex(EfirError, "SQLite"):
            from efir.__main__ import require_studio
            require_studio(legacy)

    def mock_proposal(self, *operations):
        engine = Mock(model="local", **{"plan.return_value": self.data(*operations)})
        return propose(self.project, engine, "Сделай план")

    def test_preview_commit_reject_stale_and_repeat(self):
        plan_id = self.mock_proposal({"op": "edit", "scene": self.second.id, "speech": "В эфире."},
                                     {"op": "move", "scene": self.second.id, "position": 1},
                                     {"op": "add", "title": "Третья", "frames": 75})
        baseline = self.project.revision
        self.assertEqual(StudioProject.open(self.project.root).revision, baseline)
        self.assertIn("Второй", preview_text(self.project, json.loads(proposals(self.project)[0]["operations"])[0]))
        changed, jobs = decision(self.project, plan_id, accept=True)
        self.assertEqual((changed, jobs), decision(self.project, plan_id, accept=True))
        self.assertEqual(changed, baseline + 1)
        self.assertFalse(jobs)
        self.assertEqual(StudioProject.open(self.project.root).scenes[0].speech, "В эфире.")
        stale = self.mock_proposal({"op": "add", "title": "Старое", "frames": 75})
        with self.assertRaisesRegex(EfirError, "другой ревизии"):
            decision(self.project, stale, accept=True)
        self.assertEqual(decision(self.project, stale, accept=False), (None, []))
        for proposal, reason in [(stale, "уже закрыт"), ("unknown", "не найден")]:
            with self.assertRaisesRegex(EfirError, reason):
                decision(self.project, proposal, accept=False)

    def test_invalid_plans_and_unconfigured_voice_are_atomic(self):
        invalid = [None, {"base_revision": "2", "operations": []}, self.data(),
                   self.data({"op": "shell"}), self.data({"op": "add", "title": "X"}),
                   self.data({"op": "add", "frames": True, "title": "X"}),
                   self.data({"op": "edit", "scene": "bad", "caption": "X"}),
                   self.data({"op": "voice", "scene": self.first.id}, {"op": "export"})]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(EfirError):
                validate(self.project, payload)
        for operation in ({"op": "edit", "scene": "0" * 12, "caption": "x"}, {"op": "export"}):
            with self.assertRaises(EfirError):
                self.mock_proposal(operation)
        with self.assertRaises(EfirError):
            propose(self.project, None, "  ")
        plan_id = self.mock_proposal({"op": "edit", "scene": self.first.id, "caption": "Эфир"},
                                     {"op": "voice", "scene": self.first.id})
        with self.assertRaises(EfirError):
            decision(self.project, plan_id, accept=True)
        self.assertEqual(StudioProject.open(self.project.root).revision, self.project.revision)
        self.assertEqual(Queue(self.project).jobs(), [])
        revision, jobs = decision(self.project, plan_id, accept=True, recipe=VoiceFixture.recipe)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(Queue(self.project).jobs()[0]["revision"], revision)

    def test_plan_cannot_schedule_speech_after_erasing_it(self):
        with self.assertRaisesRegex(EfirError, "без реплики"):
            self.mock_proposal({"op": "edit", "scene": self.first.id, "speech": ""},
                               {"op": "voice", "scene": self.first.id})
        self.assertEqual(proposals(self.project), [])

    def test_ollama_protocol_uses_schema_and_redacts_media_paths(self):
        model_message = self.data({"op": "add", "title": "Передача", "seconds": 16})
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, data):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode("utf-8"))

            def do_GET(self):
                if self.path == "/api/tags":
                    self.send({"models": [{"name": "qwen3:4b", "size": 400}]})
                else:
                    self.send_error(404)

            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send({"message": {"content": json.dumps(model_message)}})

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            try:
                client = Ollama("qwen3:4b", port=server.server_port, timeout=2)
                self.assertEqual(client.plan(self.project, "Сделай выпуск"),
                                 self.data({"op": "add", "title": "Передача", "frames": 400}))
                with self.assertRaisesRegex(EfirError, "отсутствует"):
                    Ollama("missing", port=server.server_port).plan(self.project, "Текст")
                with self.assertRaisesRegex(EfirError, "HTTP 404"):
                    client.request("/unsupported")
                with patch.object(client, "request", side_effect=[
                    {"models": [{"name": "qwen3:4b", "size": 400}]}, {"message": {"content": "bad json"}}
                ]), self.assertRaisesRegex(EfirError, "машиночитаемый"):
                    client.plan(self.project, "Текст")
            finally:
                server.shutdown()
                worker.join()
        route, payload = requests[0]
        self.assertEqual(route, "/api/chat")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["format"]["properties"]["base_revision"]["const"], self.project.revision)
        self.assertNotIn(str(self.project.root), json.dumps(payload))
        for model in ("https://host", "model:cloud"):
            with self.assertRaises(EfirError):
                Ollama(model)
        with patch("efir.assistant.http.client.HTTPConnection") as connect:
            connect.return_value.request.side_effect = ConnectionRefusedError()
            with self.assertRaisesRegex(EfirError, "недоступен"):
                client.request("/api/tags")
        with patch.object(client, "request", return_value={}), self.assertRaisesRegex(EfirError, "список"):
            client.plan(self.project, "Текст")
        with patch("efir.assistant.http.client.HTTPConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b"x" * 256001
            with self.assertRaisesRegex(EfirError, "велик"):
                client.request("/api/tags")

    def test_ollama_schema_disallows_unknown_ids_and_uses_seconds(self):
        from efir.assistant import from_model, schema

        variants = schema(1, 0)["properties"]["operations"]["items"]["anyOf"]
        self.assertEqual([variant["properties"]["op"]["const"] for variant in variants], ["add"])
        variants = schema(self.project.revision, 2)["properties"]["operations"]["items"]["anyOf"]
        self.assertEqual(variants[1]["properties"]["scene"]["enum"], [1, 2])
        translated = from_model(self.project, self.data({"op": "edit", "scene": 2, "seconds": 12.4}))
        self.assertEqual(translated, self.data({"op": "edit", "scene": self.second.id, "frames": 310}))
        for value in ("10", True, 0.03, float("nan"), 600.01):
            with self.subTest(value=value), self.assertRaisesRegex(EfirError, "числом от"):
                from_model(self.project, self.data({"op": "edit", "scene": 1, "seconds": value}))
        for data in ({"operations": {}}, None):
            with self.assertRaisesRegex(EfirError, "не вернул операции"):
                from_model(self.project, data)
        for operation in (None, {"op": "add", "title": "Кадры", "frames": 310}):
            with self.assertRaisesRegex(EfirError, "числом секунд"):
                from_model(self.project, self.data(operation))
        for scene in ("0" * 12, ["not an identifier"], 3):
            with self.assertRaisesRegex(EfirError, "номер вне списка"):
                from_model(self.project, self.data({"op": "edit", "scene": scene, "seconds": 12}))
        with self.assertRaisesRegex(EfirError, "превышает"):
            from_model(self.project, self.data({"op": "add", "title": "д" * 101, "seconds": 12}))
        with self.assertRaisesRegex(EfirError, "Неверные поля"):
            from_model(self.project, self.data({"op": "add", "title": "Идея", "seconds": 16, "path": "../.."}))

    def test_add_can_insert_between_existing_scenes(self):
        plan = self.mock_proposal({"op": "add", "title": "Заставка", "frames": 150, "position": 2})
        self.assertEqual([scene.title for scene in self.project.scenes], [self.first.title, self.second.title])
        decision(self.project, plan, accept=True)
        updated = StudioProject.open(self.project.root)
        self.assertEqual([scene.title for scene in updated.scenes], [self.first.title, "Заставка", self.second.title])
        self.assertEqual(updated.scenes[1].frames, 150)

    def test_explicit_creation_budget_is_checked_before_approval(self):
        from efir.assistant import creation_budget, from_model, schema

        budget = creation_budget("Вставь между сценами вставку на 9,2 секунды с субтитрами", 2)
        self.assertEqual((budget.count, budget.seconds, budget.between, budget.required_text),
                         (1, 9.2, True, ("caption",)))
        cases = schema(self.project.revision, 2, budget)["properties"]["operations"]
        self.assertEqual((cases["minItems"], cases["maxItems"]), (1, 1))
        self.assertEqual(cases["items"]["anyOf"][0]["properties"]["seconds"]["const"], 9.2)
        good = {"op": "add", "title": "Вставка", "seconds": 9.2, "position": 2, "caption": "Локальный канал"}
        self.assertEqual(from_model(self.project, self.data(dict(good)), budget)["operations"][0]["frames"], 230)
        for ops in (
            [{**good, "seconds": 8}], [{**good, "position": 1}], [good, good], [{**good, "caption": ""}],
            [{"op": "edit", "scene": 1, "title": "Другое"}],
        ):
            with self.assertRaisesRegex(EfirError, "не соблюдает"):
                from_model(self.project, self.data(*(dict(op) for op in ops)), budget)
        examples = [
            ("Создай четыре сцены общей длительностью 52 секунды", 4, 13.0),
            ("Сделай одну сцену на 8 секунд", 1, 8.0),
            ("Придумай 10 сцен на полторы минуты", 10, 9.0),
            ("Сделай две сцены по 30 секунд", 2, 30.0),
        ]
        for query, count, length in examples:
            resolved = creation_budget(query, 0)
            self.assertEqual((resolved.count, resolved.seconds), (count, length))
        for query in ("Озвучь сцену", "Создай набросок", "Создай 30 сцен", "Добавь сцену на 601 секунду",
                      "Добавь сцену и озвучь текущую"):
            self.assertIsNone(creation_budget(query, 2))

    def test_literal_edit_is_bound_to_target_and_preserves_other_fields(self):
        from efir.assistant import from_model, literal_edit, schema

        scenes = self.project.scenes
        edit = literal_edit("Замени реплику в «Второй» на «Микрофон выключен» и добавь её озвучку", scenes)
        self.assertEqual((edit.scene, edit.field, edit.value, edit.voice), (2, "speech", "Микрофон выключен", True))
        kinds = schema(2, 2, exact=edit)["properties"]["operations"]
        self.assertEqual((kinds["minItems"], kinds["maxItems"]), (2, 2))
        props = kinds["items"]["anyOf"][0]["properties"]
        self.assertEqual(props["scene"]["const"], 2)
        self.assertEqual(props["speech"]["const"], edit.value)
        first = {"op": "edit", "scene": 2, "speech": edit.value}
        second = {"op": "voice", "scene": 2}
        result = from_model(self.project, self.data(dict(first), dict(second)), exact=edit)
        self.assertEqual(result["operations"][0]["scene"], self.second.id)
        for ops in ([first], [{**first, "scene": 1}, second], [{**first, "caption": "лишнее"}, second]):
            with self.assertRaisesRegex(EfirError, "точной правке"):
                from_model(self.project, self.data(*(dict(op) for op in ops)), exact=edit)
        title = literal_edit("Переименуй первую сцену в «Ночь»", scenes)
        self.assertEqual((title.scene, title.field, title.voice), (1, "title", False))
        caption = literal_edit("Исправь субтитр в «Второй» на «Мы тут»", scenes)
        self.assertEqual((caption.scene, caption.field, caption.value), (2, "caption", "Мы тут"))
        self.assertFalse(literal_edit("Измени реплику сцены №1 на «Тишина», без озвучки", scenes).voice)
        for query in ("Привет", "Замени реплику", "Измени неизвестное на «Текст»",
                      "Замени реплику первой и второй сцены на «Текст»",
                      "Замени реплику в «Несуществующая» на «Текст»",
                      "Замени реплику первой сцены на «Текст» и перемести её",
                      "Замени реплику первой сцены на «Текст» и озвучь вторую сцену"):
            self.assertIsNone(literal_edit(query, scenes))

class PiperTests(StudioFixture):
    def setUp(self):
        super().setUp()
        self.model = self.path / "denis.onnx"
        self.model.write_bytes(b"model")
        self.model.with_suffix(".onnx.json").write_text("{}", encoding="utf-8")

    def test_subprocess_contract_and_result_validation(self):
        target = self.path / "speech.wav"
        config = {"EFIR_PIPER": sys.executable, "EFIR_PIPER_MODEL": str(self.model),
                  "EFIR_PIPER_VERSION": "fixture", "EFIR_PIPER_SPEED": "1.25"}
        with patch.dict(os.environ, config):
            engine = Piper.configured()
        self.assertEqual(engine.recipe["speed"], 1.25)
        self.assertEqual(len(engine.recipe["model_sha256"]), 64)
        self.assertNotIn(str(self.path), str(engine.recipe))
        with patch("efir.tts.subprocess.Popen") as launch:
            child = launch.return_value.__enter__.return_value
            child.returncode = 0
            child.poll.return_value = 0
            child.communicate.side_effect = [subprocess.TimeoutExpired("piper", 0.2), None]
            wav(target)
            engine.synthesize("03:33, эфир.", target, cancel=Event())
            self.assertIn("--length-scale", launch.call_args.args[0])
            self.assertEqual(child.communicate.call_args_list[0].kwargs["input"], "03:33, эфир.\n".encode())
            self.assertIsNone(child.communicate.call_args_list[1].kwargs["input"])
            child.communicate.side_effect = None
            target.write_bytes(b"bad audio")
            with self.assertRaisesRegex(EfirError, "корректный WAV"):
                engine.synthesize("text", target, cancel=Event())
            wav(target, b"")
            with self.assertRaisesRegex(EfirError, "корректный WAV"):
                engine.synthesize("text", target, cancel=Event())
            child.returncode = 3
            with self.assertRaisesRegex(EfirError, "кодом 3"):
                engine.synthesize("text", target, cancel=Event())

    def test_unavailable_changed_cancelled_timed_out(self):
        with self.assertRaises(EfirError):
            Piper("missing-piper-tool", self.model, version="test")
        with self.assertRaises(EfirError):
            Piper(sys.executable, self.model, version="", speed=100)
        with patch.dict(os.environ, EFIR_PIPER_SPEED="bad"), self.assertRaisesRegex(EfirError, "числом"):
            Piper.configured()
        engine = Piper(sys.executable, self.model, version="test", timeout=0)
        target = self.path / "speech.wav"
        with patch("efir.tts.subprocess.Popen") as launch:
            child = launch.return_value.__enter__.return_value
            child.poll.return_value = None
            with self.assertRaisesRegex(EfirError, "время"):
                engine.synthesize("text", target, cancel=Event())
            child.kill.assert_called_once()
        engine.timeout = 10
        with patch("efir.tts.subprocess.Popen") as launch:
            child = launch.return_value.__enter__.return_value
            child.poll.return_value = None
            with self.assertRaisesRegex(EfirError, "отменена"):
                engine.synthesize("text", target, cancel=Mock(**{"is_set.side_effect": [False, True]}))
            child.kill.assert_called_once()
        cancel = Event()
        cancel.set()
        with self.assertRaisesRegex(EfirError, "отменена"):
            engine.synthesize("text", target, cancel=cancel)
        self.model.write_bytes(b"updated model")
        with self.assertRaisesRegex(EfirError, "изменился"):
            engine.synthesize("text", target, cancel=Event())
