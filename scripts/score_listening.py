"""Score two independent blind transcriptions using predeclared reference normalization."""

import argparse
import csv
import json
import re
import statistics
from pathlib import Path


EQUIVALENTS = (
    (r"\b(?:33[,.]3|тридцать три и три)\s*(?:мгц|мегагерца)\b", "тридцать три целых три десятых мегагерца"),
    (r"\b03:33\b", "три часа тридцать три минуты"),
    (r"\b(?:12[,.]5|двенадцать и пять)\b", "двенадцать целых пять десятых"),
    (r"\bвэ гэ тэ эр ка\b", "вгтрк"),
    (r"\bэйч ди эм ай\b", "hdmi"),
    (r"\bэм чэ эс\b", "мчс"),
)


def words(text):
    text = text.casefold().replace("ё", "е")
    for pattern, replacement in EQUIVALENTS:
        text = re.sub(pattern, replacement, text)
    return re.findall(r"[а-яa-z0-9]+", text)


def edit_distance(reference, observed):
    previous = list(range(len(observed) + 1))
    for index, word in enumerate(reference, 1):
        current = [index]
        for position, heard in enumerate(observed, 1):
            current.append(min(previous[position] + 1, current[-1] + 1,
                               previous[position - 1] + (word != heard)))
        previous = current
    return previous[-1]


def score(references, forms):
    listeners = []
    for form in forms:
        with form.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        if len(rows) != len(references) or {row["id"] for row in rows} != references.keys():
            raise ValueError(f"{form}: требуется по одной оценке и транскрипции для каждой записи.")
        assessed = []
        for row in rows:
            reference = words(references[row["id"]])
            naturalness = int(row["naturalness"])
            if not row["transcript"].strip() or not 1 <= naturalness <= 5:
                raise ValueError(f"{form}: пустая транскрипция или оценка вне 1–5.")
            assessed.append({"id": row["id"], "errors": edit_distance(reference, words(row["transcript"])),
                             "words": len(reference), "naturalness": naturalness})
        errors = sum(item["errors"] for item in assessed)
        total = sum(item["words"] for item in assessed)
        listeners.append({"wer": errors / total, "intelligibility_pct": max(0, 1 - errors / total) * 100,
                          "median_naturalness": statistics.median(item["naturalness"] for item in assessed),
                          "items": assessed})
    return listeners


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("references", type=Path)
    parser.add_argument("forms", nargs=2, type=Path)
    args = parser.parse_args()
    if args.forms[0].resolve() == args.forms[1].resolve():
        parser.error("Укажите два отдельных бланка независимых слушателей.")
    references = json.loads(args.references.read_text(encoding="utf-8"))
    print(json.dumps(score(references, args.forms), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
