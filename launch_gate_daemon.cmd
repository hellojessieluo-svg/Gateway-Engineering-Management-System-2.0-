@echo off
rem Headless start for a logon scheduled task. Same server, no browser.
call "%~dp0launch_gate_ui.cmd" --no-open
