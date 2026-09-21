"""Durable, session-preserving supervision for an autonomous campaign."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass
class SupervisorConfig:
    state_dir: Path
    checkpoint: Path
    session_id: str
    started_at: float
    repair_command: list[str]
    cwd: Path
    python: str = sys.executable
    duration_hours: float = 12
    poll_seconds: float = 5
    hang_seconds: float = 600
    repair_seconds: float = 1800
    backoff_seconds: float = 30
    tick_seconds: float = 1

    def validate(self) -> None:
        for name in ("started_at", "duration_hours", "poll_seconds", "hang_seconds",
                     "repair_seconds", "backoff_seconds", "tick_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not self.session_id or not isinstance(self.repair_command, list) or not self.repair_command or not all(
            isinstance(part, str) and part for part in self.repair_command
        ):
            raise ValueError("A session ID and nonempty repair argv are required")


class Supervisor:
    def __init__(self, config: SupervisorConfig, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 popen: Callable = subprocess.Popen) -> None:
        config.validate()
        self.config = config
        self.clock, self.sleep, self.popen = clock, sleep, popen
        self.state: dict = {}
        self.process = None
        self.output = None
        self.stop_requested = False

    @property
    def state_path(self) -> Path:
        return self.config.state_dir / "supervisor.json"

    def remaining(self) -> float:
        return max(0, self.state["cutoff"] - self.clock())

    def event(self, kind: str, **fields) -> None:
        try:
            with (self.config.state_dir / "events.jsonl").open("a") as stream:
                stream.write(json.dumps({"at": self.clock(), "event": kind, **fields}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            self.stop_requested = True
            print(f"Supervisor audit failed; stopping: {error}", file=sys.stderr, flush=True)

    def save(self, **fields) -> None:
        self.state.update(fields)
        atomic_json(self.state_path, self.state)

    def checkpoint(self) -> dict:
        with self.config.checkpoint.open() as stream:
            checkpoint = json.load(stream)
        if not isinstance(checkpoint, dict):
            raise ValueError("Checkpoint must be a JSON object")
        if checkpoint.get("session_id") != self.config.session_id:
            raise ValueError("Checkpoint session differs from supervised session")
        if checkpoint.get("target") != "rocket_launch":
            raise ValueError("Checkpoint target must be rocket_launch")
        if checkpoint.get("status") not in {"running", "blocked", "uncertain", "completed"}:
            raise ValueError("Checkpoint status is invalid")
        return checkpoint

    def initialize(self) -> None:
        cutoff = self.config.started_at + self.config.duration_hours * 3600
        identity = {"session_id": self.config.session_id,
                    "checkpoint": str(self.config.checkpoint.resolve()),
                    "cwd": str(self.config.cwd.resolve()),
                    "started_at": self.config.started_at, "cutoff": cutoff}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if any(self.state.get(key) != value for key, value in identity.items()):
                raise ValueError("Existing supervision identity/cutoff cannot be changed")
            self.recover_process()
        else:
            self.state = {**identity, "attempt": 0, "phase": "ready", "process": None}
            self.save()
        if not self.state.get("repair_required"):
            try:
                self.save(last_valid_checkpoint=self.checkpoint())
            except (OSError, ValueError):
                if not self.state.get("last_valid_checkpoint"):
                    raise
                self.begin_repair("checkpoint_invalid_on_restart")

    @staticmethod
    def process_identity(pid: int) -> str | None:
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            return None

    def recover_process(self) -> None:
        saved = self.state.get("process")
        if not saved:
            return
        current = self.process_identity(saved["pid"])
        if current is not None and current != saved["identity"]:
            raise RuntimeError("Saved process PID was reused; refusing unsafe recovery")
        self.kill_group(saved["pid"])
        self.save(process=None, phase="recovered")
        self.event("orphan_process_group_stopped", pid=saved["pid"])

    @staticmethod
    def kill_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def launch(self, command: list[str], phase: str, prompt: Path | None = None) -> None:
        if self.remaining() <= 0 or self.stop_requested:
            return
        self.output = (self.config.state_dir / f"{phase}.log").open("ab")
        input_stream = prompt.open("rb") if prompt else subprocess.DEVNULL
        temporary = self.config.cwd / "runs" / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["TMPDIR"] = str(temporary)
        environment["PYTHONPATH"] = str(self.config.cwd / "src")
        try:
            self.process = self.popen(
                command, cwd=self.config.cwd, stdin=input_stream,
                stdout=self.output, stderr=subprocess.STDOUT, start_new_session=True,
                env=environment,
            )
        finally:
            if prompt:
                input_stream.close()
        self.save(phase=phase, process={"pid": self.process.pid,
                                      "identity": self.process_identity(self.process.pid)})
        self.event("process_started", phase=phase, pid=self.process.pid)

    def stop_process(self) -> None:
        if self.process is not None:
            self.kill_group(self.process.pid)
            self.process.wait()
            self.process = None
        if self.output is not None:
            self.output.close()
            self.output = None
        self.save(process=None)

    def pause(self, seconds: float) -> None:
        until = min(self.clock() + seconds, self.state["cutoff"])
        while not self.stop_requested and self.clock() < until:
            self.sleep(min(self.config.poll_seconds, until - self.clock()))

    def gameplay_command(self) -> list[str]:
        return [
            self.config.python, "-m", "jev_factorio", "--backend", "fle", "--resume",
            "--resume-controller", "--controller", "hierarchical", "--policy", "hybrid",
            "--model", "jev-1.13.0",
            "--target", "rocket_launch", "--checkpoint", str(self.config.checkpoint),
            "--duration-hours", str(self.remaining() / 3600),
            "--tick-seconds", str(self.config.tick_seconds),
            "--log-file", str(self.config.state_dir / "gameplay.jsonl"),
        ]

    def watch_game(self) -> str:
        checkpoint = self.checkpoint()
        self.save(last_valid_checkpoint=checkpoint)
        if checkpoint["status"] != "running":
            return checkpoint["status"]
        self.launch(self.gameplay_command(), "gameplay")
        last_change = self.clock()
        signature = self.config.checkpoint.stat().st_mtime_ns
        while self.remaining() > 0 and not self.stop_requested:
            try:
                checkpoint = self.checkpoint()
                changed = self.config.checkpoint.stat().st_mtime_ns
            except (OSError, ValueError) as error:
                return f"checkpoint_invalid: {error}"
            if checkpoint["status"] != "running":
                self.save(last_valid_checkpoint=checkpoint)
                return checkpoint["status"]
            if changed != signature:
                signature, last_change = changed, self.clock()
                self.save(last_valid_checkpoint=checkpoint)
            if self.process.poll() is not None:
                return f"process_exit: {self.process.returncode}"
            if self.clock() - last_change >= self.config.hang_seconds:
                return "checkpoint_heartbeat_timeout"
            self.pause(self.config.poll_seconds)
        return "cutoff" if not self.stop_requested else "stopped"

    def repair_prompt(self, reason: str, result: Path) -> str:
        return f"""Repair the stopped autonomous Factorio campaign in {self.config.cwd}.
