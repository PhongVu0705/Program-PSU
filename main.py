"""
main.py — Coordinator / Controller Layer
=========================================
Responsibilities:
  - Instantiate the UI (PSUView) and Logic (PSUController, ProfileEngine)
    objects.
  - Bind/wire UI events to business-logic methods.
  - Handle data flow:  UI input → Logic processing → UI output.
  - Start the application event loop under ``if __name__ == "__main__"``.

Architectural Rules:
  - This file is the *only* module that imports both ``ui`` and ``logic``.
  - It contains zero pure-business-logic (that lives in ``logic.py``).
  - It contains zero widget-creation code (that lives in ``ui.py``).
"""

import threading
import queue
import time
from typing import Optional

import serial.tools.list_ports

from ui import PSUView
from logic import PSUController, ProfileEngine


class PSUControllerApp:
    """Application controller that bridges ``PSUView`` and the logic layer.

    Responsibilities:
      - Owns the PSU device handle, worker thread, stop-event, and queue.
      - Provides all callbacks that ``PSUView`` needs for user interactions.
      - Polls the progress queue and pushes updates to the view.
    """

    def __init__(self) -> None:
        # ── Core state ─────────────────────────────────────────────────
        self._psu: Optional[PSUController] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._last_graph_draw_time = 0.0
        self._graph_needs_draw = False
        self._debounce_timer = None

        # ── Build the view (no callbacks attached yet) ─────────────────
        self.view = PSUView()

        # ── Wire all callbacks ─────────────────────────────────────────
        self._wire_callbacks()

        self.view.protocol("WM_DELETE_WINDOW", self._on_app_close)

        # ── Initial COM port scan ──────────────────────────────────────
        self._on_scan_ports()

        # ── Start queue polling ────────────────────────────────────────
        self.view.after(100, self._poll_queue)
    
    def _on_app_close(self) -> None:
        """Đảm bảo dừng thread và ngắt cổng COM an toàn trước khi tắt GUI."""
        self._stop_event.set()  # Ra lệnh dừng cho ProfileEngine
        if self._psu and self._psu.is_open():
            self._psu.close()   # Nhả cổng COM Port ra
        self.view.destroy()     # Tắt hoàn toàn UI

    # ==================================================================
    #  Callback wiring
    # ==================================================================
    def _wire_callbacks(self) -> None:
        """Bind every UI event to its controller handler."""
        self.view.set_callbacks(
            on_start=self._on_start,
            on_stop=self._on_stop,
            on_reset=self._on_reset,
            on_scan_ports=self._on_scan_ports,
            on_add_step=self._on_add_step,
            on_clear_steps=self._on_clear_steps,
            on_graph_press=self._on_graph_press,
            on_graph_drag=self._on_graph_drag,
            on_graph_release=self._on_graph_release,
            on_update_graph=self._on_update_graph,
            on_drag_start_row=self._on_drag_start_row,
            on_drag_motion_row=self._on_drag_motion_row,
            on_drag_stop_row=self._on_drag_stop_row,
            on_remove_step=self._on_remove_step,
            on_poll_queue=self._poll_queue,
        )

    # ==================================================================
    #  Event handlers (called from the UI layer)
    # ==================================================================

    # -- Start / Stop execution -----------------------------------------
    def _on_start(self) -> None:
        """Validate inputs, connect to PSU, and launch the profile worker."""
        from tkinter import messagebox

        port_raw = self.view.get_port_raw()
        if not port_raw:
            messagebox.showwarning("No Port", "Please select a COM port first.")
            return

        try:
            global_current = float(self.view.get_global_current())
            if global_current < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "Invalid Input", "Max Current limit must be non-negative.",
            )
            return

        try:
            global_power = float(self.view.get_global_power())
            if global_power < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "Invalid Input", "Max Power limit must be non-negative.",
            )
            return

        # Build step list from UI entries
        steps: list[tuple[float, float, float]] = []
        for i, row in enumerate(self.view.step_rows, start=1):
            try:
                v = float(row["voltage"].get())
                r = float(row["ramp"].get())
                d = float(row["delay"].get())
                if v < 0 or r < 0 or d < 0:
                    raise ValueError
                steps.append((v, r, d))
            except ValueError:
                messagebox.showwarning(
                    "Invalid Input",
                    f"Step {i} must have valid non-negative numbers.",
                )
                return

        if not steps:
            messagebox.showwarning("No Steps", "Please add at least one step.")
            return

        # Connect to PSU
        port = port_raw.split(" - ")[0]
        self._psu = PSUController(port)
        try:
            self._psu.open()
        except Exception as exc:
            messagebox.showerror("Connection Error", str(exc))
            return

        # Reset state and launch
        self._stop_event.clear()
        self.view.current_elapsed = 0.0
        self.view.draw_graph()
        self.view.set_running_state(True)

        self._worker_thread = threading.Thread(
            target=self._execution_worker,
            args=(steps, global_current, global_power),
            daemon=True,
        )
        self._worker_thread.start()

    def _on_stop(self) -> None:
        """Signal the worker thread to stop."""
        self._stop_event.set()
        self._queue.put(("status", "Stopping..."))

    def _on_reset(self) -> None:
        """Stop the current operation and send *RST / *CLS to the PSU.

        The stop event halts the running profile worker, while the
        thread-safe serial wrapper sends the SCPI reset/clear commands.
        """
        # Stop the current operation
        self._stop_event.set()
        self._queue.put(("status", "Resetting..."))

        # Send *RST (reset) and *CLS (clear status registers)
        if self._psu is not None and self._psu.is_open():
            try:
                self._psu.write_cmd("*RST")
                self._psu.write_cmd("*CLS")
            except Exception as exc:
                self._queue.put(("status", f"Reset Error: {exc}"))

    # -- COM port scanning -----------------------------------------------
    def _on_scan_ports(self) -> None:
        """Enumerate available COM ports and update the combobox."""
        ports = [
            f"{p.device} - {p.description}"
            for p in serial.tools.list_ports.comports()
        ]
        self.view.scan_ports(ports)

    # -- Step management -------------------------------------------------
    def _on_add_step(self) -> None:
        """Add a new step row, always keeping a trailing safety Zero Step.

        The new editable step is inserted immediately before the locked
        zero step (or, if the list is empty, the new step is followed by
        the trailing zero step).  ``ensure_zero_step`` guarantees the
        safety step is present after every addition.
        """
        self.view.add_step_row()
        self.view.ensure_zero_step()

    def _on_clear_steps(self) -> None:
        """Remove all step rows, then re-establish the trailing safety step."""
        self.view.clear_all_steps()
        self.view.ensure_zero_step()

    def _on_remove_step(self, row_data: dict) -> None:
        """Remove a specific step row."""
        self.view.remove_step_row(row_data)

    # -- Graph interaction (2D drag) -------------------------------------
    def _on_graph_press(self, event) -> None:
        """Handle mouse press on the graph — find the closest point."""
        ax = self.view.ax
        if event.inaxes != ax or not hasattr(self.view, "X_data"):
            return

        min_dist = float("inf")
        closest_idx = -1
        x_range = ax.get_xlim()[1] - ax.get_xlim()[0] + 1e-9
        y_range = ax.get_ylim()[1] - ax.get_ylim()[0] + 1e-9

        for i, (x, y) in enumerate(zip(self.view.X_data, self.view.Y_data)):
            if i == 0:
                continue  # skip origin point
            dx = (x - event.xdata) / x_range
            dy = (y - event.ydata) / y_range
            dist = dx * dx + dy * dy
            if dist < 0.01 and dist < min_dist:
                min_dist = dist
                closest_idx = i

        if closest_idx != -1:
            self.view.dragging_point_idx = closest_idx
            self.view.dragging_info = self.view.point_to_step_map.get(closest_idx)

    def _on_graph_drag(self, event) -> None:
        """Handle mouse drag — update the associated entry widget."""
        info = self.view.dragging_info
        if info is None or event.inaxes != self.view.ax:
            return

        step_idx, point_type = info
        rows = self.view.step_rows

        # Voltage (Y)
        new_v = max(0.0, event.ydata)
        rows[step_idx]["voltage"].delete(0, "end")
        rows[step_idx]["voltage"].insert(0, f"{new_v:.2f}")

        # Time (X) — prevent moving before previous point
        prev_x = self.view.X_data[self.view.dragging_point_idx - 1]
        new_x = max(prev_x, event.xdata)

        if point_type == "ramp":
            new_ramp = new_x - prev_x
            rows[step_idx]["ramp"].delete(0, "end")
            rows[step_idx]["ramp"].insert(0, f"{new_ramp:.2f}")
        elif point_type == "delay":
            new_delay = new_x - prev_x
            rows[step_idx]["delay"].delete(0, "end")
            rows[step_idx]["delay"].insert(0, f"{new_delay:.2f}")

        self.view.draw_graph()

    def _on_graph_release(self, event) -> None:
        """Handle mouse release — clear drag state."""
        self.view.dragging_info = None
        self.view.dragging_point_idx = None

    # -- Graph redraw ----------------------------------------------------
    def _on_update_graph(self, event=None) -> None:
        """Callback wrapper for ``PSUView.draw_graph()`` with 150ms debouncing."""
        if self._debounce_timer:
            self.view.after_cancel(self._debounce_timer)
        self._debounce_timer = self.view.after(150, self.view.draw_graph)

    # -- Step row drag-reorder -------------------------------------------
    def _on_drag_start_row(self, event, row_data: dict) -> None:
        """Start dragging a step row (visual highlight)."""
        self._drag_start_y = event.y_root
        row_data["frame"].configure(fg_color=("#e0e0e0", "#3a3a3a"))

    def _on_drag_motion_row(self, event, row_data: dict) -> None:
        """Handle row re-ordering by dragging.

        The locked safety zero step is never draggable: it cannot be
        moved, and no other row may be swapped into or past its trailing
        position.
        """
        if row_data.get("is_zero_step"):
            return

        current_y = event.y_root
        delta_y = current_y - self._drag_start_y
        if abs(delta_y) < 15:
            return

        rows = self.view.step_rows
        idx = rows.index(row_data)

        # Move down
        if delta_y > 0 and idx < len(rows) - 1:
            next_row = rows[idx + 1]
            # Never swap with / past the trailing zero step
            if next_row.get("is_zero_step"):
                return
            threshold = next_row["frame"].winfo_rooty() + (
                next_row["frame"].winfo_height() / 2
            )
            if current_y > threshold:
                rows[idx], rows[idx + 1] = rows[idx + 1], rows[idx]
                self.view.repack_all_rows()
                self._drag_start_y = current_y

        # Move up
        elif delta_y < 0 and idx > 0:
            prev_row = rows[idx - 1]
            threshold = prev_row["frame"].winfo_rooty() + (
                prev_row["frame"].winfo_height() / 2
            )
            if current_y < threshold:
                rows[idx], rows[idx - 1] = rows[idx - 1], rows[idx]
                self.view.repack_all_rows()
                self._drag_start_y = current_y

    def _on_drag_stop_row(self, event, row_data: dict) -> None:
        """Stop dragging a step row (restore background, redraw graph)."""
        row_data["frame"].configure(fg_color="transparent")
        self.view.draw_graph()

    # ==================================================================
    #  Queue-based progress polling (thread-safe UI updates)
    # ==================================================================
    def _execution_worker(
        self,
        steps: list[tuple[float, float, float]],
        max_current: float,
        max_power: float,
    ) -> None:
        """Run the profile in a background thread via ``ProfileEngine``."""
        if self._psu is None:
            return

        engine = ProfileEngine(
            psu=self._psu,
            stop_event=self._stop_event,
            progress_callback=lambda msg_type, value: self._queue.put(
                (msg_type, value)
            ),
        )
        engine.run_profile(steps, max_current, max_power)

    def _poll_queue(self) -> None:
        """Periodically process messages from the worker thread (runs on the main thread)."""
        try:
            while True:
                msg_type, value = self._queue.get_nowait()
                if msg_type == "status":
                    self.view.update_status(value)
                elif msg_type == "timer":
                    self.view.update_timer(value)
                elif msg_type == "progress":
                    self.view.current_elapsed = value
                    self._graph_needs_draw = True
                elif msg_type == "done":
                    self.view.set_running_state(False)
                    self.view.update_timer("-- : --")
                    self.view.current_elapsed = None
                    self._graph_needs_draw = True
                    current_status = self.view._status_var.get()
                    if current_status in ("Stopping...", "Stopped by user"):
                        self.view.update_status("Stopped")
                    elif "Completed" not in current_status and "Error" not in current_status:
                        self.view.update_status("Disconnected")
        except queue.Empty:
            pass
        finally:
            if self._graph_needs_draw:
                now = time.time()
                # Draw immediately if done/reset (current_elapsed is None) or if 250ms has elapsed since last draw
                if self.view.current_elapsed is None or (now - self._last_graph_draw_time) >= 0.25:
                    self.view.draw_graph()
                    self._last_graph_draw_time = now
                    self._graph_needs_draw = False
            self.view.after(100, self._poll_queue)

    # ==================================================================
    #  Entry point
    # ==================================================================
    def run(self) -> None:
        """Start the application."""
        self.view.run()


if __name__ == "__main__":
    app = PSUControllerApp()
    app.run()