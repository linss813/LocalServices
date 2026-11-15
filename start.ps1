$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 虚拟环境创建失败，请安装 Python 3.9+' }
}
& .\.venv\Scripts\python.exe -c 'import psutil' 2>$null
if ($LASTEXITCODE -ne 0) {
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
}
& .\.venv\Scripts\python.exe manager.py @args
