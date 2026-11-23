"""外部字典导入→候选审核→关系端点重算的端到端验收测试。

验收要点：
1. 字典文件导入生成候选映射，同简称冲突形成冲突组，独有词为无冲突候选；
2. 无冲突候选可独立生效；争议期间旧端点继续可查，裁决后才归并；
3. 每次归并保留原词、字典版本和来源；冲突判定与上传行顺序无关；
4. 导入中断恢复后已落盘候选只写一次；
5. 关系抽取读取候选映射关联实体身份与确认版本，重算端点并记录未归并证据；
6. 端点产物版本化，已交付旧关系可还原当时词义；
7. 同内容不同顺序导入得到同一组冲突。
"""
import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.services.base import DATA_ROOT
from app.services.chunk_split import ChunkSplitService
from app.services.entity_recognize import EntityRecognizeService
from app.services.format_parse import FormatParseService
from app.services.relation_extract import RelationExtractService
from app.services.term_normalize import (
    TermNormalizeService,
    candidate_key,
    conflict_key,
)
from app.store import store

DICT_VERSION = "dict-v2026.11"
DICT_SOURCE = "外部机构词表"

# 同一简称「中科」指向两个标准词（冲突），另带一个独有词「智研院」
DICT_ROWS = [
    ("中科", "中科数字集团", "ORG"),
    ("中科", "中科智造股份", "ORG"),
    ("智研院", "智能技术研究院", "ORG"),
]


def _write_dict_csv(path, rows):
    lines = ["源术语,标准术语,术语类别"]
    lines.extend(f"{alias},{standard},{category}" for alias, standard, category in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_relations(entity_set_id: str) -> str:
    """跑通 解析→分块→实体→关系 流水线，返回实体集ID。"""
    parse_svc = FormatParseService()
    task, _ = parse_svc.create_entry({
        "任务编号": f"PR-{entity_set_id}", "文档编号": entity_set_id, "解析器类型": "PDF",
    })
    parse_svc.execute_parse(task["id"])
    ChunkSplitService().split_from_document(f"{entity_set_id}_parsed")
    EntityRecognizeService().recognize_from_chunk(f"{entity_set_id}_parsed")
    RelationExtractService().extract_from_entities(f"{entity_set_id}_parsed")
    return f"{entity_set_id}_parsed"


def _seed_relations_with_alias(entity_set_id: str, aliases: list[str]) -> None:
    """在关系产物中植入含目标简称端点的关系，供端点重算验证。"""
    rel_file = DATA_ROOT / "relations" / f"{entity_set_id}_relations.jsonl"
    existing = RelationExtractService()._read_jsonl(rel_file)
    extra = []
    for idx, alias in enumerate(aliases, start=1):
        extra.append({
            "relation_id": f"REL-SEED{idx:03d}",
            "head_entity": alias,
            "head_type": "ORG",
            "tail_entity": "Beijing",
            "tail_type": "LOC",
            "relation_type": "LOCATE",
            "confidence": 0.9,
            "evidence_sentence": f"{alias} 位于 Beijing",
            "method": "Relation-BERT",
            "model_version": "v2.3.0",
            "extracted_at": 0,
        })
    RelationExtractService()._write_jsonl(rel_file, extra)
    assert len(existing) + len(extra) > 0


class TestDictionaryImportConflict:
    """验收1/3：导入含重复简称和独有词的字典，冲突检测与行序无关。"""

    def test_import_generates_candidates_and_conflict_group(self, tmp_path):
        svc = TermNormalizeService()
        dict_file = tmp_path / "dict.csv"
        _write_dict_csv(dict_file, DICT_ROWS)
        result = svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(dict_file), filename="dict.csv",
            batch_id=f"BATCH-A-{os.getpid()}",
        )
        assert "error" not in result, result
        assert result["total_rows"] == 3
        # 独有词无冲突，两个同简称记录进入待裁决
        assert result["clean_candidates"] == 1
        assert result["conflict_candidates"] == 2
        assert result["conflict_groups"] == 1

        candidates = svc.list_candidates()
        batch_candidates = [c for c in candidates if c["batch_id"] == result["batch_id"]]
        # 每条候选都保留原词、字典版本和来源
        for cand in batch_candidates:
            assert cand["original_term"] == cand["alias"]
            assert cand["dict_version"] == DICT_VERSION
            assert cand["source"] == DICT_SOURCE
            assert cand["candidate_key"].startswith("TCK-")

    def test_same_content_different_order_same_conflict(self, tmp_path):
        """同内容不同顺序导入：冲突组键一致，候选键一致，与上传行顺序无关。"""
        svc = TermNormalizeService()
        order_a = tmp_path / "a.csv"
        order_b = tmp_path / "b.csv"
        rows_b = [DICT_ROWS[1], DICT_ROWS[2], DICT_ROWS[0]]
        _write_dict_csv(order_a, DICT_ROWS)
        _write_dict_csv(order_b, rows_b)

        r_a = svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(order_a), batch_id="BATCH-ORDER-A")
        r_b = svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(order_b), batch_id="BATCH-ORDER-B")

        # 不同顺序查出同一组冲突（冲突键相同）
        assert r_a["conflict_keys"] == r_b["conflict_keys"]
        expected_ckey = conflict_key("中科", ["中科数字集团", "中科智造股份"])
        assert r_a["conflict_keys"] == [expected_ckey]

        # 逐行比对：内容相同的行候选幂等键相同，顺序不影响身份
        keys_a = {c["candidate_key"] for c in svc.list_candidates()
                  if c["batch_id"] == "BATCH-ORDER-A"}
        keys_b = {c["candidate_key"] for c in svc.list_candidates()
                  if c["batch_id"] == "BATCH-ORDER-B"}
        assert keys_a == keys_b
        for alias, standard, _ in DICT_ROWS:
            assert candidate_key(alias, standard, DICT_VERSION, DICT_SOURCE) in keys_a


