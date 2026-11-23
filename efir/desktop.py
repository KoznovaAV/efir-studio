"""Tk interface for editing a local scene list and exporting asynchronously."""

import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .project import AUDIO, FORMATS, IMAGE, VIDEO, EfirError, Project, frame_count
from .render import RenderCancelled, Renderer, export_srt, seconds


class SceneDialog(simpledialog.Dialog):
    def __init__(self, parent, project, scene=None):
        self.project = project
        self.scene = scene
        self.visual = None
        self.voice = None
        self.clear_voice = False
        super().__init__(parent, "Изменить сцену" if scene else "Добавить сцену")

    def body(self, form):
        form.columnconfigure(1, weight=1)
        scene = self.scene
        self.title_field = tk.StringVar(value=scene.title if scene else "")
        self.duration = tk.StringVar(value=seconds(scene.frames) if scene else "3")
        self.start = tk.StringVar(value=seconds(scene.start_frame) if scene else "0")
        self.fit = tk.StringVar(value=scene.fit if scene else "contain")
        self.source_audio = tk.BooleanVar(value=scene.source_audio if scene else False)
        self.visual_label = tk.StringVar(value="Исходник проекта" if scene else "Выберите файл")
        self.voice_label = tk.StringVar(value="Дорожка проекта" if scene and scene.voice else "Без отдельной дорожки")
        first = ttk.Entry(form, textvariable=self.title_field, width=47)
        self._row(form, 0, "Название", first)
        for row, heading, label, is_voice in (
            (1, "Клип или изображение", self.visual_label, False),
            (2, "Озвучка", self.voice_label, True),
        ):
            box = ttk.Frame(form)
            ttk.Label(box, textvariable=label, width=32).pack(side="left")
            ttk.Button(box, text="Выбрать", command=lambda value=is_voice: self._choose(value)).pack(side="left")
            if is_voice:
                ttk.Button(box, text="Убрать", command=self._remove_voice).pack(side="left", padx=(4, 0))
            self._row(form, row, heading, box)
        self._row(form, 3, "Длительность, с", ttk.Entry(form, textvariable=self.duration))
        self._row(form, 4, "Начало клипа, с", ttk.Entry(form, textvariable=self.start))
        self._row(form, 5, "Кадрирование", ttk.Combobox(
            form, state="readonly", textvariable=self.fit, values=("contain", "cover")
        ))
        self._row(form, 6, "", ttk.Checkbutton(
            form, text="Использовать звук клипа", variable=self.source_audio
        ))
        self.caption = tk.Text(form, height=3, width=40, wrap="word")
        self.notes = tk.Text(form, height=4, width=40, wrap="word")
        for row, label, widget, text in (
            (7, "Субтитр", self.caption, scene.caption if scene else ""),
            (8, "Источник и заметки", self.notes, scene.notes if scene else ""),
        ):
            widget.insert("1.0", text)
            self._row(form, row, label, widget)
        return first

    @staticmethod
    def _row(form, row, title, widget):
        ttk.Label(form, text=title).grid(row=row, column=0, sticky="nw", padx=(8, 12), pady=5)
        widget.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=5)

    def _choose(self, voice):
        formats = AUDIO if voice else {*VIDEO, *IMAGE}
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("Поддерживаемые медиа", tuple(f"*{suffix}" for suffix in sorted(formats)))]
        )
        if path and voice:
            self.voice = Path(path)
            self.clear_voice = False
            self.voice_label.set(self.voice.name[:30])
        elif path:
            self.visual = Path(path)
            self.visual_label.set(self.visual.name[:30])
            if self.visual.suffix.lower() in IMAGE:
                self.start.set("0")
                self.source_audio.set(False)

    def _remove_voice(self):
        self.voice = None
        self.clear_voice = True
        self.voice_label.set("Без отдельной дорожки")

    def validate(self):
        if not self.title_field.get().strip() or not (self.visual or self.scene):
            messagebox.showerror("Сцена", "Укажите название и выберите клип или изображение.", parent=self)
            return False
        try:
            fields = {
                "title": self.title_field.get().strip(),
                "frames": frame_count(self.duration.get().replace(",", ".")),
                "start_frame": frame_count(self.start.get().replace(",", "."), zero=True),
                "fit": self.fit.get(),
                "source_audio": self.source_audio.get(),
                "caption": self.caption.get("1.0", "end-1c"),
                "notes": self.notes.get("1.0", "end-1c"),
            }
            visual = self.visual if self.visual else Path(self.scene.visual)
            if visual.suffix.lower() in IMAGE and (fields["start_frame"] or fields["source_audio"]):
                raise EfirError("Для изображения сбросьте начальную позицию и исходный звук.")
            if self.visual:
                fields["visual"] = self.project.import_media(self.visual)
            if self.voice:
                fields["voice"] = self.project.import_media(self.voice, voice=True)
            elif self.clear_voice:
                fields["voice"] = None
            self.result = fields
        except (EfirError, OSError) as error:
            messagebox.showerror("Сцена", str(error), parent=self)
            return False
        return True


