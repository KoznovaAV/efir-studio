"""SQLite project revisions and verified, portable media."""

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from .project import AUDIO, SQLITE_MANIFEST, EfirError, Project, Scene, digest_file, project_lock, validate_ref


MIGRATIONS = (
    (
        "CREATE TABLE revision (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES revision, "
        "document TEXT NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE project (id TEXT PRIMARY KEY, head INTEGER NOT NULL REFERENCES revision)",
    ),
    (
        "CREATE TABLE job (id TEXT PRIMARY KEY, key TEXT NOT NULL, kind TEXT NOT NULL, "
        "revision INTEGER NOT NULL REFERENCES revision, spec TEXT NOT NULL, state TEXT NOT NULL, "
        "attempt INTEGER NOT NULL DEFAULT 0, lease REAL, worker TEXT, deadline REAL, "
        "progress TEXT NOT NULL DEFAULT '', error TEXT, result TEXT)",
        "CREATE INDEX job_lookup ON job (key, state)",
        "CREATE TABLE job_event (job TEXT NOT NULL REFERENCES job, state TEXT NOT NULL, "
        "created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    ),
    (
        "CREATE TABLE proposal (id TEXT PRIMARY KEY, revision INTEGER NOT NULL REFERENCES revision, "
        "request TEXT NOT NULL, model TEXT NOT NULL, operations TEXT NOT NULL, "
        "state TEXT NOT NULL, applied INTEGER REFERENCES revision, jobs TEXT NOT NULL DEFAULT '[]')",
    ),
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_key(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class StudioScene(Scene):
    visual: str | None
    speech: str = ""

    def validate(self):
        super().validate(allow_missing_visual=True)
        if not isinstance(self.speech, str) or "\x00" in self.speech:
            raise EfirError("Реплика должна быть текстом без NUL.")

class StudioProject(Project):
    """A working copy of one committed revision, with optimistic writes."""

    def __init__(self, root, title, aspect, scenes=(), revision=None, background=None):
        self.background = background
        if background is not None:
            validate_ref(background, AUDIO)
        super().__init__(root, title, aspect, scenes, revision)

    @property
    def revision(self):
        return self._revision

    @property
    def database(self):
        return self.inside(SQLITE_MANIFEST)

    def document(self):
        return {"title": self.title, "aspect": self.aspect, "background": self.background,
                "scenes": [asdict(scene) for scene in self.scenes]}

    @classmethod
    def from_document(cls, root, revision, document):
        return cls(root, document["title"], document["aspect"],
                   [StudioScene(**scene) for scene in document["scenes"]], revision,
                   document.get("background"))

    @classmethod
    def create(cls, root, title, aspect="9:16"):
        project = cls(root, title, aspect)
        project.root.mkdir(parents=True, exist_ok=True)
        with project_lock(project.inside(".project.lock")):
            if project.database.exists() or project.inside("project.json").exists():
                raise EfirError("Проект в этом каталоге уже существует.")
            with project.connect(create=True) as db:
                project._revision = project.write(db, "Создание проекта")
        return project

    @classmethod
    def open(cls, root, revision=None):
        project = cls(root, "Загрузка", "9:16")
        with project.connect() as db:
            return project.read(db, revision)

    @contextmanager
    def connect(self, *, create=False):
        try:
            with closing(sqlite3.connect(self.database.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                                         uri=True, timeout=5)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys = ON")
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version > len(MIGRATIONS):
                    raise EfirError("Эта версия базы проекта не поддерживается.")
                for index, statements in enumerate(MIGRATIONS[version:], version + 1):
                    for statement in statements:
                        db.execute(statement)
                    db.execute(f"PRAGMA user_version = {index}")
                yield db
        except sqlite3.Error as error:
            raise EfirError(f"Ошибка базы проекта: {error}") from error

    def read(self, db, revision=None):
        if revision is None:
            row = db.execute("SELECT revision.* FROM project JOIN revision ON revision.id = project.head").fetchone()
        else:
            row = db.execute("SELECT * FROM revision WHERE id = ?", (revision,)).fetchone()
        if row is None:
            raise EfirError("Ревизия проекта не найдена.")
        return self.from_document(self.root, row["id"], json.loads(row["document"]))

    def write(self, db, reason):
        self._validate()
        if self.background is not None:
            validate_ref(self.background, AUDIO)
        current = db.execute("SELECT head FROM project").fetchone()
        if (current["head"] if current else None) != self.revision:
            raise EfirError("Проект изменился. Откройте актуальную ревизию.")
        revision = db.execute("INSERT INTO revision (parent, document, reason) VALUES (?, ?, ?)",
                              (self.revision, encode(self.document()), reason)).lastrowid
        if current:
            db.execute("UPDATE project SET head = ?", (revision,))
        else:
            db.execute("INSERT INTO project VALUES (?, ?)", (uuid4().hex, revision))
        return revision

    def save(self, reason="Ручная правка"):
        with self.connect() as db:
            revision = self.write(db, reason)
        self._revision = revision

    def restore(self, revision):
        with self.connect() as db:
            restored = self.read(db, revision)
            restored._revision = self.revision
            head = restored.write(db, f"Восстановление ревизии {revision}")
        restored._revision = head
        return restored

    def history(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id, parent, reason, created FROM revision ORDER BY id DESC")]

    def add(self, **fields):
        scene = StudioScene(id=uuid4().hex[:12], **fields)
        scene.validate()
        self.scenes.append(scene)
        return scene

    def references(self):
        return {ref for ref in (self.background, *(ref for scene in self.scenes
                                                 for ref in (scene.visual, scene.voice))) if ref is not None}

    def portable(self, destination):
        """Copy all committed revisions and queue inputs, without disposable render cache."""
        if Path(destination).resolve().is_relative_to(self.root):
            raise EfirError("Выберите каталог вне исходного проекта.")
        with new_folder(destination) as stage:
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(stage / SQLITE_MANIFEST)) as target, target:
                    source.backup(target)
                    references = set()
                    for revision, document in target.execute("SELECT id, document FROM revision"):
                        references.update(self.from_document(self.root, revision, json.loads(document)).references())
                    for (result,) in target.execute("SELECT result FROM job WHERE kind = 'voice' AND result IS NOT NULL"):
                        references.add(json.loads(result)["asset"])
                    exports = [json.loads(row[0]) for row in target.execute(
                        "SELECT result FROM job WHERE kind = 'export' AND state = 'succeeded'"
                    )]
                    # A copied lease belongs to another process. The worker recovers it on first claim.
                    target.execute("UPDATE job SET lease = 0 WHERE state IN ('running', 'cancel-requested')")
            copy_references(self, stage, references)
            for result in exports:
                source = export_file(self, result)
                copied = stage / "exports" / source.name
                copied.parent.mkdir(exist_ok=True)
                shutil.copyfile(source, copied)
                if digest_file(copied) != result["sha256"]:
                    raise EfirError("MP4 изменился во время копирования.")
        return Path(destination).resolve()


def copy_references(source, destination, references):
    if references:
        (destination / "assets").mkdir(exist_ok=True)
    for reference in sorted(references):
        target = destination / "assets" / reference
        shutil.copyfile(source.asset(reference), target)
        if digest_file(target) != target.stem:
            raise EfirError(f"Медиа изменилось во время копирования: {reference}")


@contextmanager
def new_folder(destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise EfirError("Выберите новый каталог назначения.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".efir-copy-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "project"
        stage.mkdir()
        yield stage
        if destination.exists():
            raise EfirError("Каталог назначения уже занят.")
        stage.rename(destination)


def migrate(source, destination):
    legacy = Project.open(source)
    if Path(destination).resolve().is_relative_to(legacy.root):
        raise EfirError("Выберите каталог вне исходного проекта.")
    with new_folder(destination) as stage:
        project = StudioProject.create(stage, legacy.title, legacy.aspect)
        project.scenes = [StudioScene(**asdict(scene)) for scene in legacy.scenes]
        copy_references(legacy, stage, project.references())
        project.save("Импорт проекта v0.1")
    return StudioProject.open(destination)


def open_project(root):
    root = Path(root)
    if (root / SQLITE_MANIFEST).exists():
        return StudioProject.open(root)
    return Project.open(root)


def export_file(project, result):
    path = project.inside(result["file"])
    if path.parent != project.inside("exports") or not path.is_file() or digest_file(path) != result["sha256"]:
        raise EfirError("Файл результата отсутствует или изменился.")
    return path
