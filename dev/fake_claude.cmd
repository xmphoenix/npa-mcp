@echo off
rem Fake Claude CLI for the M4 smoke: sched_run detects a .cmd and wraps it
rem in cmd.exe, exactly like the real claude.cmd on Windows.
if not defined NPA_PYTHON set "NPA_PYTHON=python"
"%NPA_PYTHON%" "%~dp0fake_claude.py" %*
