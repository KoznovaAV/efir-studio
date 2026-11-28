"""Local Ollama structured plans and transactional confirmation."""

from dataclasses import dataclass
import http.client
import json
import re
from uuid import uuid4

from .jobs import Queue
from .project import EfirError, FPS, MAX_FRAMES, SCENE_ID, frame_count
from .store import encode


TEXT = {"type": "string", "maxLength": 1600}
FRAME_COUNT = {"type": "integer", "minimum": 1, "maximum": MAX_FRAMES}
ID = {"type": "string", "pattern": SCENE_ID.pattern}
FIELDS = {"title": TEXT, "speech": TEXT, "caption": TEXT, "notes": TEXT, "frames": FRAME_COUNT}
POSITION = {"type": "integer", "minimum": 1}
MODEL_TEXT_MAX = {"title": 100, "speech": 420, "caption": 240, "notes": 180}
OPERATIONS = {
    "add": ({**FIELDS, "position": POSITION}, ("title", "frames")),
    "edit": ({"scene": ID, **FIELDS}, ("scene",)),
    "move": ({"scene": ID, "position": POSITION}, ("scene", "position")),
    "voice": ({"scene": ID}, ("scene",)),
    "export": ({}, ()),
}
LABELS = {"add": "Добавить сцену", "edit": "Изменить сцену", "move": "Переместить сцену",
          "voice": "Поставить озвучку в очередь", "export": "Поставить черновой экспорт в очередь"}
FIELD_LABELS = {"title": "Название", "frames": "Кадры", "speech": "Реплика", "caption": "Субтитры",
                "notes": "Заметки", "position": "Позиция"}
RUSSIAN_COUNTS = dict(zip(
    ("одну", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
     "одиннадцать", "двенадцать"), range(1, 13), strict=True,
))


@dataclass(frozen=True)
class CreationBudget:
    count: int
    seconds: float | None
    between: bool
    required_text: tuple[str, ...]


@dataclass(frozen=True)
class LiteralEdit:
    scene: int
    field: str
    value: str
    voice: bool


def literal_edit(prompt, scenes):
    """Lock exact replacements quoted by the user to one identifiable scene."""
    matched = re.match(r"^(?P<verb>измени|замени|исправь|переименуй|поменяй)\b(?P<rest>.+)$", prompt, re.I | re.S)
    if not matched:
        return None
    rest = matched["rest"]
    replacement = re.search(r"\b(?:на|в)\s+«([^»]+)»", rest, re.I)
    quoted = list(re.finditer(r"«([^»]+)»", rest))
    if replacement is None or not quoted or len(quoted) > 2:
        return None
    displayed = quoted[-1][1]
    if not re.search(r"\b(?:на|в)\s*$", rest[:quoted[-1].start()], re.I):
        return None
    before = rest[:quoted[-1].start()].casefold()
    after = rest[quoted[-1].end():].casefold()
    if re.search(r"\bи\b", re.sub(r"«[^»]*»", "", before)):
        return None
    if re.search(r"\b(?:перемести|удали|сократи|продли|экспортируй|переименуй|измени|замени)\b", after):
        return None
    if matched["verb"].casefold() == "переименуй" or re.search(r"\bназвани[ея]\b", before):
        field = "title"
    elif re.search(r"\bреплик[ауи]\b", before):
        field = "speech"
    elif re.search(r"\bсубтитр[ыа]?\b", before):
        field = "caption"
    else:
        return None
    if not len(displayed) <= MODEL_TEXT_MAX[field]:
        return None
    targets = []
    ordinals = ("перв", "втор", "треть", "четв[её]рт", "пят", "шест")
    for index, scene in enumerate(scenes, 1):
        named = len(quoted) == 2 and scene.title.casefold() == quoted[0][1].casefold()
        numbered = re.search(rf"\bсцен[аеыу]\s*№?\s*{index}\b", before)
        in_order = index <= len(ordinals) and re.search(
            rf"\b{ordinals[index - 1]}(?:ую|ой|ей|ю|ая|яя)\s+сцен[аеыу]\b", before,
        )
        if named or numbered or in_order:
            targets.append(index)
    if len(targets) != 1:
        return None
    voice = field == "speech" and bool(re.search(
        r"(?<!не )\b(?:озвучь|(?:добавь|сгенерируй|поставь)\s+(?:(?:её|ее|новую)\s+)?озвучку)\b", after,
    ))
    if voice and re.search(r"\bсцен[а-я]+\b", after):
        return None
    return LiteralEdit(targets[0], field, displayed, voice)


