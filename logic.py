"""
logic.py — Pure Business Logic / Model Layer
=============================================
Responsibilities:
  - Low-level serial communication with the PSU (thread-safe).
  - Profile execution engine: step processing, countdown timing,
    safe shutdown, stop-event coordination.

Architectural Rules:
  - NO UI/GUI imports (no tkinter, customtkinter, matplotlib, etc.).
  - Completely independent and testable via unit tests without a GUI.
  - All functions / methods are pure data processing and hardware I/O.

Exports:
  - PSUController : thread-safe serial wrapper.
  - ProfileEngine : orchestrates the execution of a voltage profile.
"""

import threading
import time
from typing import Optional

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    msg = (
        "Missing dependency: pyserial.\n"
        "Install with: pip install pyserial"
    )
    raise ImportError(msg) from None


# ======================================================================
#  PSU Controller — Thread-Safe Serial Wrapper
# ======================================================================
class PSUController:
    """Low-level serial wrapper for communicating with an ITECH PSU.

    All public methods are thread-safe via an internal reentrant lock.

    Parameters
    ----------
    port : str
        The COM port name (e.g. ``"COM3"``).
    baudrate : int, optional
        Serial baud rate (default 9600).
    """

    def __init__(self, port: str, baudrate: int = 9600) -> None:
        self._port = port
        self._baudrate = baudrate
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def open(self) -> None:
        """Open the serial port connection."""
        self._ser = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1,
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close the serial port if it is open (thread-safe, idempotent)."""
        with self._lock:
            if self._ser is not None and self._ser.is_open:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None

    # ------------------------------------------------------------------
    def is_open(self) -> bool:
        """Return ``True`` if the serial port is currently open."""
        with self._lock:
            return self._ser is not None and self._ser.is_open

    # ------------------------------------------------------------------
    def write_cmd(self, cmd: str) -> None:
        """Send a SCPI command to the PSU.

        Parameters
        ----------
        cmd : str
            The SCPI command string (e.g. ``"VOLT 5.0"``).

        Raises
        ------
        RuntimeError
            If the serial port is not open.
        """
        with self._lock:
            if self._ser is not None and self._ser.is_open:
                self._ser.write(f"{cmd}\n".encode("utf-8"))
            else:
                raise RuntimeError("Serial port is not open")


# ======================================================================
#  Profile Engine — Execution Orchestrator
# ======================================================================
class ProfileEngine:
    """Executes a voltage/time profile on the PSU in a background thread.

    Parameters
    ----------
    psu : PSUController
        An opened PSU controller instance.
    stop_event : threading.Event
        External event used to signal cancellation.
    progress_callback : callable
        Called as ``progress_callback(msg_type, value)`` where
        *msg_type* is one of ``"status"``, ``"timer"``, ``"progress"``,
        or ``"done"``.
    """

    def __init__(
        self,
        psu: PSUController,
        stop_event: threading.Event,
        progress_callback: callable,
    ) -> None:
        self._psu = psu
        self._stop_event = stop_event
        self._progress = progress_callback

    # ------------------------------------------------------------------
    def run_profile(self, steps: list[tuple[float, float, float]], max_current: float) -> None:
        """Execute a multi-step voltage profile.

        Parameters
        ----------
        steps : list of (voltage, ramp_time, delay)
            Each tuple is one step.
        max_current : float
            Global current limit in Amperes.
        """
        psu = self._psu
        if not psu.is_open():
            self._progress("status", "Error: PSU not connected")
            self._progress("done", None)
            return

        try:
            self._initialise_psu(psu, max_current)

            total_elapsed = 0.0
            self._progress("progress", 0.0)

            for idx, (voltage, ramp_time, delay) in enumerate(steps, start=1):
                if self._stop_event.is_set():
                    break

                # --- Set slew rate & voltage ---
                if ramp_time > 0:
                    psu.write_cmd(f"VOLT:SLEW {ramp_time}")
                else:
                    psu.write_cmd("VOLT:SLEW MIN")

                psu.write_cmd(f"VOLT {voltage}")
                psu.write_cmd("OUTP 1")

                # --- Phase 1 : Ramp ---
                if ramp_time > 0:
                    self._progress("status", f"Ramping Step {idx}/{len(steps)} → {voltage} V")
                    self._countdown_timer(ramp_time, base_elapsed=total_elapsed)
                    total_elapsed += ramp_time
                    if self._stop_event.is_set():
                        break

                # --- Phase 2 : Delay (hold) ---
                if delay > 0:
                    self._progress("status", f"Holding Step {idx}/{len(steps)} at {voltage} V")
                    self._countdown_timer(delay, base_elapsed=total_elapsed)
                    total_elapsed += delay
                    if self._stop_event.is_set():
                        break

            # --- Finalise ---
            if self._stop_event.is_set():
                self._progress("status", "Stopped by user")
            else:
                self._progress("status", "Completed – all steps done")

        except Exception as exc:
            self._progress("status", f"Error: {exc}")

        finally:
            self._safe_shutdown(psu, self._progress)

    # ------------------------------------------------------------------
    def _initialise_psu(self, psu: PSUController, max_current: float) -> None:
        """Send initialisation commands to the PSU."""
        psu.write_cmd("SYST:REM")
        psu.write_cmd("FUNC VOLT")
        psu.write_cmd(f"CURR:LIM:POS {max_current}")
        psu.write_cmd(f"CURR:LIM:NEG -{max_current}")
        self._progress("status", f"Initialised – Limit: {max_current} A …")
        time.sleep(0.5)

    # ------------------------------------------------------------------
    def _countdown_timer(self, total_seconds: float, base_elapsed: float = 0.0) -> None:
        """Blocking countdown that pushes timer & progress updates.

        Parameters
        ----------
        total_seconds : float
            Duration of the current phase.
        base_elapsed : float
            Cumulative elapsed time before this phase started.
        """
        start = time.time()
        remaining = total_seconds
        while remaining > 0 and not self._stop_event.is_set():
            mins, secs = divmod(int(remaining), 60)
            self._progress("timer", f"{mins:02d}:{secs:02d}")
            elapsed_in_phase = total_seconds - remaining
            self._progress("progress", base_elapsed + elapsed_in_phase)
            time.sleep(0.1)
            remaining = total_seconds - (time.time() - start)

        if not self._stop_event.is_set():
            self._progress("timer", "00:00")
            self._progress("progress", base_elapsed + total_seconds)

    # ------------------------------------------------------------------
    @staticmethod
    def _safe_shutdown(psu: PSUController, progress: callable) -> None:
        """Safely turn off the output and release remote control."""
        try:
            psu.write_cmd("VOLT:SLEW MIN")
            psu.write_cmd("OUTP 0")
            time.sleep(0.3)
            psu.write_cmd("SYST:LOC")
            time.sleep(0.1)
        except Exception:
            pass
        finally:
            psu.close()
            progress("done", None)