# ChatGPT → GitHub Audit Workflow

Use the connected GitHub repository as the secure execution boundary.

## What ChatGPT supplies

Only:

```
SKILL FILE
CHANNEL URL
```

Never request or paste the YouTube API key into chat.

## How the audit starts

Create one GitHub issue in this repository:

**Title**

`[youtube-audit] <channel URL>`

**Body**

```
CHANNEL URL: <channel URL>
```

Do not include any other URL unless it is part of the same target channel, and never include credentials.

The production GitHub Actions workflow listens for this issue and will run only when the issue is created/edited by the repository owner `therandomhuman-hub` and the title begins with `[youtube-audit]`.

## Secure credential flow

```
GitHub repository secret
YOUTUBE_API_KEY
        ↓
GitHub Actions runner
        ↓
runtime/youtube_audit.py
        ↓
YouTube Data API
```

The decrypted secret is never returned to ChatGPT.

## After the run

Read the issue comment for the workflow run and artifact name. Use the produced `audit.json`, `audit.html`, CSV evidence files, coverage records, and release manifest as the evidence package for the canonical skill analysis.

## Honest boundary

The audit can only report public/API/browser evidence that was actually accessible during the run. It cannot claim private owner Analytics, hidden posts, inaccessible transcripts, or other non-public surfaces.
