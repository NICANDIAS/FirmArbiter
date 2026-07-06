# adapters/_template/lifecycle/signals.py
"""
ShutdownCoordinator — unifies the two ways VERITAS asks an adapter to stop:
  1. SIGTERM sent directly to the container process
  2. /veritas/control/shutdown.json written by the coordinator

Tool-agnostic by design: it knows nothing about FirmAE, FIRMADYNE, EMBA,
or any other candidate. Any adapter built on the template gets both
shutdown paths for free.
"""

import json
import signal
import threading
from pathlib import Path


class ShutdownCoordinator:
    def __init__(self, control_dir, poll_interval_seconds=2):
        """
        control_dir: the 'control' path from request.json's paths section
                     (e.g. "/veritas/control") — where shutdown.json appears
        poll_interval_seconds: how often to check for the shutdown marker
                                file, independent of the heartbeat interval
        """
        self._control_dir = Path(control_dir)
        self._shutdown_file = self._control_dir / "shutdown.json"
        self._poll_interval = poll_interval_seconds

        self._shutdown_event = threading.Event()
        self._reason = None  # "sigterm" or "shutdown_file"
        self._poll_thread = None

        # Register SIGTERM handler. SIGINT included too so Ctrl-C during
        # local/manual testing behaves the same way as a real shutdown.
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self._reason = "sigterm"
        self._shutdown_event.set()

    def _poll_for_marker(self):
        while not self._shutdown_event.is_set():
            if self._shutdown_file.exists():
                self._reason = "shutdown_file"
                self._shutdown_event.set()
                return
            self._shutdown_event.wait(self._poll_interval)

    def start_polling(self):
        """Begin watching for shutdown.json in a background thread.
        SIGTERM handling is already active regardless (registered in __init__)."""
        self._poll_thread = threading.Thread(target=self._poll_for_marker, daemon=True)
        self._poll_thread.start()

    def wait_for_shutdown(self, timeout=None):
        """Block until a shutdown signal is received (either kind), or
        until timeout elapses. Returns True if shutdown was signalled,
        False if it timed out waiting."""
        return self._shutdown_event.wait(timeout)

    def shutdown_requested(self):
        return self._shutdown_event.is_set()

    def reason(self):
        """Returns 'sigterm', 'shutdown_file', or None if not yet triggered."""
        return self._reason

    def read_shutdown_payload(self):
        """If shutdown.json exists and has content, return the parsed dict.
        Returns None if the file doesn't exist or shutdown was via SIGTERM."""
        if not self._shutdown_file.exists():
            return None
        try:
            return json.loads(self._shutdown_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
