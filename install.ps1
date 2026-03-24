Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host '[gitferret]'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourcePath = Join-Path $scriptDir 'gitferret.py'
if (-not (Test-Path $sourcePath)) {
    throw "source file not found: $sourcePath"
}

function Test-YesReply {
    param(
        [string]$Reply
    )

    return ($Reply -eq '' -or $Reply -eq 'y' -or $Reply -eq 'Y')
}

function Test-PathContainsEntry {
    param(
        [string]$PathValue,
        [string]$Entry
    )

    if (-not $PathValue) {
        return $false
    }

    return ($PathValue -split ';' | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') }) -contains $Entry
}

function Get-CurrentPythonRuntime {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return [pscustomobject]@{
            Command = 'py'
            Arguments = @('-3')
            LauncherCommand = 'py -3'
            Source = 'py'
        }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        $pythonCommand = Get-Command python
        if ($pythonCommand.Source -match '\\\.pyenv\\pyenv-win\\shims\\python(?:\.bat)?$') {
            $pyenvRuntime = Resolve-PyenvPythonRuntime
            if ($pyenvRuntime) {
                return $pyenvRuntime
            }
        }

        return [pscustomobject]@{
            Command = 'python'
            Arguments = @()
            LauncherCommand = 'python'
            Source = 'python'
        }
    }

    $pyenvRuntime = Resolve-PyenvPythonRuntime
    if ($pyenvRuntime) {
        return $pyenvRuntime
    }

    return $null
}

function Resolve-PyenvPythonRuntime {
    $roots = @(
        (Join-Path $env:USERPROFILE '.pyenv\pyenv-win\versions'),
        (Join-Path $env:LOCALAPPDATA 'pyenv\pyenv-win\versions')
    ) | Where-Object { $_ -and (Test-Path $_) }

    $candidates = foreach ($root in $roots) {
        Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $candidatePath = Join-Path $_.FullName 'python.exe'
            if (Test-Path $candidatePath) {
                [pscustomobject]@{
                    Path = (Get-Item $candidatePath).FullName
                    Version = $_.Name
                }
            }
        }
    }

    $candidate = $candidates |
        Sort-Object @{ Expression = { [version]$_.Version }; Descending = $true }, @{ Expression = { $_.Path }; Descending = $true } |
        Select-Object -First 1

    if (-not $candidate) {
        return $null
    }

    return [pscustomobject]@{
        Command = $candidate.Path
        Arguments = @()
        LauncherCommand = '"' + $candidate.Path + '"'
        Source = 'pyenv'
    }
}

function Find-WingetPythonPackage {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host 'Python was not found and winget is unavailable.'
        Write-Host 'Install Python 3 or winget, then run install.ps1 again.'
        exit 1
    }

    $searchCommands = @(
        @('search', '--source', 'winget', '--moniker', 'python3', '--count', '50', '--accept-source-agreements', '--disable-interactivity'),
        @('search', '--source', 'winget', '--id', 'Python.Python', '--count', '50', '--accept-source-agreements', '--disable-interactivity')
    )

    $candidates = @()
    foreach ($searchCommand in $searchCommands) {
        $lines = & winget @searchCommand 2>&1
        if ($LASTEXITCODE -ne 0) {
            continue
        }

        foreach ($line in $lines) {
            $parts = $line -split '\s{2,}'
            if ($parts.Length -lt 4) {
                continue
            }

            $packageId = $parts[1]
            $packageVersion = $parts[2]
            if ($packageId -notmatch '^Python\.Python\.\d+(?:\.\d+)?$') {
                continue
            }
            if ($packageVersion -notmatch '^\d+(?:\.\d+){1,3}$') {
                continue
            }

            $candidates += [pscustomobject]@{
                Name = $parts[0]
                Id = $packageId
                Version = $packageVersion
            }
        }

        if ($candidates.Count -gt 0) {
            break
        }
    }

    if (-not $candidates) {
        throw 'winget could not find an installable Python package.'
    }

    return $candidates |
        Sort-Object @{ Expression = { [version]$_.Version }; Descending = $true }, @{ Expression = { $_.Id }; Descending = $true } |
        Select-Object -First 1
}

function Install-PythonWithWinget {
    $candidate = Find-WingetPythonPackage
    Write-Host ("Python was not found. Installing {0} via winget..." -f $candidate.Id)

    Write-Host -NoNewline 'install Python now? via winget (Y/n) '
    $reply = Read-Host
    if (-not (Test-YesReply -Reply $reply)) {
        Write-Host 'cancelled'
        exit 0
    }

    $installCommand = @(
        'install',
        '--source', 'winget',
        '--id', $candidate.Id,
        '--exact',
        '--scope', 'user',
        '--silent',
        '--accept-package-agreements',
        '--accept-source-agreements',
        '--disable-interactivity'
    )

    & winget @installCommand
    if ($LASTEXITCODE -ne 0) {
        throw "failed to install Python via winget: $($candidate.Id)"
    }

    return $candidate
}

