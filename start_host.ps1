# 参数原样交给Python入口；不使用开发者电脑上的固定路径。
$ErrorActionPreference = 'Stop'
$eyesPython = if ($env:EYES_HOST_PYTHON) { $env:EYES_HOST_PYTHON } else {
    Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $eyesPython)) {
    throw '请先按README创建.venv并安装requirements-host.txt，或设置EYES_HOST_PYTHON。'
}
$eyesOldEncoding = $env:PYTHONIOENCODING
try {
    $env:PYTHONIOENCODING = 'utf-8'
    & $eyesPython (Join-Path $PSScriptRoot 'run_host.py') @args
    if ($LASTEXITCODE -ne 0) { throw "交流服务退出，错误码：$LASTEXITCODE" }
}
finally {
    $env:PYTHONIOENCODING = $eyesOldEncoding
}
