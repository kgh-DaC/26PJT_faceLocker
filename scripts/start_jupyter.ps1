# PC(Windows)에서 JupyterLab 시작. 노트북 폴더: notebooks\  커널: "FaceDoor (Python 3.12)"
# 사용:  powershell -ExecutionPolicy Bypass -File scripts\start_jupyter.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $root "notebooks")
& (Join-Path $root ".venv\Scripts\jupyter.exe") lab
