@echo off
rem Baut dist\ToolHub.exe (laeuft ohne installiertes Python).
cd /d "%~dp0"
python -m pip install --user --quiet pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name ToolHub --paths . ToolHub.pyw
echo.
echo Fertig: dist\ToolHub.exe
