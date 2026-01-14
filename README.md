# STRATO Fix All The Things

Quickstart — minimal instructions. Full details live in [run.sh](run.sh).

1. Copy the example env and add your GitHub token:

```bash
cp .env.sample .env
# Edit .env and set GH_TOKEN
```

2. Install prerequisites (examples):

```bash
npm install -g @anthropic-ai/claude-code
# Ensure `gh`, `git`, and `jq` are available
```

3. Run the script for one or more issues:

```bash
./run.sh 5960 5961
```

See [run.sh](run.sh) for configuration, environment variables, and advanced usage.

License: Apache-2.0 — see [LICENSE](LICENSE)
