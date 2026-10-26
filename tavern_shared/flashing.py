def start_flashing_button(widget, should_flash, normal_bg, alert_bg, interval_ms=650):
    """Alternates a button's background between normal_bg and alert_bg,
    on and off, for as long as should_flash() returns True. Checks
    should_flash() on every tick, so it starts/stops on its own as
    conditions change -- nothing else needs to explicitly turn it off.
    """
    state = {"on": False}

    def _tick():
        if not widget.winfo_exists():
            return
        if should_flash():
            state["on"] = not state["on"]
            widget.config(bg=(alert_bg if state["on"] else normal_bg))
        else:
            state["on"] = False
            widget.config(bg=normal_bg)
        widget.after(interval_ms, _tick)

    _tick()


def start_flashing_tab(notebook, tab_index, should_flash, plain_label, alert_label, interval_ms=650):
    """Same idea as start_flashing_button, but for a ttk.Notebook tab --
    alternates its text between plain_label and alert_label, since
    per-tab background color isn't something ttk.Notebook supports
    without far more style hacking than this is worth.
    """
    state = {"on": False}

    def _tick():
        if not notebook.winfo_exists():
            return
        if should_flash():
            state["on"] = not state["on"]
            notebook.tab(tab_index, text=(alert_label if state["on"] else plain_label))
        else:
            state["on"] = False
            notebook.tab(tab_index, text=plain_label)
        notebook.after(interval_ms, _tick)

    _tick()
