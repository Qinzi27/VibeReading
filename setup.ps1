# Create a project-local environment; never install into the global interpreter.
param([string]$PythonPath = "")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $localPython)) {
    if (-not $PythonPath) {
        # Use the bundled runtime when available; otherwise use Python from PATH.
        $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundledPython) {
            $PythonPath = $bundledPython
        } else {
            $PythonPath = 'python'
        }
    }
    & $PythonPath -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv; specify -PythonPath with Python 3.12+.' }
}
& $localPython -m pip --version
if ($LASTEXITCODE -ne 0) {
    & $localPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize pip in the local environment.' }
}
& $localPython -m pip install --disable-pip-version-check --only-binary=:all: -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Setup complete. Double-click start.cmd or run .venv\Scripts\python.exe run.py --open'
