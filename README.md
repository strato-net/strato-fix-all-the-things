# STRATO Fix All The Things

A multi-agent system for automatically fixing GitHub issues using Claude Code.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    MULTI-AGENT PIPELINE                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│  │  TRIAGE  │───▶│ RESEARCH │───▶│   FIX    │───▶│  REVIEW  │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘  │
│       │               │               │               │         │
│       ▼               ▼               ▼               ▼         │
│   Classify        Explore         Implement       Self-review   │
│   issue           codebase        changes         before PR     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Pipeline Stages

| Agent | Purpose | Key Outputs |
|-------|---------|-------------|
| **Triage** | Classify if issue is AI-fixable | Classification, confidence, complexity |
| **Research** | Deep codebase exploration | Root cause, files to modify, patterns |
| **Fix** | Implement minimal changes | Commit with confidence assessment |
| **Review** | Self-review before PR | APPROVE / REQUEST_CHANGES / BLOCK |

### Agent Details

#### 1. Triage Agent
Classifies issues into categories:
- `FIXABLE_CODE` - Clear bug fixable with code changes
- `FIXABLE_CONFIG` - Configuration/environment changes
- `NEEDS_CLARIFICATION` - Issue too vague
- `NEEDS_HUMAN` - Requires human judgment
- `ALREADY_DONE` - Issue appears resolved
- `OUT_OF_SCOPE` - Not suitable for AI fixing

Only `FIXABLE_CODE` and `FIXABLE_CONFIG` proceed to the next stage.

#### 2. Research Agent
Explores the codebase WITHOUT making changes:
- Locates all relevant files
- Maps architecture and data flow
- Identifies root cause
- Documents patterns to follow
- Notes risks and testing recommendations

#### 3. Fix Agent
Implements changes based on research:
- Executes focused changes
- Follows identified patterns
- Creates detailed commit message
- Reports confidence scores

#### 4. Review Agent
Self-reviews the fix before PR creation:
- Checks correctness, completeness, safety
- Validates style and scope
- Can block problematic fixes
- Provides notes for human reviewers

## Quick Start

1. **Setup environment:**
```bash
cp .env.sample .env
# Edit .env and set GH_TOKEN
```

2. **Install prerequisites:**
```bash
npm install -g @anthropic-ai/claude-code
# Ensure gh, git, jq, bc, timeout, envsubst are available
```

3. **Run on issues:**
```bash
./run.sh 5960              # Single issue
./run.sh 5960 5961 5962    # Multiple issues
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `GH_TOKEN` | GitHub personal access token | Required |
| `--project-dir` | Path to strato-platform repo | `../strato-platform` |

## Run Output

Each run creates detailed logs in `runs/`:

```
runs/2024-01-15_12-00-00-issue-5960/
├── issue.json           # Original issue data
├── triage.prompt.md     # Prompt sent to triage agent
├── triage.log           # Full triage output
├── triage.state.json    # Classification result
├── research.prompt.md   # Prompt sent to research agent
├── research.log         # Full research output
├── research.state.json  # Research findings
├── fix.prompt.md        # Prompt sent to fix agent
├── fix.log              # Full fix output
├── fix.state.json       # Fix result
├── review.prompt.md     # Prompt sent to review agent
├── review.log           # Full review output
├── review.state.json    # Review verdict
├── pipeline.state.json  # Aggregate pipeline results
└── result.json          # Final outcome with PR URL
```

## Confidence Scoring

Each agent reports confidence (0.0-1.0). The pipeline computes an aggregate:

```
Aggregate = Triage(0.15) + Research(0.20) + Fix(0.35) + Review(0.30)
```

PRs are labeled based on aggregate confidence:
- `< 0.6` → `low-confidence` (needs extra review)
- `≥ 0.8` → `high-confidence`

## File Structure

```
strato-fix-all-the-things/
├── run.sh                 # Main entry point
├── agents/
│   ├── common.sh          # Shared utilities
│   ├── orchestrator.sh    # Pipeline coordinator
│   ├── triage.sh          # Triage agent
│   ├── research.sh        # Research agent
│   ├── fix.sh             # Fix agent
│   └── review.sh          # Review agent
├── prompts/
│   ├── triage.md          # Triage prompt template
│   ├── research.md        # Research prompt template
│   ├── fix.md             # Fix prompt template
│   └── review.md          # Review prompt template
├── runs/                  # Run logs (gitignored)
├── .env                   # Environment (gitignored)
├── .env.sample            # Environment template
└── README.md
```

## How It Works

1. **Fetch issue** from GitHub
2. **Triage** classifies if it's AI-fixable
3. **Research** explores codebase, identifies root cause
4. **Fix** implements changes based on research
5. **Review** self-checks the fix
6. **Create PR** as draft with confidence labels
7. **Comment on issue** linking to PR

If any stage fails or blocks, the pipeline stops and comments on the issue explaining why.

## Safety Features

- Creates PRs as **drafts** (not ready for review)
- **Review agent** can block problematic fixes
- **Confidence labels** flag uncertain fixes
- `.env` files automatically excluded from commits
- Force-syncs to latest `origin/develop` before each issue
- Cleans up existing branches/PRs to avoid conflicts

## Requirements

- [Claude Code CLI](https://www.npmjs.com/package/@anthropic-ai/claude-code) (`npm install -g @anthropic-ai/claude-code`)
- [GitHub CLI](https://cli.github.com/) (`gh`)
- `git`, `jq`, `bc`, `timeout`, `envsubst`
- GitHub token with `repo` and `workflow` scopes

## License

Apache-2.0 — see [LICENSE](LICENSE)
