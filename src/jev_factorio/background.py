"""Opt-in productive background work, using the existing single dispatcher.

Only a fully accepted, receipt-bound craft may leave pending verification. All
uncertain dispatches retain the original write-ahead and no-replay barrier.
"""
from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .controller import HierarchicalLoop
from .craft_jobs import CraftJob, InvalidCraftEvidence
from .memory import CampaignMemory
from .planning.background_work import background_wait, independent_candidates
from .skills import Plan


@dataclass
class BackgroundMemory(CampaignMemory):
    # Named extension, not the unrelated schema-2 attempt-evidence proposal.
    # A legacy reader rejects these unknown fields rather than dropping a job.
    background_schema: int = 1
    background_job: dict | None = None

    @classmethod
    def load(cls, path: Path, session_id: str, target: str) -> BackgroundMemory:
        memory = super().load(path, session_id, target)
        if type(memory.background_schema) is not int or memory.background_schema != 1:
            raise ValueError("Unsupported background checkpoint extension")
        if memory.background_job is not None:
            job = CraftJob.from_dict(memory.background_job)
            if (job.session_id != session_id or job.goal != memory.active_goal
                    or job.started_tick > memory.last_tick
                    or job.last_progress_tick > memory.last_tick
                    or memory.status == "completed"):
                raise ValueError("Background checkpoint identity or tick mismatch")
            if memory.active_plan and any(
                (step.get("parameters") or {}).get("receipt") == job.parameters["receipt"]
                for step in memory.active_plan["steps"]
            ):
                raise ValueError("Craft cannot be both foreground and background")
        return memory

    def save(self, path: Path | None) -> None:
        super().save(path)
        if path is not None and os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