def creation_budget(prompt, scene_count):
    """Expose explicit creation constraints to the model before generation."""
    prompt = prompt.casefold()
    if not re.match(r"^(?:пожалуйста,?\s+)?(?:создай|придумай|сделай|добавь|вставь)\b", prompt):
        return None
    numbers = "|".join(RUSSIAN_COUNTS)
    counted = re.search(rf"\b(\d{{1,2}}|{numbers})(?:\s+(?:новых|новые|связанных))?\s+сцен(?:ы|у)?\b", prompt)
    if counted:
        value = counted[1]
        count = int(value) if value.isdigit() else RUSSIAN_COUNTS[value]
    elif re.search(r"\b(?:сцену|отбивку|вставку)\b", prompt):
        count = 1
    else:
        return None
    if not 1 <= count <= 24:
        return None
    # Compound requests still require the general planner and its full operation set.
    if re.search(r"\b(?:измени|удали|перемести|озвучь|экспортируй|подготовь)\b", prompt):
        return None
    per_scene = re.search(r"\b(?:по|на)\s+(\d+(?:[,.]\d+)?)\s+секунд(?:у|ы)?\b", prompt)
    total = re.search(r"\b(?:общей длительностью|всего)\s+(\d+(?:[,.]\d+)?)\s+секунд(?:у|ы)?\b", prompt)
    seconds = None
    if per_scene and (count == 1 or per_scene[0].startswith("по")):
        seconds = float(per_scene[1].replace(",", "."))
    elif total:
        seconds = float(total[1].replace(",", ".")) / count
    elif re.search(r"\b(?:полутораминутн[а-я]+|полторы минуты)\b", prompt):
        seconds = 90 / count
    if seconds is not None and not 1 / FPS <= seconds <= MAX_FRAMES / FPS:
        return None
    required_text = tuple(
        field for field, pattern in (("speech", r"\bреплик[а-я]*\b"), ("caption", r"\bсубтитр[а-я]*\b"))
        if re.search(pattern, prompt)
    )
    return CreationBudget(count, seconds, scene_count >= 2 and bool(re.search(r"\bмежду\b", prompt)), required_text)


def object_schema(properties, required):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def schema(revision, scene_count, budget=None, exact=None):
    variants = []
    for kind, (fields, required) in OPERATIONS.items():
        if (budget or not scene_count) and kind != "add":
            continue
        if exact and kind != "edit" and not (exact.voice and kind == "voice"):
            continue
        model_fields = {"op": {"type": "string", "const": kind}}
        for field, rule in fields.items():
            if field == "frames":
                model_fields["seconds"] = {"type": "number", "minimum": 1 / FPS, "maximum": MAX_FRAMES / FPS}
            elif field == "scene":
                model_fields[field] = {"type": "integer", "enum": list(range(1, scene_count + 1))}
            elif field in MODEL_TEXT_MAX:
                model_fields[field] = {**rule, "maxLength": MODEL_TEXT_MAX[field]}
            else:
                model_fields[field] = rule
        model_required = ("op", *("seconds" if key == "frames" else key for key in required))
        if budget:
            model_required += budget.required_text
            if budget.seconds is not None:
                model_fields["seconds"] = {"type": "number", "const": budget.seconds}
            if budget.between:
                model_fields["position"] = {"type": "integer", "minimum": 2, "maximum": scene_count}
                model_required += ("position",)
        if exact:
            model_fields = {key: rule for key, rule in model_fields.items() if key in ("op", "scene")}
            model_fields["scene"] = {"type": "integer", "const": exact.scene}
            if kind == "edit":
                model_fields[exact.field] = {"type": "string", "const": exact.value}
                model_required += (exact.field,)
        variants.append(object_schema(model_fields, model_required))
    lower, upper = (budget.count, budget.count) if budget else (1, 24)
    if exact:
        lower = upper = 2 if exact.voice else 1
    return object_schema(
        {"base_revision": {"type": "integer", "const": revision},
         "operations": {"type": "array", "minItems": lower, "maxItems": upper, "items": {"anyOf": variants}}},
        ("base_revision", "operations"),
    )


