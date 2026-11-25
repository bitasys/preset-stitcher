import copy
import os
from pathlib import Path
import tempfile
import unittest

from preset_core import (
    APPROVED, LOCAL, PRIVATE, REVIEW, Archive, FinalReview, Preset, Session, decode,
    format_json, write_new,
)


def entry(key, content):
    return {"identifier": key, "name": f"local label {key}", "content": content}


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = Archive(self.root / "archive")
        self.base = self.document([entry("same", "old"), entry("change", "old"),
                                   entry("private", "old"), entry("local", "old"),
                                   entry("deleted", "old"), entry("removed-personally", "old")])
        self.personal = copy.deepcopy(self.base)
        self.personal["prompts"][0]["content"] = "my unchanged customization"
        self.personal["prompts"][1]["content"] = "my conflict"
        self.personal["prompts"][4]["content"] = "my deleted customization"
        self.personal["prompts"].pop()
        self.new = self.document([entry("same", "old"), entry("change", "new SFW"),
                                  entry("private", "private sentinel"),
                                  entry("local", "local sentinel"), entry("new", "new entry"),
                                  entry("removed-personally", "old"),
                                  {"identifier": "marker", "marker": True}])
        self.session = self.make_session()

    def document(self, prompts):
        return {"prompts": prompts, "prompt_order": [{"character_id": 42, "order": [
            {"identifier": "same", "enabled": False}, {"identifier": "built-in", "enabled": True}
        ]}], "extensions": {"opaque": "embedded text sentinel"}, "metadata": {"version": 2}}

    def preset(self, name, data):
        path = self.root / name
        path.write_bytes(format_json(data))
        return Preset.read(path)

    def make_session(self):
        return Session(self.preset("base.json", self.base), self.preset("personal.json", self.personal),
                       self.preset("new.json", self.new), self.archive)

    def classify(self, key, state):
        self.archive.classify(key, self.session.upstream.hashes[key], state)

    def approved_batch(self):
        self.classify("change", APPROVED)
        payload, targets = self.session.make_export(["change"])
        self.session.save_export(self.root / "request.json", payload, targets)
        return payload

    def test_exact_classification_and_export_boundary(self):
        self.classify("private", PRIVATE)
        self.classify("local", LOCAL)
        self.classify("same", APPROVED)
        self.classify("marker", APPROVED)
        for key in ("private", "local", "same", "marker", "new", "deleted"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.session.make_export([key])
        payload = self.approved_batch()
        self.assertEqual(payload["edits"], [{"slot": "1", "content": "new SFW"}])
        self.assertEqual(set(payload), {"batch", "format", "edits"})
        self.archive = Archive(self.archive.root)
        self.session = self.make_session()
        self.assertEqual(self.session.classification("private"), PRIVATE)
        self.assertEqual(self.session.classification("change"), APPROVED)
        self.new["prompts"][1]["role"] = "system"
        self.assertEqual(self.make_session().classification("change"), REVIEW)

    def test_merge_preserves_structure_and_requires_resolution(self):
        with self.assertRaises(ValueError):
            self.session.merge()
        with self.assertRaises(ValueError):
            self.session.resolve("deleted", "personal")
        self.session.resolve("deleted", "upstream")
        self.session.resolve("removed-personally", "upstream")
        self.session.resolve("change", "personal")
        merged = self.session.merge()
        self.assertEqual(merged["prompts"][0], self.personal["prompts"][0])
        self.assertEqual(merged["prompts"][1], self.personal["prompts"][1])
        self.assertEqual([p["identifier"] for p in merged["prompts"]],
                         [p["identifier"] for p in self.new["prompts"]])
        for key in self.new.keys() - {"prompts"}:
            self.assertEqual(merged[key], self.new[key])

    def test_batch_classification_is_persistent_and_validates_all_entries(self):
        keys = ("change", "new")
        entries = [(key, self.session.upstream.hashes[key]) for key in keys]
        with self.assertRaises(ValueError):
            self.archive.classify_many([*entries, ("deleted", None)], PRIVATE)
        self.assertEqual([self.session.classification(key) for key in keys], [REVIEW, REVIEW])
        self.archive.classify_many(entries, APPROVED)
        self.assertEqual([self.session.classification(key) for key in keys], [APPROVED, APPROVED])
        self.archive.classify_many(entries, PRIVATE)
        reopened = self.make_session()
        self.assertEqual([reopened.classification(key) for key in keys], [PRIVATE, PRIVATE])
        with self.assertRaises(ValueError):
            reopened.make_export(list(keys))

    def test_patch_reloads_and_only_changes_content(self):
        payload = self.approved_batch()
        payload["edits"][0]["content"] = "integrated"
        path = self.root / "patch.json"
        path.write_bytes(format_json(payload))
        self.session = self.make_session()
        self.session.patch = self.session.read_patch(path)
        self.session.resolve("deleted", "upstream")
        self.session.resolve("removed-personally", "upstream")
        self.assertEqual(self.session.merge()["prompts"][1],
                         {**self.new["prompts"][1], "content": "integrated"})
        self.classify("change", PRIVATE)
        with self.assertRaises(ValueError):
            self.session.read_patch(path)
        with self.assertRaises(ValueError):
            self.session.merge()

    def test_export_personal_only_and_mixed_references(self):
        self.classify("change", APPROVED)
        for key in ("change", "deleted"):
            self.archive.classify(key, self.session.personal.hashes[key], APPROVED)
        with self.assertRaises(ValueError):
            self.session.make_export([], ["private"])

        references = ["change", "deleted"]
        payload, targets = self.session.make_export([], references)
        reference_file = self.root / "reference.json"
        self.session.save_export(reference_file, payload, targets, references)
        self.assertEqual(payload["edits"], [])
        self.assertEqual(payload["references"], [
            {"ref": "1", "content": "my conflict"},
            {"ref": "2", "content": "my deleted customization"},
        ])
        with self.assertRaises(ValueError):
            self.session.read_patch(reference_file)

        payload, targets = self.session.make_export(["change"], references)
        self.assertEqual(payload["references"][0]["target_slot"], "1")
        self.assertNotIn("target_slot", payload["references"][1])
        self.session.save_export(self.root / "mixed.json", payload, targets, references)
        del payload["references"]
        patch = self.root / "patch.json"
        patch.write_bytes(format_json(payload))
        self.assertEqual(self.session.read_patch(patch), {"change": "new SFW"})

    def test_revoking_personal_reference_after_preview_blocks_save(self):
        key = "change"
        entry_hash = self.session.personal.hashes[key]
        self.archive.classify(key, entry_hash, APPROVED)
        payload, targets = self.session.make_export([], [key])
        self.archive.classify(key, entry_hash, PRIVATE)
        with self.assertRaises(ValueError):
            self.session.save_export(self.root / "revoked.json", payload, targets, [key])
        self.assertFalse((self.root / "revoked.json").exists())

    def test_patch_rejects_extra_fields_duplicate_slots_and_wrong_sources(self):
        payload = self.approved_batch()
        valid_edit = payload["edits"][0]
        path = self.root / "patch.json"
        for edits in ([{**valid_edit, "identifier": "private"}],
                      [valid_edit, valid_edit], [{"slot": "2", "content": "text"}]):
            path.write_bytes(format_json({**payload, "edits": edits}))
            with self.assertRaises(ValueError):
                self.session.read_patch(path)
        path.write_bytes(format_json(payload))
        self.new["metadata"]["version"] = 999
        with self.assertRaises(ValueError):
            self.make_session().read_patch(path)

    def test_backup_preserves_bytes_and_outputs_do_not_overwrite(self):
        presets = (self.session.base, self.session.personal, self.session.upstream)
        folder = self.archive.backup(presets)
        path = folder / "new-upstream.json"
        with self.assertRaises(FileExistsError):
            write_new(path, b"overwrite")
        self.assertEqual(path.read_bytes(), presets[-1].raw)

    def test_invalid_and_ambiguous_json(self):
        for raw in (b'{"a": 1, "a": 2}', b'{"a": NaN}', b'{"a": 1e999}'):
            with self.assertRaises(ValueError):
                decode(raw)
        self.new["prompts"].append(entry("same", "duplicate"))
        with self.assertRaises(ValueError):
            self.make_session()

    def test_original_attachment_round_trip(self):
        source = os.environ.get("PRESET_TEST_SAMPLE")
        if not source:
            self.skipTest("未设置 PRESET_TEST_SAMPLE")
        original = Preset.read(source)
        self.assertEqual(len(original.entries), 211)
        self.assertEqual(sum(len(group["order"]) for group in original.data["prompt_order"]), 177)
        session = Session(original, original, original, self.archive)
        merged = session.merge()
        self.assertEqual(decode(format_json(merged)), original.data)
        folder = self.archive.backup((original, original, original))
        self.assertEqual((folder / "new-upstream.json").read_bytes(), original.raw)

    def test_saved_drafts_notes_and_manual_patch_round_trip(self):
        original = self.session.upstream
        session = Session(None, None, original, self.archive)
        session.save_draft("change", "safe-only draft")
        session.save_note("change", "local private note")
        session.save_note("new", "another note")
        self.assertEqual(session.classification("change"), REVIEW)
        self.archive.classify("change", session.review_hash("change"), APPROVED)
        payload, targets = session.make_export(["change"])
        path = self.root / "out.json"
        session.save_export(path, payload, targets)
        self.assertEqual(payload["edits"][0]["content"], "safe-only draft")
        self.assertNotIn(b"local private note", path.read_bytes())
        self.assertEqual(original.entries["change"]["content"], "new SFW")

        reopened = Session(None, None, original, Archive(self.archive.root))
        self.assertEqual(reopened.body("change"), "safe-only draft")
        self.assertEqual(reopened.notes, {"change": "local private note", "new": "another note"})
        payload["edits"][0]["content"] = "AI return"
        patch_path = self.root / "returned.json"
        patch_path.write_bytes(format_json(payload))
        reopened.patch = reopened.read_patch(patch_path)
        self.assertEqual(reopened.manual_patch, {"change"})
        reopened.patch["change"] += " " + reopened.notes["change"]
        self.assertEqual(reopened.merge()["prompts"][1]["content"], "AI return local private note")
        self.assertEqual(reopened.patch_document()["edits"][0]["content"], "AI return local private note")

    def test_two_file_mode_keeps_new_structure_and_allows_old_choice(self):
        session = Session(None, self.session.personal, self.session.upstream, self.archive)
        self.assertEqual(session.merge(), self.new)
        self.assertTrue(session.rows["change"].conflict)
        session.resolve("change", "personal")
        self.assertEqual(session.merge()["prompts"][1], self.personal["prompts"][1])

    def test_final_review_can_mix_sources_and_keep_edits_by_version(self):
        session = Session(None, self.session.personal, self.session.upstream, self.archive)
        session.patch = {"change": "AI changed body", "new": "AI new body"}
        review = FinalReview(session)
        self.assertEqual(review.choice("change"), "ai")
        self.assertEqual(review.choice("same"), "new")
        self.assertEqual(review.choice("deleted"), "omit")
        review.choose("change", "old")
        review.edit("change", "old", "my kept edit")
        review.choose("same", "old")
        review.edit("new", "ai", "AI body plus my finishing edit")
        review.choose("change", "new")
        self.assertEqual(review.body("change", "new"), "new SFW")
        review.choose("change", "old")
        reopened = FinalReview(session)
        result = reopened.result()
        entries = {row["identifier"]: row["content"] for row in result["prompts"] if "content" in row}
        self.assertEqual(entries["change"], "my kept edit")
        self.assertEqual(entries["new"], "AI body plus my finishing edit")
        self.assertEqual(entries["same"], "my unchanged customization")
        self.assertEqual(result["prompt_order"], self.new["prompt_order"])
        self.assertEqual(result["extensions"], self.new["extensions"])
        self.assertEqual(session.patch["change"], "AI changed body")
        session.patch["change"] = "another AI response"
        self.assertEqual(FinalReview(session).choice("change"), "ai")

    def test_final_review_can_restore_an_old_only_entry_and_its_order(self):
        self.personal["prompt_order"][0]["order"].append({"identifier": "deleted", "enabled": True})
        self.session = self.make_session()
        review = FinalReview(Session(None, self.session.personal, self.session.upstream, self.archive))
        review.choose("deleted", "old")
        result = review.result()
        self.assertEqual(result["prompts"][-1], entry("deleted", "my deleted customization"))
        self.assertEqual(result["prompt_order"][0]["order"],
                         [*self.new["prompt_order"][0]["order"], {"identifier": "deleted", "enabled": True}])
        review.choose("deleted", "omit")
        self.assertEqual(review.result(), self.new)

    def test_final_review_switches_each_order_group_and_remembers_old_ids(self):
        self.new["prompt_order"].append({"character_id": 99, "order": [
            {"identifier": "same", "enabled": True, "extra": "preserved"}]})
        self.personal["prompt_order"][0]["order"][0]["enabled"] = True
        self.session = self.make_session()
        session = Session(None, self.session.personal, self.session.upstream, self.archive)
        review = FinalReview(session)
        first, second = review.enabled_rows("same")
        self.assertEqual(first[2:5], (False, True, None))
        self.assertEqual(second[2:5], (True, None, None))
        review.choose("same", "old")
        review.set_enabled("same", first[0], True)
        review.set_enabled("same", second[0], False)
        review.set_enabled("new", second[0], True)
        result = FinalReview(session).result()
        self.assertTrue(result["prompt_order"][0]["order"][0]["enabled"])
        self.assertEqual(result["prompt_order"][1]["order"], [
            {"identifier": "same", "enabled": False, "extra": "preserved"},
            {"identifier": "new", "enabled": True}])
        session.patch = {"same": "different batch"}
        self.assertIn("same", FinalReview(session).old_history)
        self.assertEqual(FinalReview(session).choice("same"), "ai")

    def test_old_choice_history_imports_choices_saved_before_history_feature(self):
        self.archive.save_text("previous-scope", "final-review", "choices", '{"same":"old","change":"ai"}')
        session = Session(None, self.session.personal, self.session.upstream, self.archive)
        review = FinalReview(session)
        self.assertEqual(review.old_history, {"same"})
        self.assertEqual(review.choice("same"), "new")


if __name__ == "__main__":
    unittest.main()
