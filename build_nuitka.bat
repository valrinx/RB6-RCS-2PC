@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM One-time: pip install -r requirements.txt
REM          pip install -r requirements-dev.txt

REM Remove previous dist\RVN.exe if present
if exist "dist\RVN.exe" del /f /q "dist\RVN.exe" 2>nul

python -m nuitka ^
  --standalone ^
  --onefile ^
  --remove-output ^
  --windows-console-mode=force ^
  --output-dir=dist ^
  --output-filename=RVN.exe ^
  --include-package=mouse ^
  --include-package=makcu ^
  --include-package=serial ^
  --include-package=rich ^
  --include-package=markdown_it ^
  --include-package=mdurl ^
  --include-package=pygments ^
  --include-package=fastapi ^
  --include-package=uvicorn ^
  --include-package=starlette ^
  --include-package=pydantic ^
  --include-package=pydantic_core ^
  --include-package=anyio ^
  --include-package=idna ^
  --include-package=annotated_types ^
  --include-package=typing_extensions ^
  --include-package=typing_inspection ^
  --include-package=click ^
  --include-package=h11 ^
  --include-package=httptools ^
  --include-package=websockets ^
  --include-package=watchfiles ^
  --include-package=dotenv ^
  --include-package=yaml ^
  --include-package=colorama ^
  --include-data-dir=templates=templates ^
  --include-data-dir=static=static ^
  --include-data-dir=configs=configs ^
  --nofollow-import-to=makcu.test_suite ^
  --nofollow-import-to=colorama.tests ^
  rvn.py

if errorlevel 1 (
  echo.
  echo BUILD FAILED - see errors above.
  exit /b 1
)
echo.
echo OK: dist\RVN.exe
echo User profiles: %LOCALAPPDATA%\RVN\configs
echo Tip: For a windowless exe use --windows-console-mode=disable (no banner/errors on screen).
exit /b 0
