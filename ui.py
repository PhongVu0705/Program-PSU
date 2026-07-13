"""
ui.py — Presentation / View Layer
==================================
Responsibilities:
  - Build and layout all UI widgets (CTk frames, buttons, entries, graph).
  - Provide methods to update the display (status, timer, graph).
  - Expose widget references and event hooks so an external controller
    (main.py) can bind business-logic callbacks.

Architectural Rules:
  - ZERO business logic: no data processing, calculations, or file/DB I/O.
  - ZERO import of ``logic.py``.
  - Signals / callbacks are set externally by the coordinator (main.py).
"""

import customtkinter as ctk
from tkinter import messagebox, StringVar
import tkinter as tk

# ── Matplotlib (embedded graph) ────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    import matplotlib.pyplot as plt
    plt.style.use("seaborn-v0_8-whitegrid")
except ImportError:
    messagebox.showerror(
        "Missing Dependency",
        "Cần cài đặt matplotlib.\nHãy chạy lệnh: pip install matplotlib",
    )
    raise SystemExit(1) from None


# ======================================================================
#  Constants — shared styling
# ======================================================================
LINE_COLOR  = "#0078d4"
POINT_COLOR = "#d32f2f"
DONE_COLOR  = "#c62828"


# ======================================================================
#  Helper: labeled section frame
# ======================================================================
def _make_section(parent: ctk.CTkBaseClass, title: str, **kwargs):
    """Return ``(outer_frame, inner_frame)`` with a title label on top."""
    outer = ctk.CTkFrame(parent, corner_radius=8, **kwargs)
    lbl = ctk.CTkLabel(
        outer,
        text=title,
        font=ctk.CTkFont("Segoe UI", 12, "bold"),
        anchor="w",
        text_color=("#0078d4", "#4da3ff"),
    )
    lbl.pack(fill="x", padx=10, pady=(6, 0))
    sep = ctk.CTkFrame(outer, height=1, fg_color=("gray80", "gray30"))
    sep.pack(fill="x", padx=10, pady=(2, 0))
    inner = ctk.CTkFrame(outer, fg_color="transparent")
    inner.pack(fill="both", expand=True, padx=4, pady=4)
    return outer, inner


