# Agent Notes

This project is a Windows-focused pywebview scaffold.

- Do not reintroduce NiceGUI for the UI layer.
- Keep frontend assets under `app/web`.
- Expose Python backend actions through `app/api.py` and `window.pywebview.api`.
- Keep long-running/local process logic in `app/services`.
- Use `setup.ps1` and `run.ps1` as the canonical Windows entrypoints.
