"""Command line and desktop entry points."""

import argparse
from pathlib import Path
import sys

from . import __version__
from .project import EfirError, FORMATS, Project, frame_count
from .render import Renderer, export_srt, seconds
from .store import StudioProject, migrate, open_project


def scene_options(parser, *, new):
    parser.add_argument("--title", required=new, help="название сцены")
    parser.add_argument("--visual", type=Path, required=new, help="клип или изображение")
    parser.add_argument("--duration", required=new, help="длительность в секундах, например 2.4")
    parser.add_argument("--start", help="начало фрагмента клипа в секундах")
    parser.add_argument("--fit", choices=("contain", "cover"), help="кадрирование")
    parser.add_argument("--source-audio", action=argparse.BooleanOptionalAction, default=None,
                        help="использовать звук видеоклипа")
    voices = parser.add_mutually_exclusive_group()
    voices.add_argument("--voice", type=Path, help="отдельная звуковая дорожка")
    if not new:
        voices.add_argument("--clear-voice", action="store_true", help="убрать отдельную дорожку")
    parser.add_argument("--caption", help="субтитр на всю сцену")
    parser.add_argument("--notes", help="заметки, источник, модель или промпт")
    parser.add_argument("--speech", help="реплика в проекте SQLite")


def parser():
    root = argparse.ArgumentParser(description="EFIR Studio · локальная сборка сцен")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="создать проект")
    init.add_argument("--title", required=True)
    init.add_argument("--aspect", choices=FORMATS, default="9:16")
    init.add_argument("--sqlite", action="store_true", help="формат проекта v0.2")
    add = commands.add_parser("add", help="добавить сцену")
    scene_options(add, new=True)
    edit = commands.add_parser("edit", help="изменить сцену")
    edit.add_argument("project", type=Path, help="каталог проекта")
    edit.add_argument("scene")
    scene_options(edit, new=False)
    move = commands.add_parser("move", help="переместить сцену")
    move.add_argument("project", type=Path, help="каталог проекта")
    move.add_argument("scene")
    move.add_argument("position", type=int, help="позиция, начиная с 1")
    remove = commands.add_parser("remove", help="убрать сцену из монтажа")
    remove.add_argument("project", type=Path, help="каталог проекта")
    remove.add_argument("scene")
    listing = commands.add_parser("list", help="вывести список сцен")
    export = commands.add_parser("export", help="собрать MP4")
    export.add_argument("--ffmpeg", help="путь к FFmpeg")
    export.add_argument("--draft", action="store_true", help="вдвое уменьшить разрешение")
    srt = commands.add_parser("srt", help="сохранить внешний файл субтитров")
    for command in (init, add, listing, export, srt):
        command.add_argument("project", type=Path, help="каталог проекта")
    for command in (export, srt):
        command.add_argument("output", type=Path)
        command.add_argument("--overwrite", action="store_true")
    desktop = commands.add_parser("desktop", help="открыть настольное приложение")
    desktop.add_argument("project", nargs="?", type=Path)
    studio_commands(commands)
    return root


def scene_fields(args, project):
    fields = {}
    for field in ("title", "fit", "source_audio", "caption", "notes"):
        value = getattr(args, field)
        if value is not None:
            fields[field] = value
    if args.speech is not None:
        require_studio(project)
        fields["speech"] = args.speech
    if args.duration is not None:
        fields["frames"] = frame_count(args.duration)
    if args.start is not None:
        fields["start_frame"] = frame_count(args.start, zero=True)
    if args.visual is not None:
        fields["visual"] = project.import_media(args.visual)
    if args.voice is not None:
        fields["voice"] = project.import_media(args.voice, voice=True)
    elif getattr(args, "clear_voice", False):
        fields["voice"] = None
    return fields


