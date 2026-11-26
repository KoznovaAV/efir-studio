"""Local Ollama structured plans and transactional confirmation."""

import http.client
import json
import re
from uuid import uuid4

from .jobs import Queue
from .project import EfirError, MAX_FRAMES, SCENE_ID
from .store import encode


TEXT = {"type": "string", "maxLength": 1600}
FRAME_COUNT = {"type": "integer", "minimum": 1, "maximum": MAX_FRAMES}
ID = {"type": "string", "pattern": SCENE_ID.pattern}
FIELDS = {"title": TEXT, "speech": TEXT, "caption": TEXT, "notes": TEXT, "frames": FRAME_COUNT}
OPERATIONS = {
    "add": (FIELDS, ("title", "frames")),
    "edit": ({"scene": ID, **FIELDS}, ("scene",)),
    "move": ({"scene": ID, "position": {"type": "integer", "minimum": 1}}, ("scene", "position")),
    "voice": ({"scene": ID}, ("scene",)),
    "export": ({}, ()),
}
LABELS = {"add": "Добавить сцену", "edit": "Изменить сцену", "move": "Переместить сцену",
          "voice": "Поставить озвучку в очередь", "export": "Поставить черновой экспорт в очередь"}
FIELD_LABELS = {"title": "Название", "frames": "Кадры", "speech": "Реплика", "caption": "Субтитры",
                "notes": "Заметки", "position": "Позиция"}


def object_schema(properties, required):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def schema(revision):
    variants = [object_schema({"op": {"type": "string", "const": kind}, **fields}, ("op", *required))
                for kind, (fields, required) in OPERATIONS.items()]
    return object_schema(
        {"base_revision": {"type": "integer", "const": revision},
         "operations": {"type": "array", "minItems": 1, "maxItems": 24, "items": {"anyOf": variants}}},
        ("base_revision", "operations"),
    )