# ======================================================================
#  PSUView — main application window
# ======================================================================
class PSUView(ctk.CTk):
    """Top-level application window containing all UI widgets and the graph.

    .. tip::
        Use :meth:`set_callbacks` to attach controller logic before the
        window is displayed.

    Public attributes (widget references for the controller):
        - port_combo, port_var
        - global_current_var
        - refresh_btn, add_step_btn, clear_btn, start_btn, stop_btn
        - status_var, timer_var
        - step_rows (list of dicts with keys: frame, label, voltage, ramp,
          delay, remove_btn)
        - fig, ax, canvas (matplotlib figure / canvas for the interactive graph)
        - X_data, Y_data, point_to_step_map (graph data arrays)
        - dragging_info, dragging_point_idx (drag state)
        - current_elapsed (progress marker time)
    """

    ACCENT_COLORS = (LINE_COLOR, POINT_COLOR, DONE_COLOR)

    def __init__(self) -> None:
        super().__init__()
        self.title("ITECH PSU Voltage Controller")
        self.geometry("1200x700")
        self.minsize(1050, 620)

        # ── State ──────────────────────────────────────────────────────
        self._step_rows: list[dict] = []
        self.X_data: list[float] = [0.0]
        self.Y_data: list[float] = [0.0]
        self.point_to_step_map: dict[int, tuple[int, str]] = {}

        self.dragging_info: tuple[int, str] | None = None
        self.dragging_point_idx: int | None = None
        self.current_elapsed: float | None = None

        # ── Callbacks (set by controller via ``set_callbacks``) ────────
        self._on_start: callable = lambda: None
        self._on_stop: callable = lambda: None
        self._on_scan_ports: callable = lambda: None
        self._on_add_step: callable = lambda: None
        self._on_clear_steps: callable = lambda: None
        self._on_graph_press: callable = lambda e: None
        self._on_graph_drag: callable = lambda e: None
        self._on_graph_release: callable = lambda e: None
        self._on_update_graph: callable = lambda e=None: None
        self._on_drag_start_row: callable = lambda e, rd: None
        self._on_drag_motion_row: callable = lambda e, rd: None
        self._on_drag_stop_row: callable = lambda e, rd: None
        self._on_remove_step: callable = lambda rd: None
        self._on_poll_queue: callable = lambda: None

        # ── Build UI ───────────────────────────────────────────────────
        self._build_ui()

    # ==================================================================
    #  Public API — used by the controller
    # ==================================================================
    def set_callbacks(
        self,
        on_start: callable,
        on_stop: callable,
        on_scan_ports: callable,
        on_add_step: callable,
        on_clear_steps: callable,
        on_graph_press: callable,
        on_graph_drag: callable,
        on_graph_release: callable,
        on_update_graph: callable,
        on_drag_start_row: callable,
        on_drag_motion_row: callable,
        on_drag_stop_row: callable,
        on_remove_step: callable,
        on_poll_queue: callable,
    ) -> None:
        """Attach all controller callbacks after construction."""
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_scan_ports = on_scan_ports
        self._on_add_step = on_add_step
        self._on_clear_steps = on_clear_steps
        self._on_graph_press = on_graph_press
        self._on_graph_drag = on_graph_drag
        self._on_graph_release = on_graph_release
        self._on_update_graph = on_update_graph
        self._on_drag_start_row = on_drag_start_row
        self._on_drag_motion_row = on_drag_motion_row
        self._on_drag_stop_row = on_drag_stop_row
        self._on_remove_step = on_remove_step
        self._on_poll_queue = on_poll_queue

    # ------------------------------------------------------------------
    @property
    def step_rows(self) -> list[dict]:
        """List of step-row dicts (voltage, ramp, delay entries)."""
        return self._step_rows

    # ------------------------------------------------------------------
    def update_status(self, text: str) -> None:
        """Update the status bar text."""
        self._status_var.set(text)

    # ------------------------------------------------------------------
    def update_timer(self, text: str) -> None:
        """Update the countdown timer display."""
        self._timer_var.set(text)

    # ------------------------------------------------------------------
    def set_running_state(self, running: bool) -> None:
        """Enable/disable controls during execution."""
        state = "disabled" if running else "normal"
        self.start_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.refresh_btn.configure(state=state)
        self.add_step_btn.configure(state=state)
        self.clear_btn.configure(state=state)
        self.global_current_entry.configure(state=state)
        self.global_power_entry.configure(state=state)

        for row in self._step_rows:
            row["voltage"].configure(state=state)
            row["ramp"].configure(state=state)
            row["delay"].configure(state=state)
            row["remove_btn"].configure(state=state)

    # ------------------------------------------------------------------
    def draw_graph(self) -> None:
        """Redraw the matplotlib figure from current step data.

        This is a *pure presentation* method — it reads entry values,
        computes X/Y arrays, and renders them.  No data is persisted.
        """
        if not hasattr(self, "ax"):
            return

        self.ax.clear()
        self.ax.set_facecolor("#ffffff")
        self.ax.set_xlabel("Time (s)", fontsize=10, fontweight="bold", color="#333333")
        self.ax.set_ylabel("Voltage (V)", fontsize=10, fontweight="bold", color="#333333")
        self.ax.tick_params(colors="#444444")
        for spine in self.ax.spines.values():
            spine.set_edgecolor("#cccccc")
        self.ax.grid(True, linestyle="--", alpha=0.5, color="#dddddd")

        # Build data arrays from entry widgets
        t, v = 0.0, 0.0
        self.X_data = [t]
        self.Y_data = [v]
        self.point_to_step_map.clear()

        for i, row in enumerate(self._step_rows):
            try:
                target_v = float(row["voltage"].get().strip())
                ramp = float(row["ramp"].get().strip())
                delay = float(row["delay"].get().strip())
            except ValueError:
                target_v, ramp, delay = 0.0, 0.0, 0.0

            # Ramp end-point
            t += ramp
            self.X_data.append(t)
            self.Y_data.append(target_v)
            self.point_to_step_map[len(self.X_data) - 1] = (i, "ramp")

            # Delay end-point
            t += delay
            self.X_data.append(t)
            self.Y_data.append(target_v)
            self.point_to_step_map[len(self.X_data) - 1] = (i, "delay")
            v = target_v

        # Plot full profile in blue
        self.ax.plot(
            self.X_data, self.Y_data,
            color=LINE_COLOR, linewidth=2.5, alpha=0.9,
        )
        self.ax.plot(
            self.X_data, self.Y_data,
            "o", color=POINT_COLOR, markersize=7,
            markeredgecolor="white", markeredgewidth=1.5,
        )

        # Highlight completed portion (red) if a progress marker exists
        elapsed = self.current_elapsed
        if elapsed is not None and self.X_data:
            past_x, past_y, future_x, future_y = [], [], [], []
            for x, y in zip(self.X_data, self.Y_data):
                if x <= elapsed:
                    past_x.append(x)
                    past_y.append(y)
                else:
                    future_x.append(x)
                    future_y.append(y)

            # Interpolate at the exact elapsed point
            cur_y = past_y[-1] if past_y else (future_y[0] if future_y else 0.0)
            if past_x and future_x:
                x0, y0 = past_x[-1], past_y[-1]
                x1, y1 = future_x[0], future_y[0]
                frac = (elapsed - x0) / (x1 - x0) if x1 > x0 else 0.0
                cur_y = y0 + frac * (y1 - y0)
                past_x = past_x + [elapsed]
                past_y = past_y + [cur_y]
                future_x = [elapsed] + future_x
                future_y = [cur_y] + future_y

            if len(past_x) >= 2:
                self.ax.plot(
                    past_x, past_y,
                    color=DONE_COLOR, linewidth=3.2, alpha=0.95, zorder=5,
                )
            self.ax.axvline(
                elapsed, color=DONE_COLOR, linestyle=":",
                alpha=0.5, linewidth=1.2, zorder=1,
            )
            self.ax.plot(
                elapsed, cur_y,
                "o", color=DONE_COLOR, markersize=11,
                markeredgecolor="white", markeredgewidth=2, zorder=6,
            )

        # Dynamic scaling
        self.ax.set_ylim(bottom=0)
        max_x = max(self.X_data) if self.X_data else 1
        max_y = max(self.Y_data) if self.Y_data else 10
        self.ax.set_xlim(0, max_x * 1.05 if max_x > 0 else 1)
        self.ax.set_ylim(0, max_y * 1.2 if max_y > 0 else 10)

        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    def get_port_raw(self) -> str:
        """Return the currently selected COM-port string."""
        return self._port_var.get()

    # ------------------------------------------------------------------
    def get_global_current(self) -> str:
        """Return the global-current entry text."""
        return self._global_current_var.get().strip()

    # ------------------------------------------------------------------
    def get_global_power(self) -> str:
        """Return the global-power entry text."""
        return self._global_power_var.get().strip()

    # ==================================================================
    #  UI Construction (private helpers)
    # ==================================================================
    def _build_ui(self) -> None:
        self.left_frame = ctk.CTkFrame(
            self, width=470, corner_radius=0,
            fg_color=("gray92", "gray14"),
        )
        self.left_frame.pack(side="left", fill="y", padx=(8, 4), pady=8)
        self.left_frame.pack_propagate(False)

        self.right_frame = ctk.CTkFrame(
            self, corner_radius=0, fg_color="transparent",
        )
        self.right_frame.pack(
            side="right", fill="both", expand=True, padx=(4, 8), pady=8,
        )

        self._build_graph_frame(self.right_frame)
        self._build_connection_frame(self.left_frame)
        self._build_steps_frame(self.left_frame)
        self._build_control_frame(self.left_frame)
        self._build_status_frame(self.left_frame)
        self._build_footer(self.left_frame)

    # ------------------------------------------------------------------
    def _build_connection_frame(self, parent) -> None:
        outer, inner = _make_section(parent, " System Setup")
        outer.pack(fill="x", padx=6, pady=(8, 4))

        ctk.CTkLabel(
            inner, text="COM Port:", font=ctk.CTkFont("Segoe UI", 11),
        ).grid(row=0, column=0, padx=(6, 2), pady=8, sticky="w")

        self._port_var = StringVar()
        self.port_combo = ctk.CTkComboBox(
            inner, variable=self._port_var,
            state="readonly", width=160,
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.port_combo.grid(row=0, column=1, padx=4, pady=8, sticky="w")

        self.refresh_btn = ctk.CTkButton(
            inner, text="⟳", width=34, height=30,
            font=ctk.CTkFont("Segoe UI", 14),
            command=lambda: self._on_scan_ports(),
        )
        self.refresh_btn.grid(row=0, column=2, padx=4, pady=8, sticky="w")

        ctk.CTkLabel(
            inner, text="Max Curr (A):",
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            text_color=("#d32f2f", "#ff5252"),
        ).grid(row=1, column=0, padx=(6, 2), pady=8, sticky="w")

        self._global_current_var = StringVar(value="100")
        self.global_current_entry = ctk.CTkEntry(
            inner, textvariable=self._global_current_var, width=70,
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.global_current_entry.grid(row=1, column=1, padx=4, pady=8, sticky="w")

        ctk.CTkLabel(
            inner, text="Max Power (W):",
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            text_color=("#d32f2f", "#ff5252"),
        ).grid(row=1, column=2, padx=(10, 2), pady=8, sticky="w")

        self._global_power_var = StringVar(value="1000")
        self.global_power_entry = ctk.CTkEntry(
            inner, textvariable=self._global_power_var, width=70,
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.global_power_entry.grid(row=1, column=3, padx=4, pady=8, sticky="w")

    # ------------------------------------------------------------------
    def _build_steps_frame(self, parent) -> None:
        outer, inner = _make_section(parent, " Step Configuration")
        outer.pack(fill="both", expand=True, padx=6, pady=4)

        top_row = ctk.CTkFrame(inner, fg_color="transparent")
        top_row.pack(fill="x", pady=(4, 2))

        self.add_step_btn = ctk.CTkButton(
            top_row, text="➕  Add Step", width=120,
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            command=lambda: self._on_add_step(),
        )
        self.add_step_btn.pack(side="left", padx=(4, 6))

        self.clear_btn = ctk.CTkButton(
            top_row, text="🗑️  Clear All", width=120,
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            fg_color=("#c62828", "#d32f2f"),
            hover_color=("#b71c1c", "#b71c1c"),
            command=lambda: self._on_clear_steps(),
        )
        self.clear_btn.pack(side="left")

        # Column definitions (name, width, padx)
        self.col_cfg = [
            ("Step", 40, 5),
            ("Volt (V)", 75, 5),
            ("Ramp (s)", 75, 5),
            ("Delay (s)", 75, 5),
        ]

        # Header row
        header = ctk.CTkFrame(inner, fg_color="transparent")
        header.pack(fill="x", pady=(4, 0))

        drag_spacer = ctk.CTkFrame(header, width=22, height=1, fg_color="transparent")
        drag_spacer.pack(side="left")

        header_labels = ctk.CTkFrame(header, fg_color="transparent")
        header_labels.pack(side="left", fill="x", expand=True)

        for txt, w, p in self.col_cfg:
            ctk.CTkLabel(
                header_labels, text=txt, width=w,
                font=ctk.CTkFont("Segoe UI", 10, "bold"),
                anchor="center",
            ).pack(side="left", padx=p)

        spacer = ctk.CTkFrame(header, width=54, height=1, fg_color="transparent")
        spacer.pack(side="right")

        # Scrollable canvas area
        canvas_frame = ctk.CTkFrame(inner, fg_color="transparent")
        canvas_frame.pack(fill="both", expand=True, pady=(2, 0))

        bg = self._get_canvas_bg()
        self._canvas = tk.Canvas(canvas_frame, highlightthickness=0, bd=0, bg=bg)
        scrollbar = ctk.CTkScrollbar(
            canvas_frame, orientation="vertical", command=self._canvas.yview,
        )

        self._scrollable_frame = tk.Frame(
            self._canvas, bg=bg, bd=0, highlightthickness=0,
        )
        self._scrollable_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all"),
            ),
        )
        self._canvas.create_window((0, 0), window=self._scrollable_frame, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._canvas.bind_all(
            "<MouseWheel>",
            lambda e: self._canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units",
            ),
        )

    # ------------------------------------------------------------------
    def _get_canvas_bg(self) -> str:
        mode = ctk.get_appearance_mode()
        return "#2b2b2b" if mode == "Dark" else "#ebebeb"

    # ------------------------------------------------------------------
    def _build_control_frame(self, parent) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", padx=6, pady=6)

        self.start_btn = ctk.CTkButton(
            frame, text="▶  Start", width=155, height=38,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
            fg_color=("#2e7d32", "#388e3c"),
            hover_color=("#1b5e20", "#2e7d32"),
            command=lambda: self._on_start(),
        )
        self.start_btn.pack(side="left", padx=(0, 10))

        self.stop_btn = ctk.CTkButton(
            frame, text="■  Stop", width=155, height=38,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
            fg_color=("#c62828", "#d32f2f"),
            hover_color=("#b71c1c", "#b71c1c"),
            state="disabled",
            command=lambda: self._on_stop(),
        )
        self.stop_btn.pack(side="left")

    # ------------------------------------------------------------------
    def _build_status_frame(self, parent) -> None:
        outer, inner = _make_section(parent, " Status")
        outer.pack(fill="x", padx=6, pady=(2, 4))

        self._status_var = StringVar(value="Disconnected")
        ctk.CTkLabel(
            inner, textvariable=self._status_var,
            font=ctk.CTkFont("Segoe UI", 11, slant="italic"),
            anchor="w",
        ).pack(fill="x", padx=6)

        self._timer_var = StringVar(value="-- : --")
        ctk.CTkLabel(
            inner, textvariable=self._timer_var,
            font=ctk.CTkFont("Segoe UI", 26, "bold"),
            text_color=("#1565c0", "#4da3ff"),
            anchor="w",
        ).pack(fill="x", padx=6, pady=(2, 6))

    # ------------------------------------------------------------------
    @staticmethod
    def _build_footer(parent) -> None:
        ctk.CTkLabel(
            parent,
            text="Made by Grey Le Phong Vu",
            font=ctk.CTkFont("Segoe UI", 9, slant="italic"),
            text_color="gray55",
        ).pack(side="bottom", pady=6)

    # ------------------------------------------------------------------
    def _build_graph_frame(self, parent) -> None:
        outer, inner = _make_section(parent, " Interactive Profile Graph")
        outer.pack(fill="both", expand=True, padx=6, pady=6)

        self.fig = Figure(figsize=(6, 4), dpi=100)
        self.fig.patch.set_facecolor("#f5f5f5")
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#ffffff")

        self.canvas = FigureCanvasTkAgg(self.fig, master=inner)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=5, pady=5)

        # Bind graph mouse events to controller-provided callbacks
        self.canvas.mpl_connect("button_press_event", lambda e: self._on_graph_press(e))
        self.canvas.mpl_connect("motion_notify_event", lambda e: self._on_graph_drag(e))
        self.canvas.mpl_connect("button_release_event", lambda e: self._on_graph_release(e))

    # ==================================================================
    #  Step-row management (pure presentation, no logic)
    # ==================================================================
    def add_step_row(self) -> dict:
        """Create and return a new step-row widget dict.

        The returned dict contains:
            frame, label, voltage (Entry), ramp (Entry), delay (Entry), remove_btn
        """
        row_frame = ctk.CTkFrame(self._scrollable_frame, fg_color="transparent")
        row_frame.pack(fill="x", pady=2)
        step_idx = len(self._step_rows) + 1

        # Drag handle
        drag_handle = ctk.CTkLabel(
            row_frame, text="☰", width=20, cursor="fleur",
            font=ctk.CTkFont("Segoe UI", 12), text_color="#777777",
        )
        drag_handle.pack(side="left", padx=(2, 0))

        # Step label
        lbl = ctk.CTkLabel(
            row_frame, text=f"{step_idx}", width=40,
            font=ctk.CTkFont("Segoe UI", 10),
        )
        lbl.pack(side="left", padx=self.col_cfg[0][2])

        entry_kw = dict(
            height=26, font=ctk.CTkFont("Segoe UI", 10),
            fg_color="#ffffff", text_color="#222222",
            border_width=2, border_color="#cccccc",
        )

        v_entry = ctk.CTkEntry(row_frame, width=self.col_cfg[1][1], **entry_kw)
        v_entry.insert(0, "0.0")
        v_entry.pack(side="left", padx=self.col_cfg[1][2])
        v_entry.bind("<KeyRelease>", lambda e: self._on_update_graph(e))

        r_entry = ctk.CTkEntry(row_frame, width=self.col_cfg[2][1], **entry_kw)
        r_entry.insert(0, "0.0")
        r_entry.pack(side="left", padx=self.col_cfg[2][2])
        r_entry.bind("<KeyRelease>", lambda e: self._on_update_graph(e))

        d_entry = ctk.CTkEntry(row_frame, width=self.col_cfg[3][1], **entry_kw)
        d_entry.insert(0, "0.0")
        d_entry.pack(side="left", padx=self.col_cfg[3][2])
        d_entry.bind("<KeyRelease>", lambda e: self._on_update_graph(e))

        row_data = {
            "frame": row_frame,
            "label": lbl,
            "voltage": v_entry,
            "ramp": r_entry,
            "delay": d_entry,
        }

        rem_btn = ctk.CTkButton(
            row_frame, text="❌", width=35, height=26,
            fg_color="transparent", hover_color="#ffcccc",
            text_color="#e53935",
            command=lambda rd=row_data: self._on_remove_step(rd),
        )
        rem_btn.pack(side="left", padx=5)
        row_data["remove_btn"] = rem_btn

        # Bind row drag events
        drag_handle.bind(
            "<Button-1>",
            lambda e, rd=row_data: self._on_drag_start_row(e, rd),
        )
        drag_handle.bind(
            "<B1-Motion>",
            lambda e, rd=row_data: self._on_drag_motion_row(e, rd),
        )
        drag_handle.bind(
            "<ButtonRelease-1>",
            lambda e, rd=row_data: self._on_drag_stop_row(e, rd),
        )

        self._step_rows.append(row_data)
        self.update_idletasks()
        self._canvas.yview_moveto(1.0)
        self._on_update_graph()
        return row_data

    # ------------------------------------------------------------------
    def remove_step_row(self, row_data: dict) -> None:
        """Remove a step row widget and re-number remaining labels."""
        row_data["frame"].destroy()
        self._step_rows.remove(row_data)
        for i, row in enumerate(self._step_rows):
            row["label"].configure(text=f"{i + 1}")
        self._on_update_graph()

    # ------------------------------------------------------------------
    def clear_all_steps(self) -> None:
        """Destroy all step rows and clear the list."""
        for row in self._step_rows:
            row["frame"].destroy()
        self._step_rows.clear()
        self._on_update_graph()

    # ------------------------------------------------------------------
    def repack_all_rows(self) -> None:
        """Re-pack all rows after a drag-reorder."""
        for i, row in enumerate(self._step_rows):
            row["frame"].pack_forget()
            row["frame"].pack(fill="x", pady=2)
            row["label"].configure(text=f"{i + 1}")

    # ------------------------------------------------------------------
    def scan_ports(self, ports: list[str]) -> None:
        """Update the COM-port combobox with a list of available ports."""
        self.port_combo.configure(values=ports)
        if ports:
            self.port_combo.set(ports[0])
        else:
            self.port_combo.set("")

    # ------------------------------------------------------------------
    def run(self) -> None:
        """Start the main event loop."""
        self.mainloop()