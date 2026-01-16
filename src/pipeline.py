"""Pipeline orchestrator."""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Type

from .agents.base import Agent, AgentContext
from .agents.triage import TriageAgent
from .agents.research import ResearchAgent
from .agents.fix import FixAgent
from .agents.review import ReviewAgent
from .config import Config
from .models import AgentState, AgentStatus, Issue, PipelineState, PipelineStatus

# Maximum fix-review iterations before giving up
MAX_FIX_REVIEW_ITERATIONS = 3


class Pipeline:
    """Orchestrates the multi-agent pipeline."""

    # Pre-fix agents run once
    PRE_FIX_AGENTS: list[Type[Agent]] = [TriageAgent, ResearchAgent]

    def __init__(self, config: Config, issue: Issue, run_dir: Path):
        self.config = config
        self.issue = issue
        self.run_dir = run_dir
        self.state = PipelineState(
            status=PipelineStatus.RUNNING,
            issue_number=issue.number,
        )
        self.agent_states: dict[str, AgentState] = {}
        self.fix_iteration = 0

    @property
    def state_file(self) -> Path:
        """Path to pipeline state file."""
        return self.run_dir / "pipeline.state.json"

    def save_state(self) -> None:
        """Save pipeline state to file."""
        with open(self.state_file, "w") as f:
            json.dump(self.state.to_dict(), f, indent=2)

    def log(self, message: str) -> None:
        """Log a pipeline message."""
        print(f"[PIPELINE] {message}")

    def run(self) -> PipelineState:
        """Run the full pipeline."""
        self.log("=" * 50)
        self.log(f"  MULTI-AGENT PIPELINE - Issue #{self.issue.number}")
        self.log("=" * 50)

        # Run pre-fix agents (triage, research)
        for agent_cls in self.PRE_FIX_AGENTS:
            result = self._run_agent(agent_cls)
            if result == "stop":
                return self._finalize()

        # Run fix-review loop
        while self.fix_iteration < MAX_FIX_REVIEW_ITERATIONS:
            self.fix_iteration += 1
            self.log("=" * 40)
            self.log(f"  FIX-REVIEW ITERATION {self.fix_iteration}/{MAX_FIX_REVIEW_ITERATIONS}")
            self.log("=" * 40)

            # Run fix agent (or revision if not first iteration)
            result = self._run_fix_agent()
            if result == "stop":
                return self._finalize()

            # Run review agent
            result = self._run_review_agent()
            if result == "approved":
                # Review approved - we're done!
                self.log("[SUCCESS] Review approved the fix")
                break
            elif result == "stop":
                return self._finalize()
            elif result == "revise":
                # Review requested changes - loop again
                if self.fix_iteration < MAX_FIX_REVIEW_ITERATIONS:
                    self.log(f"[INFO] Review requested changes, attempting revision...")
                else:
                    self.log(f"[ERROR] Review requested changes but max iterations reached")
                    self.state.status = PipelineStatus.BLOCKED
                    self.state.failure_reason = f"Review still requesting changes after {MAX_FIX_REVIEW_ITERATIONS} iterations"

        # If we completed successfully
        if self.state.status == PipelineStatus.RUNNING:
            self.state.status = PipelineStatus.SUCCESS
            self._calculate_confidence()

        return self._finalize()

    def _run_agent(self, agent_cls: Type[Agent], suffix: str = "") -> str:
        """Run a single agent. Returns 'continue', 'stop', 'approved', or 'revise'."""
        agent_name = f"{agent_cls.name}{suffix}"
        self.log("-" * 40)
        self.log(f"  Stage: {agent_name.upper()}")
        self.log("-" * 40)

        self.state.current_agent = agent_name
        self.save_state()

        # Create agent context with previous states
        context = AgentContext(
            config=self.config,
            issue=self.issue,
            run_dir=self.run_dir,
            previous_states=self.agent_states,
        )

        # Run the agent
        agent = agent_cls(context)
        agent_state = agent.execute()
        self.agent_states[agent_cls.name] = agent_state

        # Handle result
        if agent_state.status == AgentStatus.FAILED:
            self.log(f"[ERROR] {agent_name.upper()} failed")
            self.state.status = PipelineStatus.FAILED
            self.state.failure_reason = agent_state.error or f"{agent_name} failed"
            self.state.agents_completed.append(f"{agent_name}:failed")
            return "stop"

        elif agent_state.status == AgentStatus.SKIPPED:
            self.log(f"[WARNING] {agent_name.upper()} skipped")
            self.state.agents_completed.append(f"{agent_name}:skipped")

            # Triage skip means we stop the pipeline
            if agent_cls.name == "triage":
                self.state.status = PipelineStatus.SKIPPED
                classification = agent_state.data.get("classification", "unknown")
                self.state.failure_reason = f"Issue classified as: {classification}"
                return "stop"

            # Fix skip means no changes made - nothing to review
            if agent_cls.name == "fix":
                self.state.status = PipelineStatus.SKIPPED
                self.state.failure_reason = "Fix agent made no changes"
                return "stop"

            # Review skip means request_changes - need revision
            if agent_cls.name == "review":
                verdict = agent_state.data.get("verdict", "unknown")
                if verdict == "BLOCK":
                    self.state.status = PipelineStatus.BLOCKED
                    self.state.failure_reason = f"Review blocked: {verdict}"
                    return "stop"
                # REQUEST_CHANGES - need to revise
                return "revise"

            return "continue"

        else:
            self.log(f"[SUCCESS] {agent_name.upper()} completed")
            self.state.agents_completed.append(agent_name)

            # Review success means approved
            if agent_cls.name == "review":
                return "approved"

            return "continue"

    def _run_fix_agent(self) -> str:
        """Run fix agent, using revision prompt if not first iteration."""
        if self.fix_iteration == 1:
            # First iteration - normal fix
            return self._run_agent(FixAgent)
        else:
            # Revision - need to set up revision context
            return self._run_fix_revision()

    def _run_fix_revision(self) -> str:
        """Run fix agent in revision mode with review feedback."""
        self.log("-" * 40)
        self.log(f"  Stage: FIX (REVISION {self.fix_iteration})")
        self.log("-" * 40)

        self.state.current_agent = f"fix-revision-{self.fix_iteration}"
        self.save_state()

        # Get review feedback
        review_state = self.agent_states.get("review")
        if not review_state:
            self.log("[ERROR] No review state for revision")
            self.state.status = PipelineStatus.FAILED
            self.state.failure_reason = "No review state for revision"
            return "stop"

        # Get git diff
        git_diff = self._get_git_diff()

        # Load revision prompt template
        prompt_file = self.config.prompts_dir / "fix-revision.md"
        if not prompt_file.exists():
            self.log("[ERROR] Missing fix-revision.md prompt")
            self.state.status = PipelineStatus.FAILED
            self.state.failure_reason = "Missing fix-revision.md prompt"
            return "stop"

        prompt = prompt_file.read_text()

        # Get research data for context
        research_state = self.agent_states.get("research")
        research_data = research_state.data if research_state else {}
        full_analysis = research_data.get("full_analysis", {})

        root_cause = research_data.get("root_cause", {})
        if isinstance(root_cause, dict):
            root_cause_str = root_cause.get("description", str(root_cause))
        else:
            root_cause_str = str(root_cause)

        patterns = full_analysis.get("patterns_to_follow", [])
        patterns_str = "\n".join(f"- {p.get('description', p)}" for p in patterns) if patterns else "See research"

        # Get previous fix files
        fix_state = self.agent_states.get("fix")
        previous_files = fix_state.data.get("files_changed", []) if fix_state else []

        # Build concerns and suggestions
        concerns = review_state.data.get("concerns", [])
        suggestions = review_state.data.get("suggestions", [])
        concerns_str = "\n".join(f"- {c}" for c in concerns) if concerns else "None specified"
        suggestions_str = "\n".join(f"- {s}" for s in suggestions) if suggestions else "None specified"

        # Substitute variables
        prompt = prompt.replace("${ISSUE_NUMBER}", str(self.issue.number))
        prompt = prompt.replace("${ISSUE_TITLE}", self.issue.title)
        prompt = prompt.replace("${ATTEMPT_NUMBER}", str(self.fix_iteration))
        prompt = prompt.replace("${MAX_ATTEMPTS}", str(MAX_FIX_REVIEW_ITERATIONS))
        prompt = prompt.replace("${REVIEW_VERDICT}", review_state.data.get("verdict", "REQUEST_CHANGES"))
        prompt = prompt.replace("${REVIEW_CONFIDENCE}", str(review_state.confidence))
        prompt = prompt.replace("${REVIEW_CONCERNS}", concerns_str)
        prompt = prompt.replace("${REVIEW_SUGGESTIONS}", suggestions_str)
        prompt = prompt.replace("${PREVIOUS_FILES}", ", ".join(previous_files) if previous_files else "Unknown")
        prompt = prompt.replace("${GIT_DIFF}", git_diff)
        prompt = prompt.replace("${ROOT_CAUSE}", root_cause_str)
        prompt = prompt.replace("${PATTERNS_TO_FOLLOW}", patterns_str)

        # Save prompt
        prompt_path = self.run_dir / f"fix-revision-{self.fix_iteration}.prompt.md"
        prompt_path.write_text(prompt)

        # Run Claude
        from .claude_runner import ClaudeTimeoutError, extract_json_from_output, run_claude

        log_file = self.run_dir / f"fix-revision-{self.fix_iteration}.log"
        self.log(f"Running Claude for revision (timeout: {self.config.fix_timeout}s)...")

        try:
            result = run_claude(
                prompt=prompt,
                cwd=self.config.project_dir,
                timeout_sec=self.config.fix_timeout,
                log_file=log_file,
            )
        except ClaudeTimeoutError as e:
            self.log(f"[ERROR] {e}")
            self.state.status = PipelineStatus.FAILED
            self.state.failure_reason = str(e)
            return "stop"

        if not result.success:
            self.log(f"[ERROR] Claude failed: {result.error}")
            self.state.status = PipelineStatus.FAILED
            self.state.failure_reason = result.error or "Claude failed"
            return "stop"

        # Extract result
        data = extract_json_from_output(result.output, "fix_applied")
        if not data:
            data = extract_json_from_output(result.output, "files_modified")

        if not data:
            self.log("[WARNING] Could not extract structured result from revision")
            data = {"confidence": 0.5, "files_changed": []}

        # Handle confidence
        conf_data = data.get("confidence", 0.5)
        if isinstance(conf_data, dict):
            confidence = float(conf_data.get("overall", 0.5))
        else:
            confidence = float(conf_data)

        files_changed = data.get("files_changed") or data.get("files_modified", [])

        # Update fix state with revision results
        self.agent_states["fix"] = AgentState(
            agent="fix",
            status=AgentStatus.SUCCESS if files_changed else AgentStatus.SKIPPED,
            issue_number=self.issue.number,
            confidence=confidence,
            data={
                "confidence": confidence,
                "files_changed": files_changed,
                "revision": self.fix_iteration,
                "concerns_addressed": data.get("concerns_addressed", []),
                "suggestions_implemented": data.get("suggestions_implemented", []),
                "full_result": data,
            },
        )

        # Save state
        state_file = self.run_dir / f"fix-revision-{self.fix_iteration}.state.json"
        with open(state_file, "w") as f:
            json.dump(self.agent_states["fix"].to_dict(), f, indent=2)

        if not files_changed and not data.get("fix_applied", True):
            self.log("[WARNING] Revision could not address feedback")
            self.state.status = PipelineStatus.BLOCKED
            self.state.failure_reason = data.get("reason", "Could not address review feedback")
            return "stop"

        self.log(f"[SUCCESS] Revision {self.fix_iteration} complete")
        self.state.agents_completed.append(f"fix-revision-{self.fix_iteration}")
        return "continue"

    def _run_review_agent(self) -> str:
        """Run review agent."""
        return self._run_agent(ReviewAgent)

    def _get_git_diff(self) -> str:
        """Get current git diff."""
        try:
            result = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=self.config.project_dir,
                capture_output=True,
                text=True,
            )
            diff = result.stdout.strip()
            if not diff:
                # Try diff against base branch
                result = subprocess.run(
                    ["git", "diff", f"origin/{self.config.base_branch}"],
                    cwd=self.config.project_dir,
                    capture_output=True,
                    text=True,
                )
                diff = result.stdout.strip()
            return diff if diff else "(no diff available)"
        except Exception:
            return "(could not get diff)"

    def _finalize(self) -> PipelineState:
        """Finalize and return pipeline state."""
        self.state.completed_at = datetime.now()
        self.save_state()

        self.log("=" * 50)
        self.log(f"  Pipeline {self.state.status.value.upper()}")
        self.log(f"  Duration: {self.state.to_dict()['duration_seconds']:.1f}s")
        if self.fix_iteration > 1:
            self.log(f"  Fix-Review iterations: {self.fix_iteration}")
        self.log("=" * 50)

        return self.state

    def _calculate_confidence(self) -> None:
        """Calculate aggregate confidence from all agents."""
        weights = {
            "triage": 0.15,
            "research": 0.20,
            "fix": 0.35,
            "review": 0.30,
        }

        total = 0.0
        breakdown = {}

        for name, weight in weights.items():
            state = self.agent_states.get(name)
            if state:
                conf = state.confidence
                breakdown[name] = conf
                total += conf * weight

        self.state.aggregate_confidence = round(total, 2)
        self.state.confidence_breakdown = breakdown
