@echo off
echo %DATE% %TIME% task fired >> "C:\Users\matth\AppData\Local\planetaria-logs\task-probe.log"
cd /d C:\Users\matth\Desktop\planetaria\backend
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 >> "C:\Users\matth\AppData\Local\planetaria-logs\engine.out.log" 2>> "C:\Users\matth\AppData\Local\planetaria-logs\engine.err.log"
echo %DATE% %TIME% python exited %ERRORLEVEL% >> "C:\Users\matth\AppData\Local\planetaria-logs\task-probe.log"