def validate(snapshot, data):
    if not isinstance(data, dict) or data.keys() != {"base_revision", "operations"}:
        raise EfirError("Неверный формат плана.")
    if type(data["base_revision"]) is not int or data["base_revision"] != snapshot.revision:
        raise EfirError("План относится к другой ревизии проекта.")
    operations = data["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= 24:
        raise EfirError("План должен содержать от 1 до 24 операций.")
    kinds = set()
    for operation in operations:
        kind = operation.get("op") if isinstance(operation, dict) else None
        if not isinstance(kind, str) or kind not in OPERATIONS:
            raise EfirError("План содержит неподдерживаемую операцию.")
        kinds.add(kind)
        fields, required = OPERATIONS[kind]
        values = operation.keys() - {"op"}
        if values - fields.keys() or set(required) - values or (kind == "edit" and values == {"scene"}):
            raise EfirError("Неверные поля операции.")
        for name in values:
            value, field = operation[name], fields[name]
            if field["type"] == "integer":
                valid = type(value) is int and field["minimum"] <= value <= field.get("maximum", MAX_FRAMES)
            else:
                valid = isinstance(value, str) and "\x00" not in value and len(value) <= field.get("maxLength", 1600)
                if name == "scene":
                    valid = valid and SCENE_ID.fullmatch(value)
            if not valid:
                raise EfirError(f"Недопустимое поле плана: {name}.")
    if {"export", "voice"} <= kinds:
        raise EfirError("Сначала завершите озвучку и затем подтвердите экспорт.")
    return operations


def execute(snapshot, operations):
    requests = []
    for operation in operations:
        fields = {name: value for name, value in operation.items() if name not in ("op", "scene")}
        kind, scene = operation["op"], operation.get("scene")
        if kind == "add":
            snapshot.add(visual=None, **fields)
        elif kind == "edit":
            snapshot.update(scene, **fields)
        elif kind == "move":
            snapshot.move(scene, fields["position"])
        elif kind == "voice":
            if not snapshot.scenes[snapshot.scene_index(scene)].speech.strip():
                raise EfirError("В плане озвучки есть сцена без реплики.")
            requests.append((kind, scene))
        else:
            requests.append((kind, None))
    if any(kind == "export" for kind, _ in requests) and (
            not snapshot.scenes or any(scene.visual is None for scene in snapshot.scenes)):
        raise EfirError("Перед экспортом прикрепите визуальный материал к каждой сцене.")
    return requests


class Ollama:
    """Use a direct loopback connection. Project files and paths stay outside the request."""

    def __init__(self, model, *, port=11434, timeout=90):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?(?::[A-Za-z0-9_.-]+)?", model):
            raise EfirError("Выберите имя локально установленной модели Ollama.")
        if re.search(r"(?i)(?:^|[-:])cloud(?:$|[-:])", model):
            raise EfirError("Ollama в EFIR использует только локальные модели.")
        self.model, self.port, self.timeout = model, port, timeout

    def request(self, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            if payload is None:
                connection.request("GET", path)
            else:
                connection.request("POST", path, encode(payload).encode("utf-8"),
                                   {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise EfirError(f"Ollama вернул HTTP {response.status}. Проверьте локальную модель.")
            body = response.read(256_001)
            if len(body) > 256_000:
                raise EfirError("Ответ Ollama слишком велик.")
            return json.loads(body)
        except (OSError, http.client.HTTPException, ValueError, RecursionError) as error:
            raise EfirError("Локальный Ollama недоступен или вернул неверный ответ.") from error
        finally:
            connection.close()

    def plan(self, project, prompt):
        tags = self.request("/api/tags")
        name = self.model if ":" in self.model else self.model + ":latest"
        try:
            available = any(item["name"] == name and not item.get("remote_host") and item["size"] > 0
                            for item in tags["models"])
        except (KeyError, TypeError, AttributeError) as error:
            raise EfirError("Ожидался список локальных моделей Ollama.") from error
        if not available:
            raise EfirError("Модель отсутствует в локальном Ollama. Настройте уже загруженную модель.")
        context = [{"id": s.id, "title": s.title[:100], "frames": s.frames, "speech": s.speech[:280],
                    "caption": s.caption[:160], "visual_attached": s.visual is not None}
                   for s in project.scenes[:24]]
        contract = schema(project.revision)
        response = self.request("/api/chat", {
            "model": name, "stream": False, "think": False, "keep_alive": 0,
            "format": contract, "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 2304},
            "messages": [
                {"role": "system", "content": (
                    "Ты ассистент по сценарию EFIR. Верни только JSON по схеме. Пиши по-русски. "
                    "fps=25, позиция с 1. Используй только существующие ID для edit, move и voice. "
                    "Для новой сцены заполни название, длительность в кадрах, реплику, субтитр, заметки. "
                    "При правке изменяй только запрошенные поля. export готовит черновой MP4. "
                    f"Текущая ревизия {project.revision}. Схема: {encode(contract)}"
                )},
                {"role": "user", "content": encode({"request": prompt, "scenes_in_order": context})},
            ],
        })
        try:
            return json.loads(response["message"]["content"])
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise EfirError("Ollama не вернул машиночитаемый план.") from error


def propose(project, engine, prompt):
    prompt = prompt.strip()
    if not 1 <= len(prompt) <= 2000:
        raise EfirError("Введите запрос длиной до 2000 символов.")
    # Preview is evaluated on a private copy. SQLite never sees its speculative changes.
    preview = project.from_document(project.root, project.revision, project.document())
    operations = validate(preview, engine.plan(preview, prompt))
    execute(preview, operations)
    proposal = uuid4().hex
    with project.connect() as db:
        db.execute("INSERT INTO proposal (id,revision,request,model,operations,state) VALUES (?,?,?,?,?,'pending')",
                   (proposal, project.revision, prompt, engine.model, encode(operations)))
    return proposal


def proposals(project):
    with project.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM proposal ORDER BY rowid")]


def decision(project, proposal, *, accept, recipe=None):
    with project.connect() as db:
        row = db.execute("SELECT * FROM proposal WHERE id = ?", (proposal,)).fetchone()
        if row is None:
            raise EfirError("План не найден.")
        if row["state"] == "applied" and accept:
            return row["applied"], json.loads(row["jobs"])
        if row["state"] != "pending":
            raise EfirError("Этот план уже закрыт.")
        if not accept:
            db.execute("UPDATE proposal SET state='rejected' WHERE id=?", (proposal,))
            return None, []
        current = project.read(db)
        operations = validate(current, {"base_revision": row["revision"], "operations": json.loads(row["operations"])})
        previous = current.document()
        requests = execute(current, operations)
        if previous != current.document():
            current._revision = current.write(db, "Подтверждённый план AI")
        queue = Queue(current)
        jobs = [queue.schedule(db, current, kind, scene, recipe, draft=True) for kind, scene in requests]
        db.execute("UPDATE proposal SET state='applied', applied=?, jobs=? WHERE id=?",
                   (current.revision, encode(jobs), proposal))
        return current.revision, jobs


def preview_text(project, operation):
    kind = operation["op"]
    label = LABELS[kind]
    if "scene" in operation:
        label += f" · {project.scenes[project.scene_index(operation['scene'])].title}"
    return "\n".join([label, *(f"  {FIELD_LABELS[key]} — {value}" for key, value in operation.items()
                              if key in FIELD_LABELS)])
