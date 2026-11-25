"""Offline preset parsing, comparison, export and merge."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass


REVIEW = "待复核"
APPROVED = "SFW · 允许导出"
PRIVATE = "私密 / NSFW"
LOCAL = "仅本地"
CLASSIFICATIONS = (REVIEW, APPROVED, PRIVATE, LOCAL)
PROTOCOL = "preset-content-patch/1"


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("JSON 含重复键。")
        obj[key] = value
    return obj


def reject_constant(_):
    raise ValueError("JSON 含非法数值。")


def decode(raw):
    obj = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object,
                     parse_constant=reject_constant)
    canonical(obj)  # Also reject overflowed floats and unpaired surrogates.
    return obj


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def format_json(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def write_new(path, data):
    """Never overwrite. Remove our own incomplete file on write failure."""
    with Path(path).open("xb") as handle:
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError:
            handle.close()
            Path(path).unlink(missing_ok=True)
            raise


@dataclass
class Preset:
    data: dict
    entries: dict
    raw: bytes
    hashes: dict

    @classmethod
    def read(cls, path):
        raw = Path(path).read_bytes()
        data = decode(raw)
        if (not isinstance(data, dict) or not isinstance(data.get("prompts"), list)
                or not isinstance(data.get("prompt_order"), list)):
            raise ValueError("需要含 prompts 和 prompt_order 数组的预设 JSON。")
        entries = {}
        for entry in data["prompts"]:
            key = entry.get("identifier") if isinstance(entry, dict) else None
            if not isinstance(key, str) or not key.strip() or key in entries:
                raise ValueError("提示词 identifier 缺失、无效或重复。")
            if "content" in entry and not isinstance(entry["content"], str):
                raise ValueError("提示词 content 必须是字符串，或省略该字段。")
            entries[key] = entry
        for group in data["prompt_order"]:
            if not isinstance(group, dict) or not isinstance(group.get("order"), list):
                raise ValueError("prompt_order 中的每组必须含 order 数组。")
            for item in group["order"]:
                if not isinstance(item, dict) or not isinstance(item.get("identifier"), str):
                    raise ValueError("排序项缺少字符串 identifier。")
        return cls(data, entries, raw, {key: digest(value) for key, value in entries.items()})


class Archive:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS choices (
                    identifier TEXT NOT NULL, hash TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(identifier, hash)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    token TEXT PRIMARY KEY, sources TEXT NOT NULL, targets TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_text (
                    scope TEXT NOT NULL, kind TEXT NOT NULL, identifier TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(scope, kind, identifier)
                );
                CREATE TABLE IF NOT EXISTS batch_review (
                    token TEXT PRIMARY KEY, details TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.root / "reviews.sqlite3")) as db, db:
            yield db

    def choice(self, key, entry_hash):
        if entry_hash is None:
            return REVIEW
        with self.connect() as db:
            row = db.execute("SELECT state FROM choices WHERE identifier=? AND hash=?",
                             (key, entry_hash)).fetchone()
        return row[0] if row and row[0] in CLASSIFICATIONS else REVIEW

    def classify(self, key, entry_hash, state):
        self.classify_many([(key, entry_hash)], state)

    def classify_many(self, entries, state):
        if state not in CLASSIFICATIONS or not entries or any(not entry_hash for _, entry_hash in entries):
            raise ValueError("该条目无法分类。")
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO choices VALUES (?, ?, ?)",
                           [(key, entry_hash, state) for key, entry_hash in entries])

    def backup(self, presets):
        folder = self.root / "backups" / uuid.uuid4().hex
        folder.mkdir(parents=True)
        for name, preset in zip(("previous-upstream", "previous-personal", "new-upstream"), presets):
            if preset is not None:
                write_new(folder / (name + ".json"), preset.raw)
        return folder

    def get_texts(self, scope, kind):
        with self.connect() as db:
            return dict(db.execute("SELECT identifier, body FROM local_text WHERE scope=? AND kind=?",
                                   (scope, kind)))

    def save_text(self, scope, kind, key, body):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO local_text VALUES (?, ?, ?, ?)",
                       (scope, kind, key, body))

    def old_choice_history(self):
        with self.connect() as db:
            migrated = db.execute(
                "SELECT 1 FROM local_text WHERE scope='app' AND kind='settings' AND identifier='old-history-imported'"
            ).fetchone()
            if not migrated:
                saved = db.execute(
                    "SELECT body FROM local_text WHERE kind='final-review' AND identifier='choices'"
                ).fetchall()
                keys = {key for (body,) in saved for key, choice in json.loads(body).items()
                        if choice == "old"}
                db.executemany("INSERT OR IGNORE INTO local_text VALUES ('app', 'old-choice-history', ?, 'chosen')",
                               [(key,) for key in keys])
                db.execute("INSERT OR IGNORE INTO local_text VALUES ('app', 'settings', 'old-history-imported', 'yes')")
            return {row[0] for row in db.execute(
                "SELECT identifier FROM local_text WHERE scope='app' AND kind='old-choice-history'")}


@dataclass(frozen=True)
class Row:
    status: str
    customized: bool
    conflict: bool


class Session:
    def __init__(self, base, personal, upstream, archive):
        self.base, self.upstream = base, upstream
        self.personal = personal if personal is not None else Preset({}, {}, b"", {})
        self.archive = archive
        self.sources = digest([digest(preset.data) if preset else None
                               for preset in (base, personal, upstream)])
        self.draft_scopes = {False: digest(upstream.data), True: digest(self.personal.data)}
        self.drafts = {side: archive.get_texts(scope, "personal-draft" if side else "new-draft")
                       for side, scope in self.draft_scopes.items()}
        self.notes = archive.get_texts(self.sources, "note")
        self.rows = {}
        self.decisions = {}
        self.patch = {}
        self.imported_slots = {}
        self.imported_batch = None
        self.manual_patch = set()
        base_entries = base.entries if base else {}
        for key in dict.fromkeys([*upstream.entries, *base_entries, *self.personal.entries]):
            mine, new = (p.hashes.get(key) for p in (self.personal, upstream))
            if base is None:
                status = ("只在老版" if new is None else "只在新版" if mine is None
                          else "相同" if mine == new else "内容不同")
                self.rows[key] = Row(status, mine is not None, mine is not None and new is not None and mine != new)
                continue
            old = base.hashes.get(key)
            if new is None:
                status = "上游删除" if old is not None else "仅旧个人版"
            elif old is None:
                status = "新增"
            elif old == new:
                status = "未变化"
            else:
                status = "已修改"
            customized = mine != old
            conflict = customized and mine != new and (
                old != new or mine is None or new is None)
            self.rows[key] = Row(status, customized, conflict)

    def classification(self, key, *, personal=False):
        return self.archive.choice(key, self.review_hash(key, personal=personal))

    def body(self, key, *, personal=False):
        preset = self.personal if personal else self.upstream
        return self.drafts[personal].get(key, preset.entries.get(key, {}).get("content"))

    def is_manual(self, key, *, personal=False):
        preset = self.personal if personal else self.upstream
        return (key in preset.entries and self.body(key, personal=personal)
                != preset.entries[key].get("content"))

    def review_hash(self, key, *, personal=False):
        preset = self.personal if personal else self.upstream
        if not self.is_manual(key, personal=personal):
            return preset.hashes.get(key)
        return digest({**preset.entries[key], "content": self.body(key, personal=personal)})

    def save_draft(self, key, body, *, personal=False):
        preset = self.personal if personal else self.upstream
        if not isinstance(preset.entries.get(key, {}).get("content"), str):
            raise ValueError("这个条目没有可编辑的原始正文。")
        self.archive.save_text(self.draft_scopes[personal], "personal-draft" if personal else "new-draft", key, body)
        self.drafts[personal][key] = body

    def save_note(self, key, body):
        self.archive.save_text(self.sources, "note", key, body)
        self.notes[key] = body

    def has_exportable_body(self, key, *, personal=False):
        preset = self.personal if personal else self.upstream
        entry = preset.entries.get(key, {})
        return isinstance(entry.get("content"), str) and (
            personal or self.base is None or self.rows[key].status in ("新增", "已修改"))

    def approved(self, key):
        return self.has_exportable_body(key) and self.classification(key) == APPROVED

    def approved_reference(self, key):
        return (self.has_exportable_body(key, personal=True)
                and self.classification(key, personal=True) == APPROVED)

    def make_export(self, selected, references=()):
        if not selected and not references:
            raise ValueError("请勾选至少一个允许导出的条目。")
        if len(set(selected)) != len(selected) or len(set(references)) != len(references):
            raise ValueError("导出选择含重复条目。")
        token = uuid.uuid4().hex
        targets = {}
        entries = []
        for index, key in enumerate(selected, 1):
            if not self.approved(key):
                raise ValueError("勾选项中有条目尚未批准或已失去导出资格。")
            slot = str(index)
            targets[slot] = key
            entries.append({"slot": slot, "content": self.body(key)})
        payload = {"format": PROTOCOL, "batch": token, "edits": entries}
        if references:
            payload["references"] = self.reference_rows(references, targets)
        return payload, targets

    def reference_rows(self, references, targets):
        slots = {key: slot for slot, key in targets.items()}
        rows = []
        for index, key in enumerate(references, 1):
            if not self.approved_reference(key):
                raise ValueError("旧个人参考中有条目尚未批准或已失去导出资格。")
            row = {"ref": str(index), "content": self.body(key, personal=True)}
            if key in slots:
                row["target_slot"] = slots[key]
            rows.append(row)
        return rows

    def save_export(self, path, payload, targets, references=()):
        # Revalidate the exact payload after the user closes the preview.
        entries = payload["edits"]
        if len(entries) != len(targets) or any(
                not self.approved(targets[entry["slot"]])
                or entry["content"] != self.body(targets[entry["slot"]])
                for entry in entries):
            raise ValueError("导出资格已变化，请重新预览。")
        if payload.get("references", []) != self.reference_rows(references, targets):
            raise ValueError("旧个人参考与导出预览不一致，请重新预览。")
        if targets:
            details = {slot: {"hash": self.review_hash(key), "manual": self.is_manual(key)
                              or (key in references and self.is_manual(key, personal=True))}
                       for slot, key in targets.items()}
            with self.archive.connect() as db:
                db.execute("INSERT INTO batches VALUES (?, ?, ?)",
                           (payload["batch"], self.sources, json.dumps(targets)))
                db.execute("INSERT INTO batch_review VALUES (?, ?)", (payload["batch"], json.dumps(details)))
        write_new(path, format_json(payload))

    def read_patch(self, path, *, with_details=False):
        obj = decode(Path(path).read_bytes())
        if (not isinstance(obj, dict) or set(obj) != {"format", "batch", "edits"}
                or obj["format"] != PROTOCOL or not isinstance(obj["batch"], str)
                or not isinstance(obj["edits"], list) or not obj["edits"]):
            raise ValueError("补丁格式不符，需保留 format、batch 和非空 edits。")
        with self.archive.connect() as db:
            batch = db.execute("SELECT sources, targets FROM batches WHERE token=?",
                               (obj["batch"],)).fetchone()
            review = db.execute("SELECT details FROM batch_review WHERE token=?", (obj["batch"],)).fetchone()
        if batch is None or batch[0] != self.sources:
            raise ValueError("这份 JSON 与当前打开的预设不匹配。请打开当时导出时用的文件和模式。")
        targets = json.loads(batch[1])
        details = json.loads(review[0]) if review else {}
        patch = {}
        manual = set()
        slots = {}
        for edit in obj["edits"]:
            if (not isinstance(edit, dict) or set(edit) != {"slot", "content"}
                    or not isinstance(edit["slot"], str) or not isinstance(edit["content"], str)):
                raise ValueError("每个补丁项只能含字符串 slot 和 content。")
            key = targets.get(edit["slot"])
            if key is None or key in patch or key not in self.upstream.entries:
                raise ValueError("补丁含未知、重复或已取消授权的条目。")
            info = details.get(edit["slot"])
            if info and self.archive.choice(key, info["hash"]) != APPROVED:
                raise ValueError("这份补丁里的条目已撤销导出许可。")
            if not info and not self.approved(key):
                raise ValueError("这份补丁里的条目已撤销导出许可。")
            if (info and info["manual"]) or self.is_manual(key):
                manual.add(key)
            patch[key] = edit["content"]
            slots[key] = edit["slot"]
        if with_details:
            return patch, manual, slots, obj["batch"]
        self.manual_patch = manual
        self.imported_slots = slots
        self.imported_batch = obj["batch"]
        return patch

    def patch_document(self):
        if not self.patch or not self.imported_batch:
            raise ValueError("请先放回 AI 改好的 JSON。")
        return {"format": PROTOCOL, "batch": self.imported_batch, "edits": [
            {"slot": self.imported_slots[key], "content": body} for key, body in self.patch.items()
        ]}

    def resolve(self, key, choice):
        if not self.rows[key].conflict or choice not in ("upstream", "personal"):
            raise ValueError("请选择有效的冲突处理方式。")
        if choice == "personal" and (key not in self.upstream.entries
                                     or key not in self.personal.entries):
            raise ValueError("此冲突只能采用新版结构，旧内容仍在本地备份。")
        self.patch.pop(key, None)
        self.decisions[key] = choice

    def merge(self):
        if self.base is not None and any(not self.approved(key) for key in self.patch):
            raise ValueError("补丁含已取消授权的条目，请清空补丁或重新导入。")
        pending = [key for key, row in self.rows.items()
                   if row.conflict and key not in self.decisions and key not in self.patch]
        if pending and self.base is not None:
            raise ValueError(f"还有 {len(pending)} 个冲突需要处理。")
        result = copy.deepcopy(self.upstream.data)
        for index, entry in enumerate(result["prompts"]):
            key = entry["identifier"]
            if key in self.patch:
                entry["content"] = self.patch[key]
            elif (self.decisions.get(key) == "personal"
                  or self.rows[key].status == "未变化" and self.rows[key].customized
                  and not self.rows[key].conflict):
                result["prompts"][index] = copy.deepcopy(self.personal.entries[key])
        return result


class FinalReview:
    """Local decisions and text edits for one set of inputs and AI results."""

    def __init__(self, session):
        self.session = session
        self.scope = digest([session.sources, session.patch, session.decisions])
        stored = session.archive.get_texts(self.scope, "final-review")
        self.choices = json.loads(stored.get("choices", "{}"))
        self.drafts = json.loads(stored.get("drafts", "{}"))
        self.enable_choices = json.loads(stored.get("enabled", "{}"))
        self.old_history = session.archive.old_choice_history()

    def options(self, key):
        session = self.session
        result = ["new"] if key in session.upstream.entries else ["omit"]
        if key in session.personal.entries:
            result.append("old")
        if key in session.patch:
            result.append("ai")
        return result

    def choice(self, key):
        options = self.options(key)
        preferred = self.choices.get(key)
        if preferred in options:
            return preferred
        if "ai" in options:
            return "ai"
        if self.session.decisions.get(key) == "personal" and "old" in options:
            return "old"
        return options[0]

    def choose(self, key, source):
        if source not in self.options(key):
            raise ValueError("这条没有所选版本。")
        self.choices[key] = source
        self.session.archive.save_text(self.scope, "final-review", "choices", json.dumps(self.choices))
        if source == "old" and key not in self.old_history:
            self.session.archive.save_text("app", "old-choice-history", key, "chosen")
            self.old_history.add(key)

    def order_groups(self):
        groups = copy.deepcopy(self.session.upstream.data["prompt_order"])
        old_only = {key for key in self.session.personal.entries
                    if key not in self.session.upstream.entries and self.choice(key) == "old"}
        if old_only:
            self.restore_old_order(groups, old_only)
        return groups

    @staticmethod
    def group_ids(groups):
        counts = {}
        ids = []
        for group in groups:
            character = digest(group.get("character_id"))
            counts[character] = counts.get(character, 0) + 1
            ids.append(f"{character}:{counts[character]}")
        return ids

    @staticmethod
    def order_state(group, key):
        return any(item["identifier"] == key and item.get("enabled") is True for item in group["order"])

    def enabled_rows(self, key):
        groups = self.order_groups()
        previous = self.session.personal.data.get("prompt_order", [])
        old_groups = dict(zip(self.group_ids(previous), previous))
        overrides = self.enable_choices.get(key, {})
        return [
            (group_id, group.get("character_id"), self.order_state(group, key),
             self.order_state(old_groups[group_id], key) if group_id in old_groups else None,
             overrides.get(group_id))
            for group_id, group in zip(self.group_ids(groups), groups)
        ]

    def set_enabled(self, key, group_id, value):
        if self.choice(key) == "omit":
            raise ValueError("先把这条从老版加入完整预设，再设置开关。")
        if value is not None and type(value) is not bool:
            raise ValueError("请选择打开、关闭或使用默认状态。")
        if group_id not in {row[0] for row in self.enabled_rows(key)}:
            raise ValueError("找不到这个排序分组。")
        choices = self.enable_choices.setdefault(key, {})
        if value is None:
            choices.pop(group_id, None)
        else:
            choices[group_id] = value
        self.session.archive.save_text(self.scope, "final-review", "enabled",
                                       json.dumps(self.enable_choices))

    def original(self, key, source):
        if source == "omit":
            return None
        if source == "ai":
            return self.session.patch[key]
        preset = self.session.personal if source == "old" else self.session.upstream
        return preset.entries[key].get("content")

    def body(self, key, source):
        return self.drafts.get(key, {}).get(source, self.original(key, source))

    def edit(self, key, source, body):
        if source not in self.options(key) or self.original(key, source) is None:
            raise ValueError("这条没有可编辑的正文。")
        self.drafts.setdefault(key, {})[source] = body
        self.session.archive.save_text(self.scope, "final-review", "drafts", json.dumps(self.drafts))

    def result(self):
        session = self.session
        result = copy.deepcopy(session.upstream.data)
        prompts = []
        for key in session.rows:
            source = self.choice(key)
            if source == "omit":
                continue
            preset = session.personal if source == "old" else session.upstream
            entry = copy.deepcopy(preset.entries[key])
            body = self.body(key, source)
            if body is not None:
                entry["content"] = body
            prompts.append(entry)
        result["prompts"] = prompts
        groups = self.order_groups()
        entries = {entry["identifier"]: entry for entry in prompts}
        for group_id, group in zip(self.group_ids(groups), groups):
            links = {}
            for item in group["order"]:
                links.setdefault(item["identifier"], []).append(item)
            for key, choices in self.enable_choices.items():
                if key not in entries or group_id not in choices:
                    continue
                enabled = choices[group_id]
                for link in links.get(key, []):
                    link["enabled"] = enabled
                if enabled and key not in links:
                    group["order"].append({"identifier": key, "enabled": True})
                # A single-group preset can keep its per-entry flag in sync too.
                if len(groups) == 1 and "enabled" in entries[key]:
                    entries[key]["enabled"] = enabled
        result["prompt_order"] = groups
        return result

    def restore_old_order(self, groups, old_only):
        for old_group in self.session.personal.data.get("prompt_order", []):
            additions = [item for item in old_group["order"] if item["identifier"] in old_only]
            if not additions:
                continue
            # A character ID is the scope of one order list. If the new preset
            # has no such list, preserve the old group with just the chosen rows.
            group = next((group for group in groups
                          if "character_id" in old_group and group.get("character_id") == old_group["character_id"]),
                         None)
            if group is None:
                group = {**copy.deepcopy(old_group), "order": []}
                groups.append(group)
            present = {item["identifier"] for item in group["order"]}
            for item in additions:
                key = item["identifier"]
                if key not in present:
                    group["order"].append(copy.deepcopy(item))
                    present.add(key)
