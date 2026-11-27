"""Versioned project files and content-addressed media."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
from uuid import uuid4

FPS = 25
MAX_FRAMES = FPS * 600
FORMATS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
VIDEO = {".mp4": "mov", ".mov": "mov", ".mkv": "matroska", ".webm": "matroska"}
IMAGE = {".png", ".jpg", ".jpeg"}
AUDIO = {".wav": "wav", ".mp3": "mp3", ".flac": "flac", ".m4a": "mov"}
ASSET = re.compile(r"[a-f0-9]{64}\.[a-z0-9]+")
SCENE_ID = re.compile(r"[a-f0-9]{12}")
SQLITE_MANIFEST = "project.sqlite"


class EfirError(Exception):
    """An error that can be presented to the user."""


def frame_count(value, *, zero=False):
    try:
        seconds = Decimal(str(value))
        if not seconds.is_finite() or not 0 <= seconds <= 600:
            raise ValueError
        frames = int((seconds * FPS).to_integral_value(rounding=ROUND_HALF_UP))
        if frames == 0 and not zero:
            raise ValueError
        return frames
    except (InvalidOperation, ValueError):
        raise EfirError("Укажите время от 0 до 600 секунд. Минимальная длительность — один кадр (0,04 с).") from None


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def comparable_path(path):
    if isinstance(path, PureWindowsPath):
        name = str(path)
        # Windows can retain this prefix when a path appears during realpath().
        if name.upper().startswith("\\\\?\\UNC\\"):
            return PureWindowsPath("\\\\" + name[8:])
        if re.match(r"^\\\\\?\\[A-Za-z]:\\", name):
            return PureWindowsPath(name[4:])
    return path


@contextmanager
def staged_file(destination):
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".efir-", suffix=destination.suffix, delete=False
    ) as stream:
        stage = Path(stream.name)
    try:
        yield stage
    finally:
        stage.unlink(missing_ok=True)


def publish(stage, destination, *, overwrite):
    if overwrite:
        stage.replace(destination)
        return
    try:
        if os.name == "nt":
            stage.rename(destination)
        else:
            os.link(stage, destination)
    except FileExistsError:
        raise EfirError(f"Файл уже существует: {destination}") from None


@contextmanager
def project_lock(path):
    with path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise EfirError("Проект сохраняется в другом окне. Повторите попытку.") from error
        yield


def validate_ref(value, allowed):
    if (not isinstance(value, str) or not ASSET.fullmatch(value)
            or Path(value).suffix not in allowed):
        raise EfirError("Проект содержит неверную ссылку на медиа.")


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    visual: str
    frames: int
    voice: str | None = None
    start_frame: int = 0
    fit: str = "contain"
    source_audio: bool = False
    caption: str = ""
    notes: str = ""

    def validate(self, *, allow_missing_visual=False):
        if not isinstance(self.id, str) or not SCENE_ID.fullmatch(self.id):
            raise EfirError("Неверный идентификатор сцены.")
        if not all(isinstance(value, str) and "\x00" not in value
                   for value in (self.title, self.caption, self.notes)) or not self.title.strip():
            raise EfirError("У каждой сцены должно быть название. Текст не должен содержать NUL.")
        if type(self.frames) is not int or not 1 <= self.frames <= MAX_FRAMES:
            raise EfirError("Длительность сцены должна быть от 1 кадра до 600 секунд.")
        if type(self.start_frame) is not int or not 0 <= self.start_frame <= MAX_FRAMES:
            raise EfirError("Начальная позиция должна быть от 0 до 600 секунд.")
        if self.fit not in ("contain", "cover") or type(self.source_audio) is not bool:
            raise EfirError("Неверный режим кадрирования или исходного звука.")
        if self.visual is None and allow_missing_visual:
            if self.start_frame or self.source_audio:
                raise EfirError("Для сцены без клипа сбросьте начальную позицию и исходный звук.")
        else:
            validate_ref(self.visual, {*VIDEO, *IMAGE})
        if self.voice is not None:
            validate_ref(self.voice, AUDIO)
        if self.visual is not None and Path(self.visual).suffix in IMAGE and (self.start_frame or self.source_audio):
            raise EfirError("Для изображения сбросьте начальную позицию и исходный звук.")


class Project:
    def __init__(self, root, title, aspect, scenes=(), revision=None):
        self.root = Path(root).resolve()
        self.title = title
        self.aspect = aspect
        self.scenes = list(scenes)
        self._revision = revision
        self._validate()

    def inside(self, *parts):
        path = self.root.joinpath(*parts)
        if not comparable_path(path.resolve()).is_relative_to(comparable_path(self.root)):
            raise EfirError("Путь проекта ведёт за пределы его каталога.")
        return path

    @classmethod
    def create(cls, root, title, aspect="9:16"):
        project = cls(root, title, aspect)
        project.root.mkdir(parents=True, exist_ok=True)
        project.save()
        return project

    @classmethod
    def open(cls, root):
        root = Path(root).resolve()
        manifest = root / "project.json"
        if not comparable_path(manifest.resolve()).is_relative_to(comparable_path(root)):
            raise EfirError("Файл проекта ведёт за пределы его каталога.")
        try:
            raw = manifest.read_bytes()
            data = json.loads(raw)
            if type(data["version"]) is not int or data["version"] != 1:
                raise EfirError("Эта версия формата проекта не поддерживается.")
            if not isinstance(data["scenes"], list):
                raise TypeError
            return cls(root, data["title"], data["aspect"],
                       [Scene(**record) for record in data["scenes"]], hashlib.sha256(raw).hexdigest())
        except FileNotFoundError:
            raise EfirError(f"В каталоге нет project.json: {root}") from None
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise EfirError(f"Файл проекта повреждён: {manifest}") from error

    def _validate(self):
        if not isinstance(self.title, str) or not self.title.strip() or "\x00" in self.title:
            raise EfirError("Введите название проекта.")
        if not isinstance(self.aspect, str) or self.aspect not in FORMATS:
            raise EfirError("Допустимые форматы: 9:16, 16:9 и 1:1.")
        for scene in self.scenes:
            scene.validate()
        if len({scene.id for scene in self.scenes}) != len(self.scenes):
            raise EfirError("В проекте повторяются идентификаторы сцен.")

    def save(self):
        self._validate()
        target = self.inside("project.json")
        data = {"version": 1, "title": self.title, "aspect": self.aspect,
                "scenes": [asdict(scene) for scene in self.scenes]}
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with project_lock(self.inside(".project.lock")):
            if (self.root / SQLITE_MANIFEST).exists():
                raise EfirError("В каталоге уже хранится проект SQLite.")
            current = digest_file(target) if target.exists() else None
            if current != self._revision:
                raise EfirError("Проект уже существует или изменился. Откройте его снова.")
            with staged_file(target) as stage:
                stage.write_bytes(payload)
                stage.replace(target)
            self._revision = hashlib.sha256(payload).hexdigest()

    def import_media(self, source, *, voice=False):
        source = Path(source)
        suffix = source.suffix.lower()
        allowed = AUDIO if voice else {*VIDEO, *IMAGE}
        if suffix not in allowed or not source.is_file():
            raise EfirError("Выберите существующий файл одного из форматов: " + ", ".join(sorted(allowed)))
        directory = self.inside("assets")
        directory.mkdir(exist_ok=True)
        with staged_file(directory / "asset") as stage:
            shutil.copyfile(source, stage)
            if stage.stat().st_size == 0:
                raise EfirError("Медиафайл пуст.")
            digest = digest_file(stage)
            target = self.inside("assets", digest + suffix)
            if not target.exists() or digest_file(target) != digest:
                stage.replace(target)
        return target.name

    def asset(self, reference):
        path = self.inside("assets", reference)
        if not path.is_file() or digest_file(path) != path.stem:
            raise EfirError(f"Медиа отсутствует или изменено: {reference}. Импортируйте исходник ещё раз.")
        return path

    def scene_index(self, scene_id):
        for index, scene in enumerate(self.scenes):
            if scene.id == scene_id:
                return index
        raise EfirError("Сцена с таким идентификатором не найдена.")

    def add(self, **fields):
        scene = Scene(id=uuid4().hex[:12], **fields)
        scene.validate()
        self.scenes.append(scene)
        return scene

    def update(self, scene_id, **fields):
        index = self.scene_index(scene_id)
        scene = replace(self.scenes[index], **fields)
        scene.validate()
        self.scenes[index] = scene
        return scene

    def move(self, scene_id, position):
        index = self.scene_index(scene_id)
        if not 1 <= position <= len(self.scenes):
            raise EfirError("Новая позиция должна входить в список сцен.")
        self.scenes.insert(position - 1, self.scenes.pop(index))

    def output(self, path, suffix):
        path = Path(path)
        resolved = comparable_path(path.resolve())
        managed = tuple(comparable_path(self.inside(folder).resolve()) for folder in ("assets", ".cache"))
        if (path.suffix.lower() != suffix or any(resolved.is_relative_to(folder) for folder in managed)
                or not path.parent.is_dir()):
            raise EfirError(f"Выберите путь {suffix} в существующей папке вне assets и .cache.")
        return path
