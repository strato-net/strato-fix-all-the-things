#!/bin/bash
#
# orchestrator.sh - Multi-Agent Pipeline Orchestrator
#
# Coordinates the execution of all agents for a single issue:
#   1. Triage  - Classify if fixable
#   2. Research - Deep codebase exploration
#   3. Fix     - Implement changes
#   4. Review  - Self-review before PR
#
# Required environment:
#   ISSUE_RUN_DIR - Directory for this issue's run data
#   ISSUE_NUMBER, ISSUE_TITLE, ISSUE_BODY, ISSUE_LABELS - Issue details
#   SCRIPT_DIR - Root directory of the fix-all-the-things project
#   PROJECT_DIR - The target repository
#   BASE_BRANCH - The base branch (e.g., develop)
#
# Outputs:
#   ${ISSUE_RUN_DIR}/pipeline.state.json - Overall pipeline state
#   Individual agent state files
#

set -euo pipefail

AGENT_NAME="ORCHESTRATOR"

# Source common utilities
source "${SCRIPT_DIR}/agents/common.sh"

agent_log AGENT "═══════════════════════════════════════════════════"
agent_log AGENT "  MULTI-AGENT PIPELINE - Issue #${ISSUE_NUMBER}"
agent_log AGENT "═══════════════════════════════════════════════════"

PIPELINE_START=$(date +%s)

# Track pipeline state
PIPELINE_STATUS="running"
CURRENT_AGENT=""
AGENTS_COMPLETED=()
FAILURE_REASON=""

# Save pipeline state
save_pipeline_state() {
    local status="$1"
    local current="${2:-}"
    local reason="${3:-}"

    cat > "${ISSUE_RUN_DIR}/pipeline.state.json" <<EOF
{
    "status": "$status",
    "issue_number": ${ISSUE_NUMBER},
    "current_agent": "$current",
    "agents_completed": $(printf '%s\n' "${AGENTS_COMPLETED[@]}" | jq -R . | jq -s .),
    "failure_reason": "$reason",
    "started_at": "$(date -Iseconds -d @$PIPELINE_START)",
    "updated_at": "$(date -Iseconds)",
    "duration_seconds": $(($(date +%s) - PIPELINE_START))
}
EOF
}

# Run an agent and handle its result
run_agent() {
    local agent_name="$1"
    local agent_script="${SCRIPT_DIR}/agents/${agent_name}.sh"

    agent_log AGENT "────────────────────────────────────────"
    agent_log AGENT "  Stage: ${agent_name^^}"
    agent_log AGENT "────────────────────────────────────────"

    CURRENT_AGENT="$agent_name"
    save_pipeline_state "running" "$agent_name"

    if [ ! -f "$agent_script" ]; then
        agent_log ERROR "Agent script not found: $agent_script"
        return 1
    fi

    chmod +x "$agent_script"

    # Run the agent
    set +e
    bash "$agent_script"
    local exit_code=$?
    set -e

    if [ $exit_code -eq 0 ]; then
        agent_log SUCCESS "${agent_name^^} completed successfully"
        AGENTS_COMPLETED+=("$agent_name")
        return 0
    elif [ $exit_code -eq 2 ]; then
        agent_log WARNING "${agent_name^^} indicated skip"
        AGENTS_COMPLETED+=("$agent_name:skipped")
        return 2
    else
        agent_log ERROR "${agent_name^^} failed with exit code $exit_code"
        return 1
    fi
}

# Export all required variables for agents
export ISSUE_RUN_DIR
export ISSUE_NUMBER
export ISSUE_TITLE
export ISSUE_BODY
export ISSUE_LABELS
export SCRIPT_DIR
export PROJECT_DIR
export BASE_BRANCH

# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1: TRIAGE
# ══════════════════════════════════════════════════════════════════════════════

set +e
run_agent "triage"
TRIAGE_RESULT=$?
set -e

if [ $TRIAGE_RESULT -eq 1 ]; then
    FAILURE_REASON="Triage agent failed"
    save_pipeline_state "failed" "triage" "$FAILURE_REASON"
    exit 1
elif [ $TRIAGE_RESULT -eq 2 ]; then
    # Triage indicated skip (e.g., NEEDS_HUMAN, NEEDS_CLARIFICATION)
    CLASSIFICATION=$(load_agent_state "${ISSUE_RUN_DIR}/triage.state.json" "classification")
    agent_log WARNING "Triage classified as: $CLASSIFICATION (not auto-fixable)"
    save_pipeline_state "skipped" "triage" "Issue classified as: $CLASSIFICATION"
    exit 2
fi

# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2: RESEARCH
# ══════════════════════════════════════════════════════════════════════════════

set +e
run_agent "research"
RESEARCH_RESULT=$?
set -e

