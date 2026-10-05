# YouTube Channel Deep Audit

Production GitHub Actions runner for the canonical YouTube Channel Deep Audit skill.

## One-time setup

Create this repository secret:

`YOUTUBE_API_KEY`

GitHub Actions injects it into the runner as `YOUTUBE_API_KEY`. The key is never committed, echoed, or written to audit artifacts.

## Run an audit

Open **Actions → YouTube Channel Deep Audit → Run workflow** and provide the public YouTube channel URL.

The workflow checks out the repository, installs the runtime dependency, runs the audit with the repository secret, scans artifacts for credential-like leakage, and uploads the results as a workflow artifact.

## Current boundary

The current collector requires a canonical `/channel/UC...` URL. Resolve a YouTube @handle to its canonical channel URL before running until handle-resolution is added.

Private/owner-only YouTube Analytics data is not collected by an API key.