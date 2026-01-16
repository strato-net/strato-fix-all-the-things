"""Pipeline orchestrator."""

import json
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


class Pipeline:
    """Orchestrates the multi-agent pipeline."""

    AGENTS: list[Type[Agent]] = [TriageAgent, ResearchAgent, FixAgent, ReviewAgent]

    def __init__(self, config: Config, issue: Issue, run_dir: Path):
        self.config = config
        self.issue = issue
        self.run_dir = run_dir
        self.state = PipelineState(
            status=PipelineStatus.RUNNING,
            issue_number=issue.number,
        )
        self.agent_states: dict[str, AgentState] = {}

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

        for agent_cls in self.AGENTS:
            self.log("-" * 40)
            self.log(f"  Stage: {agent_cls.name.upper()}")
            self.log("-" * 40)

            self.state.current_agent = agent_cls.name
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
                self.log(f"[ERROR] {agent_cls.name.upper()} failed")
                self.state.status = PipelineStatus.FAILED
                self.state.failure_reason = agent_state.error or f"{agent_cls.name} failed"
                self.state.agents_completed.append(f"{agent_cls.name}:failed")
                break

            elif agent_state.status == AgentStatus.SKIPPED:
                self.log(f"[WARNING] {agent_cls.name.upper()} skipped")
                self.state.agents_completed.append(f"{agent_cls.name}:skipped")

                # Triage skip means we stop the pipeline
                if agent_cls.name == "triage":
                    self.state.status = PipelineStatus.SKIPPED
                    classification = agent_state.data.get("classification", "unknown")
                    self.state.failure_reason = f"Issue classified as: {classification}"
                    break

                # Fix skip means no changes made - nothing to review
                if agent_cls.name == "fix":
                    self.state.status = PipelineStatus.SKIPPED
                    self.state.failure_reason = "Fix agent made no changes"
                    break

                # Review skip means blocked
                if agent_cls.name == "review":
                    self.state.status = PipelineStatus.BLOCKED
                    verdict = agent_state.data.get("verdict", "unknown")
                    self.state.failure_reason = f"Review verdict: {verdict}"
                    break

            else:
                self.log(f"[SUCCESS] {agent_cls.name.upper()} completed")
                self.state.agents_completed.append(agent_cls.name)

        # If we completed all agents successfully
        if self.state.status == PipelineStatus.RUNNING:
            self.state.status = PipelineStatus.SUCCESS
            self._calculate_confidence()

        self.state.completed_at = datetime.now()
        self.save_state()

        self.log("=" * 50)
        self.log(f"  Pipeline {self.state.status.value.upper()}")
        self.log(f"  Duration: {self.state.to_dict()['duration_seconds']:.1f}s")
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