Session: {self.config.session_id}
Controller checkpoint: {self.config.checkpoint}
Supervisor audit/log directory: {self.config.state_dir}
Read {self.config.state_dir / 'OPERATIONS.md'} first if present for native session
and repository acceptance details.
Read supervisor.json incident.checkpoint and incident.source as the immutable
pre-repair baseline. Rejected repair attempts never replace that baseline.
Failure: {reason}
Absolute wallclock cutoff (Unix seconds): {self.state['cutoff']}
Do not launch gameplay or reset/recreate/reconnect the game or its FLE client.
FLE runtime handlers are ephemeral: preserve runtime and original entity identity.
Preserve this exact session and
checkpoint. Never clear ambiguous pending actions to enable a retry; reconcile
against observed game evidence and preserve the write-ahead safety contract.
Inspect logs and source, fix the root cause, add focused tests and run them.
You are authorized to make focused commits, push a repair branch, open a PR,
and merge only after required checks pass and independent exact-head source
review approves. Synchronize origin and fork after merge. Never bypass checks,
force-push, expose credentials, or claim success from dispatch acknowledgement.
If any acceptance requirement cannot be completed, report blocked.
Write a JSON object to {result} with these fields:
status ("repaired" or "blocked"), kind ("code" or "operational"),
session_id, checkpoint (absolute path),
tests_passed (boolean), checks_passed (boolean), exact_head_reviewed (boolean),
merged (boolean), remotes_synced (boolean), commit (full 40-character SHA),
pr_url (GitHub PR URL), evidence (nonempty list of evidence strings).
If GitHub has no independent approval, use a separate Codex source-review agent,
and provide independent_review (path to its JSON artifact) and repair_agent
(your unique agent/session ID). The artifact must contain head (exact PR head),
verdict ("approved"), reviewer (different agent/session ID), and source_evidence
(nonempty list of concrete source findings). Never bypass required branch rules.
For operational reconciliation requiring no code change, omit code-only fields,
set operational_verified=true and provide receipt/inventory/observation evidence.
Operational repairs must leave tracked source and HEAD unchanged. In ALL modes
leave any existing pending action unchanged: the resumed controller must verify
its postcondition from observations. Never erase ambiguity through metadata.
Also preserve its active_plan, step_index, and reservations exactly.
After fixing observation or execution logic, you may set status to running while
retaining pending exactly: controller pending verification runs before dispatch.
Do not silently clear failure budgets or manufacture progress.
Only report repaired when every acceptance requirement is verified.
"""

    def capture(self, command: list[str]) -> tuple[int | None, str]:
        log = self.config.state_dir / "verification.log"
        offset = log.stat().st_size if log.exists() else 0
        self.launch(command, "verification")
        if self.process is None:
            return None, ""
        deadline = min(self.clock() + self.config.repair_seconds, self.state["cutoff"])
        while self.clock() < deadline and not self.stop_requested and self.process.poll() is None:
            self.pause(min(self.config.poll_seconds, deadline - self.clock()))
        returncode = self.process.poll()
        self.stop_process()
        with log.open("rb") as stream:
            stream.seek(offset)
            return returncode, stream.read().decode(errors="replace").strip()

    def source_identity(self) -> tuple[str, str] | None:
        head_code, head = self.capture(["git", "rev-parse", "HEAD"])
        diff_code, diff = self.capture(["git", "diff", "HEAD", "--"])
        status_code, status = self.capture(["git", "status", "--porcelain"])
        files_code, files = self.capture(["git", "ls-files", "-z", "--cached", "--others",
                                         "--exclude-standard"])
        if not head_code == diff_code == status_code == files_code == 0:
            return None
        digest = hashlib.sha256()
        for name in sorted(set(files.split("\0")) - {""}):
            path = self.config.cwd / name
            digest.update(name.encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        if self.remaining() <= 0 or self.stop_requested:
                            return None
                        digest.update(chunk)
            else:
                digest.update(b"<missing>")
        return head, diff + "\n" + status + "\n" + digest.hexdigest()

    def independent_review(self, result: dict, head: str) -> bool:
        path = result.get("independent_review")
        if not isinstance(path, str) or not result.get("repair_agent"):
            return False
        review_path = Path(path).resolve()
        if not review_path.is_relative_to(self.config.state_dir.resolve()):
            return False
        review = json.loads(review_path.read_text())
        evidence = review.get("source_evidence")
        return (review.get("head") == head and review.get("verdict") == "approved"
                and isinstance(review.get("reviewer"), str) and bool(review["reviewer"])
                and review["reviewer"] != result["repair_agent"]
                and isinstance(evidence, list) and bool(evidence)
                and all(isinstance(item, str) and item.strip() for item in evidence))

    def verify_code(self, result: dict) -> bool:
        commit = result["commit"]
        code, head = self.capture(["git", "rev-parse", "HEAD"])
        if code != 0 or head != commit:
            return False
        code, worktree = self.capture(["git", "status", "--porcelain"])
        if code != 0 or worktree:
            return False
        for remote in ("origin", "fork"):
            code, reference = self.capture(["git", "ls-remote", remote, "refs/heads/main"])
            if code != 0 or reference.split() != [commit, "refs/heads/main"]:
                return False
        url = result.get("pr_url", "")
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            return False
        code, raw = self.capture([
            "gh", "pr", "view", url, "--json",
            "state,headRefOid,mergeCommit,reviews,statusCheckRollup",
        ])
        if code != 0:
            return False
        pull = json.loads(raw)
        if pull["state"] != "MERGED" or pull["mergeCommit"]["oid"] != commit:
            return False
        reviews = pull.get("reviews", [])
        latest = {}
        for review in reviews:
            latest[review.get("author", {}).get("login")] = review
        approved = any(
            review.get("state") == "APPROVED"
            and review.get("commit", {}).get("oid") == pull["headRefOid"]
            for review in latest.values()
        )
        if (not approved and not self.independent_review(result, pull["headRefOid"])
                or any(review.get("state") == "CHANGES_REQUESTED" for review in latest.values())):
            return False
        checks = pull.get("statusCheckRollup", [])
        if not checks or not all(
            check.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"}
            or check.get("state") == "SUCCESS" for check in checks
        ):
            return False
        code, _ = self.capture([self.config.python, "-m", "pytest", "tests/"])
        if code != 0:
            return False
        status_code, worktree = self.capture(["git", "status", "--porcelain"])
        head_code, head = self.capture(["git", "rev-parse", "HEAD"])
        return status_code == head_code == 0 and not worktree and head == commit

    def validate_repair(self, path: Path, previous: dict,
                        source_before: tuple[str, str] | None = None) -> bool:
        try:
            result = json.loads(path.read_text())
            if result.get("status") != "repaired":
                return False
            if result.get("session_id") != self.config.session_id:
                return False
            if result.get("checkpoint") != str(self.config.checkpoint.resolve()):
                return False
            evidence = result.get("evidence")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item.strip() for item in evidence
            ):
                return False
            current = self.checkpoint()
            if current["status"] not in {"running", "completed"}:
                return False
            if previous.get("pending"):
                if any(current.get(key) != previous.get(key) for key in (
                    "pending", "active_plan", "step_index", "reservations"
                )):
                    return False
            if any(current.get("failures", {}).get(key, 0) < count
                   for key, count in previous.get("failures", {}).items()):
                return False
            if result.get("kind") == "operational":
                return (result.get("operational_verified") is True
                        and source_before is not None
                        and self.source_identity() == tuple(source_before))
            if result.get("kind") != "code" or not all(result.get(key) is True for key in (
                "tests_passed", "checks_passed", "exact_head_reviewed", "merged", "remotes_synced"
            )):
                return False
            commit = result.get("commit", "")
            if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
                return False
            return self.verify_code(result)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return False

    def begin_repair(self, reason: str) -> None:
        if self.state.get("repair_required"):
            return
        try:
            previous = self.checkpoint()
        except (OSError, ValueError):
            previous = self.state.get("last_valid_checkpoint")
            if previous is None:
                raise
        self.save(repair_required=True, incident={
            "reason": reason, "checkpoint": previous, "source": None,
        })
        incident = {**self.state["incident"], "source": self.source_identity()}
        self.save(incident=incident)
        self.event("repair_required", reason=reason)

    def repair(self, reason: str) -> bool:
        self.begin_repair(reason)
        incident = self.state["incident"]
        previous, source_before = incident["checkpoint"], incident["source"]
        attempt = self.state["attempt"] + 1
        self.save(attempt=attempt)
        result = self.config.state_dir / f"repair-{attempt}.json"
        prompt = self.config.state_dir / f"repair-{attempt}.txt"
        prompt.write_text(self.repair_prompt(reason, result))
        self.launch(self.config.repair_command, "repair", prompt)
        if self.process is None:
            return False
        deadline = min(self.clock() + self.config.repair_seconds, self.state["cutoff"])
        while (self.clock() < deadline and not self.stop_requested
               and self.process.poll() is None):
            self.pause(min(self.config.poll_seconds, deadline - self.clock()))
        returncode = self.process.poll()
        self.stop_process()
        accepted = returncode == 0 and self.validate_repair(result, previous, source_before)
        self.event("repair_finished", attempt=attempt, returncode=returncode, accepted=accepted)
        if accepted:
            self.save(repair_required=False, incident=None,
                      last_valid_checkpoint=self.checkpoint(), phase="ready")
        return accepted

    def run(self) -> int:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path().open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another supervisor owns this repository") from error
            self.initialize()
            failures = 0
            try:
                while self.remaining() > 0 and not self.stop_requested:
                    if self.state.get("repair_required"):
                        reason = self.state["incident"]["reason"]
                    else:
                        try:
                            reason = self.watch_game()
                        except (OSError, ValueError) as error:
                            reason = f"gameplay_error: {error}"
                        finally:
                            self.stop_process()
                    self.event("gameplay_stopped", reason=reason)
                    if reason == "completed":
                        self.save(phase="completed")
                        return 0
                    if reason in {"cutoff", "stopped"}:
                        break
                    self.begin_repair(reason)
                    accepted = False
                    while self.remaining() > 0 and not self.stop_requested and not accepted:
                        try:
                            accepted = self.repair(reason)
                        except (OSError, ValueError) as error:
                            self.event("repair_error", error=str(error))
                        finally:
                            self.stop_process()
                        failures = 0 if accepted else min(failures + 1, 6)
                        self.pause(min(900, self.config.backoff_seconds * 2 ** failures))
                self.save(phase="stopped" if self.stop_requested else "cutoff")
                self.event(self.state["phase"])
                return 0
            finally:
                self.stop_process()

    @staticmethod
    def lock_path() -> Path:
        return Path.home() / ".jev-factorio-supervisor.lock"


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--started-at", type=float, required=True)
    parser.add_argument("--repair-command-json", required=True)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--duration-hours", type=float, default=12)
    parser.add_argument("--hang-seconds", type=float, default=600)
    parser.add_argument("--repair-seconds", type=float, default=1800)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--backoff-seconds", type=float, default=30)
    parser.add_argument("--tick-seconds", type=float, default=1)
    arguments = vars(parser.parse_args())
    try:
        arguments["repair_command"] = json.loads(arguments.pop("repair_command_json"))
        for key in ("state_dir", "checkpoint", "cwd"):
            arguments[key] = arguments[key].resolve()
        supervisor = Supervisor(SupervisorConfig(**arguments))
    except (ValueError, TypeError) as error:
        parser.error(str(error))

    def stop(signum, frame) -> None:
        supervisor.stop_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    raise SystemExit(supervisor.run())


if __name__ == "__main__":
    cli()
