<#
.SYNOPSIS
  facedoor SD card writer for Windows (PowerShell 5.1+). Owner: hw-engineer.
  STATUS: UNVERIFIED WITH A REAL SD CARD / PI (parser check, -DryRun and unit tests only).

.DESCRIPTION
  Writes Ubuntu 24.04 preinstalled-server (arm64+raspi) to an SD card and copies cloud-init user-data,
  network-config, meta-data, the facedoor bundle and the first-boot script to the boot partition.
  Testable logic lives in sdcard_tool.py (stdlib Python >= 3.8); this script asks questions, shows
  devices, asks for typed confirmations and calls the tool.

  Default is a DRY RUN: nothing is written. Use -Write (in an elevated PowerShell) to really write.
  There is intentionally no option to skip the confirmations.

  Safety: only removable USB/SD media pass (system/boot disks, the system drive, internal/NVMe/SATA disks,
  fixed-media USB disks (external HDD/SSD), read-only cards, disks holding the image/work files,
  > 512 GiB or < 7 GB are refused by sdcard_tool.py). The selected disk is re-checked here with Get-Disk
  and again inside the tool right before writing. Passwords are read with Read-Host -AsSecureString and
  handed to Python on stdin only (never on a command line, in the log or in a file).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\sdcard\flash_sdcard.ps1
  powershell -ExecutionPolicy Bypass -File deploy\sdcard\flash_sdcard.ps1 -Write -Image D:\img\ubuntu-24.04.3-preinstalled-server-arm64+raspi.img.xz
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Write,
    [string]$Image = "",
    [string]$Sha256 = "",
    [string]$SumsFile = "",
    [switch]$Fetch,
    [string]$ImageDir = "",
    [string]$WorkDir = "",
    [double]$MaxSizeGB = 0,
    [ValidateSet("full", "quick")][string]$Verify = "full",
    [switch]$KeepStage
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

if ($DryRun -and $Write) { throw "-DryRun and -Write are mutually exclusive" }
$IsDry = -not $Write

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $Here "..\..")).Path
$Tool = Join-Path $Here "sdcard_tool.py"
if (-not $WorkDir) { $WorkDir = Join-Path $Here "out" }
if (-not $ImageDir) { $ImageDir = Join-Path $WorkDir "images" }
$Stage = Join-Path $WorkDir "stage"
New-Item -ItemType Directory -Force -Path $WorkDir, $ImageDir | Out-Null
$LogFile = Join-Path $WorkDir ("flash-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")

# Log(): the ONLY thing written to the log file. Never pass a secret to it.
function Log([string]$msg) {
    $line = (Get-Date -Format "HH:mm:ss") + " " + $msg
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}
function Fail([string]$msg, [int]$code = 2) {
    Log ("ERROR: " + $msg)
    exit $code
}

# ---- admin check ----
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$IsAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsDry -and -not $IsAdmin) {
    Write-Host "-Write needs an elevated PowerShell (Run as Administrator)." -ForegroundColor Red
    exit 1
}

# ---- python ----
$PyExe = $null
$PyPre = @()
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$candidates = @(@($venvPy), @("py", "-3"), @("python"))
foreach ($c in $candidates) {
    $exe = $c[0]
    $pre = @()
    if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" | Out-Null
        if ($LASTEXITCODE -eq 0) { $PyExe = $exe; $PyPre = $pre; break }
    } catch { continue }
}
if (-not $PyExe) { Fail "Python >= 3.8 not found (create .venv or install Python)" }

function Invoke-Tool([string[]]$ToolArgs) {
    # stdout goes to the console, not into the function result (results are read from --result files)
    & $PyExe @PyPre $Tool @ToolArgs | Out-Host
    return $LASTEXITCODE
}

function Quote-Arg([string]$a) {
    if ($a -notmatch '[\s"]') { return $a }
    $e = $a -replace '(\\*)"', '$1$1\"'
    $e = $e -replace '(\\+)$', '$1$1'
    return '"' + $e + '"'
}

# Runs the tool with $StdinText on stdin (UTF-8, no BOM). Secrets go here, never into arguments.
function Invoke-ToolStdin([string[]]$ToolArgs, [string]$StdinText) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = (Get-Command $PyExe).Source
    $all = @($PyPre) + @($Tool) + @($ToolArgs)
    $psi.Arguments = ($all | ForEach-Object { Quote-Arg $_ }) -join " "
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($StdinText)
    $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $p.StandardInput.Close()
    $p.WaitForExit()
    return $p.ExitCode
}

