from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from veritas_core.docker_supervisor import (
    DockerSupervisorError,
)
from veritas_core.event_protocol import (
    EventProtocolError,
)


class LifecycleSupervisor(Protocol):
    """
    Operations required by the neutral lifecycle watchdog.

    The protocol contains no candidate-specific behaviour.
    """

    request: dict[str, Any]
    events: list[dict[str, Any]]
    contract_root: Path

    @property
    def container_running(self) -> bool:
        ...

    def drain_events(self) -> list[dict[str, Any]]:
        ...

    def request_shutdown(self, reason: str) -> None:
        ...

    def wait_for_exit(self, timeout_seconds: float) -> int:
        ...

    def force_terminate(self) -> None:
        ...

    def exit_code_if_exited(self) -> int | None:
        ...


@dataclass(frozen=True)
class LifecycleObservation:
    metric: str
    run_outcome: str
    trigger: str
    termination_mode: str
    started_at: str
    completed_at: str
    elapsed_seconds: float
    experiment_timeout_seconds: float
    heartbeat_timeout_seconds: float
    shutdown_grace_seconds: float
    heartbeats_received: int
    last_heartbeat_received_at: str | None
    shutdown_requested: bool
    cleanup_complete_seen: bool
    adapter_stopped_seen: bool
    final_adapter_outcome: str | None
    exit_code: int | None
    force_termination_used: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LifecycleWatchdogError(RuntimeError):
    """Raised when watchdog operation itself fails."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class LifecycleWatchdog:
    """
    Supervise one adapter lifecycle independently of candidate output.

    The watchdog runs in a background thread so independent probes may operate
    while heartbeat and timeout enforcement continue.
    """

    def __init__(
        self,
        supervisor: LifecycleSupervisor,
        *,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise LifecycleWatchdogError(
                "poll_interval_seconds must be greater than zero"
            )

        self.supervisor = supervisor
        self.poll_interval_seconds = poll_interval_seconds

        lifecycle = supervisor.request["lifecycle"]

        self.experiment_timeout_seconds = float(
            lifecycle["timeout_seconds"]
        )
        self.heartbeat_timeout_seconds = float(
            lifecycle["heartbeat_timeout_seconds"]
        )
        self.shutdown_grace_seconds = float(
            lifecycle["shutdown_grace_seconds"]
        )

        self._shutdown_requested_by_core = threading.Event()
        self._finished = threading.Event()
        self._condition = threading.Condition()

        self._thread: threading.Thread | None = None
        self._result: LifecycleObservation | None = None
        self._thread_error: BaseException | None = None

        self._observed_events: list[dict[str, Any]] = []
        self._known_sequences: set[int] = set()

    @property
    def observed_events(self) -> list[dict[str, Any]]:
        with self._condition:
            return list(self._observed_events)

    def start(self) -> None:
        if self._thread is not None:
            raise LifecycleWatchdogError(
                "Lifecycle watchdog has already been started"
            )

        self._thread = threading.Thread(
            target=self._run,
            name="veritas-lifecycle-watchdog",
            daemon=True,
        )
        self._thread.start()

    def request_benchmark_shutdown(self) -> None:
        """
        Signal that all planned independent measurements have completed.
        """
        self._shutdown_requested_by_core.set()

    def wait_for_matching_event(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        description: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Wait until one validated event satisfies ``predicate``."""
        deadline = time.monotonic() + timeout_seconds

        with self._condition:
            while True:
                for event in self._observed_events:
                    if predicate(event):
                        return dict(event)

                if self._finished.is_set():
                    raise LifecycleWatchdogError(
                        "Lifecycle ended before "
                        f"{description} was observed"
                    )

                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    raise LifecycleWatchdogError(
                        f"Timed out waiting for {description}"
                    )

                self._condition.wait(
                    timeout=min(remaining, 0.25)
                )

    def wait_for_event(
        self,
        event_name: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return self.wait_for_matching_event(
            lambda event: event.get("event") == event_name,
            description=f"event {event_name!r}",
            timeout_seconds=timeout_seconds,
        )

    def wait(
        self,
        timeout_seconds: float,
    ) -> LifecycleObservation:
        if self._thread is None:
            raise LifecycleWatchdogError(
                "Lifecycle watchdog has not been started"
            )

        if not self._finished.wait(timeout_seconds):
            raise LifecycleWatchdogError(
                "Timed out waiting for lifecycle watchdog"
            )

        self._thread.join(timeout=1)

        if self._thread_error is not None:
            raise LifecycleWatchdogError(
                "Lifecycle watchdog failed"
            ) from self._thread_error

        if self._result is None:
            raise LifecycleWatchdogError(
                "Lifecycle watchdog produced no observation"
            )

        return self._result

    def _sync_supervisor_events(
        self,
    ) -> list[dict[str, Any]]:
        newly_observed: list[dict[str, Any]] = []

        with self._condition:
            for event in self.supervisor.events:
                sequence = event.get("sequence")

                if not isinstance(sequence, int):
                    continue

                if sequence in self._known_sequences:
                    continue

                self._known_sequences.add(sequence)
                copied_event = dict(event)

                self._observed_events.append(copied_event)
                newly_observed.append(copied_event)

            if newly_observed:
                self._condition.notify_all()

        return newly_observed

    def _drain_and_sync(
        self,
    ) -> list[dict[str, Any]]:
        self.supervisor.drain_events()
        return self._sync_supervisor_events()

    def _terminal_event_state(
        self,
    ) -> tuple[bool, bool, str | None]:
        cleanup_seen = False
        stopped_seen = False
        final_outcome: str | None = None

        for event in self.observed_events:
            if event.get("event") == "cleanup_complete":
                cleanup_seen = True

            if event.get("event") == "adapter_stopped":
                stopped_seen = True
                outcome = event.get("outcome")

                if isinstance(outcome, str):
                    final_outcome = outcome

        return cleanup_seen, stopped_seen, final_outcome

    def _persist_result(
        self,
        observation: LifecycleObservation,
    ) -> None:
        artifact_directory = (
            self.supervisor.contract_root / "artifacts"
        )
        artifact_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path = (
            artifact_directory
            / "lifecycle-observation.json"
        )

        temporary_path = output_path.with_suffix(".tmp")

        temporary_path.write_text(
            json.dumps(
                observation.to_dict(),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(output_path)

    def _run(self) -> None:
        started_at = _utc_now()
        started_monotonic = time.monotonic()

        last_heartbeat_monotonic = started_monotonic
        last_heartbeat_received_at: str | None = None
        heartbeats_received = 0

        trigger = "contract_error"
        trigger_reason = "Lifecycle watchdog did not initialise"
        shutdown_requested = False
        force_termination_used = False
        termination_mode = "not_started"

        try:
            while True:
                newly_observed = self._drain_and_sync()
                now = time.monotonic()

                for event in newly_observed:
                    event_name = event.get("event")

                    if event_name == "heartbeat":
                        heartbeats_received += 1
                        last_heartbeat_monotonic = now
                        last_heartbeat_received_at = _utc_now()

                    if (
                        event_name == "adapter_stopped"
                        and not
                        self._shutdown_requested_by_core.is_set()
                    ):
                        trigger = "unexpected_exit"
                        trigger_reason = (
                            "The adapter emitted adapter_stopped "
                            "before VERITAS requested shutdown"
                        )
                        break
                else:
                    event_name = None

                if trigger == "unexpected_exit":
                    break

                if self._shutdown_requested_by_core.is_set():
                    trigger = "benchmark_complete"
                    trigger_reason = (
                        "Planned independent measurements completed"
                    )
                    break

                if not self.supervisor.container_running:
                    trigger = "unexpected_exit"
                    trigger_reason = (
                        "The adapter container exited before "
                        "VERITAS requested shutdown"
                    )
                    break

                elapsed = now - started_monotonic

                if elapsed >= self.experiment_timeout_seconds:
                    trigger = "experiment_timeout"
                    trigger_reason = (
                        "The configured experiment timeout was "
                        "reached"
                    )
                    break

                heartbeat_age = (
                    now - last_heartbeat_monotonic
                )

                if heartbeat_age >= self.heartbeat_timeout_seconds:
                    trigger = "heartbeat_lost"
                    trigger_reason = (
                        "No valid adapter heartbeat was received "
                        "within the configured heartbeat timeout"
                    )
                    break

                time.sleep(self.poll_interval_seconds)

            if (
                trigger == "unexpected_exit"
                and not self.supervisor.container_running
            ):
                termination_mode = "already_exited"

            else:
                shutdown_requested = True

                self.supervisor.request_shutdown(
                    reason=trigger
                )

                try:
                    self.supervisor.wait_for_exit(
                        timeout_seconds=(
                            self.shutdown_grace_seconds
                        )
                    )

                    self._sync_supervisor_events()

                    cleanup_seen, stopped_seen, _ = (
                        self._terminal_event_state()
                    )

                    if cleanup_seen and stopped_seen:
                        termination_mode = (
                            "graceful_shutdown"
                        )
                    else:
                        termination_mode = (
                            "incomplete_shutdown"
                        )

                except DockerSupervisorError:
                    force_termination_used = True
                    termination_mode = "forced_termination"

                    self.supervisor.force_terminate()
                    self._sync_supervisor_events()

            try:
                self._drain_and_sync()
            except (
                EventProtocolError,
                DockerSupervisorError,
            ):
                # Preserve the primary lifecycle outcome even if no more
                # events can be collected after termination.
                pass

            cleanup_seen, stopped_seen, final_outcome = (
                self._terminal_event_state()
            )

            exit_code = (
                self.supervisor.exit_code_if_exited()
            )

            if (
                trigger == "benchmark_complete"
                and termination_mode == "graceful_shutdown"
                and cleanup_seen
                and stopped_seen
                and final_outcome == "completed"
                and exit_code == 0
            ):
                run_outcome = "completed"
                reason = (
                    "The benchmark completed its measurements and "
                    "the adapter shut down cleanly"
                )

            elif trigger == "benchmark_complete":
                run_outcome = "shutdown_failed"
                reason = (
                    "Measurements completed, but the adapter did "
                    "not complete the required graceful shutdown"
                )

            elif trigger == "unexpected_exit":
                run_outcome = "unexpected_early_exit"
                reason = trigger_reason

            elif trigger == "heartbeat_lost":
                run_outcome = "heartbeat_lost"
                reason = trigger_reason

            elif trigger == "experiment_timeout":
                run_outcome = "experiment_timeout"
                reason = trigger_reason

            else:
                run_outcome = "contract_error"
                reason = trigger_reason

            completed_at = _utc_now()
            elapsed_seconds = round(
                time.monotonic() - started_monotonic,
                6,
            )

            observation = LifecycleObservation(
                metric="adapter_lifecycle",
                run_outcome=run_outcome,
                trigger=trigger,
                termination_mode=termination_mode,
                started_at=started_at,
                completed_at=completed_at,
                elapsed_seconds=elapsed_seconds,
                experiment_timeout_seconds=(
                    self.experiment_timeout_seconds
                ),
                heartbeat_timeout_seconds=(
                    self.heartbeat_timeout_seconds
                ),
                shutdown_grace_seconds=(
                    self.shutdown_grace_seconds
                ),
                heartbeats_received=heartbeats_received,
                last_heartbeat_received_at=(
                    last_heartbeat_received_at
                ),
                shutdown_requested=shutdown_requested,
                cleanup_complete_seen=cleanup_seen,
                adapter_stopped_seen=stopped_seen,
                final_adapter_outcome=final_outcome,
                exit_code=exit_code,
                force_termination_used=(
                    force_termination_used
                ),
                reason=reason,
            )

            self._result = observation
            self._persist_result(observation)

        except (
            EventProtocolError,
            DockerSupervisorError,
            OSError,
            ValueError,
            KeyError,
        ) as exc:
            self._thread_error = exc

            try:
                self.supervisor.force_terminate()
            except Exception:
                pass

        except BaseException as exc:
            self._thread_error = exc

            try:
                self.supervisor.force_terminate()
            except Exception:
                pass

        finally:
            self._finished.set()

            with self._condition:
                self._condition.notify_all()
