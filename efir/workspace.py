"""Chat-first Tk workspace using the same project and job services as the CLI."""

import json
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, simpledialog, ttk

from .assistant import Ollama, decision, preview_text, proposals, propose
from .desktop import Studio
from .jobs import ACTIVE, Queue as ProjectQueue, run_one
from .project import AUDIO, EfirError
from .store import StudioProject, export_file, migrate
from .tts import Piper


JOB_STATES = dict(zip(
    ("queued", "running", "succeeded", "failed", "cancel-requested", "cancelled"),
    ("В очереди", "Выполняется", "Готово", "Ошибка", "Отмена…", "Отменено"),
))


class Workspace(Studio):
    project_type = StudioProject

    def __init__(self, window):
        self.recent = []
        self.work = Queue()
        self.busy = False
        self.closing = False
        self.task_rows = {}
        self.plans = []
        super().__init__(window)
        window.geometry("1320x820")
        window.minsize(1060, 690)
        window.after(350, self.service)

    def scene_panel(self, body):
        panes = ttk.Panedwindow(body, orient="horizontal")
        panes.pack(fill="both", expand=True)
        panels = [ttk.Frame(panes, padding=8) for _ in range(3)]
        for panel, weight in zip(panels, (0, 3, 2)):
            panes.add(panel, weight=weight)
        library, chat, details = panels
        ttk.Label(library, text="Проекты").pack(anchor="w")
        self.library = tk.Listbox(library, width=20, height=5, exportselection=False)
        self.library.pack(fill="x", pady=6)
        self.library.bind("<Double-1>", self.open_recent)
        ttk.Button(library, text="Мигрировать v0.1", command=self.migrate_project).pack(fill="x", pady=2)
        ttk.Button(library, text="Переносимая копия", command=self.backup).pack(fill="x", pady=2)
        ttk.Label(library, text="Ревизии").pack(anchor="w", pady=(14, 0))
        self.revisions = tk.Listbox(library, width=20, exportselection=False)
        self.revisions.pack(fill="both", expand=True, pady=6)
        self.revision_ids = []
        ttk.Button(library, text="Восстановить", command=self.restore).pack(fill="x")

        ttk.Label(chat, text="Сценарий и ассистент").pack(anchor="w")
        self.conversation = tk.Text(chat, state="disabled", wrap="word", width=35, height=8, padx=10, pady=8)
        self.conversation.pack(fill="both", expand=True, pady=6)
        self.prompt = tk.Text(chat, wrap="word", width=30, height=3)
        self.prompt.pack(fill="x", pady=(3, 6))
        row = ttk.Frame(chat)
        row.pack(fill="x")
        self.ask_button = self._button(row, "Предложить план", self.ask)
        self.apply_button = self._button(row, "Применить", lambda: self.resolve_plan(True))
        self.reject_button = self._button(row, "Отклонить", lambda: self.resolve_plan(False))
        model = ttk.Frame(chat, padding=(0, 8, 0, 6))
        model.pack(fill="x")
        ttk.Label(model, text="Модель Ollama").pack(side="left")
        self.model = ttk.Entry(model, width=19)
        self.model.insert(0, os.environ.get("EFIR_OLLAMA_MODEL", "qwen3:4b"))
        self.model.pack(side="left", padx=6)

        ttk.Label(details, text="Сцены").pack(anchor="w")
        table = ttk.Frame(details, height=200)
        table.pack(fill="both", expand=True, pady=6)
        audio = ttk.Frame(details)
        audio.pack(fill="x", pady=(0, 9))
        self._button(audio, "Озвучить", self.voice)
        self._button(audio, "Слушать", self.listen)
        self._button(audio, "Фон", self.background)
        ttk.Label(details, text="Задачи").pack(anchor="w")
        self.tasks = ttk.Treeview(details, columns=("kind", "state", "progress"), show="headings", height=4)
        for name, label, width in (("kind", "Задача", 84), ("state", "Статус", 88), ("progress", "Ход", 170)):
            self.tasks.heading(name, text=label)
            self.tasks.column(name, width=width, stretch=name == "progress")
        self.tasks.pack(fill="x", pady=6)
        controls = ttk.Frame(details)
        controls.pack(fill="x")
        self._button(controls, "Отмена", lambda: self.task_action("cancel"))
        self._button(controls, "Повтор", lambda: self.task_action("retry"))
        self._button(controls, "Открыть результат", lambda: self.task_action("open"))
        return table

    def studio_project(self):
        if not isinstance(self.project, StudioProject):
            raise EfirError("Откройте проект SQLite или перенесите исходный проект v0.1.")
        return self.project

    def refresh(self, scene_id=None):
        super().refresh(scene_id)
        for name, width in (("title", 150), ("time", 95), ("audio", 80)):
            self.scenes.column(name, width=width)
        is_studio = isinstance(self.project, StudioProject)
        self.revisions.delete(0, "end")
        self.revision_ids = []
        self.plans = []
        self.conversation.configure(state="normal")
        self.conversation.delete("1.0", "end")
        try:
            if is_studio:
                for entry in self.project.history():
                    self.revision_ids.append(entry["id"])
                    self.revisions.insert("end", f"{entry['id']} · {entry['reason']}")
                self.plans = proposals(self.project)
                for plan in self.plans[-8:]:
                    base = StudioProject.open(self.project.root, plan["revision"])
                    self.conversation.insert("end", f"Вы: {plan['request']}\n{plan['model']} · "
                                             f"{plan['state']} · ревизия {plan['revision']}\n")
                    for operation in json.loads(plan["operations"]):
                        self.conversation.insert("end", preview_text(base, operation) + "\n")
                    self.conversation.insert("end", "\n")
        finally:
            self.conversation.see("end")
            self.conversation.configure(state="disabled")
        self.chat_controls()

    def chat_controls(self):
        self.enabled(self.ask_button, isinstance(self.project, StudioProject) and not self.busy)
        plan = self.pending()
        self.enabled(self.apply_button, plan and plan["revision"] == self.project.revision and not self.busy)
        self.enabled(self.reject_button, plan and not self.busy)

    def pending(self):
        return next((plan for plan in reversed(self.plans) if plan["state"] == "pending"), None)

    def set_project(self, project):
        super().set_project(project)
        if project.root not in self.recent:
            self.recent.append(project.root)
            self.library.insert("end", project.title)
        self.task_rows = {}
        self.tasks.delete(*self.tasks.get_children())

    def open_recent(self, _event):
        selected = self.library.curselection()
        if selected:
            self.open(self.recent[selected[0]])

    def migrate_project(self):
        source = filedialog.askdirectory(title="Каталог проекта v0.1", parent=self.window)
        if source:
            destination = self.destination("Название новой папки проекта SQLite")
            if destination:
                self.start_work(lambda: migrate(source, destination), kind="open")

    def destination(self, prompt):
        parent = filedialog.askdirectory(title="Каталог назначения", parent=self.window)
        if parent:
            name = simpledialog.askstring("EFIR Studio", prompt, parent=self.window)
            if name and name.strip() not in ("", ".", "..") and not any(char in name for char in '<>:"/\\|?*'):
                return Path(parent) / name.strip()
        return None

    def backup(self):
        try:
            project = self.studio_project()
            destination = self.destination("Название переносимой копии")
            if destination:
                self.start_work(lambda: project.portable(destination), kind="backup")
        except (EfirError, OSError) as error:
            self.error(error)

    def restore(self):
        selected = self.revisions.curselection()
        if selected:
            try:
                self.project = self.studio_project().restore(self.revision_ids[selected[0]])
                self.refresh()
            except (EfirError, OSError) as error:
                self.error(error)

    def start_work(self, operation, *, kind="refresh"):
        if self.busy:
            self.status.set("Дождитесь завершения текущей операции.")
            return
        root = self.project.root if self.project else None
        self.busy = True
        self.chat_controls()

        def execute():
            try:
                self.work.put((root, kind, operation()))
            except (EfirError, OSError, ValueError) as error:
                self.work.put((root, "error", error))

        Thread(target=execute, daemon=True).start()

    def ask(self):
        try:
            project = self.studio_project()
            engine = Ollama(self.model.get().strip())
            request = self.prompt.get("1.0", "end-1c")
            self.status.set("Ollama готовит предложение…")
            self.start_work(lambda: propose(project, engine, request), kind="plan")
        except (EfirError, OSError) as error:
            self.error(error)

    def resolve_plan(self, accept):
        try:
            project, plan = self.studio_project(), self.pending()
            if plan:
                def apply():
                    recipe = None
                    if accept and any(op["op"] == "voice" for op in json.loads(plan["operations"])):
                        recipe = Piper.configured().recipe
                    return decision(project, plan["id"], accept=accept, recipe=recipe)
                self.start_work(apply)
        except (EfirError, OSError) as error:
            self.error(error)

    def service(self):
        try:
            root, kind, payload = self.work.get_nowait()
        except Empty:
            pass
        else:
            self.busy = False
            if kind == "error":
                self.status.set(str(payload))
            elif kind == "open":
                self.set_project(payload)
            elif kind == "backup":
                self.status.set(f"Копия сохранена в {payload}")
            elif self.project and self.project.root == root:
                try:
                    self.reload()
                    if kind == "plan":
                        self.prompt.delete("1.0", "end")
                        self.status.set("Просмотрите план и подтвердите правки.")
                except (EfirError, OSError) as error:
                    self.status.set(str(error))
            self.chat_controls()

        if self.closing and not self.busy:
            super().close()
            return
        try:
            if isinstance(self.project, StudioProject):
                self.show_jobs()
        except (EfirError, OSError) as error:
            self.status.set(str(error))
        self.window.after(350, self.service)

    def reload(self):
        selected = self.selected()
        self.project = StudioProject.open(self.project.root)
        self.refresh(selected if any(scene.id == selected for scene in self.project.scenes) else None)

    def show_jobs(self):
        project = self.project
        jobs = ProjectQueue(project).jobs()
        changed = self.task_rows != {job["id"]: job for job in jobs}
        if changed:
            self.task_rows = {job["id"]: job for job in jobs}
            for job in reversed(jobs):
                job_id = job["id"]
                values = ("Озвучка" if job["kind"] == "voice" else "Экспорт",
                          JOB_STATES[job["state"]], job["error"] or job["progress"])
                if self.tasks.exists(job_id):
                    self.tasks.item(job_id, values=values)
                else:
                    self.tasks.insert("", 0, iid=job_id, values=values)
        pending = [job for job in jobs if job["state"] in ACTIVE]
        if pending and not self.busy and not self.closing:
            def work():
                try:
                    engine = Piper.configured() if pending[-1]["kind"] == "voice" else None
                except EfirError:
                    engine = None
                return run_one(project, tts=engine)
            self.start_work(work)

    def task_action(self, action):
        try:
            project = self.studio_project()
            selected = self.tasks.selection()
            if not selected:
                return
            queue, job = ProjectQueue(project), self.task_rows[selected[0]]
            if action == "cancel":
                queue.cancel(job["id"])
            elif action == "retry":
                queue.retry(job["id"])
            elif job["state"] == "succeeded":
                result = json.loads(job["result"])
                self.open_media(project.asset(result["asset"]) if job["kind"] == "voice"
                                else export_file(project, result))
            else:
                self.status.set(job["error"] or "Результат ещё не готов.")
        except (EfirError, OSError) as error:
            self.error(error)

    def voice(self):
        try:
            project, scene = self.studio_project(), self.selected()
            if scene:
                self.start_work(lambda: ProjectQueue(project).enqueue("voice", scene_id=scene,
                                                                      recipe=Piper.configured().recipe))
        except (EfirError, OSError) as error:
            self.error(error)

    def listen(self):
        if self.project and self.selected():
            try:
                scene = self.project.scenes[self.project.scene_index(self.selected())]
                if scene.voice:
                    self.open_media(self.project.asset(scene.voice))
            except (EfirError, OSError) as error:
                self.error(error)

    def background(self):
        try:
            project = self.studio_project()
            audio = filedialog.askopenfilename(parent=self.window, title="Выберите фоновую дорожку",
                                               filetypes=[("Аудио", tuple("*" + ext for ext in AUDIO))])
            if audio:
                def attach():
                    project.background = project.import_media(audio, voice=True)
                    project.save("Фоновая дорожка")
                self.start_work(attach)
        except (EfirError, OSError) as error:
            self.error(error)

    def render(self):
        if not isinstance(self.project, StudioProject):
            super().render()
            return
        try:
            ProjectQueue(self.project).enqueue("export", draft=self.draft.get())
            self.status.set("Экспорт поставлен в очередь.")
        except (EfirError, OSError) as error:
            self.error(error)

    def close(self):
        if self.busy:
            self.closing = True
            self.status.set("Окно закроется после текущей операции. Очередь сохранится.")
        else:
            super().close()
