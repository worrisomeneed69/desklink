@echo off
rem DeskLink - start pod Windowsem 11 (uruchamia te same komendy co 'python -m rd').
rem
rem   run_windows.bat                     GUI
rem   run_windows.bat id                  moje ID + odcisk klucza
rem   run_windows.bat relay --port 7001   wlasny serwer rendezvous
rem   run_windows.bat connect 123 456 789 dzwonienie bez klikania w okno
rem
rem Ten plik nie instaluje niczego "w cicho" poza brakujacymi zaleznosciami z
rem requirements.txt i nie dotyka rejestru.
rem
rem Python jest szukany po koleji: py -3, py, python, potem typowe katalogi
rem instalacji. Mozna wskazac recznie:  set DESKLINK_PY=C:\Python311\python.exe
rem
rem Dziala rowniez pod Wine (wtedy wskaz Pythona z prefiksu):
rem   set DESKLINK_PY=C:\Python311\python.exe && wine cmd /c run_windows.bat id

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=%DESKLINK_PY%"
if defined PY (
  %PY% -c "import sys" >nul 2>nul || set "PY="
)
if not defined PY call :try py -3
if not defined PY call :try py
if not defined PY call :try python
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :try "C:\Python313\python.exe"
if not defined PY call :try "C:\Python312\python.exe"
if not defined PY call :try "C:\Python311\python.exe"

if not defined PY (
  echo [desklink] nie znalazlem dzialajacego Pythona 3.9+.
  echo [desklink] Zainstaluj Go z python.org ^("Add python.exe to PATH"^), albo podaj
  echo [desklink] sciezke:  set DESKLINK_PY=C:\sciezka\do\python.exe
  exit /b 1
)

%PY% -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 (
  echo [desklink] brakuje tkinter albo Pillow - doczytuje z requirements.txt ...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [desklink] pip nie dal rady. Reka:  %PY% -m pip install -r requirements.txt
    exit /b 1
  )
)

set "PYTHONPATH=%~dp0;%PYTHONPATH%"
%PY% -m rd %*
endlocal
exit /b 0

rem ------------------------------------------------------------------ :try <komenda>
rem Sprawdzam, czy danie wywolanie w ogole dziala - 'py' potrafi istniec i jednoczesnie
rem mowic 'No installed Python found!' (klasyczny widok pod Wine), wiec samo 'where'
rem niczego nie gwarantuje.
:try
if defined PY exit /b 0
%* -c "import sys" >nul 2>nul && set "PY=%*"
exit /b 0
