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
    if ($TargetId -eq $PID) { return $false } # never the sweeper itself
    try {
        $p = Get-Process -Id $TargetId -ErrorAction Stop
        Stop-Process -Id $TargetId -Force -ErrorAction Stop
        $script:killed += "PID $TargetId ($($p.ProcessName)) - $Reason"
        return $true
    } catch { return $false }
}

function Get-PortOwners([int]$Port) {
    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                 Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { return @() }
}

# 1) Anything still LISTENING on the dev ports - catches the uvicorn --reload
#    parent AND its worker child (8000), and a stray Vite dev server (5173).
foreach ($port in 8000, 5173) {
    foreach ($owner in (Get-PortOwners $port)) {
        # The owner's CHILDREN are collected FIRST and killed UNCONDITIONALLY,
        # whether or not the owner itself resolves.
        #
        # MEASURED 2026-08-02, twice, and the first fix was aimed one case off.
        # uvicorn --reload is a parent plus a multiprocessing worker that
        # INHERITS the listening socket, and Windows names the parent as the
        # socket's owner either way:
        #   - parent already dead (Ctrl+C): Get-Process throws, the catch
        #     swallows it, :8000 stays held while the sweep prints "Clean".
        #   - parent still ALIVE: killing it SUCCEEDS, so an `if (...) { continue }`
        #     skipped the children - and the surviving worker (PID 21428, child
        #     of 17392) went on holding :8000 exactly as before.
        # Only the second shape is common, and it was the one the first fix
        # could not reach. Sweeping children in both cases costs nothing: a
        # process listening on a dev port has only dev-stack children.
        $orphans = @()
        try {
            $orphans = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$owner" -ErrorAction Stop)
        } catch {}
        [void](Stop-ByProcessId $owner "listening on port $port")
        foreach ($child in $orphans) {
            [void](Stop-ByProcessId $child.ProcessId "worker child of PID $owner holding port $port")
        }
    }
}

# 2) Orphaned sensing helpers: the active-window loop electron/sensing.ts
#    spawns is the only powershell.exe on the machine whose command line
#    invokes GetForegroundWindow (the script rides -Command, so the whole
#    text is visible in Win32_Process.CommandLine).
try {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'GetForegroundWindow' } |
        ForEach-Object { [void](Stop-ByProcessId $_.ProcessId 'orphaned sensing helper') }
} catch {}

# 3) Leftover Electron instances launched from this project (dev runs electron
#    with the project path on its command line).
try {
    Get-CimInstance Win32_Process -Filter "Name='electron.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -match 'jarvis\s*2\.0' } |
        ForEach-Object { [void](Stop-ByProcessId $_.ProcessId 'leftover Electron') }
} catch {}

# 4) VERIFY, then report. "Clean" is a claim about the WORLD, not about what
#    this script managed to do - and the next thing to run is `npm run dev`,
#    which waits on http-get://localhost:8000/health and hangs forever against a
#    held-but-dead socket. So re-check the ports and say what is true. A sweeper
#    that reports success without looking is worse than one that fails loudly:
#    the failure is at least diagnosable.
$stillHeld = @()
foreach ($port in 8000, 5173) {
    foreach ($owner in (Get-PortOwners $port)) { $stillHeld += "port $port (PID $owner)" }
}

if ($killed.Count -gt 0) {
    Write-Host "Stopped $($killed.Count) leftover process(es):"
    $killed | ForEach-Object { Write-Host "  - $_" }
} elseif ($stillHeld.Count -eq 0) {
    Write-Host 'Clean - no leftover Jarvis dev processes found.'
}

if ($stillHeld.Count -gt 0) {
    Write-Host ''
    Write-Host 'STILL HELD after the sweep - npm run dev will not start cleanly:'
    $stillHeld | ForEach-Object { Write-Host "  - $_" }
    Write-Host '  Inspect with:  Get-NetTCPConnection -LocalPort 8000 -State Listen'
    exit 1
}
