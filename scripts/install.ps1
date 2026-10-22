<#
.SYNOPSIS
    安装 npa-mcp:创建虚拟环境、安装依赖、检查(必要时下载)前端静态资源。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -VenvPath D:\venvs\npa-mcp -WithDev
#>
[CmdletBinding()]
param(
    [string]$VenvPath = (Join-Path $env:LOCALAPPDATA "npa-mcp\venv"),
    [switch]$WithDev,
    [switch]$SkipXterm
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    $Message" -ForegroundColor DarkGray
}

if (-not (Test-Path (Join-Path $ProjectRoot "pyproject.toml"))) {
    throw "找不到 pyproject.toml,请在项目目录内运行本脚本: $ProjectRoot"
}

Write-Step "项目目录: $ProjectRoot"
Write-Step "虚拟环境: $VenvPath"

# ---------------------------------------------------------------- Python
$BasePython = $null
$BaseArgs = @()
try {
    & py -3.13 -c "import sys" *> $null
    if ($LASTEXITCODE -eq 0) {
        $BasePython = "py"
        $BaseArgs = @("-3.13")
    }
} catch {
    # py launcher 不存在,继续找 python
}
if (-not $BasePython) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "找不到 Python。请安装 Python 3.10+ 并勾选 'Add to PATH'。"
    }
    $BasePython = $cmd.Source
}
Write-Ok "使用解释器: $BasePython $($BaseArgs -join ' ')"

$PythonExe = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    Write-Step "创建虚拟环境"
    & $BasePython @BaseArgs -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "创建虚拟环境失败(退出码 $LASTEXITCODE)。"
    }
} else {
    Write-Ok "虚拟环境已存在,跳过创建"
}

# ---------------------------------------------------------------- 依赖
Write-Step "升级 pip"
& $PythonExe -m pip install --upgrade pip --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    throw "升级 pip 失败(退出码 $LASTEXITCODE)。请检查网络或 pip 源。"
}

$Target = $ProjectRoot
if ($WithDev) {
    $Target = "$ProjectRoot[dev]"
    Write-Step "安装项目与开发依赖 (editable)"
} else {
    Write-Step "安装项目 (editable)"
}
& $PythonExe -m pip install -e $Target --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    throw "安装依赖失败(退出码 $LASTEXITCODE)。请检查网络、代理或 pip 源。"
}

# ---------------------------------------------------------------- xterm.js
$StaticDir = Join-Path $ProjectRoot "src\npa_mcp\web\static"
$Assets = @(
    [pscustomobject]@{
        Name = "xterm.js"
        Url  = "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"
        Sha  = "f0aea0f75f48559013ae6643c2479dd737d26da42d5524e6d2b70915ae6523c7"
    },
    [pscustomobject]@{
        Name = "xterm.css"
        Url  = "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"
        Sha  = "832f3f2c603b43ad4351ff04970150cc7a873014276db126a6065c6dd81e4872"
    },
    [pscustomobject]@{
        Name = "xterm-addon-fit.js"
        Url  = "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"
        Sha  = "10f3194c5f17c1786fb7d5db865c1ec8539b6736a318063fd38bdaaf7c46848f"
    }
)

if (-not (Test-Path $StaticDir)) {
    New-Item -ItemType Directory -Path $StaticDir -Force | Out-Null
}

foreach ($asset in $Assets) {
    $dest = Join-Path $StaticDir $asset.Name
    $needDownload = $true
    if (Test-Path $dest) {
        $actual = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
        if ($actual -eq $asset.Sha) {
            Write-Ok "$($asset.Name) 校验通过"
            $needDownload = $false
        } else {
            Write-Host "    $($asset.Name) 校验不匹配,重新下载" -ForegroundColor Yellow
        }
    }
    if (-not $needDownload) { continue }
    if ($SkipXterm) {
        Write-Host "    跳过下载 $($asset.Name)(-SkipXterm)" -ForegroundColor Yellow
        continue
    }
    Write-Step "下载 $($asset.Name)"
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        Invoke-WebRequest -Uri $asset.Url -OutFile $tmp -UseBasicParsing
        $actual = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $asset.Sha) {
            throw "$($asset.Name) SHA256 不匹配: 期望 $($asset.Sha),实际 $actual"
        }
        Move-Item -Path $tmp -Destination $dest -Force
        Write-Ok "已写入 $dest"
    } finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
}

# ---------------------------------------------------------------- 默认配置
Write-Step "初始化默认配置"
& $PythonExe -c "from npa_mcp.config import ConfigManager; ConfigManager().ensure_default(); print(ConfigManager().path)"
if ($LASTEXITCODE -ne 0) {
    throw "初始化配置失败(退出码 $LASTEXITCODE)。"
}

# ---------------------------------------------------------------- 自检
Write-Step "导入自检"
& $PythonExe -c "import npa_mcp.server; print('npa-mcp 导入成功')"
if ($LASTEXITCODE -ne 0) {
    throw "导入 npa_mcp.server 失败(退出码 $LASTEXITCODE)。"
}

# ---------------------------------------------------------------- 输出
$ConfigPath = Join-Path $env:USERPROFILE ".npa-mcp\config.json"
$Snippet = [ordered]@{
    mcpServers = [ordered]@{
        npa_mcp = [ordered]@{
            command = $PythonExe
            args    = @("-m", "npa_mcp")
            env     = [ordered]@{
                PYTHONUTF8       = "1"
                PYTHONIOENCODING = "utf-8"
            }
        }
    }
} | ConvertTo-Json -Depth 6

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host "配置文件: $ConfigPath"
Write-Host ""
Write-Host "把下面的内容保存为项目根目录的 .mcp.json(或合并进已有配置):" -ForegroundColor Cyan
Write-Host $Snippet
Write-Host ""
Write-Host "随后重启 Claude Code,调用 open_config_page 打开配置页填写凭据。" -ForegroundColor Cyan