class TestReviewAndLegacyEndpoint:
    """验收2：无冲突独立生效；争议期旧端点可用，裁决后归并。"""

    def test_clean_candidate_takes_effect_independently(self, tmp_path):
        svc = TermNormalizeService()
        dict_file = tmp_path / "dict.csv"
        _write_dict_csv(dict_file, DICT_ROWS)
        result = svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(dict_file), batch_id="BATCH-CLEAN")

        clean = next(c for c in svc.list_candidates(status="待生效")
                     if c["batch_id"] == result["batch_id"])
        review = svc.review_candidate(clean["candidate_key"], "确认生效")
        assert review["ok"] is True
        # 冲突候选仍在待裁决，无冲突候选已经独立生效
        active = svc.active_alias_map()
        assert "智研院" in active
        assert active["智研院"][0]["standard_term"] == "智能技术研究院"
        # 工作台术语映射表同步一条
        rows = [r for r in store.rows("term_normalize")
                if r.get("映射编号") == clean["candidate_key"]]
        assert len(rows) == 1 and rows[0]["status"] == "已生效"

        # 冲突候选不能跳过裁决直接生效
        disputed = svc.list_candidates(status="待裁决")[0]
        blocked = svc.review_candidate(disputed["candidate_key"], "确认生效")
        assert "error" in blocked

    def test_legacy_endpoint_survives_dispute_and_resolves_afterward(self, tmp_path):
        svc = TermNormalizeService()
        dict_file = tmp_path / "dict.csv"
        _write_dict_csv(dict_file, DICT_ROWS)
        svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(dict_file), batch_id="BATCH-LEGACY")

        # 争议期间：旧端点继续可用，不做归并
        during = svc.query_endpoint("中科")
        assert during["legacy_during_dispute"] is True
        assert during["resolvable"] is False
        assert set(during["candidate_standards"]) == {"中科数字集团", "中科智造股份"}

        # 裁决后：查询指向被选中的实体身份与确认版本
        conflict = svc.list_conflicts(status="待裁决")[0]
        verdict = svc.resolve_conflict(conflict["conflict_key"], "中科智造股份")
        assert verdict["ok"] is True
        after = svc.query_endpoint("中科")
        assert after["resolvable"] is True
        assert after["standard_term"] == "中科智造股份"
        assert after["dict_version"] == DICT_VERSION
        assert after["identity_id"].startswith("IDENT-")

        # 落选候选保留为已驳回，证据不删除
        rejected = [c for c in svc.list_candidates(status="已驳回")
                    if c.get("conflict_key") == conflict["conflict_key"]]
        assert len(rejected) == 1
        assert rejected[0]["standard_term"] == "中科数字集团"


