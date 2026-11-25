"""新工作bench能力的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from i18n_workbench import Workbench
from i18n_workbench.approvals import apply_change_to_row, basis_hashes
from i18n_workbench.icu import parse_message, render
from i18n_workbench.state import WorkbenchState
from i18n_workbench.validation import validate_candidate


class WorkbenchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)
        self.state = WorkbenchState.open(self.tmp / "state.json")
        self.wb = Workbench(ROOT, state=self.state)
        self.rows = self.wb.rows_by_identity

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_icu_reorder_render_and_chinese_plural_collapse(self):
        message = "{files, plural, other {容量 {size} MB，共 # 项}}"
        self.assertEqual(render(message, {"files": 3, "size": 12}, locale="zh-Hans"), "容量 12 MB，共 3 项")
        parsed = parse_message("{count, plural, one {# file} other {# files}}")
        self.assertEqual(render(parsed, {"count": 1}, locale="en"), "1 file")
        self.assertEqual(render(parsed, {"count": 2}, locale="en"), "2 files")
        # 中文没有 one 类别；1 也进入 other（若没有精确 =1 分支）。
        self.assertEqual(render(parsed, {"count": 1}, locale="zh-Hans"), "1 files")

    def test_clear_contexts_are_distinguished(self):
        filter_row = self.rows[("main", "search.clear")]
        history_row = self.rows[("main", "history.clear")]

        good_filter = validate_candidate(filter_row, "清除筛选", glossary_rows=self.wb.data.glossary)
        good_history = validate_candidate(history_row, "清空记录", glossary_rows=self.wb.data.glossary)
        wrong_on_filter = validate_candidate(filter_row, "清空记录", glossary_rows=self.wb.data.glossary)
        wrong_on_history = validate_candidate(history_row, "清除筛选", glossary_rows=self.wb.data.glossary)

        self.assertFalse([f for f in good_filter if f["severity"] == "error"])
        self.assertFalse([f for f in good_history if f["severity"] == "error"])
        codes = {f["code"] for f in wrong_on_filter if f["severity"] == "error"}
        self.assertIn("wrong_object", codes)
        codes = {f["code"] for f in wrong_on_history if f["severity"] == "error"}
        self.assertIn("wrong_object", codes)

    def test_gate_failures_point_to_render_contexts(self):
        by_id = {item["id"]: item for item in self.wb.submissions()}

        t4_row = self.rows[("1.8", "sync.remaining")]
        t4 = validate_candidate(t4_row, by_id["t4"]["message"], locale="zh-Hans")
        secs_error = next(f for f in t4 if f["code"] == "missing_parameter" and f.get("parameter") == "secs")
        self.assertEqual(secs_error["release"], "1.8")
        self.assertEqual(secs_error["usage"], "同步状态/队列")
        self.assertEqual(secs_error["location"], "plural[other]")
        self.assertEqual(secs_error["render_context"]["params"]["count"], 3)
        self.assertNotIn("42", secs_error["render_context"]["rendered"])
        self.assertIn("3 个项目", secs_error["render_context"]["rendered"])

        t5_row = self.rows[("main", "search.status")]
        t5 = validate_candidate(t5_row, by_id["t5"]["message"], locale="zh-Hans")
        name_error = next(f for f in t5 if f["code"] == "missing_parameter" and f.get("parameter") == "name")
        self.assertEqual(name_error["location"], "select[saved]")
        self.assertEqual(name_error["render_context"]["params"]["state"], "saved")
        self.assertEqual(name_error["render_context"]["params"]["name"], "月度筛选")
        self.assertEqual(name_error["render_context"]["rendered"], "已选")

        t6_row = self.rows[("main", "search.chord")]
        t6 = validate_candidate(t6_row, by_id["t6"]["message"], locale="zh-Hans")
        hotkey_error = next(f for f in t6 if f["code"] == "missing_parameter" and f.get("parameter") == "hotkey")
        self.assertIn("Ctrl+F", json.dumps(hotkey_error["render_context"], ensure_ascii=False))

    def test_release_pack_uses_frozen_contract_and_context_fallback(self):
        result = self.wb.build_pack(release="1.8", output_dir=self.tmp / "dist")
        pack = result["pack"]
        self.assertEqual(pack["nav.reset"], "清除筛选")
        self.assertEqual(pack["history.reset"], "清空记录")
        self.assertIn("{secs}", pack["sync.remaining"])
        self.assertNotIn("{files}", pack["sync.remaining"])
        self.assertNotIn("{size}", pack["sync.remaining"])
        self.assertTrue(result["manifest"]["frozen"])
        self.assertEqual(result["manifest"]["contract_revision"], "r3")
        verification = self.wb.verify_pack(result["manifest_path"])
        self.assertTrue(verification["ok"], verification["errors"])

        main_result = self.wb.build_pack(release="main", output_dir=self.tmp / "dist-main")
        self.assertEqual(main_result["pack"]["search.clear"], "清除筛选")
        self.assertEqual(main_result["pack"]["history.clear"], "清空记录")
        self.assertIn("{files", main_result["pack"]["sync.remaining"])
        # 未核准/坏译稿不能进包；允许 source 层级的场景回退到源文。
        self.assertIn("{state, select", main_result["pack"]["search.status"])
        self.assertEqual(main_result["pack"]["search.chord"], "Reset {hotkey}")

    def test_mainline_translation_cannot_cross_frozen_contract(self):
        report = self.wb.reuse_report(locale="zh-Hans")
        usage = next(u for u in report["usages"] if u["release"] == "1.8" and u["key"] == "sync.remaining")
        t3 = next(c for c in usage["candidates"] if c["candidate_id"] == "t3")
        t7 = next(c for c in usage["candidates"] if c["candidate_id"] == "t7")
        self.assertFalse(t3["contract_compatible"])
        self.assertFalse(t3["technically_usable"])
        self.assertTrue(t7["technically_usable"])
        self.assertEqual(t7["relation"], "direct")

    def test_explicit_usage_prohibition_changes_pack(self):
        self.wb.decide_reuse(
            target_release="1.8",
            target_key="nav.reset",
            candidate_id="t1",
            decision="forbidden",
            reviewer="Beryl",
        )
        result = self.wb.build_pack(release="1.8", output_dir=self.tmp / "dist-forbidden")
        # 禁止 t1 后不能改用语义错误的 t2；该场景允许 source，因此回退源文。
        self.assertEqual(result["pack"]["nav.reset"], "Clear")
        history = result["pack"]["history.reset"]
        self.assertEqual(history, "清空记录")

    def test_global_reuse_ban_keeps_original_direct_approval(self):
        self.wb.decide_reuse(
            target_release="*",
            target_key="*",
            candidate_id="t2",
            decision="forbidden",
            scope="translation",
        )
        main_pack = self.wb.build_pack(release="main", output_dir=self.tmp / "dist-global")["pack"]
        one_eight_result = self.wb.build_pack(release="1.8", output_dir=self.tmp / "dist-global-18")
        one_eight = one_eight_result["pack"]
        self.assertEqual(main_pack["history.clear"], "清空记录")
        # 该场景的术语策略只允许 approved-same-context、未授权 source；
        # 全局禁止 t2 后，1.8 不能硬塞英文或另一句相似中文，只能列为未解析。
        self.assertNotIn("history.reset", one_eight)
        self.assertTrue(any(item["key"] == "history.reset" for item in one_eight_result["manifest"]["unresolved"]))

    def test_separate_meaning_and_display_approvals(self):
        draft = self.wb.submit(
            release="1.8",
            key="sync.remaining",
            message="{count, plural, other {剩余 # 个项目（{secs} 秒）}}",
        )
        # 句意核准后，目录可用性仍未核准，不能仅凭一半签字出包。
        self.wb.approve(draft["id"], "meaning", reviewer="maintainer-a")
        gate = self.wb.gate(release="1.8")
        approval_codes = {
            f["code"]
            for f in gate["errors"]
            if f.get("candidate_id") == draft["id"] and f.get("location") == "approval"
        }
        self.assertIn("display_not_approved", approval_codes)
        self.assertNotIn("meaning_not_approved", approval_codes)

        self.wb.approve(draft["id"], "display", reviewer="maintainer-b")
        gate = self.wb.gate(release="1.8")
        draft_errors = [f for f in gate["errors"] if f.get("candidate_id") == draft["id"]]
        self.assertEqual(draft_errors, [])

    def test_approval_invalidation_after_changes(self):
        t7 = next(item for item in self.wb.submissions() if item["id"] == "t7")
        row = self.rows[("1.8", "sync.remaining")]
        self.wb.approve("t7", "meaning")
        self.wb.approve("t7", "display")

        rename_change = next(c for c in self.wb.data.changes if c["id"] == "c1")
        after = apply_change_to_row(row, rename_change)
        self.assertIn("{wait}", after["source"])
        state_approvals = self.wb.state
        meaning_override = state_approvals.approval_override("t7", "meaning")
        display_override = state_approvals.approval_override("t7", "display")
        catalog = "desktop"
        self.assertEqual(meaning_override["basis"]["meaning"], basis_hashes(row, "zh-Hans", t7["message"], catalog)["meaning"])
        # 纯参数改名且语义单位不变时，句意依据仍相同，可以继承。
        self.assertEqual(meaning_override["basis"]["meaning"], basis_hashes(after, "zh-Hans", t7["message"], catalog)["meaning"])
        self.assertNotEqual(display_override["basis"]["display"], basis_hashes(after, "zh-Hans", t7["message"], catalog)["display"])

        impact = {item["change_id"]: item for item in self.wb.change_impact()}
        self.assertTrue(impact["c1"]["meaning_inheritable"])
        self.assertFalse(impact["c1"]["display_inheritable"])
        self.assertFalse(impact["c2"]["meaning_inheritable"])
        self.assertFalse(impact["c3"]["meaning_inheritable"])

    def test_changed_usage_preview_moves_to_matching_context(self):
        preview = self.wb.preview_change_selection("c2")
        self.assertEqual(preview["chosen"]["candidate_id"], "t2")
        self.assertEqual(preview["chosen"]["origin"], "approved-same-context")
        t1 = next(c for c in preview["candidates"] if c["candidate_id"] == "t1")
        self.assertFalse(t1["technically_usable"])
        self.assertIn("wrong_object", {e["code"] for e in t1["errors"]})

    def test_split_merge_lineage_is_located(self):
        lineage = self.wb.lineage()
        related = {(link["change_id"], link["related_key"]) for link in lineage}
        self.assertIn(("c3", "search.active"), related)
        self.assertIn(("c3", "search.empty"), related)
        active = next(link for link in lineage if link["related_key"] == "search.active")
        self.assertEqual(active["current_key"], "search.status")
        self.assertEqual(active["suggested_branch"], "select[saved]")
        self.assertIn("main/search.status@r7", active["location"])

    def test_glossary_is_scenario_and_release_specific(self):
        main_row = self.rows[("main", "sync.remaining")]
        old_row = self.rows[("1.8", "sync.remaining")]
        main_findings = validate_candidate(main_row, "{files, plural, other {# 项（{size} MB）}}", glossary_rows=self.wb.data.glossary)
        old_findings = validate_candidate(old_row, "{count, plural, other {# 个项目（{secs} 秒）}}", glossary_rows=self.wb.data.glossary)
        self.assertFalse([f for f in main_findings if f["severity"] == "error"])
        self.assertFalse([f for f in old_findings if f["severity"] == "error"])

        from i18n_workbench.semantics import select_glossary

        main_terms = select_glossary(self.wb.data.glossary, release="main", usage="同步状态/队列", source=main_row["source"])
        old_terms = select_glossary(self.wb.data.glossary, release="1.8", usage="同步状态/队列", source=old_row["source"])
        self.assertEqual(main_terms[0]["preferred"], "项")
        self.assertEqual(old_terms[0]["preferred"], "个项目")

    def test_pack_is_deterministic_and_verifiable(self):
        first = self.wb.build_pack(release="main", catalog="core", output_dir=self.tmp / "a")
        second = self.wb.build_pack(release="main", catalog="core", output_dir=self.tmp / "b")
        self.assertEqual(Path(first["pack_path"]).read_bytes(), Path(second["pack_path"]).read_bytes())
        self.assertEqual(first["manifest"]["pack_sha256"], second["manifest"]["pack_sha256"])
        manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["catalog"], "core")
        verified = self.wb.verify_pack(first["manifest_path"])
        self.assertTrue(verified["ok"], verified["errors"])


if __name__ == "__main__":
    unittest.main()