function Read-Json([string]$path) {
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function SecureToPlain([Security.SecureString]$s) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

function Ask([string]$prompt, [string]$default) {
    $v = Read-Host "$prompt [$default]"
    if ([string]::IsNullOrWhiteSpace($v)) { return $default }
    return $v.Trim()
}

function YesNo([string]$prompt, [bool]$default) {
    $d = "y/N"
    if ($default) { $d = "Y/n" }
    $v = Read-Host "$prompt [$d]"
    if ([string]::IsNullOrWhiteSpace($v)) { return $default }
    return ($v.Trim().ToLower().StartsWith("y"))
}

$cleanupStage = {
    if (-not $KeepStage -and (Test-Path -LiteralPath $Stage)) {
        & $PyExe @PyPre $Tool "--result" (Join-Path $WorkDir "scrub.json") "scrub-stage" "--stage" $Stage | Out-Null
        Log "removed staged user-data/network-config/meta-data (use -KeepStage to keep them)"
    }
}

try {
    if ($IsDry) {
        Log "mode: DRY RUN (nothing is written to any device; use -Write in an elevated shell to write)"
        if (-not $IsAdmin) { Log "note: not elevated; the device list may be incomplete in dry-run" }
    } else {
        Log "mode: WRITE"
    }
    Log "project=$Root python=$PyExe work=$WorkDir"

    # -----------------------------------------------------------------------------------------
    # 1. questions
    # -----------------------------------------------------------------------------------------
    Write-Host ""
    Write-Host "== facedoor SD card settings =="
    $hostName = Ask "Hostname" "facedoor-pi"
    $userName = Ask "Login user (not 'ubuntu'/'root')" "facedoor"
    $password = $null
    while ($true) {
        $s1 = Read-Host "Password for $userName (min 8 chars)" -AsSecureString
        $s2 = Read-Host "Repeat password" -AsSecureString
        $p1 = SecureToPlain $s1
        $p2 = SecureToPlain $s2
        if ($p1 -cne $p2) { Write-Host "  passwords differ, try again"; continue }
        if ($p1.Length -lt 8) { Write-Host "  too short"; continue }
        $password = $p1
        $p1 = $null; $p2 = $null
        break
    }

    $sshKey = ""
    $defaultPub = "-"
    foreach ($k in @("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")) {
        $kp = Join-Path $env:USERPROFILE (".ssh\" + $k)
        if (Test-Path -LiteralPath $kp) { $defaultPub = $kp; break }
    }
    while ($true) {
        $pub = Ask "SSH PUBLIC key file (.pub), '-' for none" $defaultPub
        if ($pub -eq "-") { break }
        if (-not (Test-Path -LiteralPath $pub -PathType Leaf)) { Write-Host "  not found: $pub"; continue }
        $content = Get-Content -LiteralPath $pub -Raw
        if ($content -match "PRIVATE KEY") { Write-Host "  that is a PRIVATE key. Never copy it; choose the .pub file"; continue }
        $line = ($content -split "`r?`n" | Where-Object { $_ -match '^(ssh-|ecdsa-|sk-)' } | Select-Object -First 1)
        if (-not $line) { Write-Host "  no 'ssh-...' public key line in $pub"; continue }
        $sshKey = $line.Trim()
        break
    }
    if (-not $sshKey) { Write-Host "  (no key: SSH password login will be enabled)" }

    $wifi = $null
    if (YesNo "Configure Wi-Fi?" $false) {
        $ssid = ""
        while (-not $ssid) { $ssid = Read-Host "  SSID" }
        $wpass = $null
        while ($true) {
            $w1 = SecureToPlain (Read-Host "  Wi-Fi passphrase (8-63 chars, empty = open network)" -AsSecureString)
            if (-not $w1) {
                if (YesNo "  open network without password?" $false) { break } else { continue }
            }
            $w2 = SecureToPlain (Read-Host "  repeat passphrase" -AsSecureString)
            if ($w1 -ceq $w2) { $wpass = $w1; $w1 = $null; $w2 = $null; break }
            Write-Host "  passphrases differ"
        }
        $country = (Ask "  Wi-Fi country code" "KR").ToUpper()
        $hidden = YesNo "  hidden SSID?" $false
        $wifi = @{ ssid = $ssid; country = $country; hidden = $hidden }
        if ($wpass) { $wifi["passphrase"] = $wpass } else { $wifi["open"] = $true }
        $wpass = $null
    }
    $tz = Ask "Timezone" "Asia/Seoul"
    $enableService = YesNo "Enable facedoor.service at boot? (say no until the wiring is checked)" $false
    $models = YesNo "Download face models on first boot?" $true

    $keys = @()
    if ($sshKey) { $keys = @($sshKey) }
    $params = @{
        hostname = $hostName; username = $userName; password = $password; timezone = $tz
        ssh_public_keys = $keys; wifi = $wifi; enable_service = $enableService; download_models = $models
    }
    $json = ConvertTo-Json -InputObject $params -Depth 4 -Compress
    $password = $null
    if ($wifi) { $wifi.Remove("passphrase") }
    $params = $null

    # -----------------------------------------------------------------------------------------
    # 2. stage (stdin only)
    # -----------------------------------------------------------------------------------------
    Log "staging cloud-init files and project bundle -> $Stage"
    if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
    $stageResult = Join-Path $WorkDir "stage-result.json"
    $rc = Invoke-ToolStdin @("--result", $stageResult, "stage", "--out", $Stage) $json
    $json = $null
    [GC]::Collect()
    if ($rc -ne 0) { Fail "staging failed (see message above)" }
    $st = Read-Json $stageResult
    Log ("bundle: {0} files, {1:N1} MB, sha256 {2}..." -f $st.bundle.files, ($st.bundle.size / 1e6), $st.bundle.sha256.Substring(0, 16))
    Log ("settings: " + ($st.settings | ConvertTo-Json -Compress))

    # -----------------------------------------------------------------------------------------
    # 3. image
    # -----------------------------------------------------------------------------------------
    $imageSize = $null
    $imageJson = Join-Path $WorkDir "image.json"
    if ($Image) {
        if (-not (Test-Path -LiteralPath $Image -PathType Leaf)) { Fail "image not found: $Image" }
        $Image = (Resolve-Path -LiteralPath $Image).Path
        $va = @("--result", $imageJson, "verify-image", "--image", $Image)
        if ($Sha256) { $va += @("--sha256", $Sha256) }
        if ($SumsFile) { $va += @("--sums-file", $SumsFile) }
        Log "verifying $Image"
        if ((Invoke-Tool $va) -ne 0) { Fail "image verification failed" 3 }
        $imageSize = (Read-Json $imageJson).uncompressed_size
    } elseif ($IsDry -and -not $Fetch) {
        Log "resolving the latest Ubuntu 24.04 raspi image (no download in dry-run; add -Fetch to download)"
        if ((Invoke-Tool @("--result", $imageJson, "resolve-image")) -eq 0) {
            $ri = Read-Json $imageJson
            Log ("would download: " + $ri.url + " sha256 " + $ri.sha256)
        } else {
            Log "WARNING: could not resolve the image (offline?); continuing the dry run"
        }
    } else {
        Log "downloading + verifying the image into $ImageDir (resumable)"
        if ((Invoke-Tool @("--result", $imageJson, "fetch-image", "--dir", $ImageDir)) -ne 0) { Fail "image download/verify failed" 5 }
        $fi = Read-Json $imageJson
        $Image = $fi.path
        $imageSize = $fi.uncompressed_size
    }
    if ($Image) { Log "image: $Image (uncompressed $imageSize bytes)" }

    # -----------------------------------------------------------------------------------------
    # 4. device selection
    # -----------------------------------------------------------------------------------------
    $limitArgs = @("--protect", $WorkDir, "--protect", $Root, "--protect", $ImageDir)
    if ($Image) { $limitArgs += @("--protect", $Image) }
    if ($imageSize) { $limitArgs += @("--image-size", [string]$imageSize) }
    if ($MaxSizeGB -gt 0) { $limitArgs += @("--max-size-gb", [string]$MaxSizeGB) }
    $devJson = Join-Path $WorkDir "devices.json"
    if ((Invoke-Tool (@("--result", $devJson, "list-devices") + $limitArgs)) -ne 0) { Fail "device listing failed" 6 }
    $devs = @((Read-Json $devJson).devices)
    Write-Host ""
    Write-Host "== disks (only entries marked OK can be selected) =="
    foreach ($d in $devs) {
        $tag = "REFUSED"
        $color = "DarkGray"
        if ($d.assessment.ok) { $tag = "OK     "; $color = "Green" }
        $rm = "no"
        if ($d.removable) { $rm = "yes" }
        Write-Host ("{0} disk {1,-3} {2,8:N1} GB bus={3,-5} media={4} removable={5}  {6}" -f $tag, $d.id, ($d.size / 1e9), $d.bus, $d.media_type, $rm, $d.model) -ForegroundColor $color
        foreach ($p in @($d.partitions)) {
            if ($null -eq $p) { continue }
            $mp = "-"
            if ($p.mountpoint) { $mp = $p.mountpoint }
            Write-Host ("           - {0}: {1:N2} GB {2} drive={3}" -f $p.name, ($p.size / 1e9), $p.fstype, $mp)
        }
        foreach ($r in @($d.assessment.reasons)) { if ($r) { Write-Host "           x $r" } }
        foreach ($w in @($d.assessment.warnings)) { if ($w) { Write-Host "           ! $w" -ForegroundColor Yellow } }
    }
    $ok = @($devs | Where-Object { $_.assessment.ok })
    if ($ok.Count -eq 0) {
        Log "no eligible SD card found (insert the card via a USB/SD reader and re-run)"
        if ($IsDry) { Log "DRY RUN finished: staging and checks OK, nothing written"; exit 0 }
        exit 6
    }

    Write-Host ""
    $sel = $null
    while (-not $sel) {
        $typed = Read-Host ("Type the disk NUMBER exactly as listed (e.g. {0}), or 'q' to quit" -f $ok[0].id)
        if ($typed -eq "q") { Log "aborted by user"; exit 1 }
        $sel = $ok | Where-Object { $_.id -ceq $typed.Trim() } | Select-Object -First 1
        if (-not $sel) { Write-Host "  '$typed' is not an eligible disk" }
    }
    $diskNo = [int]$sel.id
    $devSize = [int64]$sel.size
    Log ("selected disk {0} ({1}, {2}, {3} bytes)" -f $diskNo, $sel.path, $sel.model, $devSize)

    if ($IsDry) {
        Write-Host ""
        Write-Host "DRY RUN - this is what -Write would do (nothing was written):"
        Write-Host ("  1. re-check disk {0} ({1}, {2} bytes) with Get-Disk and the tool, ask you to type ERASE" -f $diskNo, $sel.model, $devSize)
        Write-Host "  2. Clear-Disk (remove partitions / drive letters) on that disk only"
        Write-Host ("  3. write the image -> {0}, partition table last, read back + sha256 ({1})" -f $sel.path, $Verify)
        Write-Host "  4. assign a drive letter to the FAT32 'system-boot' partition and copy + sha256-verify:"
        Write-Host "     user-data, network-config, meta-data, facedoor-firstboot.sh, facedoor-bundle.tar.gz(.sha256)"
        Write-Host "  5. On the Pi: cloud-init, then facedoor-firstboot installs to /opt/facedoor (GPIO untouched)"
        Write-Host "Real run (elevated): powershell -ExecutionPolicy Bypass -File deploy\sdcard\flash_sdcard.ps1 -Write"
        Log "DRY RUN finished"
        exit 0
    }

    # -----------------------------------------------------------------------------------------
    # 5. final confirmation + independent re-check + write
    # -----------------------------------------------------------------------------------------
    if (-not $Image) { Fail "no image" }
    Write-Host ""
    Write-Host ("!!! ALL DATA on disk {0} ({1}, {2:N1} GB) WILL BE ERASED !!!" -f $diskNo, $sel.model, ($devSize / 1e9)) -ForegroundColor Red
    Write-Host "Windows may pop up 'You need to format the disk' for the Linux partition afterwards: click CANCEL." -ForegroundColor Yellow
    $confirm = Read-Host "Type ERASE (upper case) to continue, anything else aborts"
    if ($confirm -cne "ERASE") { Log "aborted at final confirmation"; exit 1 }

    # independent Get-Disk check (does not rely on the Python assessment)
    $gd = Get-Disk -Number $diskNo
    $sysLetter = $env:SystemDrive.TrimEnd(":")
    $sysOnDisk = @(Get-Partition -DiskNumber $diskNo -ErrorAction SilentlyContinue | Where-Object { [string]$_.DriveLetter -eq $sysLetter })
    if ($gd.IsSystem -or $gd.IsBoot -or $sysOnDisk.Count -gt 0) { Fail "disk $diskNo is a system/boot disk: REFUSED" 6 }
    if (@("USB", "SD", "MMC") -notcontains [string]$gd.BusType) { Fail ("disk $diskNo bus " + $gd.BusType + " is not USB/SD/MMC: REFUSED") 6 }
    if ([int64]$gd.Size -ne $devSize) { Fail "disk $diskNo size changed (card swapped?): REFUSED" 6 }
    if ([int64]$gd.Size -gt 512GB) { Fail "disk $diskNo is larger than 512 GiB: REFUSED" 6 }
    $checkJson = Join-Path $WorkDir "check.json"
    if ((Invoke-Tool (@("--result", $checkJson, "check-device", "--target", $sel.path, "--expect-size", [string]$devSize) + $limitArgs)) -ne 0) {
        Fail "device re-check failed: NOT writing" 6
    }

    if ($gd.IsOffline) { Set-Disk -Number $diskNo -IsOffline $false }
    if ($gd.IsReadOnly) { Fail "disk $diskNo is read-only (lock switch?)" 6 }
    Log "Clear-Disk $diskNo (removes partitions and drive letters)"
    try {
        Clear-Disk -Number $diskNo -RemoveData -RemoveOEM -Confirm:$false -ErrorAction Stop
    } catch {
        Log ("note: Clear-Disk: " + $_.Exception.Message + " (continuing; RAW cards have nothing to clear)")
    }

    $writeJson = Join-Path $WorkDir "write.json"
    Log ("writing {0} -> {1}" -f $Image, $sel.path)
    $wa = @("--result", $writeJson, "write", "--image", $Image, "--target", $sel.path,
            "--expect-target-size", [string]$devSize, "--verify", $Verify)
    if ((Invoke-Tool $wa) -ne 0) { Fail "WRITE FAILED (card may be unusable; re-run)" 4 }
    Log ("write + read-back verify OK: " + (Read-Json $writeJson).written_bytes + " bytes")

    # -----------------------------------------------------------------------------------------
    # 6. boot partition files
    # -----------------------------------------------------------------------------------------
    Update-HostStorageCache
    Update-Disk -Number $diskNo
    Start-Sleep -Seconds 3
    $bootPart = $null
    for ($i = 0; $i -lt 10 -and -not $bootPart; $i++) {
        $bootPart = Get-Partition -DiskNumber $diskNo -ErrorAction SilentlyContinue | Sort-Object Offset | Select-Object -First 1
        if (-not $bootPart) { Start-Sleep -Seconds 2; Update-Disk -Number $diskNo }
    }
    if (-not $bootPart) { Fail "no partitions visible on disk $diskNo after writing" 4 }
    if (-not $bootPart.DriveLetter -or [int][char]$bootPart.DriveLetter -eq 0) {
        $bootPart | Add-PartitionAccessPath -AssignDriveLetter
        Start-Sleep -Seconds 2
        $bootPart = Get-Partition -DiskNumber $diskNo -PartitionNumber $bootPart.PartitionNumber
    }
    $letter = [string]$bootPart.DriveLetter
    if (-not $letter) { Fail "could not assign a drive letter to the boot partition" 4 }
    $vol = Get-Volume -DriveLetter $letter
    if ($vol.FileSystem -notmatch "FAT") { Fail ("partition {0}: is {1}, expected FAT32 system-boot" -f $letter, $vol.FileSystem) 4 }
    $bootDir = $letter + ":\"
    Log "copying staged files to $bootDir (label '$($vol.FileSystemLabel)')"
    $copyJson = Join-Path $WorkDir "copy.json"
    if ((Invoke-Tool @("--result", $copyJson, "copy-to-boot", "--stage", $Stage, "--boot", $bootDir)) -ne 0) {
        Fail "copy to boot partition failed" 4
    }
    try { Write-VolumeCache -DriveLetter $letter } catch { }
    Log "DONE. Use 'Safely Remove Hardware' / Eject, insert the card into the Pi 5 and power on."
    Log "First boot: cloud-init (user, SSH, network), then facedoor-firstboot (log /var/log/facedoor-firstboot.log)."
    Log "Then: ssh $userName@$hostName.local (or the IP); see docs/PORTING.md section 8."
}
finally {
    & $cleanupStage
}
