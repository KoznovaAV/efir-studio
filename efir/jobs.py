"""Durable, single-slot project queue for speech and rendering."""

from dataclasses import replace
import json
from pathlib import Path
import time
from uuid import uuid4

from .project import EfirError, digest_file, project_lock, publish, staged_file
from .render import Renderer
from .store import content_key, encode, export_file


ACTIVE = ("queued", "running", "cancel-requested")
LEASE = 15
ATTEMPTS = 3


class Queue:
    def __init__(self, project, *, clock=time.time):
        self.project = project
        self.clock = clock

    @staticmethod
    def event(db, job, state):
        db.execute("INSERT INTO job_event (job, state) VALUES (?, ?)", (job, state))

    def enqueue(self, kind, *, scene_id=None, recipe=None, draft=False, regenerate=False):
        with self.project.connect() as db:
            return self.schedule(db, self.project, kind, scene_id, recipe, draft, regenerate)

    def schedule(self, db, snapshot, kind, scene_id=None, recipe=None, draft=False, regenerate=False):
        current = self.project.read(db)
        if current.revision != snapshot.revision:
            raise EfirError("Проект изменился перед постановкой задачи.")
        if kind == "voice":
            scene = current.scenes[current.scene_index(scene_id)]
            if not scene.speech.strip() or not recipe:
                raise EfirError("Добавьте реплику и настройте локальный голос.")
            spec = {"scene": scene.id, "speech": scene.speech, "previous_voice": scene.voice,
                    "recipe": recipe, "nonce": uuid4().hex if regenerate else ""}
        elif kind == "export":
            spec = {"draft": draft, "revision": current.revision}
        else:
            raise EfirError("Неизвестный тип задачи.")
        key = content_key([kind, spec])
        match = db.execute(
            "SELECT id,state,result FROM job WHERE key = ? AND state IN ('queued','running','succeeded') "
            "ORDER BY rowid DESC LIMIT 1", (key,),
        ).fetchone()
        if match is not None:
            if match["state"] != "succeeded":
                return match["id"]
            result = json.loads(match["result"])
            try:
                if kind == "export":
                    export_file(current, result)
                    return match["id"]
                if spec["previous_voice"] == result["asset"]:
                    current.asset(result["asset"])
                    return match["id"]
            except EfirError:
                # Successful jobs are retained in history even if the media needs to be rebuilt.
                pass
        job = uuid4().hex
        db.execute("INSERT INTO job (id,key,kind,revision,spec,state) VALUES (?,?,?,?,?,'queued')",
                   (job, key, kind, current.revision, encode(spec)))
        self.event(db, job, "queued")
        return job

    def jobs(self):
        with self.project.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,kind,revision,state,attempt,progress,error,result FROM job ORDER BY rowid DESC"
            )]

    def cancel(self, job):
        with self.project.connect() as db:
            state = db.execute("SELECT state FROM job WHERE id = ?", (job,)).fetchone()
            if state and state["state"] in ("queued", "running"):
                target = "cancelled" if state["state"] == "queued" else "cancel-requested"
                db.execute("UPDATE job SET state = ? WHERE id = ?", (target, job))
                self.event(db, job, target)

    def retry(self, job):
        with self.project.connect() as db:
            row = db.execute("SELECT state,attempt FROM job WHERE id = ?", (job,)).fetchone()
            if not row or row["state"] != "failed" or row["attempt"] >= ATTEMPTS:
                raise EfirError("Повтор недоступен для этой задачи.")
            db.execute("UPDATE job SET state = 'queued', error = NULL, progress = '' WHERE id = ?", (job,))
            self.event(db, job, "queued")

    def claim(self, timeout):
        now = self.clock()
        with self.project.connect() as db:
            active = db.execute("SELECT * FROM job WHERE state IN ('running','cancel-requested')").fetchone()
            if active:
                if active["lease"] > now:
                    return None
                if active["state"] == "cancel-requested":
                    state = "cancelled"
                else:
                    state = "queued" if active["attempt"] < ATTEMPTS else "failed"
                db.execute("UPDATE job SET state=?,worker=NULL,lease=NULL,error=? WHERE id=?",
                           (state, "Исполнитель прервался." if state == "failed" else None, active["id"]))
                self.event(db, active["id"], state)
            row = db.execute("SELECT * FROM job WHERE state = 'queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            job = dict(row)
            job["worker"] = uuid4().hex
            job["deadline"] = now + timeout
            db.execute("UPDATE job SET state='running',worker=?,lease=?,deadline=?,attempt=attempt+1 "
                       "WHERE id=?", (job["worker"], now + LEASE, job["deadline"], job["id"]))
            self.event(db, job["id"], "running")
            return job

    def owns(self, db, job, now):
        row = db.execute("SELECT state,worker,lease FROM job WHERE id=?", (job["id"],)).fetchone()
        if row["worker"] != job["worker"] or row["lease"] <= now:
            return False
        return row["state"] == "running"

    def heartbeat(self, job, progress=None):
        with self.project.connect() as db:
            now = self.clock()
            if now >= job["deadline"] or not self.owns(db, job, now):
                return False
            db.execute("UPDATE job SET lease=?,progress=COALESCE(?,progress) WHERE id=?",
                       (now + LEASE, progress, job["id"]))
            return True

    def finish(self, db, job, result=None, error=None, *, now=None):
        if now is None:
            now = self.clock()
        row = db.execute("SELECT state,worker,lease FROM job WHERE id=?", (job["id"],)).fetchone()
        if row["worker"] != job["worker"] or row["lease"] <= now:
            return
        if row["state"] == "cancel-requested":
            state = "cancelled"
        elif now >= job["deadline"]:
            state, error = "failed", "Истекло время выполнения задачи."
        else:
            state = "failed" if error else "succeeded"
        db.execute("UPDATE job SET state=?,result=?,error=?,worker=NULL,lease=NULL WHERE id=?",
                   (state, encode(result) if state == "succeeded" else None, error, job["id"]))
        self.event(db, job["id"], state)


class JobProgress:
    def __init__(self, queue, job):
        self.queue = queue
        self.job = job
        self.last = 0
        self.active = True

    def report(self, text):
        self.active = self.queue.heartbeat(self.job, text)
        self.last = self.queue.clock()

    def is_set(self):
        if self.queue.clock() - self.last >= 0.4:
            self.report(None)
        return not self.active


def run_one(project, *, tts=None, timeout=3600, clock=time.time):
    with project_lock(project.inside(".worker.lock")):
        return _run_one(project, tts, timeout, clock)


def _run_one(project, tts, timeout, clock):
    queue = Queue(project, clock=clock)
    job = queue.claim(timeout)
    if job is None:
        return None
    progress = JobProgress(queue, job)
    spec = json.loads(job["spec"])
    try:
        if job["kind"] == "voice":
            _voice(queue, job, spec, progress, tts)
        else:
            _export(queue, job, spec, progress)
    except (EfirError, OSError, ValueError) as error:
        with project.connect() as db:
            queue.finish(db, job, error=str(error))
    return job["id"]


def _voice(queue, job, spec, progress, engine):
    project = queue.project
    speech, recipe = spec["speech"], spec["recipe"]
    if engine is None or engine.recipe != recipe:
        raise EfirError("Для этой задачи нужен локальный голос с исходными параметрами.")
    key = content_key([speech, recipe, spec["nonce"]])
    with project.connect() as db:
        candidates = db.execute("SELECT result FROM job WHERE kind='voice' AND state='succeeded' "
                                "ORDER BY rowid DESC").fetchall()
    asset = None
    for candidate in candidates:
        result = json.loads(candidate["result"])
        if result["cache"] == key:
            try:
                project.asset(result["asset"])
            except EfirError:
                continue
            asset = result["asset"]
            break
    if asset is None:
        progress.report("Синтез речи")
        with staged_file(project.inside("voice.wav")) as stage:
            engine.synthesize(speech, stage, cancel=progress)
            if not progress.is_set():
                asset = project.import_media(stage, voice=True)
    with project.connect() as db:
        result = None
        now = queue.clock()
        if now < job["deadline"] and queue.owns(db, job, now) and asset:
            current = project.read(db)
            selected = False
            for index, scene in enumerate(current.scenes):
                if scene.id == spec["scene"] and scene.speech == speech and scene.voice == spec["previous_voice"]:
                    if scene.voice != asset:
                        current.scenes[index] = replace(scene, voice=asset)
                        current.write(db, "Озвучка сцены")
                    selected = True
                    break
            result = {"asset": asset, "cache": key, "selected": selected, "recipe": recipe}
        queue.finish(db, job, result, error=None if result else "Создание озвучки прервано.", now=now)


def _export(queue, job, spec, progress):
    project = queue.project
    with project.connect() as db:
        snapshot = project.read(db, job["revision"])
    folder = project.inside("exports")
    folder.mkdir(exist_ok=True)
    target = project.inside("exports", f"{job['id']}.mp4")
    with staged_file(target) as stage:
        rendered = Renderer(cancel=progress, report=progress.report).export(
            snapshot, stage, draft=spec["draft"], overwrite=True,
        )
        digest = digest_file(stage)
        with project.connect() as db:
            result = None
            now = queue.clock()
            if now < job["deadline"] and queue.owns(db, job, now):
                publish(stage, target, overwrite=True)
                result = {"file": str(Path("exports", target.name).as_posix()), "sha256": digest,
                          "encoded": rendered.encoded, "reused": rendered.reused}
            queue.finish(db, job, result, error=None if result else "Экспорт прерван.", now=now)
