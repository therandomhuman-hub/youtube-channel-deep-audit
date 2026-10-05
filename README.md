# YouTube Channel Deep Audit

Production GitHub Actions execution layer for the canonical v11.3 audit skill.

## One-time setup

This repository must contain the GitHub Actions repository secret:

`YOUTUBE_API_KEY`

The value is used only inside the runner. It is never a ChatGPT input, committed file, issue body, report field, HTML value, artifact, or log value.

## Normal ChatGPT workflow

You only provide:

1. The canonical audit skill file.
2. One YouTube channel URL.

The ChatGPT GitHub connection can create an audit request issue titled:

`[youtube-audit] https://www.youtube.com/@example`

with the same channel URL in the body and no secret.

The issue event triggers the production workflow automatically. The runner reads `secrets.YOUTUBE_API_KEY`, resolves the channel, collects the public API/web evidence, validates the release package, and comments the run/artifact link on the issue.

## Manual workflow

Open **Actions → YouTube Channel Deep Audit → Run workflow** and enter one public channel URL or @handle.

## Collection

The runtime collects to the legitimately accessible public boundary:

- channel metadata
- exhaustive uploads playlist inventory with pagination
- video metadata/statistics
- public playlists
- channel sections
- public comments and separately paginated replies
- public Shorts surface
- public Posts surface
- transcript evidence audit for every targeted video using public transcript retrieval plus public UI fallback
- compact transcript hashes/word counts/claim signals only; full transcript text is processed in memory and not persisted
- per-video beginner scores for usefulness, evidence discipline, accessibility, repeatability, originality safety and policy safety
- explicit claim-evidence gaps and low-confidence metadata-only states when transcripts are unavailable
- public description/resource links
- reproducibility/quota/coverage records
- standalone interactive HTML
- JSON/CSV evidence artifacts

Private owner Analytics are explicitly unavailable unless owner authorization is separately configured.

## Release gate

A run is released only when the collector and validator succeed and the generated package passes:

- schema/data checks
- duplicate checks
- secret scanning
- HTML checks
- CSV reconciliation
- artifact hash validation
- credential exposure checks
- transcript coverage / confidence consistency
- per-video rating bounds and full-transcript requirement for high-confidence ratings

A partial or blocked collection is never represented as complete.

## Development

`python -m unittest discover -s tests -v`

`python -m compileall -q runtime youtube_audit.py tests`