if [ $RESEARCH_RESULT -eq 1 ]; then
    FAILURE_REASON="Research agent failed"
    save_pipeline_state "failed" "research" "$FAILURE_REASON"
    exit 1
elif [ $RESEARCH_RESULT -eq 2 ]; then
    agent_log WARNING "Research agent indicated skip"
    save_pipeline_state "skipped" "research" "Research could not be completed"
    exit 2
fi

# Check research confidence
RESEARCH_CONFIDENCE=$(load_agent_state "${ISSUE_RUN_DIR}/research.state.json" "confidence")
if (( $(echo "$RESEARCH_CONFIDENCE < 0.4" | bc -l 2>/dev/null || echo "0") )); then
    agent_log WARNING "Research confidence too low ($RESEARCH_CONFIDENCE), but continuing..."
fi

# ══════════════════════════════════════════════════════════════════════════════
# STAGE 3: FIX
# ══════════════════════════════════════════════════════════════════════════════

set +e
run_agent "fix"
FIX_RESULT=$?
set -e

if [ $FIX_RESULT -eq 1 ]; then
    FAILURE_REASON="Fix agent failed"
    save_pipeline_state "failed" "fix" "$FAILURE_REASON"
    exit 1
elif [ $FIX_RESULT -eq 2 ]; then
    agent_log WARNING "Fix agent skipped"
    save_pipeline_state "skipped" "fix" "Fix agent could not implement changes"
    exit 2
fi

# ══════════════════════════════════════════════════════════════════════════════
# STAGE 4: REVIEW
# ══════════════════════════════════════════════════════════════════════════════

set +e
run_agent "review"
REVIEW_RESULT=$?
set -e

if [ $REVIEW_RESULT -eq 2 ]; then
    agent_log ERROR "Review agent blocked the fix"
    save_pipeline_state "blocked" "review" "Review agent did not approve the fix"
    exit 2
fi

# Check if review approved
REVIEW_APPROVED=$(load_agent_state "${ISSUE_RUN_DIR}/review.state.json" "approved")
REVIEW_VERDICT=$(load_agent_state "${ISSUE_RUN_DIR}/review.state.json" "verdict")

if [ "$REVIEW_APPROVED" != "true" ]; then
    agent_log ERROR "Review did not approve: $REVIEW_VERDICT"
    save_pipeline_state "blocked" "review" "Review verdict: $REVIEW_VERDICT"
    exit 2
fi

# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE COMPLETE
# ══════════════════════════════════════════════════════════════════════════════

PIPELINE_DURATION=$(($(date +%s) - PIPELINE_START))

agent_log AGENT "═══════════════════════════════════════════════════"
agent_log SUCCESS "  PIPELINE COMPLETE - All agents succeeded"
agent_log AGENT "  Duration: ${PIPELINE_DURATION}s"
agent_log AGENT "═══════════════════════════════════════════════════"

# Compute aggregate confidence
TRIAGE_CONF=$(load_agent_state "${ISSUE_RUN_DIR}/triage.state.json" "confidence")
RESEARCH_CONF=$(load_agent_state "${ISSUE_RUN_DIR}/research.state.json" "confidence")
FIX_CONF=$(load_agent_state "${ISSUE_RUN_DIR}/fix.state.json" "confidence")
REVIEW_CONF=$(load_agent_state "${ISSUE_RUN_DIR}/review.state.json" "confidence")

# Weighted average (fix and review weighted more heavily)
AGGREGATE_CONF=$(echo "scale=2; ($TRIAGE_CONF * 0.15 + $RESEARCH_CONF * 0.20 + $FIX_CONF * 0.35 + $REVIEW_CONF * 0.30)" | bc -l 2>/dev/null || echo "0.7")

agent_log INFO "Aggregate confidence: $AGGREGATE_CONF"
agent_log INFO "  Triage:   $TRIAGE_CONF"
agent_log INFO "  Research: $RESEARCH_CONF"
agent_log INFO "  Fix:      $FIX_CONF"
agent_log INFO "  Review:   $REVIEW_CONF"

# Save final state
cat > "${ISSUE_RUN_DIR}/pipeline.state.json" <<EOF
{
    "status": "success",
    "issue_number": ${ISSUE_NUMBER},
    "agents_completed": $(printf '%s\n' "${AGENTS_COMPLETED[@]}" | jq -R . | jq -s .),
    "aggregate_confidence": $AGGREGATE_CONF,
    "confidence_breakdown": {
        "triage": $TRIAGE_CONF,
        "research": $RESEARCH_CONF,
        "fix": $FIX_CONF,
        "review": $REVIEW_CONF
    },
    "review_verdict": "$REVIEW_VERDICT",
    "started_at": "$(date -Iseconds -d @$PIPELINE_START)",
    "completed_at": "$(date -Iseconds)",
    "duration_seconds": $PIPELINE_DURATION
}
EOF

exit 0