def from_model(project, data, budget=None, exact=None):
    if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
        raise EfirError("Ollama не вернул операции.")
    scene_ids = [scene.id for scene in project.scenes[:24]]
    for operation in data["operations"]:
        if not isinstance(operation, dict) or "frames" in operation:
            raise EfirError("Ответ Ollama должен задавать длительность числом секунд.")
        if "scene" in operation:
            position = operation["scene"]
            if type(position) is not int or not 1 <= position <= len(scene_ids):
                raise EfirError("Ollama указал номер вне списка доступных сцен.")
            operation["scene"] = scene_ids[position - 1]
        if any(isinstance(value, str) and len(value) > MODEL_TEXT_MAX[key]
               for key, value in operation.items() if key in MODEL_TEXT_MAX):
            raise EfirError("Текст операции превышает ограничение схемы Ollama.")
        if "seconds" in operation:
            value = operation.pop("seconds")
            if type(value) not in (int, float) or not 1 / FPS <= value <= MAX_FRAMES / FPS:
                raise EfirError("Длительность в секундах должна быть числом от 0,04 до 600.")
            operation["frames"] = frame_count(value)
    validate(project, data)
    if budget and (len(data["operations"]) != budget.count or any(
        op["op"] != "add" or (budget.seconds is not None and op["frames"] != frame_count(budget.seconds))
        or (budget.between and not 2 <= op.get("position", 0) <= len(scene_ids))
        or any(not op.get(field, "").strip() for field in budget.required_text)
        for op in data["operations"]
    )):
        raise EfirError("План не соблюдает явно заданное число, длительность или положение новых сцен.")
    if exact:
        target = scene_ids[exact.scene - 1]
        expected = [{"op": "edit", "scene": target, exact.field: exact.value}]
        if exact.voice:
            expected.append({"op": "voice", "scene": target})
        if data["operations"] != expected:
            raise EfirError("План не соответствует точной правке в кавычках.")
    return data


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
            position = fields.pop("position", None)
            added = snapshot.add(visual=None, **fields)
            if position is not None:
                snapshot.move(added.id, position)
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
        context = [{"number": i, "title": s.title[:100], "seconds": s.frames / FPS, "speech": s.speech[:280],
                    "caption": s.caption[:160], "visual_attached": s.visual is not None}
                   for i, s in enumerate(project.scenes[:24], 1)]
        budget = creation_budget(prompt, len(context))
        exact = literal_edit(prompt, project.scenes[:24])
        contract = schema(project.revision, len(context), budget, exact)
        response = self.request("/api/chat", {
            "model": name, "stream": False, "think": False, "keep_alive": 0,
            "format": contract,
            "options": {"temperature": 0.25, "seed": 42, "repeat_penalty": 1.12,
                        "num_ctx": 8192, "num_predict": 2000},
            "messages": [
                {"role": "system", "content": (
                    "Ты планировщик русскоязычного проекта EFIR. Верни только JSON с base_revision и operations. "
                    "Сначала определи действие из запроса. Придумать сценарий, создать или добавить новую сцену — "
                    "add для каждой новой сцены. Одна новая сцена — один add. Не копируй сцены из контекста. "
                    "Для add укажи title, seconds, короткую speech и короткий caption. "
                    "Если указано общее время, раздели его на количество сцен и проверь сумму. "
                    "При вставке укажи конечную position, считая с 1. По умолчанию add добавляет в конец. "
                    "edit меняет только прямо запрошенные поля: title название, speech реплика диктора, "
                    "caption субтитры, notes заметки, seconds длительность. Если речь меняется, "
                    "субтитры остаются прежними, если их не просили изменить. "
                    "Длительность всегда в секундах, без преобразований. В JSON пиши десятичную точку. "
                    "В edit, move, voice укажи scene равным number нужной сцены из scenes_in_order. "
                    "Исходная первая сцена — scene:1, вторая — scene:2. Эти номера неизменны в рамках ответа. "
                    "move ставит исходную сцену scene в новую position. При обмене проверь итоговый порядок. "
                    "Текст в кавычках копируй дословно в нужное поле. voice заказывает озвучку. "
                    "export заказывает черновой MP4, только если этого попросили. "
                    f"Текущая ревизия {project.revision}."
                )},
                {"role": "user", "content": encode({
                    "request": prompt, "scenes_in_order": context,
                    "required_new_scenes": budget.count if budget else None,
                    "required_seconds_per_new_scene": budget.seconds if budget else None,
                    "literal_edit": ({"scene": exact.scene, "field": exact.field, "value": exact.value,
                                      "then_voice": exact.voice} if exact else None),
                })},
            ],
        })
        try:
            data = json.loads(response["message"]["content"])
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise EfirError("Ollama не вернул машиночитаемый план.") from error
        return from_model(project, data, budget, exact)


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
