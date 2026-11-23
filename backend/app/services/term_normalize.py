"""术语归一服务：抽取术语到标准术语的映射、同义词归并与消歧规则生效版本。

从实体识别结果读取术语并进行同义词归并和标准化映射，持久化术语映射表。

外部字典导入能力（无外部词表入口的补齐）：
- 导入 CSV/JSONL 字典文件，生成「导入术语候选映射」，同义词经审核后可作为
  关系抽取的新端点；
- 同一批次内同一简称指向不同标准词时形成冲突组，冲突判定基于内容哈希，
  与上传行顺序无关；
- 每条候选保留原词、字典版本与来源；无冲突候选可独立生效，冲突候选在
  裁决前不阻断其他候选，旧端点在争议期间继续可查；
- 导入中断后可凭检查点恢复，已确认/已落盘的候选按幂等键只写一次。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 字典文件表头别名：兼容不同出处的外部词表
HEADER_ALIAS = {
    "source_term": ["源术语", "别名", "简称", "原词", "source_term", "alias"],
    "standard_term": ["标准术语", "标准词", "标准名", "standard_term", "standard"],
    "category": ["术语类别", "类别", "实体类型", "category"],
    "source": ["来源", "映射来源", "出处", "source"],
}

# 候选/冲突生命周期状态
CANDIDATE_PENDING = "待生效"        # 无冲突候选，等待审核确认
CANDIDATE_CONFIRMED = "已生效"      # 审核确认，参与端点归并
CANDIDATE_DISPUTED = "待裁决"       # 同简称冲突，等待人工裁决
CANDIDATE_REJECTED = "已驳回"       # 冲突裁决未中选，记录保留不删除
CANDIDATE_DISCARDED = "已废弃"      # 生效后废弃，旧版本仍可追溯


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def candidate_key(alias: str, standard: str, dict_version: str, source: str) -> str:
    """候选映射幂等键：同一原词+标准词+字典版本+来源永远是同一条，与行序无关。"""
    raw = "|".join([alias.strip(), standard.strip(), dict_version.strip(), source.strip()])
    return f"TCK-{_sha1(raw)[:16]}"


def conflict_key(alias: str, standards: list[str]) -> str:
    """冲突组键：简称 + 标准词集合（排序后哈希），不同导入顺序得到同一冲突。"""
    raw = alias.strip() + "|" + ",".join(sorted({s.strip() for s in standards}))
    return f"TCF-{_sha1(raw)[:16]}"


def identity_id(standard: str) -> str:
    """标准术语对应的命名实体身份ID，关系端点归并后指向该身份。"""
    return f"IDENT-{_sha1(standard.strip())[:12]}"


class TermNormalizeService(BaseModuleService):
    MODULE = "term_normalize"
    ENTITY = "术语映射"
    OPERATION_TERM = "归并"
    LIST_FIELDS = ["映射编号", "源术语", "标准术语", "术语类别", "消歧规则", "同义词组", "生效版本", "映射来源"]
    REQUIRED_FIELDS = ["映射编号", "源术语", "标准术语"]
    STATUS_ORDER = ["草稿", "待映射", "已生效", "已废弃"]
    ACTION_RULES = {"提交映射": "待映射", "确认生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = ["标记废弃"]

    TERM_DICTIONARY = {
        "AI": ["人工智能", "机器学习", "深度学习"],
        "知识抽取": ["信息抽取", "实体抽取", "知识获取"],
        "文档解析": ["文件解析", "文本解析", "版式识别"],
        "知识图谱": ["语义网络", "本体库", "图数据库"],
        "OCR": ["光学字符识别", "文字识别", "图像识别"],
    }

    # ------------------------------------------------------------------
    # 外部字典导入
    # ------------------------------------------------------------------

    def _dict_dir(self, sub: str) -> Path:
        return self._data_dir(f"dict_{sub}")

    def _candidate_file(self) -> Path:
        return self._dict_dir("candidates") / "candidates.jsonl"

    def _conflict_file(self) -> Path:
        return self._dict_dir("candidates") / "conflicts.jsonl"

    def _import_file(self, batch_id: str) -> Path:
        return self._dict_dir("imports") / f"{batch_id}_import.jsonl"

    def _import_checkpoint(self, batch_id: str) -> Path:
        return self._dict_dir("imports") / f"{batch_id}_checkpoint.json"

    def import_dictionary(
        self,
        *,
        dict_version: str,
        source: str,
        file_path: str | None = None,
        content: str | None = None,
        filename: str | None = None,
        batch_id: str | None = None,
        fail_after_rows: int | None = None,
    ) -> dict[str, Any]:
        """导入外部字典文件并生成术语候选映射。

        冲突检测对同简称指向多个标准词的记录建冲突组，判定只依赖内容不依赖
        行序；无冲突候选进入待生效，冲突候选进入待裁决，二者互不阻塞。
        ``fail_after_rows`` 用于模拟导入中断：处理指定行数后保留现场并返回
        interrupted，随后可调用 :meth:`resume_import` 续跑。
        """
        batch_id = batch_id or f"DICT-{time.strftime('%Y%m%d%H%M%S')}-{_sha1(dict_version + source)[:6]}"
        rows, parse_errors = self._parse_dictionary_file(file_path=file_path, content=content)
        if not rows:
            return {"error": "字典文件为空或缺少「源术语/标准术语」列", "parse_errors": parse_errors}

        # 已存在检查点则直接走恢复路径，避免重复导入产生重复候选
        checkpoint = self._read_checkpoint_file(self._import_checkpoint(batch_id))
        if checkpoint is not None:
            return self.resume_import(batch_id, fail_after_rows=fail_after_rows)

        # 落一份上传原文快照（保留原词与文件行号），中断恢复也以它为准
        staged = []
        for idx, row in enumerate(rows, start=1):
            staged.append({
                "batch_id": batch_id,
                "file_row": idx,
                "filename": filename or (Path(file_path).name if file_path else "inline-content"),
                "alias": row["source_term"].strip(),
                "standard_term": row["standard_term"].strip(),
                "category": (row.get("category") or "OTHER").strip() or "OTHER",
                "row_source": (row.get("source") or source).strip() or source,
                "dict_version": dict_version,
                "source": source,
                "staged_at": time.time(),
            })
        self._write_jsonl(self._import_file(batch_id), staged)

        checkpoint_state = {
            "batch_id": batch_id,
            "dict_version": dict_version,
            "source": source,
            "filename": filename,
            "total_rows": len(staged),
            "processed_rows": 0,
            "written_keys": [],
            "status": "running",
        }
        self._write_checkpoint_file(self._import_checkpoint(batch_id), checkpoint_state)
        return self._process_import(batch_id, fail_after_rows=fail_after_rows)

    def resume_import(self, batch_id: str, *, fail_after_rows: int | None = None) -> dict[str, Any]:
        """从中断点继续导入：已落盘候选按幂等键跳过，保证只写一次。"""
        checkpoint = self._read_checkpoint_file(self._import_checkpoint(batch_id))
        if checkpoint is None:
            return {"error": f"导入批次 {batch_id} 不存在检查点，无法恢复"}
        if checkpoint.get("status") == "completed":
            return self._import_summary(batch_id, resumed=False, already_completed=True)
        return self._process_import(batch_id, fail_after_rows=fail_after_rows)

    def _process_import(self, batch_id: str, *, fail_after_rows: int | None = None) -> dict[str, Any]:
        checkpoint = self._read_checkpoint_file(self._import_checkpoint(batch_id))
        staged = self._read_jsonl(self._import_file(batch_id))
        # 幂等去重限定在同一导入批次内：不同批次导入相同内容仍各自留痕，
        # 同批次中断恢复时已落盘候选只写一次
        existing_keys = {
            c["candidate_key"] for c in self._read_jsonl(self._candidate_file())
            if c["batch_id"] == batch_id
        }
        written_keys = set(checkpoint.get("written_keys", []))
        start_row = int(checkpoint.get("processed_rows", 0))

        # 先按简称聚合标准词集合做冲突判定——聚合前排序，结果与上传行顺序无关
        alias_standards: dict[str, set[str]] = {}
        for row in staged:
            alias_standards.setdefault(row["alias"], set()).add(row["standard_term"])
        conflict_aliases = {alias for alias, standards in alias_standards.items() if len(standards) > 1}

        processed = start_row
        interrupted = False
        for row in staged[start_row:]:
            key = candidate_key(row["alias"], row["standard_term"], row["dict_version"], row["source"])
            if key not in existing_keys and key not in written_keys:
                is_conflict = row["alias"] in conflict_aliases
                candidate = {
                    "candidate_key": key,
                    "batch_id": batch_id,
                    "alias": row["alias"],
                    "standard_term": row["standard_term"],
                    "category": row["category"],
                    "original_term": row["alias"],
                    "dict_version": row["dict_version"],
                    "source": row["source"],
                    "row_source": row["row_source"],
                    "file_row": row["file_row"],
                    "filename": row["filename"],
                    "status": CANDIDATE_DISPUTED if is_conflict else CANDIDATE_PENDING,
                    "conflict_key": conflict_key(row["alias"], sorted(alias_standards[row["alias"]]))
                    if is_conflict else None,
                    "identity_id": identity_id(row["standard_term"]),
                    "created_at": time.time(),
                    "confirmed_at": None,
                }
                self._write_jsonl(self._candidate_file(), [candidate])
                existing_keys.add(key)
                written_keys.add(key)
                if is_conflict:
                    prior = self._record_conflict(row, alias_standards[row["alias"]], batch_id)
                    # 该冲突组在历史批次已裁决：新批次同内容候选沿用既有裁决，
                    # 选中标准词直接生效（同步工作台），其余驳回留痕
                    if prior is not None and prior["status"] == "已裁决":
                        if row["standard_term"] == prior["chosen_standard"]:
                            self._update_candidate(key, {
                                "status": CANDIDATE_CONFIRMED, "confirmed_at": time.time()})
                            candidate["status"] = CANDIDATE_CONFIRMED
                            self._mirror_to_store(candidate)
                        else:
                            self._update_candidate(key, {"status": CANDIDATE_REJECTED})

            processed += 1
            checkpoint.update({
                "processed_rows": processed,
                "written_keys": sorted(written_keys),
            })
            # 每行推进一次检查点，中断时已确认候选不会因重跑而重复写入
            self._write_checkpoint_file(self._import_checkpoint(batch_id), checkpoint)

            if fail_after_rows is not None and processed - start_row >= fail_after_rows:
                checkpoint["status"] = "interrupted"
                self._write_checkpoint_file(self._import_checkpoint(batch_id), checkpoint)
                interrupted = True
                break

        if not interrupted:
            checkpoint["status"] = "completed"
            checkpoint["completed_at"] = time.time()
            self._write_checkpoint_file(self._import_checkpoint(batch_id), checkpoint)
        return self._import_summary(batch_id, resumed=start_row > 0, interrupted=interrupted)

    def _record_conflict(self, row: dict[str, Any], standards: set[str],
                         batch_id: str) -> dict[str, Any] | None:
        """冲突组按内容键幂等落盘：同内容换序导入仍指向同一冲突组。

        返回已存在的冲突组记录（含历史裁决），不存在则新建并返回 None。
        """
        ckey = conflict_key(row["alias"], sorted(standards))
        records = self._read_jsonl(self._conflict_file())
        for rec in records:
            if rec["conflict_key"] == ckey:
                # 同一冲突被多个批次命中时合并批次来源，不重复建组
                if batch_id not in rec["batch_ids"]:
                    rec["batch_ids"].append(batch_id)
                    self._rewrite_jsonl(self._conflict_file(), records)
                return rec
        self._write_jsonl(self._conflict_file(), [{
            "conflict_key": ckey,
            "alias": row["alias"],
            "candidate_standards": sorted(standards),
            "batch_ids": [batch_id],
            "status": "待裁决",
            "detected_at": time.time(),
            "resolved_at": None,
            "chosen_standard": None,
        }])
        return None

    def _import_summary(self, batch_id: str, *, resumed: bool, interrupted: bool = False,
                        already_completed: bool = False) -> dict[str, Any]:
        checkpoint = self._read_checkpoint_file(self._import_checkpoint(batch_id))
        candidates = [c for c in self._read_jsonl(self._candidate_file()) if c["batch_id"] == batch_id]
        conflicts = [c for c in self._read_jsonl(self._conflict_file())
                     if batch_id in c.get("batch_ids", [])]
        return {
            "batch_id": batch_id,
            "dict_version": checkpoint.get("dict_version"),
            "source": checkpoint.get("source"),
            "total_rows": checkpoint.get("total_rows"),
            "processed_rows": checkpoint.get("processed_rows"),
            "status": "already_completed" if already_completed else checkpoint.get("status"),
            "resumed": resumed,
            "candidates": len(candidates),
            "conflict_candidates": sum(1 for c in candidates if c["status"] == CANDIDATE_DISPUTED),
            "clean_candidates": sum(1 for c in candidates if c["status"] == CANDIDATE_PENDING),
            "conflict_groups": len(conflicts),
            "conflict_keys": [c["conflict_key"] for c in conflicts],
        }

    def get_import_batch(self, batch_id: str) -> dict[str, Any]:
        checkpoint = self._read_checkpoint_file(self._import_checkpoint(batch_id))
        if checkpoint is None:
            return {"error": f"导入批次 {batch_id} 不存在"}
        staged = self._read_jsonl(self._import_file(batch_id))
        candidates = [c for c in self._read_jsonl(self._candidate_file()) if c["batch_id"] == batch_id]
        return {
            "checkpoint": checkpoint,
            "staged_rows": staged,
            "candidates": candidates,
        }

    # ------------------------------------------------------------------
    # 候选审核与冲突裁决
    # ------------------------------------------------------------------

    def list_candidates(self, status: str | None = None) -> list[dict[str, Any]]:
        candidates = self._read_jsonl(self._candidate_file())
        if status:
            candidates = [c for c in candidates if c["status"] == status]
        return candidates

    def list_conflicts(self, status: str | None = None) -> list[dict[str, Any]]:
        conflicts = self._read_jsonl(self._conflict_file())
        if status:
            conflicts = [c for c in conflicts if c["status"] == status]
        return conflicts

    def review_candidate(self, candidate_key_id: str, action: str) -> dict[str, Any]:
        """审核单条候选：无冲突候选可独立确认生效，不被其他批次的冲突阻塞。"""
        candidates = self._read_jsonl(self._candidate_file())
        target = next((c for c in candidates if c["candidate_key"] == candidate_key_id), None)
        if target is None:
            return {"error": f"候选 {candidate_key_id} 不存在"}
        if action == "确认生效":
            if target["status"] == CANDIDATE_DISPUTED:
                return {"error": "冲突候选必须先在冲突组中裁决，不能直接生效"}
            if target["status"] == CANDIDATE_CONFIRMED:
                return {"ok": True, "message": "候选已生效，重复审核不会重复写入", "candidate": target}
            self._update_candidate(candidate_key_id, {
                "status": CANDIDATE_CONFIRMED, "confirmed_at": time.time()})
        elif action == "标记废弃":
            if target["status"] == CANDIDATE_DISPUTED:
                return {"error": "冲突候选必须先在冲突组中裁决，不能单独废弃"}
            self._update_candidate(candidate_key_id, {"status": CANDIDATE_DISCARDED})
        else:
            return {"error": f"不支持的审核动作：{action}"}
        target = next(c for c in self._read_jsonl(self._candidate_file())
                      if c["candidate_key"] == candidate_key_id)
        self._mirror_to_store(target)
        return {"ok": True, "message": f"候选已{action}", "candidate": target}

    def resolve_conflict(self, conflict_key_id: str, chosen_standard: str) -> dict[str, Any]:
        """裁决冲突组：选中的标准词对应候选生效，其余候选驳回但证据保留。

        争议期间已经在用旧标准词查询的关系端点不受影响；裁决后新查询才指向
        被选中的实体身份。
        """
        conflicts = self._read_jsonl(self._conflict_file())
        conflict = next((c for c in conflicts if c["conflict_key"] == conflict_key_id), None)
        if conflict is None:
            return {"error": f"冲突组 {conflict_key_id} 不存在"}
        if chosen_standard not in conflict["candidate_standards"]:
            return {"error": f"裁决标准词必须属于冲突候选：{conflict['candidate_standards']}"}

        candidates = self._read_jsonl(self._candidate_file())
        chosen = None
        for cand in candidates:
            if cand.get("conflict_key") != conflict_key_id:
                continue
            if cand["standard_term"] == chosen_standard and cand["status"] == CANDIDATE_DISPUTED:
                self._update_candidate(cand["candidate_key"], {
                    "status": CANDIDATE_CONFIRMED, "confirmed_at": time.time()})
                chosen = cand
            elif cand["status"] == CANDIDATE_DISPUTED:
                self._update_candidate(cand["candidate_key"], {"status": CANDIDATE_REJECTED})
        self._update_conflict(conflict_key_id, {
            "status": "已裁决", "resolved_at": time.time(), "chosen_standard": chosen_standard})
        if chosen:
            self._mirror_to_store(chosen)
        return {
            "ok": True,
            "message": f"冲突组已裁决，选用「{chosen_standard}」",
            "conflict_key": conflict_key_id,
            "chosen_candidate": chosen,
        }

    def _mirror_to_store(self, candidate: dict[str, Any]) -> None:
        """生效候选同步一条术语映射到工作台内存表，幂等不重复。"""
        rows = store.rows(self.MODULE)
        if any(str(r.get("映射编号")) == candidate["candidate_key"] for r in rows):
            return
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": new_id,
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "映射编号": candidate["candidate_key"],
            "源术语": candidate["alias"],
            "标准术语": candidate["standard_term"],
            "术语类别": candidate["category"],
            "消歧规则": f"外部字典 {candidate['dict_version']} 审核生效",
            "同义词组": candidate["alias"],
            "生效版本": candidate["dict_version"],
            "映射来源": candidate["source"],
        })

    # ------------------------------------------------------------------
    # 生效索引与争议期旧端点查询
    # ------------------------------------------------------------------

    def active_alias_map(self) -> dict[str, list[dict[str, Any]]]:
        """已生效候选按别名聚合：关系抽取据此把同义词端点归并到实体身份。"""
        index: dict[str, list[dict[str, Any]]] = {}
        for cand in self._read_jsonl(self._candidate_file()):
            if cand["status"] != CANDIDATE_CONFIRMED:
                continue
            index.setdefault(cand["alias"], []).append(cand)
        return index

    def disputed_alias_map(self) -> dict[str, list[dict[str, Any]]]:
        """争议中别名（同简称多标准词且未裁决）：旧端点在争议期仍可查询。"""
        index: dict[str, list[dict[str, Any]]] = {}
        for cand in self._read_jsonl(self._candidate_file()):
            if cand["status"] == CANDIDATE_DISPUTED:
                index.setdefault(cand["alias"], []).append(cand)
        return index

    def query_endpoint(self, term: str) -> dict[str, Any]:
        """查询某个词当前可作为什么端点使用。

        - 已归并：返回实体身份与确认版本；
        - 争议中：旧端点保留可用（legacy_during_dispute），同时给出候选；
        - 未归并：提示暂无外部词表映射。
        """
        active = self.active_alias_map().get(term, [])
        if active:
            standards = {c["standard_term"] for c in active}
            if len(standards) == 1:
                c = active[0]
                return {
                    "term": term, "resolvable": True, "identity_id": c["identity_id"],
                    "standard_term": c["standard_term"], "dict_version": c["dict_version"],
                    "source": c["source"], "candidate_key": c["candidate_key"],
                    "legacy_during_dispute": False,
                }
        disputed = self.disputed_alias_map().get(term, [])
        if disputed:
            return {
                "term": term, "resolvable": False, "legacy_during_dispute": True,
                "conflict_key": disputed[0]["conflict_key"],
                "candidate_standards": sorted({c["standard_term"] for c in disputed}),
                "message": "简称存在冲突且未裁决，旧端点继续可用，暂不归并",
            }
        return {"term": term, "resolvable": False, "legacy_during_dispute": False,
                "message": "未命中任何导入术语候选映射"}

    # ------------------------------------------------------------------
    # 文件与检查点辅助
    # ------------------------------------------------------------------

    def _parse_dictionary_file(
        self, *, file_path: str | None, content: str | None
    ) -> tuple[list[dict[str, str]], list[str]]:
        if content is not None:
            text = content
        elif file_path:
            path = Path(file_path)
            if not path.exists():
                return [], [f"文件不存在：{file_path}"]
            text = path.read_text(encoding="utf-8-sig")
        else:
            return [], ["未提供字典文件路径或内容"]

        if file_path and file_path.endswith(".jsonl"):
            return self._parse_jsonl_dict(text)
        return self._parse_csv_dict(text)

    def _parse_csv_dict(self, text: str) -> tuple[list[dict[str, str]], list[str]]:
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            return [], ["CSV 缺少表头"]
        header_map = self._map_headers(reader.fieldnames)
        if "source_term" not in header_map or "standard_term" not in header_map:
            return [], ["CSV 表头需包含源术语/别名与标准术语/标准词列"]
        rows, errors = [], []
        for line_no, raw in enumerate(reader, start=2):
            parsed = {key: (raw.get(col) or "").strip() for key, col in header_map.items()}
            if not parsed["source_term"] or not parsed["standard_term"]:
                errors.append(f"第{line_no}行缺少源术语或标准术语，已跳过")
                continue
            rows.append(parsed)
        return rows, errors

    def _parse_jsonl_dict(self, text: str) -> tuple[list[dict[str, str]], list[str]]:
        rows, errors = [], []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"第{line_no}行不是合法JSON，已跳过")
                continue
            parsed = {key: str(raw.get(col, "")).strip()
                      for key, cols in HEADER_ALIAS.items()
                      for col in cols if col in raw}
            if not parsed.get("source_term") or not parsed.get("standard_term"):
                errors.append(f"第{line_no}行缺少源术语或标准术语，已跳过")
                continue
            rows.append(parsed)
        return rows, errors

    @staticmethod
    def _map_headers(fieldnames: list[str]) -> dict[str, str]:
        header_map: dict[str, str] = {}
        for key, aliases in HEADER_ALIAS.items():
            for name in fieldnames:
                if name.strip() in aliases:
                    header_map[key] = name
                    break
        return header_map

    def _read_checkpoint_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_checkpoint_file(self, path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        state["_updated_at"] = time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)

    def _update_candidate(self, key: str, changes: dict[str, Any]) -> None:
        path = self._candidate_file()
        records = self._read_jsonl(path)
        for rec in records:
            if rec["candidate_key"] == key:
                rec.update(changes)
        self._rewrite_jsonl(path, records)

    def _update_conflict(self, key: str, changes: dict[str, Any]) -> None:
        path = self._conflict_file()
        records = self._read_jsonl(path)
        for rec in records:
            if rec["conflict_key"] == key:
                rec.update(changes)
        self._rewrite_jsonl(path, records)

    def _rewrite_jsonl(self, path: Path, records: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    # ------------------------------------------------------------------
    # 既有实体产物归一流程
    # ------------------------------------------------------------------

    def normalize_from_entities(self, entity_set_id: str) -> dict[str, Any]:
        """从实体识别结果进行术语归一化，将识别到的实体映射到标准术语并持久化。"""
        entity_file = self._data_dir("entities") / f"{entity_set_id}_entities.jsonl"
        entities = self._read_jsonl(entity_file)
        if not entities:
            return {"error": f"实体产物 {entity_set_id} 不存在，请先执行实体识别"}

        mappings = []
# 数据操作：标准化术语数据映射到标准术语数据集
        map_counter = 0
        seen_terms = set()

        for ent in entities:
            term_text = ent.get("entity_text", "")
            if term_text in seen_terms:
                continue
            seen_terms.add(term_text)

            # 在术语字典中查找同义词/标准术语
            for standard, synonyms in self.TERM_DICTIONARY.items():
                for syn in synonyms:
                    if syn in term_text or term_text in syn:
                        map_counter += 1
                        mappings.append({
                            "mapping_id": f"MAP-{map_counter:06d}",
                            "source_term": term_text,
                            "standard_term": standard,
                            "category": ent.get("entity_type", "TECH"),
                            "disambiguation": f"基于同义词组「{standard}」消歧匹配",
# 数据产物：术语映射数据集写入terms目录持久化文件
                            "synonym_group": synonyms,
                            "version": "v1.0",
                            "source": f"entity_{entity_set_id}",
                            "mapped_at": time.time(),
                        })
                        break

        # 对未匹配术语创建待确认映射
        for ent in entities:
            term_text = ent.get("entity_text", "")
            if not any(m["source_term"] == term_text for m in mappings):
                map_counter += 1
                mappings.append({
                    "mapping_id": f"MAP-{map_counter:06d}",
                    "source_term": term_text,
                    "standard_term": f"<待归一: {term_text[:8]}>",
                    "category": ent.get("entity_type", "OTHER"),
                    "disambiguation": "待人工确认标准术语映射",
                    "synonym_group": [],
                    "version": "v1.0-draft",
# 数据文件导入：外部字典文件批量更新术语映射数据集
                    "source": f"entity_{entity_set_id}",
                    "mapped_at": time.time(),
                })

        output_path = self._data_dir("terms") / f"{entity_set_id}_term_map.jsonl"
        written = self._write_jsonl(output_path, mappings)

        self._write_checkpoint(f"term_{entity_set_id}", {
            "entity_set_id": entity_set_id,
            "total_terms": len(seen_terms),
            "mapped": sum(1 for m in mappings if "<待归一" not in m["standard_term"]),
            "unmapped": sum(1 for m in mappings if "<待归一" in m["standard_term"]),
        })

        return {
            "entity_set_id": entity_set_id,
            "total_terms": len(seen_terms),
            "mappings": len(mappings),
            "records_written": written,
            "output": str(output_path),
            "mapped_count": sum(1 for m in mappings if "<待归一" not in m["standard_term"]),
        }

    def batch_apply_dictionary(self, terms: list[str]) -> list[dict[str, Any]]:
        """批量对术语列表应用字典匹配，返回映射清单。"""
        results = []
        for term in terms:
            mapped = None
            for standard, synonyms in self.TERM_DICTIONARY.items():
                if any(syn in term for syn in synonyms):
                    mapped = {"source": term, "standard": standard, "status": "已匹配"}
                    break
            if not mapped:
                mapped = {"source": term, "standard": None, "status": "待映射"}
            results.append(mapped)
        return results
