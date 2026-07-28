# adapters/_template/lifecycle/heartbeat.py
"""
HeartbeatWorker — background thread that emits periodic heartbeat events
so the FIRMARBITER coordinator's watchdog does not treat a slow-but-alive
adapter (e.g. QEMU under ARM64 translation) as hung.
"""

import threading
import time


class HeartbeatWorker:
    def __init__(self, event_writer, interval_seconds, state_provider=None):
        """
        event_writer:    the EventWriter instance to emit heartbeats through
        interval_seconds: how often to emit, taken from request.json's
                           lifecycle.heartbeat_interval_seconds — never hardcode this
        state_provider:  optional zero-arg callable returning a short string
                         describing current pipeline state (e.g. "unpacking",
                         "waiting_for_boot"), passed through to the heartbeat
                         event's 'state' field. Optional because not every
                         adapter needs to report state.
        """
        self._event_writer = event_writer
        self._interval = interval_seconds
        self._state_provider = state_provider
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            raise RuntimeError("HeartbeatWorker already started")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        # Emit immediately on start so the coordinator sees liveness right
        # away, then on the configured interval.
        while not self._stop_event.is_set():
            state = self._state_provider() if self._state_provider else None
            self._event_writer.heartbeat(state=state)
            # wait() returns early if stop() is called, so shutdown isn't
            # delayed by up to a full interval
            self._stop_event.wait(self._interval)

    def stop(self, timeout_seconds=5):
        """Signal the thread to stop and wait for it to actually exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)
            if self._thread.is_alive():
                # Non-fatal — the process is shutting down anyway, but the
                # entrypoint should log this as a minor anomaly, not crash.
                return False
        return True
