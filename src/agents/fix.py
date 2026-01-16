"""Fix agent - implements the actual code changes."""

import json
from typing import Any

from ..claude_runner import ClaudeTimeoutError, extract_json_from_output, run_claude
from ..models import AgentStatus
from .base import Agent


class FixAgent(Agent):
    """Implements code changes to fix the issue."""

    name = "fix"

    def run(self) -> tuple[AgentStatus, dict[str, Any]]:
        """Execute the fix."""
        self.info(f"Starting fix for issue #{self.context.issue.number}")

        # Check that research passed
        research_state = self.context.previous_states.get("research")
        if not research_state or research_state.status != AgentStatus.SUCCESS:
            self.error("Research did not complete successfully")
            return AgentStatus.FAILED, {"error": "Research not completed"}

        # Load prompt template and add research context
        prompt = self.load_prompt_template()

        # Add research findings to prompt
        research_data = research_state.data
        research_context = f"""
## Research Findings

**Root Cause:** {research_data.get('root_cause')}
**Proposed Fix:** {research_data.get('proposed_fix')}
**Affected Areas:** {', '.join(research_data.get('affected_areas', []))}
**Test Strategy:** {research_data.get('test_strategy')}

Files to modify:
{json.dumps(research_data.get('files_analyzed', []), indent=2)}

Full research:
```json
{json.dumps(research_data.get('full_analysis', {}), indent=2)}
```
"""
        prompt = prompt.replace("${RESEARCH_SUMMARY}", research_context)
        self.prompt_file.write_text(prompt)

        # Run Claude
        self.info(f"Running Claude (timeout: {self.context.config.fix_timeout}s)...")
        try:
            result = run_claude(
                prompt=prompt,
                cwd=self.context.config.project_dir,
                timeout_sec=self.context.config.fix_timeout,
                log_file=self.log_file,
            )
        except ClaudeTimeoutError as e:
            self.error(str(e))
            return AgentStatus.FAILED, {"error": str(e)}

        if not result.success:
            self.error(f"Claude failed: {result.error}")
            return AgentStatus.FAILED, {"error": result.error}

        # Extract JSON output - try multiple possible field names
        self.info("Extracting fix results...")
        data = extract_json_from_output(result.output, "fix_applied")
        if not data:
            data = extract_json_from_output(result.output, "files_modified")
        if not data:
            data = extract_json_from_output(result.output, "files_changed")

        if not data:
            self.warning("Could not extract structured result")
            data = {
                "confidence": 0.5,
                "files_changed": [],
                "summary": "Fix completed but no structured output",
                "tests_added": [],
            }

        # Handle confidence as either dict or float
        conf_data = data.get("confidence", 0.5)
        if isinstance(conf_data, dict):
            confidence = float(conf_data.get("overall", 0.5))
        else:
            confidence = float(conf_data)

        # Handle files_changed vs files_modified
        files_changed = data.get("files_changed") or data.get("files_modified", [])

        self.success(f"Fix complete (confidence: {confidence})")
        self.info(f"Files changed: {len(files_changed)}")

        if not files_changed:
            self.warning("No files were changed")
            return AgentStatus.SKIPPED, {
                "confidence": confidence,
                "files_changed": [],
                "summary": "No changes made",
                "tests_added": [],
            }

        return AgentStatus.SUCCESS, {
            "confidence": confidence,
            "files_changed": files_changed,
            "summary": data.get("summary", ""),
            "tests_added": data.get("tests_added", []),
            "full_result": data,
        }
