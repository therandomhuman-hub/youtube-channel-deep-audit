"""Release-contract helpers shared by production validation and CI."""
CANONICAL_VERSION = "12.1"
CANONICAL_SCHEMA_ID = "youtube-channel-deep-audit-v12.1.schema.json"
CANONICAL_SCHEMA_TITLE = "YouTube Channel Deep Audit v12.1 Production Report"

def canonical_contract_errors(report, schema):
    errors = []
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    if metadata.get("methodology_version") != CANONICAL_VERSION:
        errors.append("metadata.methodology_version must equal 12.1")
    if metadata.get("schema_version") != CANONICAL_VERSION:
        errors.append("metadata.schema_version must equal 12.1")
    if not isinstance(schema, dict) or schema.get("$id") != CANONICAL_SCHEMA_ID:
        errors.append("schema.$id must equal the canonical v12.1 schema id")
    if not isinstance(schema, dict) or schema.get("title") != CANONICAL_SCHEMA_TITLE:
        errors.append("schema.title must equal the canonical v12.1 schema title")
    return errors

def canonical_contract_ok(report, schema):
    return not canonical_contract_errors(report, schema)
