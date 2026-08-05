# Locate an interpreter that can actually run DriftWatch.
#
# WHY this exists as its own file: on this machine `python` on PATH is the
# Microsoft Store alias stub, which exits with "Python was not found" instead
# of running anything. A scheduled task pointed at that stub fails at boot,
# hours before anyone looks. The task gets an absolute path to a real
# interpreter, resolved once, here.
#
# Dot-source it:  . "$PSScriptRoot\Resolve-Python.ps1"

function Resolve-DriftWatchPython {
    param([Parameter(Mandatory)][string] $RepoRoot)

    $candidates = @()

    # A project virtualenv wins: it is the only one whose packages are pinned
    # to this project.
    $candidates += (Join-Path $RepoRoot '.venv\Scripts\python.exe')
    $candidates += (Join-Path $RepoRoot 'venv\Scripts\python.exe')

    # The py launcher knows where the real installs are, and is not a stub.
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) {
        $found = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { $candidates += $found.Trim() }
    }

    $candidates += @(
        "$env:ProgramFiles\Python313\python.exe"
        "$env:ProgramFiles\Python312\python.exe"
        "$env:ProgramFiles\Python311\python.exe"
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )

    foreach ($c in $candidates) {
        if (-not $c) { continue }
        if (-not (Test-Path $c)) { continue }
        # WindowsApps holds the Store alias stubs. Never select one.
        if ($c -like '*\WindowsApps\*') { continue }
        return (Resolve-Path $c).Path
    }
    return $null
}

function Test-DriftWatchDeps {
    param([Parameter(Mandatory)][string] $Python)

    & $Python -c "import googleapiclient, google.oauth2, openpyxl" 2>$null
    return ($LASTEXITCODE -eq 0)
}
