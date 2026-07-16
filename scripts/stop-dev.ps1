# Jarvis OS - dev process sweeper ("npm run stop").
#
# Ctrl+C on `npm run dev` can strand pieces of the process tree on Windows:
# the uvicorn --reload parent keeping port 8000 (recorded live incident), a
# stray Vite dev server on 5173, an orphaned Electron, and (before the
# parent-PID watchdog in electron/sensing.ts) the PowerShell active-window
# helper. This script sweeps them all and reports exactly what it stopped.
#
# Every step is best-effort: a permission hiccup on one process never aborts
# the sweep.

$ErrorActionPreference = 'Continue'
$killed = @()

function Stop-ByProcessId([int]$TargetId, [string]$Reason) {
    if ($TargetId -eq $PID) { return } # never the sweeper itself
    try {
        $p = Get-Process -Id $TargetId -ErrorAction Stop
        Stop-Process -Id $TargetId -Force -ErrorAction Stop
        $script:killed += "PID $TargetId ($($p.ProcessName)) - $Reason"
    } catch {}
}

# 1) Anything still LISTENING on the dev ports - catches the uvicorn --reload
#    parent AND its worker child (8000), and a stray Vite dev server (5173).
foreach ($port in 8000, 5173) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
        foreach ($c in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
            Stop-ByProcessId $c "listening on port $port"
        }
    } catch {}
}

# 2) Orphaned sensing helpers: the active-window loop electron/sensing.ts
#    spawns is the only powershell.exe on the machine whose command line
#    invokes GetForegroundWindow (the script rides -Command, so the whole
#    text is visible in Win32_Process.CommandLine).
try {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'GetForegroundWindow' } |
        ForEach-Object { Stop-ByProcessId $_.ProcessId 'orphaned sensing helper' }
} catch {}

# 3) Leftover Electron instances launched from this project (dev runs electron
#    with the project path on its command line).
try {
    Get-CimInstance Win32_Process -Filter "Name='electron.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -match 'jarvis\s*2\.0' } |
        ForEach-Object { Stop-ByProcessId $_.ProcessId 'leftover Electron' }
} catch {}

if ($killed.Count -eq 0) {
    Write-Host 'Clean - no leftover Jarvis dev processes found.'
} else {
    Write-Host "Stopped $($killed.Count) leftover process(es):"
    $killed | ForEach-Object { Write-Host "  - $_" }
}
