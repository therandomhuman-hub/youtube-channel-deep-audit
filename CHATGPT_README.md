# ChatGPT workflow

1. Keep the repository secret named `YOUTUBE_API_KEY`.
2. In ChatGPT, provide the repository URL and the public channel URL.
3. Trigger the GitHub Actions workflow manually.
4. Inspect/download the resulting audit artifact in GitHub.
5. Feed the artifact to the canonical audit skill for evidence analysis.

The API key is read only by the GitHub Actions runner through `secrets.YOUTUBE_API_KEY`; it is never a required ChatGPT input and is never stored in audit artifacts.
