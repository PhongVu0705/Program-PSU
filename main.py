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
        self._connected_port: Optional[str] = None
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

        # ── Auto-connect to the ITECH PSU on launch (background) ────────
        # Run in a daemon thread so the UI window appears immediately and
        # is not frozen while we probe COM ports (each probe can take ~1 s).
        threading.Thread(target=self._auto_connect_psu, daemon=True).start()

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

        # Connect to PSU (reuse the auto-connected handle if it matches)
        port = port_raw.split(" - ")[0]
        if self._psu is not None and self._psu.is_open() and self._connected_port == port:
            pass  # already connected via auto-connect
        else:
            if self._psu is not None and self._psu.is_open():
                self._psu.close()
            self._psu = PSUController(port)
            try:
                self._psu.open()
            except Exception as exc:
                messagebox.showerror("Connection Error", str(exc))
                return
            self._connected_port = port

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

    # -- COM port scanning -----------------------------------------------
    def _on_scan_ports(self) -> None:
        """Enumerate available COM ports and update the combobox."""
        ports = [
            f"{p.device} - {p.description}"
            for p in serial.tools.list_ports.comports()
        ]
        self.view.scan_ports(ports)

    # -- Auto-connect on launch ------------------------------------------
    def _auto_connect_psu(self) -> None:
        """Background thread: scan only Windows COMx ports for the ITECH
        IT6005C-80-150 PSU.

        Each port is probed with a hard 1-second serial read timeout —
        no ``time.sleep`` blocks the scan.  A small CTkToplevel dialog
        keeps the user informed and closes automatically when the scan
        ends.  All UI mutations happen via ``view.after(0, …)`` so they
        run safely on the main thread.
        """
        import serial  # pyserial (already a dependency via logic.py)
        import customtkinter as ctk

        target_idn = "ITECH Electronics,IT6005C-80-150"
        found_port: Optional[str] = None

        # ── Collect only real COMx ports ──────────────────────────────
        all_ports = serial.tools.list_ports.comports()
        com_ports = [p for p in all_ports if p.device.upper().startswith("COM")]

        print(f"\n[AutoConnect] Starting PSU scan — target IDN: '{target_idn}'")
        print(f"[AutoConnect] Total ports detected : {len(all_ports)}")
        print(f"[AutoConnect] COM ports to probe   : {len(com_ports)}")
        if not com_ports:
            print("[AutoConnect] ⚠  No COM ports found. Is the PSU plugged in?")

        # ── Open scanning dialog on main thread ───────────────────────
        scan_dialog: list = []   # mutable container so the nested func can access it

        def _open_scan_dialog() -> None:
            dlg = ctk.CTkToplevel(self.view)
            dlg.title("Scanning…")
            dlg.geometry("360x160")
            dlg.resizable(False, False)
            dlg.attributes("-topmost", True)

            # Center over the main window
            self.view.update_idletasks()
            mx = self.view.winfo_x() + (self.view.winfo_width()  - 360) // 2
            my = self.view.winfo_y() + (self.view.winfo_height() - 160) // 2
            dlg.geometry(f"360x160+{mx}+{my}")

            # Prevent accidental close during scan
            dlg.protocol("WM_DELETE_WINDOW", lambda: None)

            ctk.CTkLabel(
                dlg,
                text="🔍  Scanning COM Ports for ITECH PSU…",
                font=ctk.CTkFont("Segoe UI", 12, "bold"),
            ).pack(pady=(18, 6))

            port_var = ctk.StringVar(value="Initialising…")
            ctk.CTkLabel(
                dlg,
                textvariable=port_var,
                font=ctk.CTkFont("Segoe UI", 11),
                text_color=("gray40", "gray70"),
            ).pack(pady=2)

            total = max(len(com_ports), 1)
            prog = ctk.CTkProgressBar(dlg, width=300)
            prog.set(0)
            prog.pack(pady=10)

            result_var = ctk.StringVar(value="")
            ctk.CTkLabel(
                dlg,
                textvariable=result_var,
                font=ctk.CTkFont("Segoe UI", 10, slant="italic"),
                text_color=("gray50", "gray60"),
            ).pack()

            scan_dialog.append({
                "dlg": dlg,
                "port_var": port_var,
                "prog": prog,
                "result_var": result_var,
                "total": total,
            })

        self.view.after(0, _open_scan_dialog)
        # Give Tkinter a moment to actually render the dialog before we start
        time.sleep(0.05)

        # ── Probe each port ───────────────────────────────────────────
        for idx, p in enumerate(com_ports):
            port = p.device
            desc = p.description
            print(f"[AutoConnect]   → Probing {port} ({desc}) …", end=" ", flush=True)

            # Update dialog label + progress bar
            def _update_dialog(i=idx, prt=port, dsc=desc) -> None:
                if not scan_dialog:
                    return
                d = scan_dialog[0]
                d["port_var"].set(f"Probing {prt}  ({dsc})")
                d["prog"].set((i + 1) / d["total"])

            self.view.after(0, _update_dialog)

            # Run the entire probe (open + write + read) in a daemon thread.
            # join(timeout=1.5) enforces a hard wall-clock deadline so that
            # ports like Bluetooth that hang on serial.Serial() itself are
            # abandoned after 1.5 s — not just reads.
            probe_result: list = []   # ["ok", raw_str] | ["err", exc_str] | ["timeout"]

            def _probe(prt=port, result=probe_result) -> None:
                try:
                    with serial.Serial(prt, baudrate=9600, timeout=1) as ser:
                        ser.reset_input_buffer()
                        ser.write(b"*IDN?\n")
                        data = ser.read(256).decode("utf-8", errors="ignore").strip()
                    result.append(("ok", data))
                except Exception as exc:
                    result.append(("err", str(exc)))

            probe_thread = threading.Thread(target=_probe, daemon=True)
            probe_thread.start()
            probe_thread.join(timeout=1.5)   # ← hard wall-clock limit

            if probe_thread.is_alive():
                # Thread is still blocked (e.g. Bluetooth open hanging) — skip
                print("✗ timeout (port did not respond within 1.5 s)")
                continue

            if not probe_result:
                print("✗ no result (unknown error)")
                continue

            status, payload = probe_result[0]

            if status == "err":
                print(f"✗ error: {payload}")
                continue

            raw = payload  # status == "ok"
            if raw:
                print(f"response: '{raw}'", end=" ")
            else:
                print("no response", end=" ")

            if target_idn in raw:
                print("✔ MATCH!")
                found_port = port
                break
            else:
                print("✗ no match")

        # ── Close the scanning dialog (always, on main thread) ────────
        def _close_dialog(fp=found_port) -> None:
            if not scan_dialog:
                return
            d = scan_dialog[0]
            if fp:
                d["result_var"].set(f"✔  PSU found on {fp}")
                d["prog"].set(1.0)
                d["prog"].configure(progress_color="#2e7d32")
            else:
                d["result_var"].set("✗  No ITECH PSU detected")
                d["prog"].set(1.0)
                d["prog"].configure(progress_color="#c62828")
            # Show result briefly, then close
            d["dlg"].after(900, d["dlg"].destroy)

        self.view.after(0, _close_dialog)

        # ── Result ────────────────────────────────────────────────────
        if found_port is None:
            print("[AutoConnect] ✗ PSU not found on any COM port.\n")
            # Wait a moment so the user can read the dialog result message
            time.sleep(1.0)
            self.view.after(
                0,
                lambda: __import__("tkinter.messagebox", fromlist=["showerror"]).showerror(
                    "No PSU Found",
                    "Please connect to the ITECH PSU IT6005C.",
                ),
            )
            self.view.after(0, lambda: self.view.update_status("Disconnected – ITECH PSU not found"))
            return

        print(f"[AutoConnect] ✔ PSU found on {found_port}. Opening via PSUController …")

        # Open the matching port through the logic layer and remember it.
        self._psu = PSUController(found_port)
        try:
            self._psu.open()
            print(f"[AutoConnect] ✔ PSUController opened on {found_port}.\n")
        except Exception as exc:
            exc_str = str(exc)
            print(f"[AutoConnect] ✗ Failed to open {found_port}: {exc_str}\n")
            self.view.after(
                0,
                lambda: __import__("tkinter.messagebox", fromlist=["showerror"]).showerror(
                    "Connection Error", exc_str
                ),
            )
            self._psu = None
            return

        self._connected_port = found_port

        # UI updates must happen on the main thread.
        def _apply_ui() -> None:
            for item in self.view.port_combo.cget("values"):
                if item.startswith(found_port):
                    self.view.port_combo.set(item)
                    break
            self.view.update_status(f"Connected – {found_port}")

        self.view.after(0, _apply_ui)

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