class TestImportResumeIdempotency:
    """验收4：导入中断恢复，已落盘候选只写一次。"""

    def test_interrupted_resume_writes_each_candidate_once(self, tmp_path):
        svc = TermNormalizeService()
        dict_file = tmp_path / "dict.csv"
        _write_dict_csv(dict_file, DICT_ROWS)

        first = svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(dict_file), batch_id="BATCH-RESUME",
            fail_after_rows=2,
        )
        assert first["status"] == "interrupted"
        assert first["processed_rows"] == 2

        # 再次调用同批次导入应自动转入恢复，不会重写已落盘候选
        second = svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(dict_file), batch_id="BATCH-RESUME")
        assert second["resumed"] is True
        assert second["status"] == "completed"
        assert second["processed_rows"] == 3

        candidate_file = DATA_ROOT / "dict_candidates" / "candidates.jsonl"
        all_candidates = svc._read_jsonl(candidate_file)
        batch_candidates = [c for c in all_candidates if c["batch_id"] == "BATCH-RESUME"]
        # 3 行内容对应 3 条候选，无重复写入
        assert len(batch_candidates) == 3
        keys = [c["candidate_key"] for c in batch_candidates]
        assert len(keys) == len(set(keys))

        # 完成后再恢复直接返回已完成，不产生任何重复
        again = svc.resume_import("BATCH-RESUME")
        assert again["status"] == "already_completed"
        batch_candidates = [c for c in svc._read_jsonl(candidate_file)
                            if c["batch_id"] == "BATCH-RESUME"]
        assert len(batch_candidates) == 3


