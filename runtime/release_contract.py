"""Canonical release identity contract for YouTube Channel Deep Audit.

This module is intentionally small and dependency-free so validators and release
builders can share exactly the same release identity without duplicating strings.
"""
from __future__ import annotations

CANONICAL_METHODOLOGY_VERSION = "12.1"
CANONICAL_SCHEMA_VERSION = "12.1"
CANONICAL_SCHEMA_ID = "youtube-channel-deep-audit-v12.1.schema.json"
CANONICAL_SCHEMA_TITLE = "YouTube Channel Deep Audit v12.1 Production Report"

def errors(report: dict, schema: dict) -> list[str]:
    md = report.get("metadata", {}) or {}
    problems: list[str] = []
    if md.get("methodology_version") != CANONICAL_METHODOLOGY_VERSION:
        problems.append("metadata.methodology_version is not canonical v12.1")
    if md.get("schema_version") != CANONICAL_SCHEMA_VERSION:
        problems.append("metadata.schema_version is not canonical v12.1")
    if schema.get("$id") != CANONICAL_SCHEMA_ID:
        problems.append("schema.$id is not canonical v12.1")
    if schema.get("title") != CANONICAL_SCHEMA_TITLE:
        problems.append("schema.title is not canonical v12.1")
    return problems

def is_valid(report: dict, schema: dict) -> bool:
    return not errors(report, schema)
