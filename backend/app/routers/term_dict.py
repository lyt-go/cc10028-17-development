"""术语字典导入与关系端点重算的专用接口。

通用资源路由 /api/{module} 只能做列表/登记/动作，这里承载字典文件导入、
候选审核、冲突裁决以及关系端点重算等真实业务动作。路由在资源路由之前
注册，避免被 /api/{module} 动态路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.relation_extract import RelationExtractService
from app.services.term_normalize import TermNormalizeService

router = APIRouter(prefix="/api", tags=["术语字典与端点归并"])


class DictionaryImportPayload(BaseModel):
    dict_version: str
    source: str
    content: str | None = None
    file_path: str | None = None
    filename: str | None = None
    batch_id: str | None = None
    fail_after_rows: int | None = Field(default=None, ge=1)


class CandidateReviewPayload(BaseModel):
    action: str


class ConflictResolvePayload(BaseModel):
    chosen_standard: str


def _ok_or_404(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ----------------------------------------------------------------------
# 术语归一：外部字典导入 / 候选审核 / 冲突裁决
# ----------------------------------------------------------------------

@router.post("/term_normalize/dictionary/import")
def import_dictionary(payload: DictionaryImportPayload) -> dict[str, Any]:
    svc = TermNormalizeService()
    result = svc.import_dictionary(
        dict_version=payload.dict_version,
        source=payload.source,
        file_path=payload.file_path,
        content=payload.content,
        filename=payload.filename,
        batch_id=payload.batch_id,
        fail_after_rows=payload.fail_after_rows,
    )
    return _ok_or_404(result)


@router.get("/term_normalize/dictionary/batches/{batch_id}")
def get_import_batch(batch_id: str) -> dict[str, Any]:
    return _ok_or_404(TermNormalizeService().get_import_batch(batch_id))


@router.post("/term_normalize/dictionary/batches/{batch_id}/resume")
def resume_import(batch_id: str) -> dict[str, Any]:
    return _ok_or_404(TermNormalizeService().resume_import(batch_id))


@router.get("/term_normalize/dictionary/candidates")
def list_candidates(status: str | None = None) -> dict[str, Any]:
    items = TermNormalizeService().list_candidates(status=status)
    return {"items": items, "total": len(items)}


@router.get("/term_normalize/dictionary/conflicts")
def list_conflicts(status: str | None = None) -> dict[str, Any]:
    items = TermNormalizeService().list_conflicts(status=status)
    return {"items": items, "total": len(items)}


@router.post("/term_normalize/dictionary/candidates/{candidate_key}/review")
def review_candidate(candidate_key: str, payload: CandidateReviewPayload) -> dict[str, Any]:
    return _ok_or_404(TermNormalizeService().review_candidate(candidate_key, payload.action))


@router.post("/term_normalize/dictionary/conflicts/{conflict_key}/resolve")
def resolve_conflict(conflict_key: str, payload: ConflictResolvePayload) -> dict[str, Any]:
    return _ok_or_404(
        TermNormalizeService().resolve_conflict(conflict_key, payload.chosen_standard))


@router.get("/term_normalize/dictionary/endpoint")
def query_endpoint(term: str) -> dict[str, Any]:
    return TermNormalizeService().query_endpoint(term)


# ----------------------------------------------------------------------
# 关系抽取：读取候选映射重算端点 / 版本还原 / 未归并证据
# ----------------------------------------------------------------------

@router.post("/relation_extract/{entity_set_id}/resolve-endpoints")
def resolve_endpoints(entity_set_id: str) -> dict[str, Any]:
    return _ok_or_404(RelationExtractService().resolve_relation_endpoints(entity_set_id))


@router.get("/relation_extract/{entity_set_id}/endpoint-versions")
def endpoint_versions(entity_set_id: str) -> dict[str, Any]:
    items = RelationExtractService().list_endpoint_versions(entity_set_id)
    return {"items": items, "total": len(items)}


@router.get("/relation_extract/{entity_set_id}/endpoints/{version}")
def restore_endpoints(entity_set_id: str, version: str) -> dict[str, Any]:
    return _ok_or_404(RelationExtractService().restore_endpoint_snapshot(entity_set_id, version))


@router.get("/relation_extract/{entity_set_id}/unmerged-evidence")
def unmerged_evidence(entity_set_id: str, version: str | None = None) -> dict[str, Any]:
    items = RelationExtractService().list_unmerged_evidence(entity_set_id, version=version)
    return {"items": items, "total": len(items)}


@router.get("/relation_extract/{entity_set_id}/query-endpoint")
def query_relation_endpoint(entity_set_id: str, term: str) -> dict[str, Any]:
    return _ok_or_404(RelationExtractService().query_relations_by_endpoint(entity_set_id, term))
