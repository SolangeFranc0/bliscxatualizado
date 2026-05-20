@echo off
REM Configura o Windows Task Scheduler para rodar updater.py todo dia as 09:00
REM Execute este arquivo como Administrador uma unica vez.

set TASK_NAME=ZendeskDashboardUpdate2026
set PYTHON=%~dp0venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python

set SCRIPT=%~dp0updater.py
set LOG=%~dp0output\scheduler_setup.log

echo Registrando tarefa agendada: %TASK_NAME%
echo Horario: todos os dias as 09:00
echo Script: %SCRIPT%

schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

schtasks /Create ^
  /TN "%TASK_NAME%" ^
  /TR "\"%PYTHON%\" \"%SCRIPT%\"" ^
  /SC DAILY ^
  /ST 09:00 ^
  /RU "SYSTEM" ^
  /F

if %errorlevel%==0 (
    echo.
    echo [OK] Tarefa criada com sucesso.
    echo      Para verificar: schtasks /Query /TN "%TASK_NAME%"
    echo      Para testar agora: schtasks /Run /TN "%TASK_NAME%"
    echo      Para remover: schtasks /Delete /TN "%TASK_NAME%" /F
) else (
    echo.
    echo [ERRO] Falha ao criar tarefa. Execute como Administrador.
)

pause
