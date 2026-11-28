"""Compare applied Qwen3 plans to the fixed Russian acceptance corpus."""

import argparse
import json
from pathlib import Path
import re


def normalize(text):
    return text.strip().rstrip(".!?").casefold().replace("ё", "е")


def validate_case(record):
    if "error" in record:
        return False, record["error"]
    case = record["case"]
    before = record["before"]["scenes"]
    after = record["after"]["scenes"]
    existing_ids = [scene["id"] for scene in before]
    added = [scene for scene in after if scene["id"] not in existing_ids]
    old = [scene for scene in after if scene["id"] in existing_ids]
    unchanged = old == before
    kinds = sorted(job["kind"] for job in record["jobs"])
    operations = record.get("operations") or json.loads(record["raw"]["message"]["content"])["operations"]
    voiced = sorted(op["scene"] for op in operations if op["op"] == "voice")

    def edited(index, field, expected):
        return len(after) == len(before) and all(
            previous == current if n != index else (
                {**current, field: previous[field]} == previous
                and (expected(current[field]) if callable(expected) else current[field] == expected)
            ) for n, (previous, current) in enumerate(zip(before, after))
        )

    if case == 1:
        duration = sum(s["frames"] for s in added)
        valid = len(added) == 6 and 2200 <= duration <= 2300
        note = "6 сцен, 88–92 секунды. Связность проверяется по тексту."
    elif case == 2:
        valid = [s["frames"] for s in added] == [450, 450]
        note = "Две сцены по 18 секунд. Тема проверяется по тексту."
    elif case == 3:
        valid = len(added) == 1 and added[0]["frames"] == 100 and normalize(added[0]["speech"]) == "мы в эфире"
        note = "Новая сцена на 4 секунды с заданной репликой."
    elif case in (4, 5, 20):
        frames = {4: 150, 5: 80, 20: 275}[case]
        valid = len(added) == 1 and unchanged and added[0]["frames"] == frames
        if valid and case == 4:
            valid = added[0] in after[1:-1]
        if valid and case == 5:
            valid = added[0] == after[-1] and normalize(added[0]["title"]) == "технический перерыв"
        if valid and case == 20:
            valid = normalize(added[0]["title"]) == "коммутатор" and len(added[0]["speech"].split()) >= 3
        note = "Добавление с нужным таймингом, названием и позицией по запросу."
    elif case in (6, 7, 8, 9, 12, 13, 14, 18, 19):
        fields = {
            6: (1, "title", lambda s: normalize(s) == "несанкционированный сигнал"),
            7: (1, "speech", lambda s: normalize(s) == "не выключайте приемник"),
            8: (0, "frames", 200), 9: (2, "frames", 310),
            12: (1, "caption", lambda s: normalize(s) == "контрольный канал"),
            13: (2, "caption", lambda s: [normalize(line) for line in s.splitlines()] == ["конец эфира", "до завтра"]),
            14: (0, "notes", lambda s: bool(re.search(r"экран.*зелен", normalize(s)))),
            18: (2, "frames", 150),
            19: (1, "speech", lambda s: normalize(s) == "здесь никого нет"),
        }
        valid = edited(*fields[case])
        note = "Изменено только запрошенное поле, сохранены остальные поля и сцены."
    elif case in (10, 11):
        expected = (2, 0, 1) if case == 10 else (0, 2, 1)
        valid = after == [before[index] for index in expected]
        note = "Изменён порядок, поля сцен сохранены."
    else:
        valid = before == after
        note = "Поставлены только запрошенные задачи, сцены сохранены."
    expected_kinds = {15: ["voice"], 16: ["voice", "voice"], 17: ["export"], 18: ["export"], 19: ["voice"]}
    expected_voices = {15: [existing_ids[1]], 16: [existing_ids[0], existing_ids[-1]],
                       19: [existing_ids[1]]} if before else {}
    if (kinds != expected_kinds.get(case, []) or voiced != sorted(expected_voices.get(case, []))):
        return False, "Не совпал набор поставленных задач или целевых сцен."
    return bool(valid), note if valid else "Итог проекта отклоняется от ожидаемого."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    scores = []
    for artifact in sorted(args.directory.glob("plan-*.json")):
        record = json.loads(artifact.read_text(encoding="utf-8"))
        valid, note = validate_case(record)
        scores.append({"case": record["case"], "matched": valid, "note": note, "seconds": record["seconds"]})
    print(json.dumps({"matched": sum(case["matched"] for case in scores), "cases": scores},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