class TestRelationEndpointResolution:
    """验收5/6：关系抽取读取候选映射重算端点，版本化并记录未归并证据。"""

    def _prepare(self, tmp_path):
        svc = TermNormalizeService()
        dict_file = tmp_path / "dict.csv"
        _write_dict_csv(dict_file, DICT_ROWS)
        svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(dict_file), batch_id="BATCH-REL")
        entity_set_id = f"E2E-{os.getpid()}-{id(tmp_path) % 100000}"
        full_id = _build_relations(entity_set_id)
        _seed_relations_with_alias(full_id, ["中科", "智研院", "未登录简称"])
        return svc, full_id

    def test_endpoint_recompute_before_and_after_review(self, tmp_path):
        svc, full_id = self._prepare(tmp_path)
        rel_svc = RelationExtractService()
        # 流水线内 extract_from_entities 已自动产出一版端点，显式重算顺延版本号
        baseline = rel_svc._read_json_pointer(rel_svc._latest_pointer(full_id))["latest_version"]

        # 审核前重算：植入的简称端点全部未归并（含一条未登录词），并产出未归并证据
        before = rel_svc.resolve_relation_endpoints(full_id)
        before_ver = f"v{baseline + 1}"
        assert before["latest_version"] == baseline + 1
        assert before["merged_endpoints"] == 0
        evidence_before = rel_svc.list_unmerged_evidence(full_id, before_ver)
        reasons = {e["endpoint_original"]: e["reason"] for e in evidence_before}
        assert "未登录简称" in reasons
        assert "中科" in reasons and "冲突" in reasons["中科"]

        # 无冲突候选独立生效 + 冲突裁决
        clean = next(c for c in svc.list_candidates(status="待生效")
                     if c["batch_id"] == "BATCH-REL")
        svc.review_candidate(clean["candidate_key"], "确认生效")
        conflict = next(c for c in svc.list_conflicts(status="待裁决")
                        if "BATCH-REL" in c["batch_ids"])
        svc.resolve_conflict(conflict["conflict_key"], "中科智造股份")

        # 审核后重新打开关系结果：受影响端点重算为新版本
        after = rel_svc.resolve_relation_endpoints(full_id)
        after_ver = f"v{baseline + 2}"
        assert after["latest_version"] == baseline + 2
        merged_records = [
            r for r in rel_svc._read_jsonl(
                DATA_ROOT / "relation_endpoints" / f"{full_id}_relation_endpoints_{after_ver}.jsonl")
            if r["resolution"] == "merged"
        ]
        merged_terms = {r["endpoint_original"]: r for r in merged_records}
        assert set(merged_terms) == {"中科", "智研院"}
        # 关联实体身份与确认版本，原词保留
        zk = merged_terms["中科"]
        assert zk["standard_term"] == "中科智造股份"
        assert zk["entity_identity_id"].startswith("IDENT-")
        assert zk["dict_version"] == DICT_VERSION
        assert zk["endpoint_original"] == "中科"
        zy = merged_terms["智研院"]
        assert zy["standard_term"] == "智能技术研究院"

        # 未登录词仍未归并，继续留证据
        evidence_after = rel_svc.list_unmerged_evidence(full_id, after_ver)
        assert any(e["endpoint_original"] == "未登录简称" for e in evidence_after)
        assert not any(e["endpoint_original"] == "中科" for e in evidence_after)

    def test_versioned_snapshots_restore_legacy_meaning(self, tmp_path):
        svc, full_id = self._prepare(tmp_path)
        rel_svc = RelationExtractService()
        baseline = rel_svc._read_json_pointer(rel_svc._latest_pointer(full_id))["latest_version"]
        before_snapshot = rel_svc.resolve_relation_endpoints(full_id)
        before_ver = f"v{before_snapshot['latest_version']}"
        clean = next(c for c in svc.list_candidates(status="待生效")
                     if c["batch_id"] == "BATCH-REL")
        svc.review_candidate(clean["candidate_key"], "确认生效")
        after_snapshot = rel_svc.resolve_relation_endpoints(full_id)
        after_ver = f"v{after_snapshot['latest_version']}"

        versions = rel_svc.list_endpoint_versions(full_id)
        assert versions[-2]["version"] == before_ver and versions[-1]["version"] == after_ver
        # 还原审核前版本：已交付旧关系当时的词义仍是原词、未归并状态
        old = rel_svc.restore_endpoint_snapshot(full_id, before_ver)
        assert "error" not in old
        zy_old = next(r for r in old["endpoints"] if r["endpoint_original"] == "智研院")
        assert zy_old["resolution"] == "unmerged"
        assert zy_old["standard_term"] is None
        # 新版本同端点已归并，两版并存不互相覆盖
        new = rel_svc.restore_endpoint_snapshot(full_id, after_ver)
        zy_new = next(r for r in new["endpoints"] if r["endpoint_original"] == "智研院")
        assert zy_new["resolution"] == "merged"
        assert zy_new["standard_term"] == "智能技术研究院"

    def test_query_hits_both_legacy_term_and_identity(self, tmp_path):
        svc, full_id = self._prepare(tmp_path)
        rel_svc = RelationExtractService()
        clean = next(c for c in svc.list_candidates(status="待生效")
                     if c["batch_id"] == "BATCH-REL")
        svc.review_candidate(clean["candidate_key"], "确认生效")
        rel_svc.resolve_relation_endpoints(full_id)

        # 用原简称查得到关系，用标准词（新端点）也查得到同一关系
        by_alias = rel_svc.query_relations_by_endpoint(full_id, "智研院")
        by_standard = rel_svc.query_relations_by_endpoint(full_id, "智能技术研究院")
        assert by_alias["matched_relations"] >= 1
        alias_ids = {r["relation_id"] for r in by_alias["relations"]}
        standard_ids = {r["relation_id"] for r in by_standard["relations"]}
        assert alias_ids == standard_ids
        assert "REL-SEED002" in alias_ids