def run(args):
    if args.command == "desktop":
        from .desktop import launch
        launch(args.project)
        return
    if args.command == "init":
        format_class = StudioProject if args.sqlite else Project
        print(format_class.create(args.project, args.title, args.aspect).root)
        return
    if args.command == "migrate":
        print(migrate(args.project, args.destination).root)
        return
    project = open_project(args.project)
    match args.command:
        case "add" | "edit":
            if args.command == "edit":
                project.scene_index(args.scene)
            fields = scene_fields(args, project)
            scene = project.add(**fields) if args.command == "add" else project.update(args.scene, **fields)
            project.save()
            print(scene.id)
        case "move" | "remove":
            if args.command == "move":
                project.move(args.scene, args.position)
            else:
                project.scenes.pop(project.scene_index(args.scene))
            project.save()
        case "list":
            print(f"{project.title} · {project.aspect}")
            for order, scene in enumerate(project.scenes, 1):
                print(f"{order:>3}  {scene.id}  {seconds(scene.frames):>6} с  {scene.title}")
        case "export":
            result = Renderer(args.ffmpeg, report=lambda message: print(message, file=sys.stderr)).export(
                project, args.output, overwrite=args.overwrite, draft=args.draft
            )
            print(f"{result.path}\nКодирование: {result.encoded}; из кэша: {result.reused}.")
        case "srt":
            export_srt(project, args.output, overwrite=args.overwrite)
            print(args.output.resolve())
        case _:
            run_studio(args, require_studio(project))


def require_studio(project):
    if not isinstance(project, StudioProject):
        raise EfirError("Эта команда требует SQLite. Перенесите проект командой migrate.")
    return project


def studio_commands(commands):
    for name, help_text in {
        "migrate": "скопировать проект v0.1 в SQLite", "portable": "сохранить переносимый проект",
        "history": "показать ревизии", "restore": "восстановить ревизию",
        "background": "прикрепить фон или убрать его", "plan": "предложить план через Ollama",
        "apply": "подтвердить план", "reject": "отклонить план",
        "voice": "поставить озвучку в очередь", "queue-export": "поставить MP4 в очередь",
        "jobs": "показать очередь", "work": "выполнить одну задачу", "cancel": "отменить задачу",
        "retry": "повторить задачу",
    }.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument("project", type=Path)
        match name:
            case "migrate" | "portable":
                command.add_argument("destination", type=Path)
            case "restore":
                command.add_argument("revision", type=int)
            case "background":
                command.add_argument("audio", type=Path, nargs="?")
            case "plan":
                command.add_argument("text")
                command.add_argument("--model")
            case "apply" | "reject":
                command.add_argument("proposal")
            case "voice":
                command.add_argument("scene")
                command.add_argument("--regenerate", action="store_true")
            case "queue-export":
                command.add_argument("--draft", action="store_true")
            case "work":
                command.add_argument("--piper", action="store_true", help="использовать настроенный Piper")
            case "cancel" | "retry":
                command.add_argument("job")


def run_studio(args, project):
    import json
    import os

    from .assistant import Ollama, decision, preview_text, proposals, propose
    from .jobs import Queue, run_one
    from .tts import Piper

    queue = Queue(project)
    match args.command:
        case "portable":
            print(project.portable(args.destination))
        case "history":
            for revision in project.history():
                print(revision["id"], revision["created"], revision["reason"])
        case "restore":
            print(project.restore(args.revision).revision)
        case "background":
            project.background = project.import_media(args.audio, voice=True) if args.audio else None
            project.save("Фоновая дорожка")
        case "plan":
            model = args.model or os.environ.get("EFIR_OLLAMA_MODEL", "qwen3:4b")
            plan_id = propose(project, Ollama(model), args.text)
            operations = next(p for p in proposals(project) if p["id"] == plan_id)["operations"]
            print(plan_id)
            for operation in json.loads(operations):
                print(preview_text(project, operation))
        case "apply":
            pending = next((p for p in proposals(project) if p["id"] == args.proposal), None)
            tts = Piper.configured() if pending and any(
                op["op"] == "voice" for op in json.loads(pending["operations"])) else None
            revision, jobs = decision(project, args.proposal, accept=True, recipe=tts.recipe if tts else None)
            print(revision, *jobs)
        case "reject":
            decision(project, args.proposal, accept=False)
        case "voice":
            print(queue.enqueue("voice", scene_id=args.scene, recipe=Piper.configured().recipe,
                                regenerate=args.regenerate))
        case "queue-export":
            print(queue.enqueue("export", draft=args.draft))
        case "jobs":
            for job in queue.jobs():
                print(json.dumps(job, ensure_ascii=False))
        case "work":
            print(run_one(project, tts=Piper.configured() if args.piper else None) or "Нет доступных задач.")
        case "cancel":
            queue.cancel(args.job)
        case "retry":
            queue.retry(args.job)


def main(argv=None):
    try:
        run(parser().parse_args(argv))
    except (EfirError, OSError, ImportError) as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
