@echo off
rem 手动测试自主工作窗口入口。
rem 任务计划实际调用 pythonw -m npa_mcp.sched_run,不经过本脚本。
rem 如默认 python 不是安装 npa-mcp 的解释器,先设置 NPA_PYTHON。
setlocal
if "%NPA_PYTHON%"=="" set "NPA_PYTHON=python"
"%NPA_PYTHON%" -m npa_mcp.sched_run %*
exit /b %ERRORLEVEL%