class BackgroundWorkLoop(HierarchicalLoop):
    memory_type = BackgroundMemory

    def __init__(self, backend, jev=None, **options) -> None:
        if options.get("factory_scheduling") != "ready-work":
            raise ValueError("Background work requires ready-work scheduling")
        self._save_poisoned = False
        super().__init__(backend, jev, **options)
        if self.catalog is None:
            raise ValueError("Background work requires a native production catalog")
        # Backend decorators install telemetry only for this opt-in controller.
        # Test backends implement the same receipt protocol without game access.
        native = getattr(backend, "_factory", None)
        if native is not None:
            from .backends.craft_jobs import CraftJobFactory

            if not isinstance(native, CraftJobFactory):
                backend._factory = CraftJobFactory(native)
        elif getattr(backend, "craft_jobs_supported", False) is not True:
            raise ValueError("Backend does not support native craft receipts")

    def _save(self) -> None:
        if self._save_poisoned:
            raise RuntimeError("Checkpoint persistence failed; reconstruct before continuing")
        try:
            super()._save()
        except BaseException:
            self._save_poisoned = True
            raise

    def _job(self) -> CraftJob | None:
        data = self.memory.background_job if self.memory else None
        return CraftJob.from_dict(data) if data is not None else None

    def _observe(self):
        if self._save_poisoned:
            raise RuntimeError("Checkpoint persistence failed; reconstruct before continuing")
        snapshot = super()._observe()
        job = self._job()
        if job:
            try:
                complete = job.observe(snapshot)
            except InvalidCraftEvidence as error:
                job.failed = str(error)
                self.memory.background_job = job.to_dict()
                self.memory.status, self.memory.reason = "uncertain", job.failed
                self.memory.event("background_job_uncertain", job=job.parameters["receipt"],
                                  reason=job.failed, tick=snapshot.tick)
            else:
                self.memory.background_job = None if complete else job.to_dict()
                if complete:
                    self.memory.event("background_job_completed", job=job.parameters["receipt"],
                                      plan=job.plan_id, outputs=job.outputs, tick=snapshot.tick)
            # Persist updates before another action; this also protects the
            # release of output locks when completion is observed after restart.
            self._save()
        return snapshot

    def _execution_barrier(self, snapshot) -> bool:
        job = self._job()
        return self._save_poisoned or bool(job and job.failed)

    def _step_allowed(self, step, snapshot) -> bool:
        job = self._job()
        return (not self._execution_barrier(snapshot)
                and (job is None or job.permits(step)) and super()._step_allowed(step, snapshot))

    def _refresh_goals(self, snapshot) -> None:
        if self._job() is None:
            super()._refresh_goals(snapshot)

    def _record_extras(self) -> dict:
        return {"background_work": True, "background_schema": 1,
                "background_job": deepcopy(self.memory.background_job)}

    def _admit_background(self, snapshot) -> bool:
        if (self.memory.status != "running" or self.memory.background_job is not None
                or not self.memory.active_plan or not self.memory.pending):
            return False
        plan = Plan.from_dict(self.memory.active_plan)
        try:
            job = CraftJob.admit(plan, self.memory.pending, snapshot, self.catalog)
            # Also require all already produced output to be present before
            # freeing the actor. Admission is not completion verification.
            job.observe(snapshot)
        except InvalidCraftEvidence:
            return False  # Keep pending and its original deadline; never retry.
        self.memory.background_job = job.to_dict()
        self.memory.event("background_job_admitted", job=job.parameters["receipt"],
                          plan=plan.id, inputs_paid=job.inputs, outputs_locked=job.outputs,
                          tick=snapshot.tick)
        self._clear_plan()  # Releases inputs proven already paid, not future outputs.
        self._save()
        return True

    def _record(self, before, action, outcome, after=None, verified=False):
        # The base dispatcher has already persisted dispatch=returned, then
        # taken its normal post-action observation. No extra game poll here.
        if not verified and after is not None and self._admit_background(after):
            outcome += "; tracked in background, output not yet verified"
        return super()._record(before, action, outcome, after, verified)

    def _verify_pending(self, snapshot):
        if self._execution_barrier(snapshot):
            return self._record(snapshot, "observe", self.memory.reason)
        pending = self.memory.pending
        plan = Plan.from_dict(self.memory.active_plan)
        step = plan.steps[self.memory.step_index]
        if not step.satisfied(snapshot) and self._admit_background(snapshot):
            return self._record(snapshot, "observe", "Acknowledged craft continues in background")
        if (self.memory.status == "running" and pending.get("dispatch") == "returned"
                and step.action == "factory_wait"
                and step.effect in {"crafting_idle", "research_progress"}
                and not step.satisfied(snapshot)):
            candidates, _ = self._compile_candidates(snapshot)
            if any(candidate.steps[0].action != "factory_wait"
                   and self.memory.failures.get(candidate.id, 0) < 2
                   and self._step_allowed(candidate.steps[0], snapshot)
                   for candidate in candidates):
                self.memory.event("background_wait_yielded", plan=plan.id, tick=snapshot.tick)
                self._clear_plan()
                return self._record(snapshot, "observe", "Yield passive wait to independent work")
        return super()._verify_pending(snapshot)

    def _tracked_plan(self, plan: Plan, snapshot) -> Plan:
        if len(plan.steps) != 1 or plan.steps[0].action != "factory_craft":
            return plan
        step = plan.steps[0]
        recipe = self.catalog.recipes[step.parameters["recipe"]]
        products = recipe.get("products", [])
        ingredients = recipe.get("ingredients", [])
        if (len(products) != 1 or products[0]["type"] != "item"
                or products[0].get("probability", 1) != 1
                or not ingredients or any(entry["type"] != "item" for entry in ingredients)
                or any(entry["name"] == products[0]["name"] for entry in ingredients)):
            return plan  # Unsupported recipes retain foreground verification.
        parameters = {**step.parameters, "receipt": uuid4().hex}
        return replace(plan, description=plan.description + "; native receipt-tracked output",
                       steps=(replace(step, action="factory_craft_job", effect="craft_job_complete",
                                      parameters=parameters),))

    def _compile_candidates(self, snapshot):
        job = self._job()
        if job:
            plans = independent_candidates(self.memory.active_goal, snapshot, self.catalog, job)
            plans = [plan for plan in plans if self.memory.failures.get(plan.id, 0) < 2]
            return plans or [background_wait(self.memory.active_goal, job, snapshot.tick)], ""
        plans, blocker = super()._compile_candidates(snapshot)
        if (plans and plans[0].steps[0].action == "factory_wait"
                and plans[0].steps[0].effect == "research_progress"):
            independent = independent_candidates(self.memory.active_goal, snapshot, self.catalog)
            plans = [plan for plan in independent
                     if self.memory.failures.get(plan.id, 0) < 2] or plans
        return [self._tracked_plan(plan, snapshot) for plan in plans], blocker
