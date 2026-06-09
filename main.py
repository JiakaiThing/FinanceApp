"""Entry point for the Finance app.

Launches the Tkinter UI by delegating to :func:`finance_app.ui.main_window.run_app`.
Run from the project root with ``python main.py``.
"""
from finance_app.ui.main_window import run_app


if __name__ == "__main__":
    run_app()
