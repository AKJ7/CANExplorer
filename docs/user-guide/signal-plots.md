# Signal plots

The **Signals** view plots database signals over time.

![Signal plots](../assets/screenshots/signals.png)

1. [Load a signal database](signal-databases.md).
2. Tick the signals to plot in the list on the left; the search field filters the list.
3. Choose the **Time window**: 5, 10, 30, 60 or 300 seconds.

Each signal gets its own plot with its unit, the value range of the window and the current value. Signals with
value tables are plotted with their numeric values. Plots follow the newest data; **Pause view** freezes them.

Only frames carrying a ticked signal are decoded for plotting, so plotting does not slow down the rest of the
traffic. Each signal keeps its most recent 50 000 to 100 000 values.