function Resolve-PythonExeAfterWinget {
    param(
        [string]$Version
    )

    $roots = @(
        (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Python'),
        (Join-Path $env:ProgramFiles 'Python'),
        (Join-Path ${env:ProgramFiles(x86)} 'Python')
    ) | Where-Object { $_ -and (Test-Path $_) }

    $folderNames = @()
    if ($Version -match '^(?<major>\d+)\.(?<minor>\d+)') {
        $minor = [int]$matches.minor
        $folderNames += ('Python{0}{1:D2}' -f $matches.major, $minor)
        $folderNames += ('Python{0}{1:D2}-64' -f $matches.major, $minor)
        $folderNames += ('Python{0}{1:D2}-32' -f $matches.major, $minor)
    }

    foreach ($root in $roots) {
        foreach ($folderName in $folderNames) {
            $candidatePath = Join-Path (Join-Path $root $folderName) 'python.exe'
            if (Test-Path $candidatePath) {
                return (Get-Item $candidatePath).FullName
            }
        }
    }

    $fallback = Get-ChildItem -Path $roots -Recurse -Filter python.exe -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if ($fallback) {
        return $fallback.FullName
    }

    throw 'Python was installed, but python.exe could not be located.'
}

$pythonRuntime = Get-CurrentPythonRuntime
if (-not $pythonRuntime) {
    $wingetPackage = Install-PythonWithWinget
    $pythonExe = Resolve-PythonExeAfterWinget -Version $wingetPackage.Version
    $pythonRuntime = [pscustomobject]@{
        Command = $pythonExe
        Arguments = @()
        LauncherCommand = '"' + $pythonExe + '"'
        Source = 'winget'
    }
}

$pythonExe = $pythonRuntime.Command
$pythonArgs = $pythonRuntime.Arguments
$pythonLauncher = $pythonRuntime.LauncherCommand

$probeArgs = $pythonArgs + @('-c', 'import curses')
& $pythonExe @probeArgs | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'windows-curses is not installed.'
    Write-Host -NoNewline 'install windows-curses now? (Y/n) '
    $reply = Read-Host
    if (Test-YesReply -Reply $reply) {
        & $pythonExe @($pythonArgs + @('-m', 'pip', 'install', 'windows-curses'))
        if ($LASTEXITCODE -ne 0) {
            throw 'failed to install windows-curses'
        }
    } else {
        throw 'Windows support requires the windows-curses package.'
    }

    & $pythonExe @probeArgs | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'windows-curses is still unavailable after installation'
    }
}

$installRoot = Join-Path $env:USERPROFILE '.gitferret'
$binDir = Join-Path $installRoot 'bin'
$targetPath = Join-Path $binDir 'gitferret.py'
$launcherPath = Join-Path $binDir 'gitferret.cmd'
$isInstalled = (Test-Path $targetPath) -and (Test-Path $launcherPath)

if (-not $isInstalled) {
    Write-Host ("install to {0}? (Y/n) " -f $targetPath) -NoNewline
    $reply = Read-Host
    if (-not (Test-YesReply -Reply $reply)) {
        Write-Host 'cancelled'
        exit 0
    }
}

New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
Copy-Item -Force $sourcePath $targetPath

$launcher = @"
@echo off
setlocal
$pythonLauncher "%~dp0gitferret.py" %*
exit /b %errorlevel%
"@

Set-Content -Path $launcherPath -Value $launcher -Encoding ASCII

$pathUpdated = $false
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$userPathContainsBin = Test-PathContainsEntry -PathValue $userPath -Entry $binDir
$envPathContainsBin = Test-PathContainsEntry -PathValue $env:Path -Entry $binDir
if (-not $userPathContainsBin -and -not $envPathContainsBin) {
    Write-Host ("add to PATH now? (Y/n) ") -NoNewline
    $reply = Read-Host
    if (Test-YesReply -Reply $reply) {
        if ($userPath) {
            $updatedPath = "$userPath;$binDir"
        } else {
            $updatedPath = $binDir
        }
        [Environment]::SetEnvironmentVariable('Path', $updatedPath, 'User')
        if ($env:Path) {
            $env:Path = "$binDir;$env:Path"
        } else {
            $env:Path = $binDir
        }
        $pathUpdated = $true
    } else {
        Write-Host 'skipped'
    }
} elseif ($userPathContainsBin -and -not $envPathContainsBin) {
    if ($env:Path) {
        $env:Path = "$binDir;$env:Path"
    } else {
        $env:Path = $binDir
    }
    $pathUpdated = $true
}

Write-Host "installed: $launcherPath"
if ($pathUpdated) {
    Write-Host "available in new shells after PATH refresh: $binDir"
} else {
    Write-Host "PATH was not updated: $binDir"
}

# Write-Host -NoNewline 'press Enter to exit... '
# [void](Read-Host)
