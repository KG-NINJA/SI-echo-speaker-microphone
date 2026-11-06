$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Create .venv with Python 3.10 and install requirements.txt first.'
}
& $pythonExe -m unittest discover -s $PSScriptRoot -p 'test_echo_*.py'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $pythonExe -m py_compile (Join-Path $PSScriptRoot 'echo_audio.py') (Join-Path $PSScriptRoot 'echo_aec.py') (Join-Path $PSScriptRoot 'vm_audio.py')
exit $LASTEXITCODE
