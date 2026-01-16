#!/bin/bash
#
# common.sh - Shared utilities for all agents
#
# This file should be sourced by agent scripts, not executed directly.
#

# Ensure we have required environment
if [ -z "$ISSUE_RUN_DIR" ]; then
    echo "ERROR: ISSUE_RUN_DIR not set. This script must be sourced by an agent." >&2
    exit 1
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

# Agent logging with agent name prefix
agent_log() {
    local level="$1"
    local message="$2"
    local color=""

    case "$level" in
        INFO) color="$BLUE" ;;
        SUCCESS) color="$GREEN" ;;
        WARNING) color="$YELLOW" ;;
        ERROR) color="$RED" ;;
        AGENT) color="$MAGENTA" ;;
        *) color="$NC" ;;
    esac

    echo -e "${color}[${AGENT_NAME:-UNKNOWN}]${NC} ${color}[$level]${NC} $message"
}

# Run Claude with a prompt file and save output
# Usage: run_claude <prompt_file> <output_file> [timeout_seconds]
run_claude() {
    local prompt_file="$1"
    local output_file="$2"
    local timeout_sec="${3:-600}"

    if [ ! -f "$prompt_file" ]; then
        agent_log ERROR "Prompt file not found: $prompt_file"
        return 1
    fi

    local prompt
    prompt=$(cat "$prompt_file")

    agent_log INFO "Running Claude (timeout: ${timeout_sec}s)..."

    timeout "$timeout_sec" claude \
        --dangerously-skip-permissions \
        --verbose \
        --output-format stream-json \
        -p "$prompt" 2>&1 | tee "$output_file"

    local exit_code=${PIPESTATUS[0]}

    if [ $exit_code -eq 124 ]; then
        agent_log ERROR "Claude timed out after ${timeout_sec}s"
        return 124
    elif [ $exit_code -ne 0 ]; then
        agent_log WARNING "Claude exited with code $exit_code"
        return $exit_code
    fi

    return 0
}

# Extract structured output from Claude's response
# Looks for JSON between ```json and ``` markers
extract_json_output() {
    local log_file="$1"

    # Extract the last assistant message content that contains JSON
    grep -o '```json[^`]*```' "$log_file" | tail -1 | sed 's/```json//;s/```//' | jq -r '.' 2>/dev/null
}

# Save agent state to a JSON file
save_agent_state() {
    local state_file="$1"
    shift

    # Build JSON from key=value pairs
    local json="{"
    local first=true

    while [ $# -gt 0 ]; do
        local key="${1%%=*}"
        local value="${1#*=}"

        if [ "$first" = true ]; then
            first=false
        else
            json+=","
        fi

        # Quote strings, leave numbers/booleans as-is
        if [[ "$value" =~ ^[0-9.]+$ ]] || [ "$value" = "true" ] || [ "$value" = "false" ] || [ "$value" = "null" ]; then
            json+="\"$key\":$value"
        else
            # Escape quotes in value
            value="${value//\"/\\\"}"
            json+="\"$key\":\"$value\""
        fi

        shift
    done

    json+="}"

    echo "$json" | jq '.' > "$state_file"
}

# Load agent state from JSON file
load_agent_state() {
    local state_file="$1"
    local key="$2"

    if [ ! -f "$state_file" ]; then
        echo ""
        return 1
    fi

    jq -r ".$key // empty" "$state_file"
}

# Check if previous agent completed successfully
check_previous_agent() {
    local agent_name="$1"
    local state_file="${ISSUE_RUN_DIR}/${agent_name}.state.json"

    if [ ! -f "$state_file" ]; then
        agent_log ERROR "Previous agent '$agent_name' did not run"
        return 1
    fi

    local status
    status=$(load_agent_state "$state_file" "status")

    if [ "$status" != "success" ]; then
        agent_log ERROR "Previous agent '$agent_name' did not succeed (status: $status)"
        return 1
    fi

    return 0
}