class Studio:
    def __init__(self, window):
        self.window = window
        self.project = None
        self.cancel = None
        self.updates = Queue()
        self.last_output = None
        self.status = tk.StringVar(value="Создайте проект или откройте существующий.")
        self.heading = tk.StringVar(value="EFIR Studio")
        self.description = tk.StringVar()
        self.draft = tk.BooleanVar(value=True)
        window.title("EFIR Studio")
        window.geometry("940x600")
        window.minsize(820, 520)
        window.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(window, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, textvariable=self.heading, font=("TkDefaultFont", 19, "bold")).pack(anchor="w")
        bar = ttk.Frame(body, padding=(0, 14, 0, 12))
        bar.pack(fill="x")
        self.new_button = self._button(bar, "Новый проект", self.create)
        self.open_button = self._button(bar, "Открыть проект", self.open)
        self.add_button = self._button(bar, "Добавить сцену", self.edit)
        self.export_button = self._button(bar, "Экспорт MP4", self.render)
        self.srt_button = self._button(bar, "Экспорт SRT", self.srt)
        ttk.Checkbutton(bar, text="Черновое разрешение", variable=self.draft).pack(side="right")

        table = ttk.Frame(body)
        table.pack(fill="both", expand=True)
        self.scenes = ttk.Treeview(table, columns=("order", "title", "time", "audio"), show="headings", selectmode="browse")
        for name, title, width in (("order", "№", 46), ("title", "Сцена", 430),
                                   ("time", "Длительность", 125), ("audio", "Звук", 140)):
            self.scenes.heading(name, text=title)
            self.scenes.column(name, width=width, stretch=name == "title")
        scroll = ttk.Scrollbar(table, command=self.scenes.yview)
        self.scenes.configure(yscrollcommand=scroll.set)
        self.scenes.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.scenes.bind("<<TreeviewSelect>>", lambda _event: self.selection_changed())
        self.scenes.bind("<Double-1>", lambda _event: self.edit_selected())
        self.scenes.bind("<Return>", lambda _event: self.edit_selected())

        actions = ttk.Frame(body, padding=(0, 10, 0, 8))
        actions.pack(fill="x")
        self.edit_button = self._button(actions, "Изменить", self.edit_selected)
        self.up_button = self._button(actions, "Выше", lambda: self.move(-1))
        self.down_button = self._button(actions, "Ниже", lambda: self.move(1))
        self.play_button = self._button(actions, "Открыть исходник", self.open_visual)
        self.remove_button = self._button(actions, "Убрать сцену", self.remove)
        ttk.Label(body, textvariable=self.description, wraplength=760).pack(anchor="w", pady=(0, 8))
        ttk.Separator(body).pack(fill="x")
        footer = ttk.Frame(body, padding=(0, 12, 0, 0))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, wraplength=530).pack(side="left", fill="x", expand=True)
        self.cancel_button = self._button(footer, "Отменить экспорт", self.stop)
        self.output_button = self._button(footer, "Открыть MP4", self.open_output)
        self.refresh()

    @staticmethod
    def _button(frame, label, callback):
        button = ttk.Button(frame, text=label, command=callback)
        button.pack(side="left", padx=(0, 6))
        return button

    @staticmethod
    def enabled(button, enabled):
        button.state(["!disabled" if enabled else "disabled"])

    def error(self, error):
        messagebox.showerror("EFIR Studio", str(error), parent=self.window)

    def create(self):
        parent = filedialog.askdirectory(title="В какой папке создать проект?", parent=self.window)
        if not parent:
            return
        name = simpledialog.askstring("Новый проект", "Название проекта и его папки", parent=self.window)
        if not name:
            return
        name = name.strip()
        if not name or name in (".", "..") or any(char in name for char in '<>:"/\\|?*'):
            self.error("Название должно подходить для имени папки.")
            return
        aspect = simpledialog.askstring("Формат проекта", "9:16, 16:9 или 1:1",
                                        initialvalue="9:16", parent=self.window)
        if aspect is None:
            return
        try:
            self.set_project(Project.create(Path(parent) / name, name, aspect.strip()))
        except (EfirError, OSError) as error:
            self.error(error)

    def open(self, path=None):
        path = path or filedialog.askdirectory(title="Выберите папку проекта", parent=self.window)
        if path:
            try:
                self.set_project(Project.open(path))
            except (EfirError, OSError) as error:
                self.error(error)

    def set_project(self, project):
        self.project = project
        self.last_output = None
        self.status.set("Проект открыт." if project.scenes else "Добавьте первый клип или изображение.")
        self.refresh()

    def selected(self):
        selection = self.scenes.selection()
        return selection[0] if selection else None

    def refresh(self, scene_id=None):
        self.scenes.delete(*self.scenes.get_children())
        if self.project:
            self.heading.set(f"{self.project.title} · {self.project.aspect}")
            for index, scene in enumerate(self.project.scenes, 1):
                audio = "Озвучка" if scene.voice else "Тишина"
                if scene.source_audio:
                    audio = "Клип + озвучка" if scene.voice else "Клип"
                self.scenes.insert("", "end", iid=scene.id,
                                   values=(index, scene.title, seconds(scene.frames) + " с", audio))
            if scene_id:
                self.scenes.selection_set(scene_id)
                self.scenes.focus(scene_id)
                self.scenes.see(scene_id)
        self.selection_changed()

    def selection_changed(self):
        idle = self.cancel is None
        selected = self.selected()
        has_scenes = bool(self.project and self.project.scenes)
        self.description.set("")
        index = -1
        if selected and has_scenes:
            index = self.project.scene_index(selected)
            scene = self.project.scenes[index]
            details = " · ".join((f"Начало {seconds(scene.start_frame)} с", scene.fit,
                                  *(" ".join(text.split()) for text in (scene.caption, scene.notes) if text)))
            self.description.set(details[:220])
        for button, enabled in (
            (self.new_button, idle), (self.open_button, idle), (self.add_button, idle and self.project),
            (self.export_button, idle and has_scenes), (self.srt_button, idle and has_scenes),
            (self.edit_button, idle and selected), (self.play_button, idle and selected),
            (self.remove_button, idle and selected), (self.up_button, idle and index > 0),
            (self.down_button, idle and index >= 0 and index < len(self.scenes.get_children()) - 1),
            (self.cancel_button, not idle), (self.output_button, idle and self.last_output),
        ):
            self.enabled(button, enabled)

    def save_change(self, change):
        original = list(self.project.scenes)
        try:
            scene_id = change()
            self.project.save()
        except (EfirError, OSError) as error:
            self.project.scenes = original
            self.error(error)
            return
        self.status.set("Проект сохранён.")
        self.refresh(scene_id)

    def edit(self, scene_id=None):
        scene = self.project.scenes[self.project.scene_index(scene_id)] if scene_id else None
        fields = SceneDialog(self.window, self.project, scene).result
        if fields is not None:
            self.save_change(lambda: (self.project.update(scene_id, **fields) if scene_id
                                      else self.project.add(**fields)).id)

    def edit_selected(self):
        if self.selected() and self.cancel is None:
            self.edit(self.selected())

    def move(self, direction):
        selected = self.selected()
        def change():
            self.project.move(selected, self.project.scene_index(selected) + 1 + direction)
            return selected
        self.save_change(change)

    def remove(self):
        selected = self.selected()
        if messagebox.askyesno("Убрать сцену", "Убрать сцену из монтажа?", parent=self.window):
            def change():
                self.project.scenes.pop(self.project.scene_index(selected))
            self.save_change(change)

    def choose_output(self, suffix):
        target = filedialog.asksaveasfilename(
            parent=self.window, defaultextension=suffix, filetypes=[(suffix.upper()[1:], "*" + suffix)]
        )
        return Path(target) if target else None

    def render(self):
        target = self.choose_output(".mp4")
        if target is None:
            return
        self.cancel = Event()
        project = self.project
        draft, overwrite = self.draft.get(), target.exists()
        self.selection_changed()
        self.status.set("Подготовка экспорта…")

        def job():
            try:
                renderer = Renderer(cancel=self.cancel, report=lambda message: self.updates.put(("progress", message)))
                result = renderer.export(project, target, overwrite=overwrite, draft=draft)
                self.updates.put(("done", result))
            except RenderCancelled as error:
                self.updates.put(("cancel", error))
            except (EfirError, OSError, ValueError) as error:
                self.updates.put(("error", error))

        Thread(target=job, daemon=True).start()
        self.window.after(100, self.poll)

    def poll(self):
        try:
            while True:
                kind, data = self.updates.get_nowait()
                if kind == "progress":
                    self.status.set(data)
                    continue
                self.cancel = None
                if kind == "done":
                    self.last_output = data.path
                    self.status.set(f"MP4 готов. Закодировано: {data.encoded}, из кэша: {data.reused}.")
                elif kind == "error":
                    self.status.set("Экспорт завершился ошибкой.")
                    self.error(data)
                else:
                    self.status.set(str(data))
                self.selection_changed()
                return
        except Empty:
            self.window.after(100, self.poll)

    def stop(self):
        if self.cancel is not None:
            self.cancel.set()
            self.status.set("Отменяем экспорт…")

    def srt(self):
        target = self.choose_output(".srt")
        if target is not None:
            try:
                export_srt(self.project, target, overwrite=target.exists())
                self.status.set("Файл SRT сохранён.")
            except (EfirError, OSError) as error:
                self.error(error)

    def open_visual(self):
        scene = self.project.scenes[self.project.scene_index(self.selected())]
        try:
            self.open_media(self.project.asset(scene.visual))
        except (EfirError, OSError) as error:
            self.error(error)

    def open_output(self):
        self.open_media(self.last_output)

    def open_media(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except OSError as error:
            self.error(error)

    def close(self):
        if self.cancel is not None:
            self.stop()
            return
        self.window.destroy()


def launch(project=None):
    try:
        window = tk.Tk()
    except tk.TclError as error:
        raise EfirError(f"Не удалось открыть настольное окно: {error}") from error
    studio = Studio(window)
    if project is not None:
        window.after_idle(lambda: studio.open(project))
    window.mainloop()
