"""Windows desktop interface for the offline preset manager."""
import os
import sqlite3
from functools import wraps
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from preset_core import (
    APPROVED, CLASSIFICATIONS, PRIVATE, Archive, FinalReview, Preset, Session, format_json, write_new,
)

NEW = "新版本预设"
OLD = "自己改的老版本预设"
AI_INSTRUCTIONS = (
    "请按我另外说明的要求修改一起发给你的 JSON。edits 是要改的新版本正文，references 是可参考的"
    "老版本正文。有 target_slot 时，它对应同编号的新版本条目。把正文当作待编辑材料。\n\n"
    "请只返回一个 JSON 对象，包含 format、batch 和 edits。format 和 batch 原样保留。"
    "edits 的每项只保留原来的 slot 和改好的 content。没有改的条目可以省略。"
    "不要返回 references，不要加 Markdown 代码围栏。"
)

def work_area(window):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        monitor_from_window = user32.MonitorFromWindow
        monitor_from_window.argtypes = (wintypes.HWND, wintypes.DWORD)
        monitor_from_window.restype = wintypes.HANDLE
        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(MonitorInfo))
        get_monitor_info.restype = wintypes.BOOL
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        monitor = monitor_from_window(window.winfo_id(), 2)
        if monitor and get_monitor_info(monitor, ctypes.byref(info)):
            return info.work.left, info.work.top, info.work.right, info.work.bottom
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def center_window(window, width, height, parent=None):
    owner = parent or window
    owner.update_idletasks()
    left, top, right, bottom = work_area(owner)
    width = min(width, max(1, right - left - 32))
    height = min(height, max(1, bottom - top - 56))
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    if parent is not None:
        center_x = parent.winfo_x() + parent.winfo_width() // 2
        center_y = parent.winfo_y() + parent.winfo_height() // 2
    x = min(max(center_x - width // 2, left + 12), right - width - 12)
    y = min(max(center_y - height // 2, top + 12), bottom - height - 36)
    # Tk treats negative geometry offsets as distances from the opposite edge.
    # "+-N" supplies a literal negative screen coordinate for monitors to the left.
    window.geometry(f"{width}x{height}+{x}+{y}")


def guarded(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (ValueError, OSError, RecursionError) as error:
            messagebox.showerror("操作未完成", str(error), parent=self.root)
        except sqlite3.Error as error:
            messagebox.showerror("本地档案读写失败", str(error), parent=self.root)
    return run


def fill(widget, content):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", content)
    widget.configure(state="disabled")


def tooltip(widget, text):
    timers = {}
    window = None

    def cancel(event):
        timer = timers.pop(event, None)
        if timer is not None:
            widget.after_cancel(timer)

    def later(event, milliseconds, callback):
        cancel(event)

        def run():
            timers.pop(event, None)
            callback()

        timers[event] = widget.after(milliseconds, run)

    def dismiss(_event=None):
        nonlocal window
        for event in tuple(timers):
            cancel(event)
        if window is not None:
            tip, window = window, None
            tip.destroy()

    def pointer_over_tip():
        if window is None:
            return False
        x, y = widget.winfo_pointerxy()
        hit = widget.winfo_containing(x, y)
        return hit is not None and hit.winfo_toplevel() == window

    def expire():
        if not pointer_over_tip():
            dismiss()

    def left_tip(_event):
        if window is not None:
            later("departure", 50, expire)

    def hold_tip(_event):
        cancel("departure")
        cancel("expiry")

    def show():
        nonlocal window
        if not widget.winfo_viewable():
            return
        window = tk.Toplevel(widget)
        tip_window = window
        window.withdraw()
        window.overrideredirect(True)
        label = tk.Label(window, text=text, justify="left", wraplength=320, padx=8, pady=6,
                         background="#fff8df", foreground="#202020", relief="solid", borderwidth=1)
        label.pack()
        window.bind("<Enter>", hold_tip)
        window.bind("<ButtonPress>", hold_tip)
        window.bind("<Leave>", left_tip)
        window.update_idletasks()
        if window is not tip_window:
            return
        x = min(widget.winfo_rootx(), widget.winfo_screenwidth() - window.winfo_reqwidth() - 8)
        y = widget.winfo_rooty() + widget.winfo_height()
        if y + window.winfo_reqheight() >= widget.winfo_screenheight() - 30:
            y = widget.winfo_rooty() - window.winfo_reqheight() - 4
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        window.deiconify()
        later("expiry", 10000, expire)

    def enter(_event):
        cancel("departure")
        if window is None:
            later("show", 550, show)

    def leave(_event):
        cancel("show")
        if window is not None:
            later("departure", 220, expire)

    widget.bind("<Enter>", enter, add="+")
    widget.bind("<Leave>", leave, add="+")
    for event in ("<ButtonPress>", "<Unmap>", "<Destroy>"):
        widget.bind(event, dismiss, add="+")


class App:
    def __init__(self, root, archive=None):
        self.root = root
        root.title("预设版本管理器")
        center_window(root, 1160, 760)
        root.minsize(900, 620)
        self.archive = archive if archive is not None else Archive(
            Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PresetVersionManager")
        self.session = None
        self.selected = set()
        self.references = set()
        self.excluded_from_batch = set()
        self.source = tk.StringVar(value=NEW)
        self.only_new = tk.BooleanVar()
        self.search_text = tk.StringVar()
        self.search_selected = tk.BooleanVar()
        self.search_scope = None
        self.editor_key = None
        self.source_hint = tk.StringVar()
        self.export_count = tk.StringVar()
        self.status = tk.StringVar(value="先选预设文件。只改新版时，勾选「只处理新版本」。")
        self.paths = [tk.StringVar() for _ in range(2)]
        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(7, weight=1)
        for index, (label, hint) in enumerate((
            (OLD, "选你上次在类脑预设上改过、自己一直在用的那份 JSON。只改新版时这里可以空着。"),
            (NEW, "选从大佬那里下载下来的新版本。先找到下载的文件夹，再选名字以 .json 结尾的预设文件。"),
        )):
            caption = ttk.Label(main, text=label)
            caption.grid(row=index, column=0, sticky="w", padx=(0, 8))
            tooltip(caption, hint)
            ttk.Entry(main, textvariable=self.paths[index]).grid(
                row=index, column=1, sticky="ew", pady=2)
            pick = ttk.Button(main, text="找文件…", command=lambda i=index: self.pick(i))
            pick.grid(row=index, column=2, padx=(8, 0))
            tooltip(pick, hint)

        actions = ttk.Frame(main)
        actions.grid(row=2, columnspan=3, sticky="ew", pady=8)
        mode = ttk.Checkbutton(actions, text="只处理新版本", variable=self.only_new)
        mode.pack(side="left", padx=(0, 8))
        tooltip(mode, "手上只有大佬的新版？勾上这个，只选下面的新版文件也能打开。切换后再点一次打开预设。")
        for label, command, hint in (
            ("① 打开预设", self.load, "把选好的文件读进工具。原文件照样留着，工具还会在本机留一份备份。"),
            ("怎么用？", self.help, "从选文件到把 AI 改过的内容装回新版，跟着这里的步骤做。"),
        ):
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            tooltip(button, hint)

        export_bar = ttk.LabelFrame(main, text="③ 导出给 AI", padding=6)
        export_bar.grid(row=8, columnspan=3, sticky="ew", pady=(8, 0))
        self.export_button = ttk.Button(export_bar, text="预览并导出", command=self.export)
        self.export_button.pack(side="left", padx=(0, 8))
        tooltip(self.export_button, "预览会显示这份文件实际包含的全部正文，确认后才保存。")
        self.batch_export_button = ttk.Button(export_bar, text="批量导出所选 SFW",
                                              command=self.export_selected)
        self.batch_export_button.pack(side="left", padx=(0, 8))
        tooltip(self.batch_export_button, "把左边选中的、已经标记 SFW 的条目合成一个 JSON。每条的编号会帮工具把 AI 改好的正文放回原位。")
        ttk.Label(export_bar, textvariable=self.export_count).pack(side="left")
        prompt_button = ttk.Button(export_bar, text="编辑 / 复制给 AI 的话", command=self.ai_prompt)
        prompt_button.pack(side="right")
        tooltip(prompt_button, "这里的话可以改。点复制，再到你平常用的 AI 聊天框里粘贴，附上刚导出的 JSON 一起发。")
        sources = ttk.Frame(main)
        sources.grid(row=4, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(sources, text="② 挑选要分享的正文").pack(side="left", padx=(0, 8))
        for option, hint in (
            (NEW, "看大佬刚发的新版正文。这部分送出去后，AI 改完可以装回新版。"),
            (OLD, "看你上次自己改过的正文。可以把想保留的写法发给 AI 当参考。"),
        ):
            button = ttk.Radiobutton(sources, text=option, value=option, variable=self.source,
                                     command=self.change_source)
            button.pack(side="left", padx=(0, 10))
            tooltip(button, hint)
            if option == OLD:
                self.old_source_button = button
        ttk.Label(sources, textvariable=self.source_hint).pack(side="left")
        filters = ttk.Frame(main)
        filters.grid(row=5, columnspan=3, sticky="ew", pady=(6, 2))
        ttk.Label(filters, text="筛选").pack(side="left")
        self.filter = tk.StringVar(value="全部")
        self.selector = ttk.Combobox(filters, state="readonly", width=15, textvariable=self.filter)
        self.selector.pack(side="left", padx=6)
        self.selector.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        tooltip(self.selector, "条目太多时，在这里选想看的内容。比如只看已准备导出的，或者只看自己删改过的。")
        select_all = ttk.Button(filters, text="全选当前列表", command=self.select_visible)
        select_all.pack(side="left", padx=(0, 6))
        tooltip(select_all, "一次选中左边列表里显示的条目。只想选几条，可以按住 Ctrl 一条条点。")
        self.selection_count = ttk.Label(filters, text="已选 0 条")
        self.selection_count.pack(side="left", padx=8)
        self.safe_button = ttk.Button(filters, text="筛选已确认安全的新版并导出", command=self.pick_safe)
        self.safe_button.pack(side="left", padx=4)
        tooltip(self.safe_button, "只处理新版时，一键把以前确认过 SFW、而且正文没变的条目放进导出清单。没看过的条目先留着，等你亲自确认。")
        search_row = ttk.Frame(filters)
        search_row.pack(side="bottom", fill="x", pady=(4, 0), before=filters.winfo_children()[0])
        ttk.Label(search_row, text="关键词").pack(side="left", padx=(0, 4))
        search = ttk.Entry(search_row, textvariable=self.search_text, width=26)
        search.pack(side="left")
        tooltip(search, "搜条目名字、编号和当前来源的送审稿。多个词可以用空格隔开。")
        search.bind("<Return>", lambda _: self.refresh())
        ttk.Button(search_row, text="搜索", command=self.refresh).pack(side="left", padx=4)
        within = ttk.Checkbutton(search_row, text="只搜这批已选条目",
                                 variable=self.search_selected, command=self.limit_search)
        within.pack(side="left", padx=4)
        tooltip(within, "先在列表多选，再勾这里，后续搜索就只找这一批。取消勾选可以恢复搜索整个列表。")
        ttk.Button(search_row, text="清空搜索", command=self.clear_search).pack(side="left", padx=4)
        self.search_count = ttk.Label(search_row)
        self.search_count.pack(side="left", padx=4)

        review = ttk.Frame(main)
        review.grid(row=6, columnspan=3, sticky="ew", pady=(2, 6))
        ttk.Label(review, text="所选条目").pack(side="left", padx=(0, 6))
        self.export_choices = {}
        for label, versions in (("仅旧版", (False, True)), ("仅新版", (True, False)),
                                ("新旧都导出", (True, True)), ("不导出", (False, False))):
            button = ttk.Button(review, text=label,
                                command=lambda pair=versions: self.set_entry_export(*pair))
            button.pack(side="left", padx=(0, 4))
            self.export_choices[versions] = button
            hints = {
                (False, True): "只把你自己改的老版正文发给 AI 看，作为参考。",
                (True, False): "只发大佬新版的正文。AI 改完可以放回新版。",
                (True, True): "把这个条目的两份正文一起发，方便 AI 对照着改新版。",
                (False, False): "把选中的条目从这次发给 AI 的清单里拿掉，本机里还留着。",
            }
            tooltip(button, hints[versions] + (" 想发的正文请先标记 SFW。" if any(versions) else ""))
        self.mark_private = ttk.Button(review, text="标记 NSFW",
                                       command=lambda: self.classify_selection(PRIVATE))
        self.mark_private.pack(side="left")
        tooltip(self.mark_private, "这段不适合发给外部 AI。点一下会从导出清单里移走它。")
        self.mark_safe = ttk.Button(review, text="标记 SFW",
                                    command=lambda: self.classify_selection(APPROVED))
        self.mark_safe.pack(side="left", padx=(4, 0))
        tooltip(self.mark_safe, "把你选中的这些正文记为可以分享。可以多选后一次标记。")
        more = ttk.Menubutton(review, text="仅保存分类")
        menu = tk.Menu(more, tearoff=False)
        for state in CLASSIFICATIONS:
            if state in (APPROVED, PRIVATE):
                continue
            menu.add_command(label=state, command=lambda value=state: self.classify_selection(value))
        more.configure(menu=menu)
        more.pack(side="left", padx=6)
        tooltip(more, "只记一笔这段正文是否可以分享。要把它放进文件，还可以点左边的「仅新版」或「仅旧版」。")
        self.review_hint = ttk.Label(review)
        self.review_hint.pack(side="left")

        pane = ttk.Panedwindow(main, orient="horizontal")
        pane.grid(row=7, columnspan=3, sticky="nsew")
        left, right = ttk.Frame(pane), ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(left, weight=3)
        pane.add(right, weight=2)
        columns = ("name", "status", "classification", "merge", "selected")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended")
        self.tree.tag_configure("manual", foreground="#ad241c", background="#fff0e7")
        self.keys = {}
        for column, label, width in zip(columns, ("条目", "上游变化", "分类", "个人合并", "导出设置"),
                                        (140, 78, 110, 95, 95)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=45, stretch=column == "name")
        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.details)

        self.identifier = ttk.Label(right, text="请选择条目", wraplength=380)
        self.identifier.pack(anchor="w")
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True, pady=4)
        self.views = []
        for index, (label, hint) in enumerate((
            ("新版送审稿", "这里是准备发给 AI 的新版正文。可以删掉不想发的几句，点保存后只影响送出去的内容。"),
            ("老版送审稿", "这里是准备给 AI 参考的老版正文。留想分享的内容，保存后原来的预设文件不变。"),
            ("AI 改完的正文", "放回 AI 的 JSON 后，可以在这里接着改。把本地便笺里的内容复制回来，再点保存。"),
            ("私密便笺", "每条有自己的便笺。把从送审稿删下来的话粘在这里。它只在本机保存，不会放进给 AI 的文件。"),
        )):
            panel = ttk.Frame(self.tabs, padding=4)
            self.tabs.add(panel, text=label)
            ttk.Label(panel, text=hint, wraplength=320).pack(anchor="w", pady=(0, 4))
            view = ScrolledText(panel, width=30, height=8, wrap="word", state="disabled", undo=True)
            view.pack(fill="both", expand=True)
            self.views.append(view)
            button = ttk.Button(panel, text="保存到本机" if index == 3 else "保存这段修改",
                                command=lambda i=index: self.save_editor(i))
            button.pack(anchor="w", pady=(4, 0))
            tooltip(button, hint)
            tooltip(view, hint)
            if index == 3:
                copy_note = ttk.Button(panel, text="复制整张便笺", command=self.copy_note)
                copy_note.pack(anchor="w", pady=(4, 0))
                tooltip(copy_note, "把这条的便笺复制到剪贴板。切到「AI 改完的正文」，点要补回的位置，再按 Ctrl+V。")

        self.review_old = ttk.Button(right, text="审核此条目的旧个人正文",
                                     command=self.review_old_entry, state="disabled")
        self.review_old.pack(anchor="w", pady=(0, 6))
        tooltip(self.review_old, "切换到这个条目的旧个人版。可用上方「仅旧版」或「新旧都导出」设置导出版本。")
        self.resolution = ttk.Frame(right)
        ttk.Label(self.resolution, text="本地合并冲突").pack(side="left", padx=(0, 4))
        self.use_upstream = ttk.Button(self.resolution, text="采用新版",
                                        command=lambda: self.resolve("upstream"), state="disabled")
        self.use_upstream.pack(side="left")
        tooltip(self.use_upstream, "这条在最终完整预设里用大佬的原文。若这里有已导入的 AI 修改，会从这条移走。")
        self.use_personal = ttk.Button(self.resolution, text="采用旧个人条目",
                                        command=lambda: self.resolve("personal"), state="disabled")
        self.use_personal.pack(side="left", padx=4)
        tooltip(self.use_personal, "有合并冲突时可把旧个人条目保留在本地个人版。要把它分享给 AI，请用上方的旧个人正文审核按钮。")
        output = ttk.LabelFrame(main, text="④ 收到 AI 修改后", padding=6)
        output.grid(row=9, columnspan=3, sticky="ew", pady=(6, 0))
        for label, command, hint in (
            ("放回 AI 改好的 JSON", self.import_patch, "把外部 AI 回给你的 JSON 文件放回这里。打开后可以逐条查看和接着改。"),
            ("另存完整预设", self.save, "把改好的正文装进一份新的预设文件。这个文件可以留着自己导入使用。"),
            ("另存改好的补丁", self.save_patch, "只保存这次 AI 改过、以及你后来补回去的正文。它可能含私密内容，请留在本机。"),
            ("清空已导入修改", self.clear_patch, "把这次放回来的 AI 修改从窗口里清掉。已经保存的文件还在。"),
        ):
            button = ttk.Button(output, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            tooltip(button, hint)
        ttk.Label(main, textvariable=self.status, wraplength=850).grid(
            row=10, columnspan=3, sticky="w", pady=(7, 0))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.change_source()

    def help(self):
        messagebox.showinfo(
            "使用指南",
            "1. 新版本是大佬刚发的文件。老版本是你自己改过的那份。"
            "手上只有新版，就勾「只处理新版本」，然后点「打开预设」。\n\n"
            "2. 点左边一条，右边就能看正文。想删几句话再发，就剪下来放进「私密便笺」，"
            "然后保存送审稿。选这条要发新版、老版，还是两边都发。\n\n"
            "3. 点「预览并导出」，选一个找得到的位置存文件。"
            "再打开「编辑 / 复制给 AI 的话」，把复制的话和导出的 JSON 一起发给你平常用的 AI。\n\n"
            "4. AI 回来 JSON 后点「放回 AI 改好的 JSON」。红色的条目提醒你以前手动删改过。"
            "去「私密便笺」复制要补回的话，粘到「AI 改完的正文」里并保存，最后另存预设或补丁。",
            parent=self.root)

    @property
    def reviewing_personal(self):
        return self.source.get() == OLD

    def review_preset(self):
        return self.session.personal if self.reviewing_personal else self.session.upstream

    def change_source(self):
        filters = ("全部", "已加入导出", "手动删改过") if self.reviewing_personal else (
            "全部", "新旧不同", "已加入导出", "手动删改过")
        self.selector.configure(values=filters)
        self.filter.set("全部")
        self.tabs.select(1 if self.reviewing_personal else 0)
        self.refresh()

    @guarded
    def review_old_entry(self):
        self.require_session()
        key = self.require_key()
        self.source.set(OLD)
        self.change_source()
        item = next((item for item, candidate in self.keys.items() if candidate == key), None)
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.see(item)
            self.details()

    def pick(self, index):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON", "*.json")])
        if path:
            self.paths[index].set(path)

    @guarded
    def load(self):
        paths = [value.get().strip().strip('"') for value in self.paths]
        if not paths[1] or not (paths[0] or self.only_new.get()):
            raise ValueError("请先选新版和自己改的老版。手上只有新版时，勾选「只处理新版本」。")
        if self.session and not messagebox.askokcancel(
                "重新比较", "重新比较会清除当前勾选、冲突决定及已导入的补丁。分类和备份仍可使用。",
                parent=self.root):
            return
        self.flush_editors()
        personal = None if self.only_new.get() else Preset.read(paths[0])
        upstream = Preset.read(paths[1])
        session = Session(None, personal, upstream, self.archive)
        self.archive.backup((None, personal, upstream))
        self.session = session
        self.editor_key = None
        self.search_text.set("")
        self.search_selected.set(False)
        self.search_scope = None
        self.selected.clear()
        self.references.clear()
        self.excluded_from_batch.clear()
        self.tree.selection_remove(self.tree.selection())
        self.source.set(NEW)
        self.change_source()
        self.status.set(f"已打开并备份预设，共 {len(session.rows)} 个条目。")

    @guarded
    def pick_safe(self):
        session = self.require_session()
        self.flush_editors()
        allowed = {key for key in session.upstream.entries
                   if key not in self.excluded_from_batch and session.approved(key)}
        self.selected.update(allowed)
        self.source.set(NEW)
        self.change_source()
        self.filter.set("已加入导出" if allowed else "全部")
        self.refresh()
        self.status.set(f"已把 {len(allowed)} 条以前确认过安全的新版正文放进清单。"
                        if allowed else "还没有已确认安全的新版正文。请先选中条目，点「仅新版」审核。")
        if allowed:
            self.export()

    def ai_prompt(self):
        window = tk.Toplevel(self.root)
        window.title("复制这里的话和导出的 JSON 一起发给外部 AI")
        window.transient(self.root)
        center_window(window, 700, 400, self.root)
        ttk.Label(window, text="下面的话可以改。复制后，到 AI 聊天框里粘贴，再附上导出的 JSON。").pack(
            anchor="w", padx=12, pady=8)
        view = ScrolledText(window, wrap="word", height=10)
        view.pack(fill="both", expand=True, padx=12)
        view.insert("1.0", self.archive.get_texts("app", "settings").get("prompt", AI_INSTRUCTIONS))
        status = ttk.Label(window)
        status.pack(side="left", padx=12)

        @guarded
        def store(app, copy=False):
            text = view.get("1.0", "end-1c")
            app.archive.save_text("app", "settings", "prompt", text)
            if copy:
                app.root.clipboard_clear()
                app.root.clipboard_append(text)
                status.configure(text="已复制。现在可以到 AI 聊天框里粘贴。")
            else:
                window.destroy()

        ttk.Button(window, text="一键复制", command=lambda: store(self, True)).pack(side="right", padx=12, pady=8)
        ttk.Button(window, text="保存并关闭", command=lambda: store(self)).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", lambda: store(self))

    def show_export_prompt(self):
        window = tk.Toplevel(self.root)
        window.title("已导出 · 把这段话和 JSON 一起发给 AI")
        window.transient(self.root)
        center_window(window, 690, 370, self.root)
        ttk.Label(window, text="先点一键复制，到 AI 聊天框粘贴，再附上刚才保存的 JSON。").pack(
            anchor="w", padx=12, pady=10)
        view = ScrolledText(window, wrap="word", height=9)
        view.pack(fill="both", expand=True, padx=12)
        text = self.archive.get_texts("app", "settings").get("prompt", AI_INSTRUCTIONS)
        fill(view, text)
        actions = ttk.Frame(window, padding=12)
        actions.pack(fill="x")
        status = ttk.Label(actions)
        status.pack(side="left")

        def copy():
            window.clipboard_clear()
            window.clipboard_append(text)
            status.configure(text="已复制。到 AI 聊天框粘贴即可。")

        ttk.Button(actions, text="关闭", command=window.destroy).pack(side="right")
        ttk.Button(actions, text="一键复制", command=copy).pack(side="right", padx=8)
        window.bind("<Escape>", lambda _: window.destroy())

    def key(self):
        selection = self.tree.selection()
        focused = self.tree.focus()
        if focused in selection:
            return self.keys.get(focused)
        return self.keys.get(selection[0]) if selection else None

    def select_visible(self):
        if self.session is None:
            return
        items = [item for item, key in self.keys.items() if key in self.review_preset().entries]
        self.tree.selection_set(items)
        if items:
            self.tree.focus(items[0])
        self.details()

    def limit_search(self):
        self.search_scope = set(self.selected_keys()) if self.search_selected.get() else None
        if self.search_scope == set():
            self.search_selected.set(False)
            self.search_scope = None
            self.status.set("先在左边选几条，再勾「只搜这批已选条目」。")
            return
        self.refresh()

    def clear_search(self):
        self.search_text.set("")
        self.search_selected.set(False)
        self.search_scope = None
        self.refresh()

    def refresh(self):
        self.flush_editors()
        session = self.session
        hint = "老版正文供 AI 参考" if self.reviewing_personal else "新版正文可由 AI 修改后装回"
        self.source_hint.set(hint)
        self.safe_button.configure(state="normal" if session and not session.personal.data else "disabled")
        self.update_export()
        if session is None:
            self.details()
            return
        self.old_source_button.configure(state="normal" if session.personal.data else "disabled")
        preset = self.review_preset()
        checked = self.selected | self.references
        keywords = self.search_text.get().casefold().split()
        current = self.key()
        selected_keys = {self.keys[item] for item in self.tree.selection()}
        restored = []
        self.tree.delete(*self.tree.get_children())
        self.keys.clear()
        for key, row in session.rows.items():
            decision = session.decisions.get(key)
            pending = row.conflict and not decision and key not in session.patch
            mode = self.filter.get()
            manual = key in session.manual_patch or any(session.is_manual(key, personal=side)
                                                         for side in (False, True))
            if (self.search_scope is not None and key not in self.search_scope
                    or self.reviewing_personal and key not in preset.entries
                    or mode == "新旧不同" and row.status == "相同"
                    or mode == "手动删改过" and not manual
                    or mode == "已加入导出" and key not in checked):
                continue
            entry = (preset.entries.get(key) or session.personal.entries.get(key)
                     or session.base.entries[key])
            if keywords:
                text = " ".join((str(entry.get("name", "")), key,
                                 session.body(key, personal=self.reviewing_personal) or "")).casefold()
                if not all(word in text for word in keywords):
                    continue
            state = (session.classification(key, personal=self.reviewing_personal)
                     if key in preset.entries else "无新版条目")
            merge = ("手动删改过" if manual else "已放回 AI 修改" if key in session.patch else "新旧内容不同" if pending else
                     "采用新版" if decision == "upstream" else "采用旧个人版" if decision else
                     "沿用旧定制" if row.customized and row.status == "未变化" else
                     "新旧已一致" if row.customized else "")
            setting = ("不导出" if key in self.excluded_from_batch else
                       "新旧都导出" if key in self.selected and key in self.references else
                       "仅新版" if key in self.selected else "仅旧版" if key in self.references else "—")
            item = self.tree.insert("", "end", values=(entry.get("name") or key, row.status, state,
                                                      merge, setting), tags=("manual",) if manual else ())
            self.keys[item] = key
            if key in selected_keys:
                restored.append(item)
            if key == current:
                self.tree.focus(item)
        self.tree.selection_set(restored)
        self.search_count.configure(text=f"显示 {len(self.keys)} 条")
        self.details()

    def update_export(self):
        available = self.selected or self.references
        can_batch = self.session and any(self.batch_versions(key) for key in self.selected_keys())
        self.export_button.configure(state="normal" if available or can_batch else "disabled")
        counts = f"清单  新版 {len(self.selected)} 条 · 老版 {len(self.references)} 条"
        if not available:
            counts = ("还没有单独设置清单，本次可直接导出所选 SFW。"
                      if can_batch else "先选中要分享的条目，再点「标记 SFW」。")
        self.export_count.set(counts)

    def selected_keys(self):
        return [self.keys[item] for item in self.tree.selection()]

    @guarded
    def details(self, _event=None):
        self.flush_editors()
        key = self.key()
        keys = self.selected_keys()
        count = len(keys)
        self.selection_count.configure(text=f"已选 {count} 条")
        single = count == 1 and key is not None
        has_old = single and key in self.session.personal.entries
        conflict = single and self.session.rows[key].conflict
        self.review_old.configure(state="normal" if has_old and not self.reviewing_personal else "disabled")
        self.use_upstream.configure(state="normal" if conflict else "disabled")
        self.use_personal.configure(
            state="normal" if conflict and has_old and key in self.session.upstream.entries else "disabled")
        if conflict:
            self.resolution.pack(fill="x")
        else:
            self.resolution.pack_forget()
        for versions, button in self.export_choices.items():
            upstream, personal = versions
            eligible = (any(self.valid_export_versions(item, upstream, personal) for item in keys)
                        if upstream or personal else any(key not in self.excluded_from_batch for key in keys))
            button.configure(state="normal" if eligible else "disabled")
        classifiable = any(item in self.review_preset().entries for item in keys)
        self.mark_private.configure(state="normal" if classifiable else "disabled")
        self.mark_safe.configure(state="normal" if classifiable else "disabled")
        self.batch_export_button.configure(state="normal" if any(self.batch_versions(key) for key in keys)
                                           else "disabled")
        self.update_export()
        hint = "先在下面选条目" if not keys else ""
        self.review_hint.configure(text=hint)
        self.editor_key = key if single else None
        key = self.editor_key
        flagged = key is not None and (key in self.session.manual_patch or any(
            self.session.is_manual(key, personal=side) for side in (False, True)))
        self.identifier.configure(
            text=(f"手动删改过 · {key}" if flagged else key) or "请选择一条，看正文或写便笺",
            foreground="#ad241c" if flagged else "")
        texts = (None, None, None, None)
        if key is not None:
            texts = (self.session.body(key), self.session.body(key, personal=True),
                     self.session.patch.get(key), self.session.notes.get(key, ""))
        for view, content in zip(self.views, texts):
            fill(view, content if content is not None else "这里暂时没有可编辑的正文。")
            view.configure(state="normal" if content is not None else "disabled")
            view.edit_reset()
            view.edit_modified(False)

    def flush_editors(self):
        if self.editor_key is None:
            return
        key, session = self.editor_key, self.session
        for index, view in enumerate(self.views):
            if not view.edit_modified() or str(view.cget("state")) == "disabled":
                continue
            body = view.get("1.0", "end-1c")
            if index < 2:
                personal = index == 1
                if body != session.body(key, personal=personal):
                    session.save_draft(key, body, personal=personal)
                    (self.references if personal else self.selected).discard(key)
            elif index == 2:
                session.patch[key] = body
            else:
                session.save_note(key, body)
            view.edit_modified(False)
        self.update_export()

    @guarded
    def save_editor(self, index):
        self.require_key()
        if str(self.views[index].cget("state")) == "disabled":
            raise ValueError("这个页签目前没有可编辑的正文。")
        self.flush_editors()
        self.refresh()
        self.status.set(("便笺已留在本机。" if index == 3 else
                         "已保存到当前补丁。请用下方按钮另存文件。" if index == 2 else
                         "送审稿已留在本机。要发给 AI，请重新选择这条的导出版本。"))

    @guarded
    def copy_note(self):
        key = self.require_key()
        self.flush_editors()
        self.root.clipboard_clear()
        self.root.clipboard_append(self.session.notes.get(key, ""))
        self.status.set("已复制这条的便笺。切到「AI 改完的正文」，在要补回的位置按 Ctrl+V。")

    @guarded
    def close(self):
        self.flush_editors()
        self.root.destroy()

    def require_session(self):
        if self.session is None:
            raise ValueError("请选好文件，再点「打开预设」。")
        return self.session

    def require_key(self):
        if len(self.tree.selection()) > 1:
            raise ValueError("此操作需要单选。批量标记请用列表上方的按钮。")
        key = self.key()
        if key is None:
            raise ValueError("请先选中一个条目。")
        return key

    def clear_revoked(self, keys):
        for key in keys:
            if not self.session.approved(key):
                self.selected.discard(key)
                self.session.patch.pop(key, None)
            if not self.session.approved_reference(key):
                self.references.discard(key)

    @guarded
    def classify_selection(self, state):
        session = self.require_session()
        self.flush_editors()
        keys = self.selected_keys()
        entries = [(key, session.review_hash(key, personal=self.reviewing_personal))
                   for key in keys if key in self.review_preset().entries]
        if not entries:
            raise ValueError("请选中当前来源的条目。可用 Ctrl 多选或 Shift 连选。")
        count = len(entries)
        session.archive.classify_many(entries, state)
        self.clear_revoked([key for key, _ in entries])
        self.refresh()
        skipped = len(keys) - count
        self.status.set(f"已将 {count} 条标记为 {state}。"
                        + (f"已跳过 {skipped} 条不适用的条目。" if skipped else ""))

    def valid_export_versions(self, key, upstream, personal):
        return ((not upstream or self.session.has_exportable_body(key))
                and (not personal or self.session.has_exportable_body(key, personal=True)))

    @guarded
    def set_entry_export(self, upstream, personal):
        session = self.require_session()
        self.flush_editors()
        selected = self.selected_keys()
        keys = [key for key in selected if self.valid_export_versions(key, upstream, personal)]
        if not keys:
            raise ValueError("所选条目没有这个版本的可导出正文。")

        eligible = [key for key in keys if (not upstream or session.approved(key))
                    and (not personal or session.approved_reference(key))]

        for include, checked in ((upstream, self.selected), (personal, self.references)):
            checked.difference_update(eligible)
            if include:
                checked.update(eligible)
        if upstream or personal:
            self.excluded_from_batch.difference_update(eligible)
        else:
            self.excluded_from_batch.update(eligible)
        self.refresh()
        skipped = len(selected) - len(eligible)
        self.status.set(f"已更新 {len(eligible)} 条的导出设置。"
                        + (f"跳过 {skipped} 条。请先在对应版本把正文标记 SFW。" if skipped else ""))

    def batch_versions(self, key):
        session = self.session
        if key in self.excluded_from_batch:
            return ()
        if key in self.selected or key in self.references:
            return tuple(side for side, enabled in (
                (NEW, key in self.selected and session.approved(key)),
                (OLD, key in self.references and session.approved_reference(key))) if enabled)
        if self.reviewing_personal:
            return (OLD,) if session.approved_reference(key) else ()
        return (NEW,) if session.approved(key) else ()

    @guarded
    def export_selected(self):
        self.require_session()
        self.flush_editors()
        choices = {key: self.batch_versions(key) for key in self.selected_keys()}
        edits = [key for key, versions in choices.items() if NEW in versions]
        references = [key for key, versions in choices.items() if OLD in versions]
        skipped = sum(not versions for versions in choices.values())
        self.export_entries(edits, references, skipped)

    @guarded
    def resolve(self, decision):
        self.require_session().resolve(self.require_key(), decision)
        self.refresh()

    def preview(self, title, description, content, accept, *, parent=None):
        parent = parent or self.root
        window = tk.Toplevel(parent)
        window.title(title)
        window.transient(parent)
        center_window(window, 780, 560, parent)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=description, wraplength=720).pack(anchor="w", pady=(0, 8))
        view = ScrolledText(frame, wrap="word")
        view.pack(fill="both", expand=True)
        fill(view, content)
        confirmed = tk.BooleanVar(master=window)

        def confirm():
            confirmed.set(True)
            window.destroy()

        ttk.Button(frame, text=accept, command=confirm).pack(side="right", pady=(8, 0))
        ttk.Button(frame, text="取消", command=window.destroy).pack(side="right", padx=8, pady=(8, 0))
        window.grab_set()
        parent.wait_window(window)
        return confirmed.get()

    def destination(self, title, name):
        return filedialog.asksaveasfilename(parent=self.root, title=title,
                                            initialfile=name, defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])

    @guarded
    def export(self):
        session = self.require_session()
        self.flush_editors()
        edits = [key for key in session.upstream.entries if key in self.selected]
        references = [key for key in session.personal.entries if key in self.references]
        if edits or references:
            self.export_entries(edits, references)
        else:
            self.export_selected()

    def export_entries(self, edits, references, skipped=0):
        session = self.require_session()
        if not edits and not references:
            self.status.set("没有可以分享的正文。先选中条目并点「标记 SFW」。")
            return
        payload, targets = session.make_export(edits, references)
        hint = ("edits 是可以回填的新版正文，references 是旧个人参考。"
                if references else "edits 是可以回填的新版正文。")
        if not edits:
            hint = "这份文件只含旧个人参考，供 AI 对话使用。"
        skipped_hint = f"已跳过 {skipped} 个未批准或没有正文的条目。" if skipped else ""
        if self.preview("核对 AI 导出",
                        f"一个 JSON 文件包含新版 {len(edits)} 条、老版 {len(references)} 条。{skipped_hint}{hint}",
                        format_json(payload).decode("utf-8"), "确认可分享"):
            name = "ai-edit-request.json" if edits else "ai-personal-reference.json"
            path = self.destination("保存 AI 交换文件", name)
            if path:
                session.save_export(path, payload, targets, references)
                self.status.set(f"已保存 {len(edits)} 条可编辑正文和 {len(references)} 条旧个人参考。{path}")
                self.show_export_prompt()

    @guarded
    def import_patch(self):
        session = self.require_session()
        self.flush_editors()
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON", "*.json")])
        if not path:
            return
        patch, manual, slots, batch = session.read_patch(path, with_details=True)
        preview = [
            {"条目": key, "提醒": "手动删改过，记得检查私密便笺" if key in manual else "",
             "AI 改完的正文": value}
            for key, value in patch.items()
        ]
        if self.preview("核对 AI 补丁",
                        "本次补丁替换当前已导入的补丁。列出的条目将采用新上游字段和补丁正文。",
                        format_json(preview).decode("utf-8"), "采用这份补丁"):
            session.patch = patch
            session.manual_patch = manual
            session.imported_slots = slots
            session.imported_batch = batch
            self.refresh()
            if manual:
                item = next((item for item, key in self.keys.items() if key in manual), None)
                if not item:
                    self.source.set(NEW)
                    self.change_source()
                    item = next((item for item, key in self.keys.items() if key in manual), None)
                if item:
                    self.tree.selection_set(item)
                    self.tree.focus(item)
                    self.tree.see(item)
                    self.details()
                    self.tabs.select(2)
            self.status.set(f"已放回 {len(patch)} 条。{len(manual)} 条剔除或改过送审稿，请检查红色提醒和便笺。")

    @guarded
    def clear_patch(self):
        session = self.require_session()
        self.flush_editors()
        session.patch.clear()
        session.manual_patch.clear()
        self.refresh()
        self.status.set("已清空当前补丁。")

    @guarded
    def save(self):
        session = self.require_session()
        self.flush_editors()
        window = MergeReviewWindow(self, FinalReview(session))
        self.root.wait_window(window.window)
        self.refresh()

    @guarded
    def save_patch(self):
        session = self.require_session()
        self.flush_editors()
        payload = session.patch_document()
        path = self.destination("另存改好的补丁（只留在本机）", "personal-patch.json")
        if path:
            write_new(path, format_json(payload))
            self.status.set(f"已保存这一轮改好的补丁。{path}")


class MergeReviewWindow:
    LABELS = {"new": "用新版", "old": "用我的老版", "ai": "用 AI 改好的", "omit": "不加入"}

    def __init__(self, app, review):
        self.app, self.review = app, review
        self.window = tk.Toplevel(app.root)
        self.root = self.window  # Parent for the shared error handler.
        self.window.title("保存前检查 · 选择每条最终用哪一版")
        self.window.transient(app.root)
        center_window(self.window, 1060, 700, app.root)
        self.window.minsize(850, 560)
        self.active_key = None
        self.active_source = None
        self.items = {}
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="点左边一条，在右边选最终版本。默认有 AI 修改就用 AI 版，否则用新版。只在老版的条目可手动加入。").pack(anchor="w")
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=8)
        self.filter = tk.StringVar(value="全部条目")
        selector = ttk.Combobox(controls, textvariable=self.filter, state="readonly", width=16,
                                values=("全部条目", "新旧不同", "只在我的老版", "有 AI 修改", "手动选过", "以前用过我的老版"))
        selector.pack(side="left")
        selector.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        self.keyword = tk.StringVar()
        ttk.Label(controls, text="搜条目").pack(side="left", padx=(10, 4))
        search = ttk.Entry(controls, textvariable=self.keyword, width=25)
        search.pack(side="left")
        search.bind("<Return>", lambda _: self.refresh())
        ttk.Button(controls, text="搜索", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(controls, text="清空", command=self.clear_search).pack(side="left")
        history = ttk.Button(controls, text="找以前用过我的老版的条目", command=self.show_old_history)
        history.pack(side="left", padx=8)
        tooltip(history, "按条目编号找出你以前在保存检查中选过老版的条目。只筛选列表，方便你重新决定。")

        pane = ttk.Panedwindow(frame, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left, right = ttk.Frame(pane), ttk.Frame(pane, padding=(10, 0, 0, 0))
        pane.add(left, weight=2)
        pane.add(right, weight=3)
        self.tree = ttk.Treeview(left, columns=("name", "source"), show="headings", selectmode="browse")
        for column, heading, width in (("name", "条目", 200), ("source", "最终采用", 110)):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, stretch=column == "name")
        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_item)

        self.title = ttk.Label(right, text="先选左边一条", wraplength=440)
        self.title.pack(anchor="w")
        choices = ttk.Frame(right)
        choices.pack(fill="x", pady=6)
        self.source = tk.StringVar()
        self.buttons = {}
        for source, label in self.LABELS.items():
            button = ttk.Radiobutton(choices, text=label, value=source, variable=self.source,
                                     command=lambda value=source: self.change_choice(value))
            button.pack(side="left", padx=(0, 6))
            self.buttons[source] = button
        ttk.Button(right, text="对照各版本原文", command=self.compare).pack(anchor="w")
        enabled_box = ttk.LabelFrame(right, text="发消息时要不要用这条？", padding=5)
        enabled_box.pack(fill="x", pady=(6, 0))
        self.groups = []
        self.group_selector = ttk.Combobox(enabled_box, width=25, state="readonly")
        self.group_selector.pack(anchor="w")
        self.group_selector.bind("<<ComboboxSelected>>", lambda _: self.show_enabled())
        self.enabled_value = ttk.Label(enabled_box)
        self.enabled_value.pack(anchor="w", pady=2)
        enable_buttons = ttk.Frame(enabled_box)
        enable_buttons.pack(anchor="w")
        self.enable_buttons = {}
        for action, label in (("on", "开启"), ("off", "关闭"), ("old", "用老版开关"), ("default", "恢复默认")):
            button = ttk.Button(enable_buttons, text=label,
                                command=lambda value=action: self.change_enabled(value))
            button.pack(side="left", padx=(0, 4))
            self.enable_buttons[action] = button
        tooltip(enabled_box, "这里决定 SillyTavern 发消息时是否使用这条。上面的版本按钮决定用哪份正文，两处可以分别设置。")
        notebook = ttk.Notebook(right)
        notebook.pack(fill="both", expand=True, pady=6)
        self.text = ScrolledText(notebook, width=38, height=10, wrap="word", undo=True)
        notebook.add(self.text, text="最终正文（可直接改）")
        note_panel = ttk.Frame(notebook, padding=4)
        notebook.add(note_panel, text="这条的私密便笺")
        ttk.Label(note_panel, text="和主窗口是同一张便笺，只保存在本机。").pack(anchor="w")
        self.note = ScrolledText(note_panel, width=38, height=8, wrap="word", undo=True)
        self.note.pack(fill="both", expand=True)
        ttk.Button(note_panel, text="复制整张便笺", command=self.copy_note).pack(anchor="w", pady=(4, 0))
        self.status = ttk.Label(frame, text="编辑和版本选择留在本机。最终预设另存为一份新文件。")
        self.status.pack(anchor="w", pady=(5, 0))
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(6, 0))
        ttk.Button(footer, text="保存这条的修改", command=self.save_item).pack(side="left")
        ttk.Button(footer, text="另存完整预设文件…", command=self.write).pack(side="right")
        ttk.Button(footer, text="返回主窗口", command=self.close).pack(side="right", padx=8)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        if self.items:
            item = next(iter(self.items))
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.show_item()
        self.window.grab_set()

    def name(self, key):
        session = self.review.session
        entry = session.upstream.entries.get(key) or session.personal.entries[key]
        return str(entry.get("name") or key)

    def update_row(self, item, key):
        source = self.review.choice(key)
        label = self.LABELS[source]
        if key in self.review.drafts and source in self.review.drafts[key]:
            label += " · 已编辑"
        self.tree.item(item, values=(self.name(key), label))

    @guarded
    def refresh(self):
        self.flush()
        current = self.active_key
        mode = self.filter.get()
        query = self.keyword.get().strip().casefold()
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        session = self.review.session
        for key, row in session.rows.items():
            if (mode == "新旧不同" and row.status == "相同"
                    or mode == "只在我的老版" and key in session.upstream.entries
                    or mode == "有 AI 修改" and key not in session.patch
                    or mode == "手动选过" and key not in self.review.choices
                    or mode == "以前用过我的老版" and (
                        key not in self.review.old_history or key not in session.personal.entries)
                    or query and query not in f"{key} {self.name(key)}".casefold()):
                continue
            item = self.tree.insert("", "end")
            self.items[item] = key
            self.update_row(item, key)
            if key == current:
                self.tree.selection_set(item)
        self.show_item()

    def clear_search(self):
        self.keyword.set("")
        self.refresh()

    def show_old_history(self):
        self.filter.set("以前用过我的老版")
        self.clear_search()

    def flush(self):
        key, source, session = self.active_key, self.active_source, self.review.session
        if key is None:
            return
        if self.text.edit_modified() and self.text.cget("state") == "normal":
            self.review.edit(key, source, self.text.get("1.0", "end-1c"))
            self.text.edit_modified(False)
        if self.note.edit_modified() and self.note.cget("state") == "normal":
            session.save_note(key, self.note.get("1.0", "end-1c"))
            self.note.edit_modified(False)
        for item, item_key in self.items.items():
            if item_key == key:
                self.update_row(item, key)
                break

    @guarded
    def show_item(self, _event=None):
        self.flush()
        selected = self.tree.selection()
        key = self.items.get(selected[0]) if selected else None
        source = self.review.choice(key) if key is not None else None
        self.active_key, self.active_source = key, source
        self.source.set(source or "")
        self.title.configure(text=f"{self.name(key)} · {key}" if key is not None else "先选左边一条")
        options = self.review.options(key) if key is not None else ()
        for version, button in self.buttons.items():
            button.configure(state="normal" if version in options else "disabled")
        previous_group = self.group_selector.current()
        self.groups = self.review.enabled_rows(key) if key is not None and source != "omit" else []
        labels = [f"分组 {index}（角色编号 {character}）" if character is not None else f"分组 {index}"
                  for index, (_, character, *_states) in enumerate(self.groups, 1)]
        self.group_selector.configure(values=labels, state="readonly" if labels else "disabled")
        if labels:
            self.group_selector.current(previous_group if 0 <= previous_group < len(labels) else 0)
        else:
            self.group_selector.set("")
        self.show_enabled()
        body = self.review.body(key, source) if key is not None else None
        note = self.review.session.notes.get(key, "") if key is not None else None
        for widget, text in ((self.text, body), (self.note, note)):
            fill(widget, text if text is not None else "这里没有可编辑的正文。")
            widget.configure(state="normal" if text is not None else "disabled")
            widget.edit_reset()
            widget.edit_modified(False)

    def show_enabled(self):
        index = self.group_selector.current()
        row = self.groups[index] if index >= 0 else None
        if row is None:
            self.enabled_value.configure(text="先选要保存的条目。没有排序分组时，这里无法设置开关。")
        else:
            _, _, default, old, override = row
            state = lambda value: "开启" if value else "关闭"
            old_label = state(old) if old is not None else "无对应分组"
            current = state(default if override is None else override)
            self.enabled_value.configure(text=f"最终：{current} ｜ 默认：{state(default)} ｜ 我的老版：{old_label}")
        for action, button in self.enable_buttons.items():
            enabled = row is not None and (action != "old" or row[3] is not None)
            button.configure(state="normal" if enabled else "disabled")

    @guarded
    def change_enabled(self, action):
        self.flush()
        index = self.group_selector.current()
        group_id, _, _, old, _ = self.groups[index]
        value = {"on": True, "off": False, "old": old, "default": None}[action]
        self.review.set_enabled(self.active_key, group_id, value)
        self.groups = self.review.enabled_rows(self.active_key)
        self.show_enabled()
        self.status.configure(text="这条的开关已保存到本机，会写入最终预设。")

    @guarded
    def change_choice(self, source):
        self.flush()
        self.review.choose(self.active_key, source)
        self.show_item()

    @guarded
    def compare(self):
        key = self.active_key
        if key is None:
            return
        content = "\n\n".join(
            f"【{self.LABELS[source]}】\n{self.review.original(key, source) or '（无正文）'}"
            for source in self.review.options(key) if source != "omit")
        # A child dialog of the modal review window can take its own grab.
        self.app.preview("对照各版本原文", "以下是各版本原文，最终正文可以在检查窗口里编辑。",
                         content, "返回检查", parent=self.window)
        self.window.grab_set()

    @guarded
    def copy_note(self):
        self.flush()
        if self.active_key is not None:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.review.session.notes.get(self.active_key, ""))
            self.status.configure(text="已复制便笺。切到「最终正文」，按 Ctrl+V 粘贴。")

    @guarded
    def save_item(self):
        self.flush()
        if self.active_key is not None:
            self.status.configure(text="这条的选择、正文和便笺已保存到本机。")

    @guarded
    def close(self):
        self.flush()
        self.window.destroy()

    @guarded
    def write(self):
        self.flush()
        path = filedialog.asksaveasfilename(parent=self.window, title="另存完整预设文件",
                                            initialfile="personal-merged.json", defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            write_new(path, format_json(self.review.result()))
            self.app.status.set(f"已保存完整预设。{path}")
            self.window.destroy()


if __name__ == "__main__":
    screen = tk.Tk()
    try:
        App(screen)
    except (OSError, sqlite3.Error) as issue:
        messagebox.showerror("无法打开本地档案", str(issue), parent=screen)
        screen.destroy()
    else:
        screen.mainloop()