class TestDictionaryImportSafety:
    """脏数据与JSONL入口的兜底。"""

    def test_bad_rows_skipped_and_jsonl_supported(self, tmp_path):
        svc = TermNormalizeService()
        jsonl_file = tmp_path / "dict.jsonl"
        jsonl_file.write_text(
            '{"源术语": "中科", "标准术语": "中科数字集团"}\n'
            '{"源术语": "", "标准术语": "缺原词"}\n'
            "not-a-json-line\n",
            encoding="utf-8",
        )
        result = svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(jsonl_file), batch_id="BATCH-JSONL")
        assert result["total_rows"] == 1
        assert result["clean_candidates"] == 1


class TestServiceRuleRegression:
    """服务元数据与审核规则回归。"""

    def test_relation_action_rules_keep_reject_action(self):
        # 防止动作键被误写成状态值，前端动作按钮依赖该键
        assert RelationExtractService.ACTION_RULES["标记驳回"] == "已驳回"

    def test_disputed_candidate_cannot_be_discarded_directly(self, tmp_path):
        svc = TermNormalizeService()
        dict_file = tmp_path / "dict.csv"
        _write_dict_csv(dict_file, DICT_ROWS)
        svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(dict_file), batch_id="BATCH-GUARD")
        disputed = next(c for c in svc.list_candidates(status="待裁决")
                        if c["batch_id"] == "BATCH-GUARD")
        blocked = svc.review_candidate(disputed["candidate_key"], "标记废弃")
        assert "error" in blocked
        # 冲突组裁决流程仍然可用
        conflict = svc.list_conflicts(status="待裁决")[0]
        verdict = svc.resolve_conflict(conflict["conflict_key"], "中科数字集团")
        assert verdict["ok"] is True

    def test_later_batch_inherits_resolved_verdict(self, tmp_path):
        """已裁决冲突组对后续批次同内容候选自动沿用裁决，不再卡在待裁决。"""
        svc = TermNormalizeService()
        first_file = tmp_path / "a.csv"
        _write_dict_csv(first_file, DICT_ROWS)
        svc.import_dictionary(
            dict_version=DICT_VERSION, source=DICT_SOURCE,
            file_path=str(first_file), batch_id="BATCH-PRIOR")
        conflict = svc.list_conflicts(status="待裁决")[0]
        svc.resolve_conflict(conflict["conflict_key"], "中科智造股份")

        second_file = tmp_path / "b.csv"
        _write_dict_csv(second_file, DICT_ROWS)
        second = svc.import_dictionary(
            dict_version="dict-v2026.12", source=DICT_SOURCE,
            file_path=str(second_file), batch_id="BATCH-NEXT")
        # 新批次没有新增冲突组（同内容沿用同一冲突键）
        assert second["conflict_groups"] == 1
        next_candidates = [c for c in svc.list_candidates() if c["batch_id"] == "BATCH-NEXT"]
        statuses = {(c["alias"], c["standard_term"]): c["status"] for c in next_candidates}
        assert statuses[("中科", "中科智造股份")] == "已生效"
        assert statuses[("中科", "中科数字集团")] == "已驳回"
        assert statuses[("智研院", "智能技术研究院")] == "待生效"
