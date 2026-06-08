param(
    [ValidateSet('', 'WriteUsb', 'CreateImg', 'CreateIso', 'CreateDisk', 'Clone',
                 'Wipe', 'Check', 'Format', 'ListImage')]
    [string]$Action = '',
    [int]$DiskNumber = -1,
    [int]$SourceDiskNumber = -1,
    [string]$PartStyle = 'GPT',
    [string]$PartitionsJson = '',
    [string]$FileSystem = 'FAT32',
    [string]$SourceDir = '',
    [string[]]$Includes = @(),
    [string]$Label = 'UEFITOOLS',
    [string]$OutputFile = '',
    [int]$SizeMB = 32,
    [switch]$UseGpt,
    [switch]$SkipConfirm,
    [switch]$Verbose,
    [switch]$Verify,
    [switch]$Udf,
    [switch]$Hybrid,
    [string]$ProgressFile = '',
    [string]$BootImage = ''
)

<#
.SYNOPSIS
    mkimage - Bootable Media Creator (native Windows, no WSL required)

.DESCRIPTION
    Creates FAT32 (.img) or ISO (.iso) images containing UEFI applications.
    Can also write directly to USB flash drives (including unformatted ones).

    All operations use native Windows APIs:
    - FAT32 .img: diskpart VHD create/attach/format/robocopy, strip footer (no Hyper-V needed)
    - ISO: oscdimg.exe (Windows ADK) or IMAPI2 COM fallback
    - USB: Clear-Disk/Initialize-Disk/New-Partition/Format-Volume/robocopy

    Safety: Never writes to the C: drive. Rejects drives larger than 256GB.

    CLI mode (called from mkimage.py):
    - mkimage.ps1 -Action WriteUsb -DiskNumber 2 -SourceDir C:\files -Label TOOLS -SkipConfirm
    - mkimage.ps1 -Action CreateImg -SourceDir C:\files -OutputFile C:\out.img -Label TOOLS -SizeMB 64
    - mkimage.ps1 -Action CreateIso -SourceDir C:\files -OutputFile C:\out.iso -Label TOOLS
    - mkimage.ps1  (no args = launch WinForms GUI)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File mkimage.ps1
#>

$MAX_USB_SIZE_GB = 2048

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Get-UsbDrives {
    $drives = @()
    try {
        $disks = Get-Disk | Where-Object {
            $_.BusType -eq 'USB' -and $_.Size -le ($MAX_USB_SIZE_GB * 1GB)
        }
        foreach ($disk in $disks) {
            # Safety: skip if any partition has drive letter C
            $partitions = Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue
            $hasCDrive = $partitions | Where-Object { $_.DriveLetter -eq 'C' }
            if ($hasCDrive) { continue }

            $sizeGB = [math]::Round($disk.Size / 1GB, 1)
            $drives += [PSCustomObject]@{
                Number    = $disk.Number
                Name      = "Disk $($disk.Number)"
                Size      = "${sizeGB}GB"
                SizeBytes = $disk.Size
                Model     = $disk.FriendlyName
                Path      = "\\.\PhysicalDrive$($disk.Number)"
            }
        }
    } catch {
        # Get-Disk may not be available on all systems
    }
    return $drives
}

function New-UefiImage {
    param(
        [string]$SourceDir,
        [string[]]$Includes,
        [string]$OutputFile,
        [string]$Label = "UEFITOOLS",
        [int]$SizeMB = 32,
        [switch]$Verbose,
        [string]$ProgressFile = "",
        $LogBox = $null
    )

    function Write-Log([string]$Message) {
        if ($ProgressFile) {
            $Message | Out-File -Append $ProgressFile
        } elseif ($LogBox) {
            $LogBox.AppendText("$Message`r`n")
        } else {
            Write-Host $Message
        }
    }
    function Refresh-Log() {
        if ($LogBox -and $LogBox.Parent) { $LogBox.Parent.Refresh() }
    }

    $ext = [System.IO.Path]::GetExtension($OutputFile).ToLower()
    $isImg = ($ext -eq ".img")

    # Count files and total size
    $allFiles = @()
    if (Test-Path $SourceDir) {
        $allFiles += Get-ChildItem -Path $SourceDir -Recurse -File
    }
    foreach ($inc in $Includes) {
        if (-not $inc) { continue }
        if (Test-Path $inc -PathType Leaf) { $allFiles += Get-Item $inc }
        elseif (Test-Path $inc -PathType Container) {
            $allFiles += Get-ChildItem -Path $inc -Recurse -File
        }
    }

    if ($allFiles.Count -eq 0) {
        Write-Log "[ERROR] No files found in source directory."
        return $false
    }

    $totalBytes = ($allFiles | Measure-Object -Property Length -Sum).Sum
    $totalMB = [math]::Ceiling($totalBytes / 1MB)
    $fileCount = $allFiles.Count

    # Auto-size: content + extra space (default 32MB)
    # FAT32 minimum is ~36MB usable; VHD overhead needs ~4MB extra, so 40MB floor
    $SizeMB = [math]::Max(40, $totalMB + $SizeMB)
    Write-Log "Image size: ${SizeMB}MB (${totalMB}MB content + $($SizeMB - $totalMB)MB free)"
    Write-Log "$fileCount files ($([math]::Round($totalBytes/1024))KB) to include"
    Refresh-Log

    if ($isImg) {
        return New-Fat32Image -SourceDir $SourceDir -Includes $Includes `
            -OutputFile $OutputFile -Label $Label -SizeMB $SizeMB `
            -Verbose:$Verbose -ProgressFile $ProgressFile -LogBox $LogBox
    } else {
        return New-IsoImage -SourceDir $SourceDir -Includes $Includes `
            -OutputFile $OutputFile -Label $Label `
            -Verbose:$Verbose -LogBox $LogBox
    }
}

# Create a FAT32 .img file using VHD as intermediate format.
# Creates a fixed VHD, mounts it, formats FAT32, copies files via robocopy,
# dismounts, then strips the 512-byte VHD footer to produce a raw image.
function New-Fat32Image {
    param(
        [string]$SourceDir,
        [string[]]$Includes,
        [string]$OutputFile,
        [string]$Label,
        [int]$SizeMB,
        [switch]$Verbose,
        [string]$ProgressFile = "",
        $LogBox = $null
    )

    function Write-Log([string]$Message) {
        if ($ProgressFile) {
            $Message | Out-File -Append $ProgressFile
        } elseif ($LogBox) {
            $LogBox.AppendText("$Message`r`n")
        } else {
            Write-Host $Message
        }
    }
    function Refresh-Log() {
        if ($LogBox -and $LogBox.Parent) { $LogBox.Parent.Refresh() }
    }

    $labelTrim = $Label.Substring(0, [Math]::Min($Label.Length, 11))
    $vhdPath = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.vhd'

    try {
        Write-Log "Creating VHD ($SizeMB MB)..."
        Refresh-Log

        # Create fixed-size VHD via diskpart (works on all Windows editions,
        # no Hyper-V required — unlike New-VHD which needs Hyper-V)
        # Use a script file instead of pipe to avoid broken stdin in
        # non-elevated or subprocess contexts.
        $dpScript = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.txt'
        try {
            @"
create vdisk file="$vhdPath" maximum=$SizeMB type=fixed
select vdisk file="$vhdPath"
attach vdisk
create partition primary
active
format fs=fat32 quick label=$labelTrim
assign
"@ | Set-Content -Path $dpScript -Encoding ASCII
            Write-Log "  diskpart: create + attach + format..."
            $dpOut = (diskpart /s $dpScript 2>&1) | Out-String
            if ($dpOut -notmatch "successfully formatted" -and $dpOut -notmatch "DiskPart successfully") {
                throw "diskpart failed: $($dpOut.Trim() -replace '[\r\n]+', ' | ')"
            }
        } finally {
            Remove-Item $dpScript -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1

        # Find the assigned drive letter from the attached VHD
        $driveLetter = $null
        $vhdDisks = Get-Disk | Where-Object { $_.Location -eq $vhdPath -or $_.FriendlyName -match 'Msft Virtual Disk' }
        foreach ($vd in $vhdDisks) {
            $parts = Get-Partition -DiskNumber $vd.Number -ErrorAction SilentlyContinue
            foreach ($p in $parts) {
                if ($p.DriveLetter -and $p.DriveLetter -ne "`0") {
                    $driveLetter = $p.DriveLetter
                    break
                }
            }
            if ($driveLetter) { break }
        }

        if (-not $driveLetter) {
            throw "Could not find drive letter for attached VHD"
        }
        Write-Log "  Mounted as ${driveLetter}:"

        $destRoot = "${driveLetter}:\"
        Write-Log "Copying files to ${driveLetter}:..."
        Refresh-Log

        # Copy source directory
        if (Test-Path $SourceDir -PathType Container) {
            $srcNorm = $SourceDir.TrimEnd('\', '/')
            if ($Verbose) {
                $rcOut = robocopy $srcNorm $destRoot /S /E /NP /NJH /NJS 2>&1
                foreach ($line in $rcOut) {
                    $t = ("$line").Trim()
                    if ($t) { Write-Log "  $t" }
                }
            } else {
                robocopy $srcNorm $destRoot /S /E /NP /NFL /NDL /NJH /NJS 2>&1 | Out-Null
            }
        }

        # Copy includes
        foreach ($inc in $Includes) {
            if (-not $inc) { continue }
            if (Test-Path $inc -PathType Leaf) {
                $fileName = Split-Path $inc -Leaf
                $srcDir = Split-Path $inc -Parent
                if ($Verbose) { Write-Log "  $fileName" }
                robocopy $srcDir $destRoot $fileName /NJH /NJS /NP 2>&1 | Out-Null
            } elseif (Test-Path $inc -PathType Container) {
                $incNorm = $inc.TrimEnd('\', '/')
                if ($Verbose) {
                    $rcOut = robocopy $incNorm $destRoot /S /E /NP /NJH /NJS 2>&1
                    foreach ($line in $rcOut) {
                        $t = ("$line").Trim()
                        if ($t) { Write-Log "  $t" }
                    }
                } else {
                    robocopy $incNorm $destRoot /S /E /NP /NFL /NDL /NJH /NJS 2>&1 | Out-Null
                }
            }
        }

        $copiedCount = (Get-ChildItem $destRoot -Recurse -File).Count
        Write-Log "Copied $copiedCount files to image"

        # Detach VHD via diskpart (script file, not pipe)
        Write-Log "Detaching VHD..."
        $dpScript2 = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.txt'
        try {
            @"
select vdisk file="$vhdPath"
detach vdisk
"@ | Set-Content -Path $dpScript2 -Encoding ASCII
            (diskpart /s $dpScript2 2>&1) | Out-Null
        } finally {
            Remove-Item $dpScript2 -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1

        # Strip the 512-byte VHD footer to produce a raw FAT32 image
        Write-Log "Converting to raw image..."
        $vhdSize = (Get-Item $vhdPath).Length
        $rawSize = $vhdSize - 512  # VHD fixed footer is exactly 512 bytes
        $fs = [System.IO.File]::OpenRead($vhdPath)
        $outFs = [System.IO.File]::Create($OutputFile)
        $buffer = New-Object byte[] (4 * 1MB)
        $remaining = $rawSize
        while ($remaining -gt 0) {
            $toRead = [Math]::Min($buffer.Length, $remaining)
            $read = $fs.Read($buffer, 0, $toRead)
            $outFs.Write($buffer, 0, $read)
            $remaining -= $read
        }
        $fs.Close()
        $outFs.Close()

        $size = (Get-Item $OutputFile).Length
        Write-Log "[OK] Created $OutputFile ($([math]::Round($size/1KB))KB, FAT32)"
        return $true

    } catch {
        Write-Log "[ERROR] $_"
        # Cleanup: detach if still attached
        $dpScript3 = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.txt'
        try {
            @"
select vdisk file="$vhdPath"
detach vdisk
"@ | Set-Content -Path $dpScript3 -Encoding ASCII
            (diskpart /s $dpScript3 2>&1) | Out-Null
        } finally {
            Remove-Item $dpScript3 -ErrorAction SilentlyContinue
        }
        return $false
    } finally {
        Remove-Item $vhdPath -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Multi-partition disk image (MBR or GPT) via a diskpart-driven fixed VHD.
# Each partition gets its own filesystem / size / label / cluster / source.
# Produces a raw .img by stripping the 512-byte VHD footer. diskpart can make
# fat32/ntfs/exfat (and a GPT ESP); udf/ext4 are not natively supported here.
# ---------------------------------------------------------------------------
function New-DiskImage {
    param(
        [string]$OutputFile,
        [string]$PartStyle = 'GPT',   # 'MBR' or 'GPT'
        [object[]]$Partitions,        # @{ Fs; SizeMB; Label; Cluster; Source }
        [switch]$Verbose,
        [string]$ProgressFile = "",
        $LogBox = $null
    )

    function Write-Log([string]$Message) {
        if ($ProgressFile) { $Message | Out-File -Append $ProgressFile }
        elseif ($LogBox) { $LogBox.AppendText("$Message`r`n") }
        else { Write-Host $Message }
    }
    function Refresh-Log() { if ($LogBox -and $LogBox.Parent) { $LogBox.Parent.Refresh() } }

    if (-not $Partitions -or $Partitions.Count -eq 0) {
        Write-Log "[ERROR] No partitions specified."; return $false
    }
    $gpt = ($PartStyle -eq 'GPT')

    # Normalize inputs (GUI hashtables or CLI JSON objects) into working
    # records; resolve filesystem + size (auto -> content + slack).
    $norm = @()
    foreach ($p in $Partitions) {
        $fs = "$($p.Fs)".ToLower()
        if ($fs -in @('udf', 'ext4', 'ext2', 'ext3')) {
            Write-Log "[ERROR] Filesystem '$fs' is not supported natively on Windows."; return $false
        }
        $isEsp = ($fs -eq 'esp')
        $effFs = if ($isEsp) { 'fat32' } else { $fs }
        $minMB = if ($effFs -eq 'fat32') { 34 } else { 16 }  # FAT32 needs ~33MB min
        $sizeMB = if ($p.SizeMB) { [int]$p.SizeMB } else { 0 }
        if ($sizeMB -le 0) {
            $bytes = 0
            if ($p.Source -and (Test-Path "$($p.Source)")) {
                $bytes = (Get-ChildItem -LiteralPath "$($p.Source)" -Recurse -File -ErrorAction SilentlyContinue |
                          Measure-Object -Property Length -Sum).Sum
            }
            $sizeMB = [Math]::Max($minMB, [int][Math]::Ceiling(($bytes / 1MB)) + 16)
        } elseif ($sizeMB -lt $minMB) {
            $sizeMB = $minMB
        }
        $norm += [PSCustomObject]@{
            Fs      = $effFs
            Esp     = $isEsp
            SizeMB  = $sizeMB
            Label   = "$($p.Label)"
            Cluster = if ($p.Cluster) { [int]$p.Cluster } else { 0 }
            Source  = "$($p.Source)"
        }
    }
    $Partitions = $norm

    $totalMB = (($Partitions | Measure-Object -Property SizeMB -Sum).Sum) + 4 + $Partitions.Count

    $vhdPath = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.vhd'
    $dpScript = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.txt'

    try {
        Write-Log "Creating $PartStyle image ($($Partitions.Count) partition(s), ~$totalMB MB)..."
        Refresh-Log

        $lines = @(
            "create vdisk file=`"$vhdPath`" maximum=$totalMB type=fixed"
            "select vdisk file=`"$vhdPath`""
            "attach vdisk"
        )
        if ($gpt) { $lines += "convert gpt" }
        for ($i = 0; $i -lt $Partitions.Count; $i++) {
            $p = $Partitions[$i]
            $lbl = "$($p.Label)"
            $lblMax = if ($p.Fs -eq 'fat32') { 11 } else { 32 }
            $lbl = $lbl.Substring(0, [Math]::Min($lbl.Length, $lblMax))
            # diskpart label is unquoted (matches New-Fat32Image); drop spaces.
            $lbl = $lbl -replace '\s', '_'
            $fmt = "format fs=$($p.Fs) quick label=$lbl"
            if ($p.Cluster -and [int]$p.Cluster -gt 0) { $fmt += " unit=$([int]$p.Cluster)" }
            if ($gpt -and $p.Esp) {
                $lines += "create partition efi size=$([int]$p.SizeMB)"
            } else {
                $lines += "create partition primary size=$([int]$p.SizeMB)"
                if (-not $gpt -and $i -eq 0) { $lines += "active" }
            }
            $lines += $fmt
            $lines += "assign"   # auto-pick a free letter (avoids stale-pick collisions)
        }
        ($lines -join "`r`n") | Set-Content -Path $dpScript -Encoding ASCII

        Write-Log "  diskpart: create + attach + partition + format..."
        Refresh-Log
        $dpOut = (diskpart /s $dpScript 2>&1) | Out-String
        if (($dpOut -split 'successfully formatted').Count - 1 -lt $Partitions.Count) {
            throw "diskpart did not format all partitions: $($dpOut.Trim() -replace '[\r\n]+', ' | ')"
        }
        Start-Sleep -Seconds 2

        # Read back the letters diskpart assigned, in partition order. The
        # attached VHD is the Msft Virtual Disk whose Location is our file.
        $vd = Get-Disk | Where-Object { $_.Location -eq $vhdPath } | Select-Object -First 1
        if (-not $vd) { throw "Could not locate the attached VHD disk" }
        $vparts = @(Get-Partition -DiskNumber $vd.Number -ErrorAction SilentlyContinue |
                    Sort-Object PartitionNumber)
        $letters = @()
        foreach ($vp in $vparts) {
            if ($vp.DriveLetter -and $vp.DriveLetter -ne "`0") {
                $letters += "$($vp.DriveLetter)"
            } else {
                # An ESP/efi partition may not auto-assign; force one.
                try { $vp | Add-PartitionAccessPath -AssignDriveLetter -ErrorAction Stop
                      $vp2 = Get-Partition -DiskNumber $vd.Number -PartitionNumber $vp.PartitionNumber
                      $letters += "$($vp2.DriveLetter)" } catch { $letters += "" }
            }
        }
        if ($letters.Count -lt $Partitions.Count) {
            throw "Expected $($Partitions.Count) partitions but found $($letters.Count) on the VHD"
        }
        Start-Sleep -Seconds 1

        # Copy each partition's source onto its drive letter.
        for ($i = 0; $i -lt $Partitions.Count; $i++) {
            $p = $Partitions[$i]
            $dest = "$($letters[$i]):\"
            if (-not (Test-Path $dest)) { throw "Partition $($i+1) ($dest) not accessible after format" }
            if ($p.Source -and (Test-Path $p.Source -PathType Container)) {
                $srcNorm = "$($p.Source)".TrimEnd('\', '/')
                Write-Log "  Partition $($i+1) [$($p.Fs)]: copying $srcNorm -> $dest"
                Refresh-Log
                if ($Verbose) {
                    robocopy $srcNorm $dest /S /E /NP /NJH /NJS 2>&1 | ForEach-Object { $t = "$_".Trim(); if ($t) { Write-Log "    $t" } }
                } else {
                    robocopy $srcNorm $dest /S /E /NP /NFL /NDL /NJH /NJS 2>&1 | Out-Null
                }
            } else {
                Write-Log "  Partition $($i+1) [$($p.Fs)]: (empty)"
            }
        }

        # Detach, then strip the 512-byte fixed-VHD footer to a raw image.
        Write-Log "Detaching VHD..."
        "select vdisk file=`"$vhdPath`"`r`ndetach vdisk" | Set-Content -Path $dpScript -Encoding ASCII
        (diskpart /s $dpScript 2>&1) | Out-Null
        Start-Sleep -Seconds 1

        Write-Log "Converting to raw image..."
        $rawSize = (Get-Item $vhdPath).Length - 512
        $in = [System.IO.File]::OpenRead($vhdPath)
        $out = [System.IO.File]::Create($OutputFile)
        $buf = New-Object byte[] (4 * 1MB); $remaining = $rawSize
        while ($remaining -gt 0) {
            $toRead = [Math]::Min([int64]$buf.Length, $remaining)
            $read = $in.Read($buf, 0, [int]$toRead)
            if ($read -le 0) { break }
            $out.Write($buf, 0, $read); $remaining -= $read
        }
        $in.Close(); $out.Close()

        $size = (Get-Item $OutputFile).Length
        Write-Log "[OK] Created $OutputFile ($([math]::Round($size/1MB))MB, $PartStyle, $($Partitions.Count) partition(s))"
        return $true
    } catch {
        Write-Log "[ERROR] $_"
        "select vdisk file=`"$vhdPath`"`r`ndetach vdisk" | Set-Content -Path $dpScript -Encoding ASCII
        (diskpart /s $dpScript 2>&1) | Out-Null
        return $false
    } finally {
        Remove-Item $dpScript -ErrorAction SilentlyContinue
        Remove-Item $vhdPath -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Gzip-compress a file (produces standard .gz, readable by gzip/gunzip/zcat).
# ---------------------------------------------------------------------------
function Compress-FileGzip {
    param(
        [string]$InputFile,
        [string]$OutputFile,
        [string]$ProgressFile = "",
        $LogBox = $null
    )
    function Write-Log([string]$Message) {
        if ($ProgressFile) { $Message | Out-File -Append $ProgressFile }
        elseif ($LogBox) { $LogBox.AppendText("$Message`r`n") }
        else { Write-Host $Message }
    }
    try {
        Write-Log "Compressing to $([System.IO.Path]::GetFileName($OutputFile)) (gzip)..."
        $in = [System.IO.File]::OpenRead($InputFile)
        $outFs = [System.IO.File]::Create($OutputFile)
        $gz = New-Object System.IO.Compression.GZipStream($outFs, [System.IO.Compression.CompressionMode]::Compress)
        $buf = New-Object byte[] (4 * 1MB)
        while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) { $gz.Write($buf, 0, $n) }
        $gz.Close(); $outFs.Close(); $in.Close()
        $orig = (Get-Item $InputFile).Length
        $comp = (Get-Item $OutputFile).Length
        Write-Log "[OK] Compressed $([math]::Round($orig/1MB))MB -> $([math]::Round($comp/1MB))MB"
        return $true
    } catch {
        Write-Log "[ERROR] gzip compression failed: $_"
        return $false
    }
}

# ---------------------------------------------------------------------------
# Drive tools: wipe, bad-blocks check, and image listing.
# ---------------------------------------------------------------------------

# Run a self-contained elevated PowerShell worker that appends progress lines
# (including a final OK:/ERROR: line) to $ProgressFile, polling it live to the
# log. Returns the final status line. Mirrors Write-UsbDrive's elevation flow.
function Invoke-ElevatedWorker {
    param([string]$Body, [string]$ProgressFile, $LogBox = $null)
    function WL([string]$m) { if ($LogBox) { $LogBox.AppendText("$m`r`n") } else { Write-Host $m } }
    $tmp = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.ps1'
    [System.IO.File]::WriteAllText($tmp, $Body)
    try {
        $proc = Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $tmp `
            -Verb RunAs -PassThru -WindowStyle Hidden
        if ($null -eq $proc) { WL "Aborted - Administrator access denied."; return "" }
        $read = 0
        while (-not $proc.HasExited) {
            if ($LogBox) { [System.Windows.Forms.Application]::DoEvents() }
            Start-Sleep -Milliseconds 300
            if (Test-Path $ProgressFile) {
                $all = @(Get-Content $ProgressFile -ErrorAction SilentlyContinue)
                for ($i = $read; $i -lt $all.Count; $i++) { if ($all[$i]) { WL "  $($all[$i])" } }
                $read = $all.Count
                if ($LogBox -and $LogBox.Parent) { $LogBox.Parent.Refresh() }
            }
        }
        Start-Sleep -Milliseconds 400
        $final = ""
        if (Test-Path $ProgressFile) {
            $all = @(Get-Content $ProgressFile -ErrorAction SilentlyContinue)
            for ($i = $read; $i -lt $all.Count; $i++) { if ($all[$i]) { WL "  $($all[$i])" } }
            if ($all.Count -gt 0) { $final = $all[-1].Trim() }
        }
        return $final
    } catch {
        if ($_.Exception.Message -match 'canceled by the user') { WL "Aborted - Administrator access denied." }
        else { WL "[ERROR] $_" }
        return ""
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
}

# Shared destructive-device safety: reject the C: disk and oversized disks.
function Test-DriveSafe {
    param([int]$DiskNumber, [double]$DiskSizeBytes, $LogBox = $null)
    function WL([string]$m) { if ($LogBox) { $LogBox.AppendText("$m`r`n") } else { Write-Host $m } }
    $parts = Get-Partition -DiskNumber $DiskNumber -ErrorAction SilentlyContinue
    if ($parts | Where-Object { $_.DriveLetter -eq 'C' }) {
        WL "ERROR: Disk $DiskNumber contains the C: drive. Refusing."
        if ($LogBox) { [System.Windows.Forms.MessageBox]::Show("Disk $DiskNumber contains the C: drive. Refusing.", "Safety Check Failed", "OK", "Error") }
        return $false
    }
    if ($DiskSizeBytes -gt ($MAX_USB_SIZE_GB * 1GB)) {
        WL "ERROR: Disk $DiskNumber larger than ${MAX_USB_SIZE_GB}GB. Refusing."
        return $false
    }
    return $true
}

# Wipe all partition signatures from a disk (diskpart clean).
function Invoke-WipeDrive {
    param([int]$DiskNumber, [double]$DiskSizeBytes = 0, [string]$Model = '',
          [switch]$SkipConfirm, [string]$CliProgressFile = '', $LogBox = $null)
    function WL([string]$m) { if ($CliProgressFile) { $m | Out-File -Append $CliProgressFile } elseif ($LogBox) { $LogBox.AppendText("$m`r`n") } else { Write-Host $m } }
    if (-not (Test-DriveSafe -DiskNumber $DiskNumber -DiskSizeBytes $DiskSizeBytes -LogBox $LogBox)) { return }
    if (-not $SkipConfirm) {
        if ($LogBox) {
            $c = [System.Windows.Forms.MessageBox]::Show("WARNING: ALL DATA on \\.\PhysicalDrive$DiskNumber ($Model) WILL BE ERASED.`n`nProceed?", "Confirm Wipe", "YesNo", "Warning")
            if ($c -ne "Yes") { WL "Aborted."; return }
        } else { WL "WARNING: would wipe disk $DiskNumber. Aborted (use -SkipConfirm)."; return }
    }
    $pf = if ($CliProgressFile) { $CliProgressFile } else { [System.IO.Path]::GetTempFileName() }
    $pfEsc = $pf -replace "'", "''"
    $body = @"
try {
    '  Wiping disk __N__ (diskpart clean)...' | Out-File -Append '$pfEsc'
    Set-Disk -Number __N__ -IsReadOnly `$false -ErrorAction SilentlyContinue
    "select disk __N__`r`nclean" | diskpart | Out-Null
    'OK:wiped' | Out-File -Append '$pfEsc'
} catch { "ERROR: `$_" | Out-File -Append '$pfEsc' }
"@.Replace('__N__', "$DiskNumber")
    $final = Invoke-ElevatedWorker -Body $body -ProgressFile $pf -LogBox $LogBox
    if (-not $CliProgressFile) { Remove-Item $pf -ErrorAction SilentlyContinue }
    if ($final -match '^OK:') { WL "[OK] Disk $DiskNumber wiped." } elseif ($final -match '^ERROR:') { WL "[ERROR] $final" }
}

# Destructive bad-blocks test: write a pattern across the raw device, read it
# back, and count mismatches. (Windows has no `badblocks`.)
function Test-DriveBadBlocks {
    param([int]$DiskNumber, [double]$DiskSizeBytes = 0, [string]$Model = '',
          [switch]$SkipConfirm, [string]$CliProgressFile = '', $LogBox = $null)
    function WL([string]$m) { if ($CliProgressFile) { $m | Out-File -Append $CliProgressFile } elseif ($LogBox) { $LogBox.AppendText("$m`r`n") } else { Write-Host $m } }
    if (-not (Test-DriveSafe -DiskNumber $DiskNumber -DiskSizeBytes $DiskSizeBytes -LogBox $LogBox)) { return }
    if (-not $SkipConfirm) {
        if ($LogBox) {
            $c = [System.Windows.Forms.MessageBox]::Show("WARNING: a bad-blocks check is DESTRUCTIVE - ALL DATA on \\.\PhysicalDrive$DiskNumber ($Model) WILL BE ERASED.`n`nProceed?", "Confirm Check", "YesNo", "Warning")
            if ($c -ne "Yes") { WL "Aborted."; return }
        } else { WL "WARNING: destructive check on disk $DiskNumber. Aborted (use -SkipConfirm)."; return }
    }
    $pf = if ($CliProgressFile) { $CliProgressFile } else { [System.IO.Path]::GetTempFileName() }
    $pfEsc = $pf -replace "'", "''"
    $body = @"
`$prog = '$pfEsc'
function P([string]`$m){ `$m | Out-File -Append `$prog }
try {
    Set-Disk -Number __N__ -IsReadOnly `$false -ErrorAction SilentlyContinue
    Set-Disk -Number __N__ -IsOffline `$true -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    "select disk __N__`r`nclean" | diskpart | Out-Null
    Set-Disk -Number __N__ -IsOffline `$true -ErrorAction SilentlyContinue
    `$dev = "\\.\PhysicalDrive__N__"
    `$total = [int64](Get-Disk -Number __N__).Size
    `$chunk = 4MB
    `$pat = New-Object byte[] `$chunk
    for (`$i=0; `$i -lt `$chunk; `$i++){ `$pat[`$i] = 0xA5 }
    P "Bad-blocks check: writing pattern over `$total bytes..."
    `$w = [System.IO.FileStream]::new(`$dev,[System.IO.FileMode]::Open,[System.IO.FileAccess]::Write,[System.IO.FileShare]::ReadWrite)
    [int64]`$done = 0; `$lp = -1
    while (`$done -lt `$total) {
        `$n = [Math]::Min([int64]`$chunk, `$total - `$done)
        `$w.Write(`$pat, 0, [int]`$n); `$done += `$n
        `$pct = [int]((`$done*100)/`$total); if (`$pct -ne `$lp -and (`$pct % 25) -eq 0){ P "  write `$pct%"; `$lp=`$pct }
    }
    `$w.Flush(); `$w.Close()
    P "Verifying..."
    `$r = [System.IO.FileStream]::new(`$dev,[System.IO.FileMode]::Open,[System.IO.FileAccess]::Read,[System.IO.FileShare]::ReadWrite)
    `$buf = New-Object byte[] `$chunk; [int64]`$rd = 0; `$bad = 0; `$lp = -1
    while (`$rd -lt `$total) {
        `$want = [Math]::Min([int64]`$chunk, `$total - `$rd)
        `$got = `$r.Read(`$buf, 0, [int]`$want); if (`$got -le 0){ break }
        for (`$j=0; `$j -lt `$got; `$j++){ if (`$buf[`$j] -ne 0xA5){ `$bad++ } }
        `$rd += `$got
        `$pct = [int]((`$rd*100)/`$total); if (`$pct -ne `$lp -and (`$pct % 25) -eq 0){ P "  verify `$pct%"; `$lp=`$pct }
    }
    `$r.Close()
    Set-Disk -Number __N__ -IsOffline `$false -ErrorAction SilentlyContinue
    if (`$bad -eq 0){ P "OK:0 bad bytes" } else { P "OK:`$bad bad bytes" }
} catch {
    "ERROR: `$_" | Out-File -Append `$prog
    Set-Disk -Number __N__ -IsOffline `$false -ErrorAction SilentlyContinue
}
"@.Replace('__N__', "$DiskNumber")
    $final = Invoke-ElevatedWorker -Body $body -ProgressFile $pf -LogBox $LogBox
    if (-not $CliProgressFile) { Remove-Item $pf -ErrorAction SilentlyContinue }
    if ($final -match '^OK:(\d+)') {
        if ([int]$Matches[1] -eq 0) { WL "[OK] No bad blocks found on disk $DiskNumber." }
        else { WL "[WARN] $($Matches[1]) bad bytes on disk $DiskNumber." }
    } elseif ($final -match '^ERROR:') { WL "[ERROR] $final" }
}

# List an image's partition table (MBR/GPT) or report ISO 9660. Read-only.
function Get-ImageInfo {
    param([string]$ImagePath, [string]$CliProgressFile = '', $LogBox = $null)
    function WL([string]$m) { if ($CliProgressFile) { $m | Out-File -Append $CliProgressFile } elseif ($LogBox) { $LogBox.AppendText("$m`r`n") } else { Write-Host $m } }
    if (-not (Test-Path $ImagePath -PathType Leaf)) { WL "[ERROR] Image not found: $ImagePath"; return }
    try {
        $f = [System.IO.File]::OpenRead($ImagePath)
        $size = $f.Length
        WL "Image: $ImagePath ($([math]::Round($size/1MB))MB)"
        $s0 = New-Object byte[] 512; [void]$f.Read($s0, 0, 512)
        if ($s0[510] -eq 0x55 -and $s0[511] -eq 0xAA -and $s0[450] -eq 0xEE) {
            WL "Partition table: GPT"
            $f.Seek(512, 0) | Out-Null; $hdr = New-Object byte[] 512; [void]$f.Read($hdr, 0, 512)
            $partLba = [BitConverter]::ToUInt64($hdr, 72)
            $num = [BitConverter]::ToUInt32($hdr, 80)
            $esz = [BitConverter]::ToUInt32($hdr, 84)
            $f.Seek([long]$partLba * 512, 0) | Out-Null
            $shown = 0
            for ($i = 0; $i -lt [Math]::Min($num, 128); $i++) {
                $e = New-Object byte[] $esz; [void]$f.Read($e, 0, $esz)
                if ((($e[0..15] | Measure-Object -Sum).Sum) -eq 0) { continue }
                $first = [BitConverter]::ToUInt64($e, 32); $last = [BitConverter]::ToUInt64($e, 40)
                $szMB = [math]::Round((($last - $first + 1) * 512) / 1MB)
                $guid = [Guid]::new([byte[]]($e[0..15])).ToString()
                $nm = [System.Text.Encoding]::Unicode.GetString($e, 56, 72).Trim([char]0)
                $kind = if ($guid -eq 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b') { 'EFI System' } else { 'data' }
                WL ("  Partition {0}: {1}MB  {2}  {3}" -f (++$shown), $szMB, $kind, $nm)
            }
        } elseif ($s0[510] -eq 0x55 -and $s0[511] -eq 0xAA) {
            WL "Partition table: MBR"
            for ($i = 0; $i -lt 4; $i++) {
                $o = 446 + $i * 16; $t = $s0[$o + 4]
                if ($t -eq 0) { continue }
                $lba = [BitConverter]::ToUInt32($s0, $o + 8); $cnt = [BitConverter]::ToUInt32($s0, $o + 12)
                WL ("  Partition {0}: {1}MB  type=0x{2:X2}  start LBA {3}" -f ($i + 1), [math]::Round(($cnt * 512) / 1MB), $t, $lba)
            }
        } else {
            $f.Seek(0x8001, 0) | Out-Null; $cd = New-Object byte[] 5; [void]$f.Read($cd, 0, 5)
            if ([System.Text.Encoding]::ASCII.GetString($cd) -eq 'CD001') { WL "Format: ISO 9660" }
            else { WL "Format: raw / no partition table" }
        }
        $f.Close()
        WL "[OK] Listed image contents"
    } catch { WL "[ERROR] $_" }
}

# Create an ISO image using oscdimg.exe (Windows ADK) or a staging directory.
# Falls back to a simple copy-to-directory if no ISO tool is found.
# Compile the IStream interop helper: CopyToFile (drain a COM IStream to a
# file) and OpenFileStream (wrap a file as an IStream for IMAPI2 boot images).
function Add-IStreamCopierType {
    if ([System.Management.Automation.PSTypeName]'IStreamCopier'.Type) { return }
    Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class IStreamCopier {
    [DllImport("shlwapi.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    static extern int SHCreateStreamOnFileEx(string file, uint mode, uint attrs,
        bool create, IStream template, out IStream stream);

    public static void CopyToFile(object comStream, string path) {
        IStream src = (IStream)comStream;
        using (FileStream dst = File.Create(path)) {
            byte[] buf = new byte[65536];
            while (true) {
                IntPtr pcbRead = Marshal.AllocHGlobal(sizeof(int));
                try {
                    src.Read(buf, buf.Length, pcbRead);
                    int cbRead = Marshal.ReadInt32(pcbRead);
                    if (cbRead <= 0) break;
                    dst.Write(buf, 0, cbRead);
                } finally {
                    Marshal.FreeHGlobal(pcbRead);
                }
            }
        }
    }

    public static IStream OpenFileStream(string path) {
        IStream s;
        // STGM_READ | STGM_SHARE_DENY_WRITE
        int hr = SHCreateStreamOnFileEx(path, 0x20u, 0u, false, null, out s);
        if (hr != 0) throw new IOException("SHCreateStreamOnFileEx failed: 0x" + hr.ToString("X"));
        return s;
    }
}
"@
}


function New-IsoImage {
    param(
        [string]$SourceDir,
        [string[]]$Includes,
        [string]$OutputFile,
        [string]$Label,
        [string]$BootImage = '',
        [switch]$Udf,
        [switch]$Hybrid,
        [switch]$Verbose,
        $LogBox = $null
    )

    function Write-Log([string]$Message) {
        if ($LogBox) {
            $LogBox.AppendText("$Message`r`n")
        } else {
            Write-Host $Message
        }
    }
    function Refresh-Log() {
        if ($LogBox -and $LogBox.Parent) { $LogBox.Parent.Refresh() }
    }

    $isoLabel = $Label.Substring(0, [Math]::Min($Label.Length, 32))

    # Stage all files into a temp directory
    $staging = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "mkimage-iso-$([guid]::NewGuid().ToString('N').Substring(0,8))")
    New-Item -ItemType Directory -Path $staging -Force | Out-Null

    try {
        Write-Log "Staging files for ISO..."
        Refresh-Log

        if (Test-Path $SourceDir -PathType Container) {
            $srcNorm = $SourceDir.TrimEnd('\', '/')
            robocopy $srcNorm $staging /S /E /NP /NJH /NJS 2>&1 | Out-Null
        }
        foreach ($inc in $Includes) {
            if (-not $inc) { continue }
            if (Test-Path $inc -PathType Leaf) {
                Copy-Item -LiteralPath $inc -Destination $staging -Force
            } elseif (Test-Path $inc -PathType Container) {
                robocopy $inc.TrimEnd('\', '/') $staging /S /E /NP /NJH /NJS 2>&1 | Out-Null
            }
        }

        $stagedCount = (Get-ChildItem $staging -Recurse -File).Count
        Write-Log "Staged $stagedCount files"

        # Try oscdimg.exe (from Windows ADK)
        $oscdimg = $null
        $adkPaths = @(
            "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe",
            "${env:ProgramFiles}\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe"
        )
        foreach ($p in $adkPaths) {
            if (Test-Path $p) { $oscdimg = $p; break }
        }

        if ($oscdimg) {
            Write-Log "Creating ISO with oscdimg.exe..."
            Refresh-Log
            $args = "-l$isoLabel", "-o", "-m"
            if ($Udf) {
                # UDF 2.x bridge (ISO 9660 + UDF) — supports files larger than 4GB.
                $args += "-u2"
                Write-Log "  (UDF bridge: ISO 9660 + UDF 2.x, >4GB files)"
            }
            if ($BootImage -and (Test-Path $BootImage)) {
                # UEFI El Torito: no-emulation EFI boot from the FAT boot image
                $args += "-bootdata:1#pEF,e,b$BootImage"
                Write-Log "  (UEFI bootable: EFI El Torito)"
                if ($Hybrid) {
                    # The EFI El Torito ISO is already dd-writable to USB for UEFI
                    # boot; oscdimg has no BIOS isohybrid MBR option.
                    Write-Log "  (Hybrid: dd-writable to USB for UEFI boot)"
                }
            }
            $args += $staging, $OutputFile
            $proc = Start-Process -FilePath $oscdimg -ArgumentList $args `
                -NoNewWindow -Wait -PassThru
            if ($proc.ExitCode -ne 0) {
                Write-Log "[ERROR] oscdimg.exe failed (exit $($proc.ExitCode))"
                return $false
            }
        } else {
            # No ISO tool found -- create ISO using .NET (basic ISO 9660)
            Write-Log "oscdimg.exe not found (install Windows ADK for ISO support)"
            Write-Log "Creating ISO with built-in writer..."
            Refresh-Log

            # Use IMAPI2 COM object (available on Windows 7+).
            # FsiFileSystem flags: ISO9660=1, Joliet=2, UDF=4. The default (4 =
            # UDF) is already UEFI-bootable; -Udf adds ISO9660+Joliet to make a
            # proper bridge (broad compatibility + UDF for >4GB files).
            $fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
            $fsi.FileSystemsToCreate = if ($Udf) { 7 } else { 4 }
            if ($Udf) { Write-Log "  (UDF bridge: ISO 9660 + Joliet + UDF)" }
            $fsi.VolumeName = $isoLabel

            # Add all staged files
            $fsi.Root.AddTree($staging, $false)

            # UEFI El Torito boot (no emulation) from the FAT boot image
            if ($BootImage -and (Test-Path $BootImage)) {
                if (-not ([System.Management.Automation.PSTypeName]'IStreamCopier').Type) {
                    Add-IStreamCopierType
                }
                $bootStream = [IStreamCopier]::OpenFileStream($BootImage)
                $bopt = New-Object -ComObject IMAPI2FS.BootOptions
                $bopt.PlatformId = 0xEF   # EFI
                $bopt.Emulation = 0       # FsiBootEmulationNone
                $bopt.AssignBootImage($bootStream)
                $fsi.BootImageOptions = $bopt
                Write-Log "  (UEFI bootable: EFI El Torito)"
            }

            $resultStream = $fsi.CreateResultImage()
            $isoStream = $resultStream.ImageStream

            # Write IStream to file using .NET COM interop helper
            # (IStream::Read is not directly callable from PowerShell)
            Add-IStreamCopierType
            [IStreamCopier]::CopyToFile($isoStream, $OutputFile)
            [System.Runtime.InteropServices.Marshal]::ReleaseComObject($isoStream) | Out-Null
            [System.Runtime.InteropServices.Marshal]::ReleaseComObject($fsi) | Out-Null
        }

        $size = (Get-Item $OutputFile).Length
        Write-Log "[OK] Created $OutputFile ($([math]::Round($size/1KB))KB, ISO)"
        return $true

    } catch {
        Write-Log "[ERROR] $_"
        return $false
    } finally {
        Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Write-UsbDrive {
    param(
        [PSCustomObject]$TargetDrive,
        [string]$SourceDir,
        [string[]]$Includes,
        [string]$Label = "UEFITOOLS",
        [string]$FileSystem = "FAT32",
        [switch]$UseGpt,
        [switch]$Verbose,
        [switch]$Verify,
        [switch]$SkipConfirm,
        [string]$CliProgressFile = '',
        $LogBox = $null
    )

    # Helper: log to GUI LogBox or console
    function Write-Log([string]$Message) {
        if ($LogBox) {
            $LogBox.AppendText("$Message`r`n")
        } else {
            Write-Host $Message
        }
    }

    # Safety: reject C: drive
    $partitions = Get-Partition -DiskNumber $TargetDrive.Number -ErrorAction SilentlyContinue
    $hasCDrive = $partitions | Where-Object { $_.DriveLetter -eq 'C' }
    if ($hasCDrive) {
        if ($LogBox) {
            [System.Windows.Forms.MessageBox]::Show(
                "This disk contains the C: drive. Refusing to write.",
                "Safety Check Failed", "OK", "Error")
        } else {
            Write-Log "ERROR: This disk contains the C: drive. Refusing to write."
        }
        return
    }

    # Safety: reject > 256GB
    if ($TargetDrive.SizeBytes -gt ($MAX_USB_SIZE_GB * 1GB)) {
        if ($LogBox) {
            [System.Windows.Forms.MessageBox]::Show(
                "This disk is larger than ${MAX_USB_SIZE_GB}GB. Refusing to write.",
                "Safety Check Failed", "OK", "Error")
        } else {
            Write-Log "ERROR: This disk is larger than ${MAX_USB_SIZE_GB}GB. Refusing to write."
        }
        return
    }

    # Confirmation (skipped in CLI mode)
    if (-not $SkipConfirm) {
        if ($LogBox) {
            $confirm = [System.Windows.Forms.MessageBox]::Show(
                "WARNING: ALL DATA on $($TargetDrive.Path) ($($TargetDrive.Size) $($TargetDrive.Model)) WILL BE DESTROYED.`n`nAre you sure?",
                "Confirm Write", "YesNo", "Warning")
            if ($confirm -ne "Yes") {
                Write-Log "Aborted."
                return
            }
        } else {
            Write-Log "WARNING: ALL DATA on $($TargetDrive.Path) ($($TargetDrive.Size) $($TargetDrive.Model)) WILL BE DESTROYED."
            Write-Log "Aborted (use -SkipConfirm to proceed)."
            return
        }
    }

    $diskNum = $TargetDrive.Number
    $labelTrim = $Label.Substring(0, [Math]::Min($Label.Length, 11))

    # Use diskpart to clean, partition, and format the USB drive.
    # Then copy files directly — no raw disk write needed.
    $ownProgressFile = $false
    if ($CliProgressFile) {
        $progressFile = $CliProgressFile
    } else {
        $progressFile = [System.IO.Path]::GetTempFileName()
        $ownProgressFile = $true
    }
    $tmpScript = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.ps1'

    # Collect all source files for the elevated script
    $fileSources = @()
    if (Test-Path $SourceDir) { $fileSources += $SourceDir }
    foreach ($inc in $Includes) {
        if ($inc -and (Test-Path $inc)) { $fileSources += $inc }
    }
    $fileSourcesStr = ($fileSources | ForEach-Object { "'$($_ -replace "'","''")'" }) -join ","

    $writeScript = @'
try {
    "Preparing USB drive (disk __DISKNUM__)... [native Windows - no WSL]" | Out-File -Append __PROGRESS__

    $useGpt = __USEGPT__
    $partStyle = if ($useGpt) { "GPT" } else { "MBR" }

    # Step 1: Take disk offline, clean + convert via diskpart while offline.
    # Windows auto-creates an MBR when bringing a clean disk online,
    # so we must do clean + convert in one diskpart session while offline.
    "  Taking disk offline..." | Out-File -Append __PROGRESS__
    Set-Disk -Number __DISKNUM__ -IsOffline $true -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    # Step 1a: clean the disk (clean all wipes entire disk including
    # stale GPT backup headers and filesystem signatures)
    $dpClean = @"
select disk __DISKNUM__
clean all
"@
    "  diskpart: clean all (wiping signatures)..." | Out-File -Append __PROGRESS__
    ($dpClean | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 2

    # Step 1b: convert to target type. After clean, the disk retains
    # its previous partition style. convert only works when switching,
    # so we try it and ignore failure (means it's already correct).
    $dpConvert = @"
select disk __DISKNUM__
convert $($partStyle.ToLower())
"@
    "  diskpart: convert $partStyle..." | Out-File -Append __PROGRESS__
    ($dpConvert | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1

    # Step 1c: create partition + format
    if ($useGpt) {
        $dpSetup = @"
select disk __DISKNUM__
create partition primary
select partition 1
format fs=__FILESYSTEM__ quick label=__LABEL__
"@
    } else {
        $dpSetup = @"
select disk __DISKNUM__
create partition primary
active
format fs=__FILESYSTEM__ quick label=__LABEL__
"@
    }
    "  diskpart: create + format..." | Out-File -Append __PROGRESS__
    $dpOut = ($dpSetup | diskpart 2>&1) | Out-String
    $dpSummary = $dpOut.Trim() -replace '[\r\n]+', ' | '
    "  diskpart: $dpSummary" | Out-File -Append __PROGRESS__
    Start-Sleep -Seconds 1

    if ($dpOut -notmatch "successfully formatted") {
        throw "diskpart failed. Output: $dpSummary"
    }

    # Disable automount, then bring online
    "  Disabling automount, bringing disk online..." | Out-File -Append __PROGRESS__
    "automount disable" | diskpart 2>&1 | Out-Null
    Set-Disk -Number __DISKNUM__ -IsOffline $false -ErrorAction SilentlyContinue
    Set-Disk -Number __DISKNUM__ -IsReadOnly $false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    # Get the partition and check if it already has a drive letter
    $part = Get-Partition -DiskNumber __DISKNUM__ -ErrorAction SilentlyContinue |
        Where-Object { $_.Type -ne 'Reserved' -and $_.Type -ne 'System' } |
        Select-Object -First 1
    if (-not $part) { throw "No partition found after diskpart" }

    $driveLetter = $part.DriveLetter
    if ($driveLetter -and $driveLetter -ne "`0") {
        "  Partition already has drive letter ${driveLetter}:" | Out-File -Append __PROGRESS__
    } else {
        # Assign a free letter
        $used = @((Get-CimInstance Win32_LogicalDisk).DeviceID -replace ':', '')
        $driveLetter = (69..90 | ForEach-Object { [char]$_ } |
            Where-Object { "$_" -notin $used } | Select-Object -First 1)
        if (-not $driveLetter) { throw "No free drive letters" }
        "  Add-PartitionAccessPath ${driveLetter}:\" | Out-File -Append __PROGRESS__
        $part | Add-PartitionAccessPath -AccessPath "${driveLetter}:\" -ErrorAction Stop
    }
    Start-Sleep -Seconds 3

    $destRoot = "${driveLetter}:\"
    if (Test-Path $destRoot) {
        "  Drive ${driveLetter}: accessible (FAT32)" | Out-File -Append __PROGRESS__
    } else {
        throw "Drive ${driveLetter}: not accessible after mount"
    }

    # Copy files using robocopy (built into Windows, reliable, per-file logging)
    $sources = @(__SOURCES__)
    $verbose = __VERBOSE__
    $totalFiles = 0
    foreach ($s in $sources) {
        if (-not $s) { continue }
        if (Test-Path $s -PathType Leaf) { $totalFiles++ }
        elseif (Test-Path $s -PathType Container) {
            $totalFiles += (Get-ChildItem -LiteralPath $s -Recurse -File).Count
        }
    }
    "Copying $totalFiles files from $($sources.Count) source path(s) to ${destRoot} [robocopy]..." | Out-File -Append __PROGRESS__
    $totalCopied = 0
    $totalFailed = 0

    foreach ($src in $sources) {
        if (-not $src) { continue }
        if (Test-Path $src -PathType Leaf) {
            $fileName = Split-Path $src -Leaf
            $srcDir = Split-Path $src -Parent
            "  Copying file: $fileName" | Out-File -Append __PROGRESS__
            robocopy $srcDir $destRoot $fileName /NJH /NJS /NP 2>&1 | Out-Null
            $rcExit = $LASTEXITCODE
            if ($rcExit -lt 8) { $totalCopied++ } else {
                "  WARN: robocopy exit $rcExit for $fileName" | Out-File -Append __PROGRESS__
                $totalFailed++
            }
        } elseif (Test-Path $src -PathType Container) {
            $srcNorm = $src.TrimEnd('\', '/')
            "Copying directory: $srcNorm -> ${destRoot}" | Out-File -Append __PROGRESS__
            if ($verbose) {
                $rcOut = robocopy $srcNorm $destRoot /S /E /NP /NJH /NJS 2>&1
            } else {
                $rcOut = robocopy $srcNorm $destRoot /S /E /NP /NFL /NDL /NJH /NJS 2>&1
            }
            $rcExit = $LASTEXITCODE
            $rcText = ($rcOut | Out-String).Trim()
            if ($verbose -and $rcText) {
                "ROBOCOPY output:`r`n$rcText" | Out-File -Append __PROGRESS__
            }
            if ($rcExit -lt 8) {
                $fileCount = (Get-ChildItem -LiteralPath $srcNorm -Recurse -File).Count
                $totalCopied += $fileCount
                "Copied $fileCount files (robocopy exit $rcExit)" | Out-File -Append __PROGRESS__
            } else {
                "robocopy FAILED for $srcNorm (exit $rcExit)`r`nOutput: $rcText" | Out-File -Append __PROGRESS__
                $totalFailed++
            }
        }
    }

    # Verify the drive is accessible
    if (Test-Path "${driveLetter}:\") {
        $fileCount = (Get-ChildItem "${driveLetter}:\" -Recurse -File -ErrorAction SilentlyContinue).Count
        "  Drive ${driveLetter}: accessible, $fileCount files on disk" | Out-File -Append __PROGRESS__
    } else {
        "  WARNING: Drive ${driveLetter}: not accessible" | Out-File -Append __PROGRESS__
    }


    # Verify files if requested
    $verify = __VERIFY__
    $verifyFailed = 0
    if ($verify -and $totalCopied -gt 0) {
        "Verifying $totalCopied files [Get-FileHash MD5]..." | Out-File -Append __PROGRESS__
        foreach ($src in $sources) {
            if (-not $src) { continue }
            if (Test-Path $src -PathType Leaf) {
                $fileName = Split-Path $src -Leaf
                $destFile = Join-Path $destRoot $fileName
                $srcHash = (Get-FileHash -LiteralPath $src -Algorithm MD5).Hash
                if (Test-Path $destFile) {
                    $dstHash = (Get-FileHash -LiteralPath $destFile -Algorithm MD5).Hash
                    if ($srcHash -ne $dstHash) {
                        "  VERIFY FAIL: $fileName (hash mismatch)" | Out-File -Append __PROGRESS__
                        $verifyFailed++
                    } else {
                        if ($verbose) { "  VERIFY OK: $fileName" | Out-File -Append __PROGRESS__ }
                    }
                } else {
                    "  VERIFY FAIL: $fileName (missing on USB)" | Out-File -Append __PROGRESS__
                    $verifyFailed++
                }
            } elseif (Test-Path $src -PathType Container) {
                $srcNorm = $src.TrimEnd('\', '/')
                $files = Get-ChildItem -LiteralPath $srcNorm -Recurse -File
                foreach ($f in $files) {
                    $relPath = $f.FullName.Substring($srcNorm.Length + 1)
                    $destFile = Join-Path $destRoot $relPath
                    if (Test-Path $destFile) {
                        $srcHash = (Get-FileHash -LiteralPath $f.FullName -Algorithm MD5).Hash
                        $dstHash = (Get-FileHash -LiteralPath $destFile -Algorithm MD5).Hash
                        if ($srcHash -ne $dstHash) {
                            "  VERIFY FAIL: $relPath (hash mismatch)" | Out-File -Append __PROGRESS__
                            $verifyFailed++
                        } else {
                            if ($verbose) { "  VERIFY OK: $relPath" | Out-File -Append __PROGRESS__ }
                        }
                    } else {
                        "  VERIFY FAIL: $relPath (missing on USB)" | Out-File -Append __PROGRESS__
                        $verifyFailed++
                    }
                }
            }
        }
        if ($verifyFailed -eq 0) {
            "Verification passed: all $totalCopied files match" | Out-File -Append __PROGRESS__
        } else {
            "Verification FAILED: $verifyFailed file(s) differ" | Out-File -Append __PROGRESS__
        }
    }

    if ($totalFailed -gt 0 -or $verifyFailed -gt 0) {
        "OK:${totalCopied}:WARN:${totalFailed} copy errors, ${verifyFailed} verify errors" | Out-File -Append __PROGRESS__
    } else {
        "OK:$totalCopied" | Out-File -Append __PROGRESS__
    }
} catch {
    "ERROR: $_" | Out-File -Append __PROGRESS__
} finally {
    # Always re-enable automount
    "automount enable" | diskpart 2>&1 | Out-Null
}
'@
    $writeScript = $writeScript.Replace('__PROGRESS__', "'$($progressFile -replace "'","''")'")
    $writeScript = $writeScript.Replace('__DISKNUM__', "$diskNum")
    $writeScript = $writeScript.Replace('__LABEL__', $labelTrim)
    $writeScript = $writeScript.Replace('__FILESYSTEM__', $FileSystem.ToLower())
    $writeScript = $writeScript.Replace('__SOURCES__', $fileSourcesStr)
    $writeScript = $writeScript.Replace('__VERBOSE__', "$(if ($Verbose) { '$true' } else { '$false' })")
    $writeScript = $writeScript.Replace('__VERIFY__', "$(if ($Verify) { '$true' } else { '$false' })")
    $writeScript = $writeScript.Replace('__USEGPT__', "$(if ($UseGpt) { '$true' } else { '$false' })")
    [System.IO.File]::WriteAllText($tmpScript, $writeScript)

    # Log the operation summary
    Write-Log "Target: Disk $diskNum ($($TargetDrive.Size) $($TargetDrive.Model))"
    Write-Log "Source: $SourceDir"
    if ($Includes.Count -gt 0) {
        Write-Log "Includes: $($Includes -join ', ')"
    }
    Write-Log "Label: $Label"
    Write-Log "Formatting disk $diskNum as FAT32 and copying files..."
    if ($LogBox) { $LogBox.Parent.Refresh() }

    try {
        Write-Log "Requesting Administrator access..."
        if ($LogBox) { $LogBox.Parent.Refresh() }

        $proc = Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $tmpScript `
            -Verb RunAs -PassThru -WindowStyle Hidden

        if ($null -eq $proc) {
            Write-Log "Aborted - Administrator access denied."
            return
        }

        # Poll progress file for new lines while subprocess runs
        $linesRead = 0
        while (-not $proc.HasExited) {
            if ($LogBox) { [System.Windows.Forms.Application]::DoEvents() }
            Start-Sleep -Milliseconds 300
            if (Test-Path $progressFile) {
                $allLines = @(Get-Content $progressFile -ErrorAction SilentlyContinue)
                if ($allLines.Count -gt $linesRead) {
                    for ($i = $linesRead; $i -lt $allLines.Count; $i++) {
                        $line = $allLines[$i]
                        if ($line) { Write-Log "  $line" }
                    }
                    $linesRead = $allLines.Count
                    if ($LogBox) { $LogBox.Parent.Refresh() }
                }
            }
        }

        # Read any remaining lines after process exits
        Start-Sleep -Milliseconds 500
        $finalMsg = ""
        if (Test-Path $progressFile) {
            $allLines = @(Get-Content $progressFile -ErrorAction SilentlyContinue)
            if ($allLines.Count -gt $linesRead) {
                for ($i = $linesRead; $i -lt $allLines.Count; $i++) {
                    $line = $allLines[$i]
                    if ($line) { Write-Log "  $line" }
                }
            }
            # Last line is the status
            $finalMsg = if ($allLines.Count -gt 0) { $allLines[-1].Trim() } else { "" }
        }
        if ($finalMsg -match "^OK:(\d+)") {
            $count = $Matches[1]
            if ($LogBox) {
                # GUI mode: try to open the drive in Explorer
                $driveLine = $allLines | Where-Object { $_ -match "Assigned drive letter: (.):$" } | Select-Object -Last 1
                $usbLetter = $null
                if ($driveLine -match "Assigned drive letter: (.):") { $usbLetter = $Matches[1] }

                if ($usbLetter -and (Test-Path "${usbLetter}:\")) {
                    Write-Log "  Opening ${usbLetter}: in Explorer..."
                    Start-Process "explorer.exe" "${usbLetter}:\" -ErrorAction SilentlyContinue
                } else {
                    Write-Log "  NOTE: Eject and re-insert the USB drive to see it in Explorer."
                }
            }
            Write-Log "[OK] Wrote $count files to USB drive ($($TargetDrive.Size)). You can safely remove it."
        } elseif ($finalMsg -match "^ERROR:") {
            Write-Log "[ERROR] $finalMsg"
        } elseif ($finalMsg) {
            Write-Log "  $finalMsg"
        } else {
            Write-Log "[ERROR] Write subprocess produced no output. Check if Administrator access was granted."
        }
    } catch {
        if ($_.Exception.Message -match "canceled by the user") {
            Write-Log "Aborted - Administrator access denied."
        } else {
            Write-Log "[ERROR] $_"
        }
    } finally {
        Remove-Item $tmpScript -ErrorAction SilentlyContinue
        if ($ownProgressFile) {
            Remove-Item $progressFile -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# Raw disk clone: device <-> image file, all directions. The actual raw I/O
# runs in an elevated worker (PhysicalDrive access + Set-Disk need admin),
# using the same progress-file polling pattern as Write-UsbDrive.
#
# Modes (by which of SourceDiskNum / DestDiskNum are >= 0):
#   device -> file   clone a USB/disk to an .img   (SourceDiskNum set)
#   file   -> device write an .img to a USB/disk   (DestDiskNum set)
#   device -> device clone one disk to another     (both set)
# ---------------------------------------------------------------------------
function Copy-DiskImage {
    param(
        [string]$SourcePath = "",     # image file path (file source), else ""
        [int]$SourceDiskNum = -1,     # source PhysicalDrive number, else -1
        [double]$SourceSizeBytes = 0, # source disk size (required for device source)
        [string]$DestPath = "",       # image file path (file dest), else ""
        [int]$DestDiskNum = -1,       # dest PhysicalDrive number, else -1
        [double]$DestSizeBytes = 0,   # dest disk size (device dest, for safety)
        [string]$DestModel = "",
        [switch]$Verify,
        [switch]$SkipConfirm,
        [string]$CliProgressFile = "",
        $LogBox = $null
    )

    function Write-Log([string]$Message) {
        if ($LogBox) { $LogBox.AppendText("$Message`r`n") } else { Write-Host $Message }
    }

    if ($SourceDiskNum -lt 0 -and -not $SourcePath) {
        Write-Log "ERROR: no clone source specified."; return
    }
    if ($DestDiskNum -lt 0 -and -not $DestPath) {
        Write-Log "ERROR: no clone destination specified."; return
    }

    # Destructive-device-write safety (mirrors Write-UsbDrive).
    if ($DestDiskNum -ge 0) {
        $parts = Get-Partition -DiskNumber $DestDiskNum -ErrorAction SilentlyContinue
        if ($parts | Where-Object { $_.DriveLetter -eq 'C' }) {
            Write-Log "ERROR: Destination disk contains the C: drive. Refusing to write."
            if ($LogBox) { [System.Windows.Forms.MessageBox]::Show("Destination disk contains the C: drive. Refusing.", "Safety Check Failed", "OK", "Error") }
            return
        }
        if ($DestSizeBytes -gt ($MAX_USB_SIZE_GB * 1GB)) {
            Write-Log "ERROR: Destination disk larger than ${MAX_USB_SIZE_GB}GB. Refusing to write."
            return
        }
        # An image-file source must fit on the destination disk.
        if ($SourceDiskNum -lt 0 -and (Test-Path $SourcePath)) {
            $srcLen = (Get-Item $SourcePath).Length
            if ($srcLen -gt $DestSizeBytes) {
                Write-Log "ERROR: Image ($srcLen bytes) is larger than the destination disk ($DestSizeBytes bytes)."
                return
            }
        }
        if (-not $SkipConfirm) {
            if ($LogBox) {
                $c = [System.Windows.Forms.MessageBox]::Show(
                    "WARNING: ALL DATA on \\.\PhysicalDrive$DestDiskNum ($DestModel) WILL BE DESTROYED.`n`nProceed?",
                    "Confirm Clone", "YesNo", "Warning")
                if ($c -ne "Yes") { Write-Log "Aborted."; return }
            } else {
                Write-Log "WARNING: would destroy all data on disk $DestDiskNum. Aborted (use -SkipConfirm)."
                return
            }
        }
    }

    $ownProgressFile = $false
    if ($CliProgressFile) {
        $progressFile = $CliProgressFile
    } else {
        $progressFile = [System.IO.Path]::GetTempFileName()
        $ownProgressFile = $true
    }
    $tmpScript = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.ps1'

    $worker = @'
$prog = '__PROGRESS__'
function P([string]$m){ $m | Out-File -Append $prog }
$srcDisk = __SRCDISK__
$dstDisk = __DSTDISK__
try {
    $srcName = if ($srcDisk -ge 0) { "\\.\PhysicalDrive$srcDisk" } else { '__SRCPATH__' }
    $dstName = if ($dstDisk -ge 0) { "\\.\PhysicalDrive$dstDisk" } else { '__DSTPATH__' }
    $declTotal = [int64]'__SIZE__'
    $doVerify = __VERIFY__
    $sector = 512

    # Offline the destination disk so raw sector writes are permitted, and wipe
    # its partition table so nothing auto-mounts mid-write.
    if ($dstDisk -ge 0) {
        P "Taking destination disk $dstDisk offline..."
        Set-Disk -Number $dstDisk -IsReadOnly $false -ErrorAction SilentlyContinue
        Set-Disk -Number $dstDisk -IsOffline $true -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        "select disk $dstDisk`r`nclean" | diskpart | Out-Null
        Start-Sleep -Seconds 1
        Set-Disk -Number $dstDisk -IsOffline $true -ErrorAction SilentlyContinue
    }
    if ($srcDisk -ge 0) {
        P "Taking source disk $srcDisk offline (consistent read)..."
        Set-Disk -Number $srcDisk -IsOffline $true -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }

    $in  = [System.IO.FileStream]::new($srcName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    $outMode = if ($dstDisk -ge 0) { [System.IO.FileMode]::Open } else { [System.IO.FileMode]::Create }
    $out = [System.IO.FileStream]::new($dstName, $outMode, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)

    # total bytes: declared size for a device source, file length for a file.
    $total = if ($srcDisk -ge 0) { $declTotal } else { $in.Length }
    $buf = New-Object byte[] (4MB)
    [int64]$done = 0
    $lastPct = -1
    P "Cloning $total bytes: $srcName -> $dstName"
    while ($done -lt $total) {
        $want = [Math]::Min([int64]$buf.Length, $total - $done)
        $read = $in.Read($buf, 0, [int]$want)
        if ($read -le 0) { break }
        $writeLen = $read
        if ($dstDisk -ge 0 -and ($writeLen % $sector) -ne 0) {
            $pad = $sector - ($writeLen % $sector)
            for ($z = $writeLen; $z -lt $writeLen + $pad; $z++) { $buf[$z] = 0 }
            $writeLen += $pad
        }
        $out.Write($buf, 0, [int]$writeLen)
        $done += $read
        $pct = [int](($done * 100) / $total)
        if ($pct -ne $lastPct -and ($pct % 5) -eq 0) { P "  $pct% ($done / $total bytes)"; $lastPct = $pct }
    }
    $out.Flush(); $out.Close(); $in.Close()
    P "Copy complete: $done bytes"

    if ($doVerify) {
        P "Verifying $total bytes (SHA256)..."
        function HashN($name, $n) {
            $f = [System.IO.FileStream]::new($name, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            $h = [System.Security.Cryptography.SHA256]::Create(); $b = New-Object byte[] (4MB); [int64]$d = 0
            while ($d -lt $n) { $w = [Math]::Min([int64]$b.Length, $n - $d); $r = $f.Read($b, 0, [int]$w); if ($r -le 0) { break }; [void]$h.TransformBlock($b, 0, $r, $null, 0); $d += $r }
            [void]$h.TransformFinalBlock((New-Object byte[] 0), 0, 0); $f.Close()
            return ([BitConverter]::ToString($h.Hash))
        }
        # Dest device is still offline here, so we read back the raw sectors.
        $sh = HashN $srcName $total
        $dh = HashN $dstName $total
        if ($sh -eq $dh) { P "  VERIFY OK" } else { throw "VERIFY FAIL (hash mismatch)" }
    }

    P "OK:$done"
} catch {
    "ERROR: $_" | Out-File -Append $prog
} finally {
    if ($dstDisk -ge 0) { Set-Disk -Number $dstDisk -IsOffline $false -ErrorAction SilentlyContinue }
    if ($srcDisk -ge 0) { Set-Disk -Number $srcDisk -IsOffline $false -ErrorAction SilentlyContinue }
}
'@
    $worker = $worker.Replace('__PROGRESS__', ($progressFile -replace "'", "''"))
    $worker = $worker.Replace('__SRCDISK__', "$SourceDiskNum")
    $worker = $worker.Replace('__DSTDISK__', "$DestDiskNum")
    $worker = $worker.Replace('__SRCPATH__', ($SourcePath -replace "'", "''"))
    $worker = $worker.Replace('__DSTPATH__', ($DestPath -replace "'", "''"))
    $worker = $worker.Replace('__SIZE__', "$([int64]$SourceSizeBytes)")
    $worker = $worker.Replace('__VERIFY__', "$(if ($Verify) { '$true' } else { '$false' })")
    [System.IO.File]::WriteAllText($tmpScript, $worker)

    $srcDesc = if ($SourceDiskNum -ge 0) { "Disk $SourceDiskNum" } else { $SourcePath }
    $dstDesc = if ($DestDiskNum -ge 0) { "Disk $DestDiskNum ($DestModel)" } else { $DestPath }
    Write-Log "Clone: $srcDesc -> $dstDesc"

    try {
        Write-Log "Requesting Administrator access..."
        if ($LogBox) { $LogBox.Parent.Refresh() }
        $proc = Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $tmpScript `
            -Verb RunAs -PassThru -WindowStyle Hidden
        if ($null -eq $proc) { Write-Log "Aborted - Administrator access denied."; return }

        $linesRead = 0
        while (-not $proc.HasExited) {
            if ($LogBox) { [System.Windows.Forms.Application]::DoEvents() }
            Start-Sleep -Milliseconds 300
            if (Test-Path $progressFile) {
                $allLines = @(Get-Content $progressFile -ErrorAction SilentlyContinue)
                if ($allLines.Count -gt $linesRead) {
                    for ($i = $linesRead; $i -lt $allLines.Count; $i++) {
                        if ($allLines[$i]) { Write-Log "  $($allLines[$i])" }
                    }
                    $linesRead = $allLines.Count
                    if ($LogBox) { $LogBox.Parent.Refresh() }
                }
            }
        }
        Start-Sleep -Milliseconds 500
        $finalMsg = ""
        if (Test-Path $progressFile) {
            $allLines = @(Get-Content $progressFile -ErrorAction SilentlyContinue)
            if ($allLines.Count -gt $linesRead) {
                for ($i = $linesRead; $i -lt $allLines.Count; $i++) {
                    if ($allLines[$i]) { Write-Log "  $($allLines[$i])" }
                }
            }
            $finalMsg = if ($allLines.Count -gt 0) { $allLines[-1].Trim() } else { "" }
        }
        if ($finalMsg -match "^OK:(\d+)") {
            Write-Log "[OK] Cloned $($Matches[1]) bytes."
        } elseif ($finalMsg -match "^ERROR:") {
            Write-Log "[ERROR] $finalMsg"
        } elseif ($finalMsg) {
            Write-Log "  $finalMsg"
        } else {
            Write-Log "[ERROR] Clone subprocess produced no output (Administrator access may have been denied)."
        }
    } catch {
        if ($_.Exception.Message -match "canceled by the user") {
            Write-Log "Aborted - Administrator access denied."
        } else {
            Write-Log "[ERROR] $_"
        }
    } finally {
        Remove-Item $tmpScript -ErrorAction SilentlyContinue
        if ($ownProgressFile) { Remove-Item $progressFile -ErrorAction SilentlyContinue }
    }
}

# ---------------------------------------------------------------------------
# Main WinForms GUI
# ---------------------------------------------------------------------------

function Show-MainForm {
    # --- Theme palette --------------------------------------------------------
    $clrAccent   = [System.Drawing.Color]::FromArgb(0, 120, 215)    # Windows accent blue
    $clrAccentDk = [System.Drawing.Color]::FromArgb(0, 78, 158)
    $clrBg       = [System.Drawing.Color]::FromArgb(245, 246, 248)  # soft window bg
    $clrText     = [System.Drawing.Color]::FromArgb(32, 38, 52)     # near-black ink
    $clrBorder   = [System.Drawing.Color]::FromArgb(205, 210, 218)

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "mkimage - Bootable Media Creator"
    $form.ClientSize = New-Object System.Drawing.Size(612, 526)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedSingle"
    $form.MaximizeBox = $false
    $form.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $form.BackColor = $clrBg
    $form.KeyPreview = $true

    # --- Header banner (gradient) --------------------------------------------
    $header = New-Object System.Windows.Forms.Panel
    $header.Location = New-Object System.Drawing.Point(0, 0)
    $header.Size = New-Object System.Drawing.Size(612, 66)
    $header.Add_Paint({
        param($s, $e)
        $rect = $s.ClientRectangle
        $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
            $rect,
            [System.Drawing.Color]::FromArgb(0, 120, 215),
            [System.Drawing.Color]::FromArgb(0, 78, 158),
            [System.Drawing.Drawing2D.LinearGradientMode]::Horizontal)
        $e.Graphics.FillRectangle($brush, $rect)
        $brush.Dispose()
    })
    [void]$form.Controls.Add($header)

    $lblTitle = New-Object System.Windows.Forms.Label
    $lblTitle.Text = "mkimage"
    $lblTitle.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 17, [System.Drawing.FontStyle]::Bold)
    $lblTitle.ForeColor = [System.Drawing.Color]::White
    $lblTitle.BackColor = [System.Drawing.Color]::Transparent
    $lblTitle.AutoSize = $true
    $lblTitle.Location = New-Object System.Drawing.Point(18, 9)
    $header.Controls.Add($lblTitle)

    $lblSub = New-Object System.Windows.Forms.Label
    $lblSub.Text = "Bootable Media Creator"
    $lblSub.Font = New-Object System.Drawing.Font("Segoe UI", 9.5)
    $lblSub.ForeColor = [System.Drawing.Color]::FromArgb(208, 226, 246)
    $lblSub.BackColor = [System.Drawing.Color]::Transparent
    $lblSub.AutoSize = $true
    $lblSub.Location = New-Object System.Drawing.Point(21, 40)
    $header.Controls.Add($lblSub)

    # --- Tab control (mirrors the Dear PyGui layout) -------------------------
    $tabs = New-Object System.Windows.Forms.TabControl
    $tabs.Location = New-Object System.Drawing.Point(6, 72)
    $tabs.Size = New-Object System.Drawing.Size(600, 432)
    $tabBuild   = New-Object System.Windows.Forms.TabPage "Build"
    $tabOptions = New-Object System.Windows.Forms.TabPage "Options"
    $tabTools   = New-Object System.Windows.Forms.TabPage "Tools"
    $tabLog     = New-Object System.Windows.Forms.TabPage "Log"
    $tabHelp    = New-Object System.Windows.Forms.TabPage "Help"
    foreach ($tp in @($tabBuild, $tabOptions, $tabTools, $tabLog, $tabHelp)) {
        $tp.UseVisualStyleBackColor = $false
        $tp.BackColor = $clrBg
        [void]$tabs.TabPages.Add($tp)
    }
    [void]$form.Controls.Add($tabs)

    # Placeholders for tabs filled in by later parity phases.
    foreach ($pair in @(, @($tabHelp, 'Help & shortcuts'))) {
        $note = New-Object System.Windows.Forms.Label
        $note.Text = "$($pair[1]) -- coming in a later phase.`r`nSee docs/Native-GUI-Parity-Plan.md"
        $note.AutoSize = $true
        $note.ForeColor = [System.Drawing.Color]::FromArgb(120, 128, 140)
        $note.Location = New-Object System.Drawing.Point(18, 18)
        [void]$pair[0].Controls.Add($note)
    }

    # --- Tools tab: shared drive selector + Format / Wipe / Check / List -----
    $script:toolDrives = @()
    $lblToolDrive = New-Object System.Windows.Forms.Label
    $lblToolDrive.Text = "Drive:"
    $lblToolDrive.Location = New-Object System.Drawing.Point(12, 16)
    $lblToolDrive.AutoSize = $true
    $lblToolDrive.ForeColor = $clrAccentDk
    $lblToolDrive.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9, [System.Drawing.FontStyle]::Bold)
    $tabTools.Controls.Add($lblToolDrive)

    $cmbToolDrive = New-Object System.Windows.Forms.ComboBox
    $cmbToolDrive.Location = New-Object System.Drawing.Point(60, 13)
    $cmbToolDrive.Size = New-Object System.Drawing.Size(420, 23)
    $cmbToolDrive.DropDownStyle = "DropDownList"
    $cmbToolDrive.Font = New-Object System.Drawing.Font("Consolas", 9)
    $tabTools.Controls.Add($cmbToolDrive)

    $btnToolRefresh = New-Object System.Windows.Forms.Button
    $btnToolRefresh.Text = "Refresh"
    $btnToolRefresh.Location = New-Object System.Drawing.Point(488, 12)
    $btnToolRefresh.Size = New-Object System.Drawing.Size(90, 25)
    $btnToolRefresh.Add_Click({
        $cmbToolDrive.Items.Clear()
        $script:toolDrives = Get-UsbDrives
        if ($script:toolDrives.Count -eq 0) { $cmbToolDrive.Items.Add("(no USB drives found)") }
        else { foreach ($d in $script:toolDrives) { $cmbToolDrive.Items.Add("$($d.Path)  $($d.Size)  $($d.Model)") } }
        if ($cmbToolDrive.Items.Count -gt 0) { $cmbToolDrive.SelectedIndex = 0 }
    })
    $tabTools.Controls.Add($btnToolRefresh)

    # Helper: resolve the selected tool drive (or $null with a message box).
    $getToolDrive = {
        if ($script:toolDrives.Count -eq 0 -or $cmbToolDrive.SelectedIndex -lt 0) {
            [System.Windows.Forms.MessageBox]::Show("No USB drive selected. Click Refresh.", "Error", "OK", "Error")
            return $null
        }
        return $script:toolDrives[$cmbToolDrive.SelectedIndex]
    }

    # Format group
    $grpFmt = New-Object System.Windows.Forms.GroupBox
    $grpFmt.Text = "Format Drive"
    $grpFmt.Location = New-Object System.Drawing.Point(12, 46)
    $grpFmt.Size = New-Object System.Drawing.Size(566, 78)
    $tabTools.Controls.Add($grpFmt)

    $lblFmtFs = New-Object System.Windows.Forms.Label
    $lblFmtFs.Text = "Filesystem:"; $lblFmtFs.Location = New-Object System.Drawing.Point(12, 24); $lblFmtFs.AutoSize = $true
    $grpFmt.Controls.Add($lblFmtFs)
    $cmbFmtFs = New-Object System.Windows.Forms.ComboBox
    $cmbFmtFs.DropDownStyle = "DropDownList"; $cmbFmtFs.Items.AddRange(@("FAT32", "NTFS", "exFAT"))
    $cmbFmtFs.SelectedIndex = 0; $cmbFmtFs.Location = New-Object System.Drawing.Point(85, 21); $cmbFmtFs.Size = New-Object System.Drawing.Size(80, 23)
    $grpFmt.Controls.Add($cmbFmtFs)
    $lblFmtLabel = New-Object System.Windows.Forms.Label
    $lblFmtLabel.Text = "Label:"; $lblFmtLabel.Location = New-Object System.Drawing.Point(180, 24); $lblFmtLabel.AutoSize = $true
    $grpFmt.Controls.Add($lblFmtLabel)
    $txtFmtLabel = New-Object System.Windows.Forms.TextBox
    $txtFmtLabel.Text = "UEFITOOLS"; $txtFmtLabel.Location = New-Object System.Drawing.Point(225, 21); $txtFmtLabel.Size = New-Object System.Drawing.Size(120, 23)
    $grpFmt.Controls.Add($txtFmtLabel)
    $chkFmtGpt = New-Object System.Windows.Forms.CheckBox
    $chkFmtGpt.Text = "GPT"; $chkFmtGpt.Location = New-Object System.Drawing.Point(360, 23); $chkFmtGpt.AutoSize = $true
    $grpFmt.Controls.Add($chkFmtGpt)
    $btnFmt = New-Object System.Windows.Forms.Button
    $btnFmt.Text = "Format"; $btnFmt.Location = New-Object System.Drawing.Point(458, 20); $btnFmt.Size = New-Object System.Drawing.Size(95, 27)
    $btnFmt.Add_Click({
        $drv = & $getToolDrive; if (-not $drv) { return }
        $tabs.SelectedTab = $tabLog
        $empty = Join-Path $env:TEMP ("mkimage-empty-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Force -Path $empty | Out-Null
        $txtLog.AppendText("`r`n--- Format $($drv.Path) ($($cmbFmtFs.SelectedItem)) ---`r`n")
        Write-UsbDrive -TargetDrive $drv -SourceDir $empty -Label $txtFmtLabel.Text -FileSystem $cmbFmtFs.SelectedItem -UseGpt:$chkFmtGpt.Checked -LogBox $txtLog
        Remove-Item $empty -Recurse -Force -ErrorAction SilentlyContinue
    })
    $grpFmt.Controls.Add($btnFmt)

    # Wipe + Check group
    $grpWipe = New-Object System.Windows.Forms.GroupBox
    $grpWipe.Text = "Wipe / Check (destructive)"
    $grpWipe.Location = New-Object System.Drawing.Point(12, 132)
    $grpWipe.Size = New-Object System.Drawing.Size(566, 70)
    $tabTools.Controls.Add($grpWipe)
    $lblWipe = New-Object System.Windows.Forms.Label
    $lblWipe.Text = "Wipe removes all partition signatures. Check writes & verifies a test pattern (erases data)."
    $lblWipe.Location = New-Object System.Drawing.Point(12, 20); $lblWipe.AutoSize = $true
    $grpWipe.Controls.Add($lblWipe)
    $btnWipe = New-Object System.Windows.Forms.Button
    $btnWipe.Text = "Wipe"; $btnWipe.Location = New-Object System.Drawing.Point(348, 38); $btnWipe.Size = New-Object System.Drawing.Size(95, 25)
    $btnWipe.Add_Click({
        $drv = & $getToolDrive; if (-not $drv) { return }
        $tabs.SelectedTab = $tabLog
        $txtLog.AppendText("`r`n--- Wipe $($drv.Path) ---`r`n")
        Invoke-WipeDrive -DiskNumber $drv.Number -DiskSizeBytes $drv.SizeBytes -Model $drv.Model -LogBox $txtLog
    })
    $grpWipe.Controls.Add($btnWipe)
    $btnCheck = New-Object System.Windows.Forms.Button
    $btnCheck.Text = "Check"; $btnCheck.Location = New-Object System.Drawing.Point(458, 38); $btnCheck.Size = New-Object System.Drawing.Size(95, 25)
    $btnCheck.Add_Click({
        $drv = & $getToolDrive; if (-not $drv) { return }
        $tabs.SelectedTab = $tabLog
        $txtLog.AppendText("`r`n--- Check $($drv.Path) ---`r`n")
        Test-DriveBadBlocks -DiskNumber $drv.Number -DiskSizeBytes $drv.SizeBytes -Model $drv.Model -LogBox $txtLog
    })
    $grpWipe.Controls.Add($btnCheck)

    # List Image group
    $grpList = New-Object System.Windows.Forms.GroupBox
    $grpList.Text = "List Image Contents"
    $grpList.Location = New-Object System.Drawing.Point(12, 210)
    $grpList.Size = New-Object System.Drawing.Size(566, 66)
    $tabTools.Controls.Add($grpList)
    $txtListPath = New-Object System.Windows.Forms.TextBox
    $txtListPath.Location = New-Object System.Drawing.Point(12, 26); $txtListPath.Size = New-Object System.Drawing.Size(340, 23)
    $grpList.Controls.Add($txtListPath)
    $btnListBrowse = New-Object System.Windows.Forms.Button
    $btnListBrowse.Text = "Browse..."; $btnListBrowse.Location = New-Object System.Drawing.Point(360, 25); $btnListBrowse.Size = New-Object System.Drawing.Size(90, 25)
    $btnListBrowse.Add_Click({
        $ofd = New-Object System.Windows.Forms.OpenFileDialog
        $ofd.Filter = "Disk images (*.img;*.iso)|*.img;*.iso|All Files (*.*)|*.*"
        if ($ofd.ShowDialog() -eq "OK") { $txtListPath.Text = $ofd.FileName }
    })
    $grpList.Controls.Add($btnListBrowse)
    $btnList = New-Object System.Windows.Forms.Button
    $btnList.Text = "List"; $btnList.Location = New-Object System.Drawing.Point(458, 25); $btnList.Size = New-Object System.Drawing.Size(95, 25)
    $btnList.Add_Click({
        $p = $txtListPath.Text.Trim()
        if (-not $p) { [System.Windows.Forms.MessageBox]::Show("Select an image file.", "Error", "OK", "Error"); return }
        $tabs.SelectedTab = $tabLog
        $txtLog.AppendText("`r`n--- List $p ---`r`n")
        Get-ImageInfo -ImagePath $p -LogBox $txtLog
    })
    $grpList.Controls.Add($btnList)

    # Persistent partition (live Linux, ext4) -- not native on Windows.
    $chkPersist = New-Object System.Windows.Forms.CheckBox
    $chkPersist.Text = "Persistent partition (live Linux, ext4) -- not available on Windows"
    $chkPersist.Location = New-Object System.Drawing.Point(14, 285)
    $chkPersist.AutoSize = $true
    $chkPersist.Enabled = $false
    $tabTools.Controls.Add($chkPersist)
    $toolTip = New-Object System.Windows.Forms.ToolTip
    $toolTip.SetToolTip($chkPersist, "ext4 cannot be created natively on Windows. Use the Linux/macOS build, or the cross-platform Python GUI, for persistent live-Linux media.")

    # --- Options tab: partition scheme + multi-partition editor --------------
    $lblScheme = New-Object System.Windows.Forms.Label
    $lblScheme.Text = "Partition Scheme:"
    $lblScheme.Location = New-Object System.Drawing.Point(15, 18)
    $lblScheme.AutoSize = $true
    $lblScheme.ForeColor = $clrAccentDk
    $lblScheme.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9, [System.Drawing.FontStyle]::Bold)
    $tabOptions.Controls.Add($lblScheme)

    $rbSchemeNone = New-Object System.Windows.Forms.RadioButton
    $rbSchemeNone.Text = "None (raw FAT32)"
    $rbSchemeNone.Location = New-Object System.Drawing.Point(135, 16)
    $rbSchemeNone.AutoSize = $true
    $rbSchemeNone.Checked = $true
    $tabOptions.Controls.Add($rbSchemeNone)

    $rbSchemeMbr = New-Object System.Windows.Forms.RadioButton
    $rbSchemeMbr.Text = "MBR"
    $rbSchemeMbr.Location = New-Object System.Drawing.Point(280, 16)
    $rbSchemeMbr.AutoSize = $true
    $tabOptions.Controls.Add($rbSchemeMbr)

    $rbSchemeGpt = New-Object System.Windows.Forms.RadioButton
    $rbSchemeGpt.Text = "GPT"
    $rbSchemeGpt.Location = New-Object System.Drawing.Point(350, 16)
    $rbSchemeGpt.AutoSize = $true
    $tabOptions.Controls.Add($rbSchemeGpt)

    $lblParts = New-Object System.Windows.Forms.Label
    $lblParts.Text = "Partitions (used when scheme is MBR/GPT; blank Source falls back to the Build source):"
    $lblParts.Location = New-Object System.Drawing.Point(15, 50)
    $lblParts.AutoSize = $true
    $tabOptions.Controls.Add($lblParts)

    $grid = New-Object System.Windows.Forms.DataGridView
    $grid.Location = New-Object System.Drawing.Point(15, 72)
    $grid.Size = New-Object System.Drawing.Size(566, 188)
    $grid.AllowUserToAddRows = $false
    $grid.AllowUserToResizeRows = $false
    $grid.RowHeadersVisible = $false
    $grid.SelectionMode = [System.Windows.Forms.DataGridViewSelectionMode]::FullRowSelect
    $grid.BackgroundColor = [System.Drawing.Color]::White
    $grid.AutoSizeColumnsMode = [System.Windows.Forms.DataGridViewAutoSizeColumnsMode]::None

    $colFs = New-Object System.Windows.Forms.DataGridViewComboBoxColumn
    $colFs.HeaderText = "Filesystem"
    $colFs.Name = "Fs"
    [void]$colFs.Items.AddRange(@("esp", "fat32", "ntfs", "exfat"))
    $colFs.Width = 90
    [void]$grid.Columns.Add($colFs)

    foreach ($c in @(@("Size", "Size (MB, blank=auto)", 120),
                     @("Label", "Label", 110),
                     @("Cluster", "Cluster (bytes)", 100),
                     @("Source", "Source dir (blank=Build source)", 140))) {
        $col = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
        $col.Name = $c[0]; $col.HeaderText = $c[1]; $col.Width = $c[2]
        [void]$grid.Columns.Add($col)
    }
    $tabOptions.Controls.Add($grid)

    $btnAddPart = New-Object System.Windows.Forms.Button
    $btnAddPart.Text = "Add Partition"
    $btnAddPart.Location = New-Object System.Drawing.Point(15, 268)
    $btnAddPart.Size = New-Object System.Drawing.Size(110, 26)
    $btnAddPart.Add_Click({
        $r = $grid.Rows.Add()
        $grid.Rows[$r].Cells["Fs"].Value = "fat32"
        $grid.Rows[$r].Cells["Label"].Value = "UEFITOOLS"
    })
    $tabOptions.Controls.Add($btnAddPart)

    $btnRemovePart = New-Object System.Windows.Forms.Button
    $btnRemovePart.Text = "Remove Last"
    $btnRemovePart.Location = New-Object System.Drawing.Point(135, 268)
    $btnRemovePart.Size = New-Object System.Drawing.Size(110, 26)
    $btnRemovePart.Add_Click({
        if ($grid.Rows.Count -gt 0) { $grid.Rows.RemoveAt($grid.Rows.Count - 1) }
    })
    $tabOptions.Controls.Add($btnRemovePart)

    # ISO options (apply when Output Format is ISO on the Build tab).
    $lblIso = New-Object System.Windows.Forms.Label
    $lblIso.Text = "ISO options:"
    $lblIso.Location = New-Object System.Drawing.Point(15, 305)
    $lblIso.AutoSize = $true
    $lblIso.ForeColor = $clrAccentDk
    $lblIso.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9, [System.Drawing.FontStyle]::Bold)
    $tabOptions.Controls.Add($lblIso)

    $chkHybrid = New-Object System.Windows.Forms.CheckBox
    $chkHybrid.Text = "Hybrid ISO (dd-writable to USB)"
    $chkHybrid.Location = New-Object System.Drawing.Point(15, 327)
    $chkHybrid.AutoSize = $true
    $tabOptions.Controls.Add($chkHybrid)

    $chkUdf = New-Object System.Windows.Forms.CheckBox
    $chkUdf.Text = "UDF bridge (ISO 9660 + UDF, supports files >4GB)"
    $chkUdf.Location = New-Object System.Drawing.Point(15, 351)
    $chkUdf.AutoSize = $true
    $tabOptions.Controls.Add($chkUdf)

    # Selecting MBR/GPT seeds a sensible default partition row; None clears.
    $schemeChanged = {
        $on = -not $rbSchemeNone.Checked
        $grid.Enabled = $on; $btnAddPart.Enabled = $on; $btnRemovePart.Enabled = $on
        if ($on -and $grid.Rows.Count -eq 0) {
            if ($rbSchemeGpt.Checked) {
                $r = $grid.Rows.Add(); $grid.Rows[$r].Cells["Fs"].Value = "esp"
                $grid.Rows[$r].Cells["Label"].Value = "BOOT"
            } else {
                $r = $grid.Rows.Add(); $grid.Rows[$r].Cells["Fs"].Value = "fat32"
                $grid.Rows[$r].Cells["Label"].Value = "UEFITOOLS"
            }
        }
    }
    $rbSchemeNone.Add_CheckedChanged($schemeChanged)
    $rbSchemeMbr.Add_CheckedChanged($schemeChanged)
    $rbSchemeGpt.Add_CheckedChanged($schemeChanged)
    & $schemeChanged

    # --- Native status bar (docked bottom): status text left, progress right --
    $statusStrip = New-Object System.Windows.Forms.StatusStrip
    $statusStrip.SizingGrip = $false
    $lblStatus = New-Object System.Windows.Forms.ToolStripStatusLabel
    $lblStatus.Text = "Ready"
    $lblStatus.Spring = $true
    $lblStatus.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
    $progress = New-Object System.Windows.Forms.ToolStripProgressBar
    $progress.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
    $progress.MarqueeAnimationSpeed = 0
    $progress.Visible = $false
    [void]$statusStrip.Items.Add($lblStatus)
    [void]$statusStrip.Items.Add($progress)
    [void]$form.Controls.Add($statusStrip)

    $y = 12

    # --- Source: a folder/image file, or a whole USB drive to clone ---------
    $lblSrc = New-Object System.Windows.Forms.Label
    $lblSrc.Text = "Source:"
    $lblSrc.Location = New-Object System.Drawing.Point(15, $y)
    $lblSrc.AutoSize = $true
    $tabBuild.Controls.Add($lblSrc)

    $rbSrcFile = New-Object System.Windows.Forms.RadioButton
    $rbSrcFile.Text = "Folder / image file"
    $rbSrcFile.Location = New-Object System.Drawing.Point(80, ($y - 3))
    $rbSrcFile.AutoSize = $true
    $rbSrcFile.Checked = $true
    $tabBuild.Controls.Add($rbSrcFile)

    $rbSrcUsb = New-Object System.Windows.Forms.RadioButton
    $rbSrcUsb.Text = "USB drive (clone)"
    $rbSrcUsb.Location = New-Object System.Drawing.Point(225, ($y - 3))
    $rbSrcUsb.AutoSize = $true
    $tabBuild.Controls.Add($rbSrcUsb)

    $y += 26
    # File-mode source: a directory, .img, or .iso path.
    $txtSrc = New-Object System.Windows.Forms.TextBox
    $txtSrc.Location = New-Object System.Drawing.Point(15, $y)
    $txtSrc.Size = New-Object System.Drawing.Size(470, 23)
    $tabBuild.Controls.Add($txtSrc)

    $btnSrc = New-Object System.Windows.Forms.Button
    $btnSrc.Text = "Browse..."
    $btnSrc.Location = New-Object System.Drawing.Point(495, ($y - 1))
    $btnSrc.Size = New-Object System.Drawing.Size(90, 25)
    $btnSrc.Add_Click({
        # Folder picker by default; hold Shift while clicking to pick an image.
        if ([System.Windows.Forms.Control]::ModifierKeys -band [System.Windows.Forms.Keys]::Shift) {
            $ofd = New-Object System.Windows.Forms.OpenFileDialog
            $ofd.Title = "Select Source Image (.img/.iso)"
            $ofd.Filter = "Disk images (*.img;*.iso)|*.img;*.iso|All Files (*.*)|*.*"
            if ($ofd.ShowDialog() -eq "OK") { $txtSrc.Text = $ofd.FileName }
        } else {
            $fbd = New-Object System.Windows.Forms.FolderBrowserDialog
            $fbd.Description = "Select Source Directory (Shift+Browse to pick an image file)"
            if ($fbd.ShowDialog() -eq "OK") { $txtSrc.Text = $fbd.SelectedPath }
        }
    })
    $tabBuild.Controls.Add($btnSrc)

    # USB-mode source: pick a drive to clone FROM (hidden until selected).
    $cmbSrcDrive = New-Object System.Windows.Forms.ComboBox
    $cmbSrcDrive.Location = New-Object System.Drawing.Point(15, $y)
    $cmbSrcDrive.Size = New-Object System.Drawing.Size(470, 23)
    $cmbSrcDrive.DropDownStyle = "DropDownList"
    $cmbSrcDrive.Font = New-Object System.Drawing.Font("Consolas", 9)
    $cmbSrcDrive.Visible = $false
    $tabBuild.Controls.Add($cmbSrcDrive)

    $btnSrcRefresh = New-Object System.Windows.Forms.Button
    $btnSrcRefresh.Text = "Refresh"
    $btnSrcRefresh.Location = New-Object System.Drawing.Point(495, ($y - 1))
    $btnSrcRefresh.Size = New-Object System.Drawing.Size(90, 25)
    $btnSrcRefresh.Visible = $false
    $btnSrcRefresh.Add_Click({
        $cmbSrcDrive.Items.Clear()
        $script:srcDrives = Get-UsbDrives
        if ($script:srcDrives.Count -eq 0) {
            $cmbSrcDrive.Items.Add("(no USB drives found)")
        } else {
            foreach ($d in $script:srcDrives) {
                $cmbSrcDrive.Items.Add("$($d.Path)  $($d.Size)  $($d.Model)")
            }
        }
        if ($cmbSrcDrive.Items.Count -gt 0) { $cmbSrcDrive.SelectedIndex = 0 }
    })
    $tabBuild.Controls.Add($btnSrcRefresh)
    $script:srcDrives = @()

    # Extra includes
    $y += 35
    $lblInc = New-Object System.Windows.Forms.Label
    $lblInc.Text = "Extra Includes:"
    $lblInc.Location = New-Object System.Drawing.Point(15, $y)
    $lblInc.AutoSize = $true
    $tabBuild.Controls.Add($lblInc)

    $btnAddFile = New-Object System.Windows.Forms.Button
    $btnAddFile.Text = "Add File"
    $btnAddFile.Location = New-Object System.Drawing.Point(120, ($y - 3))
    $btnAddFile.Size = New-Object System.Drawing.Size(70, 23)
    $tabBuild.Controls.Add($btnAddFile)

    $btnAddDir = New-Object System.Windows.Forms.Button
    $btnAddDir.Text = "Add Dir"
    $btnAddDir.Location = New-Object System.Drawing.Point(195, ($y - 3))
    $btnAddDir.Size = New-Object System.Drawing.Size(70, 23)
    $tabBuild.Controls.Add($btnAddDir)

    $btnClear = New-Object System.Windows.Forms.Button
    $btnClear.Text = "Clear"
    $btnClear.Location = New-Object System.Drawing.Point(270, ($y - 3))
    $btnClear.Size = New-Object System.Drawing.Size(60, 23)
    $tabBuild.Controls.Add($btnClear)

    $y += 25
    $lstInc = New-Object System.Windows.Forms.ListBox
    $lstInc.Location = New-Object System.Drawing.Point(15, $y)
    $lstInc.Size = New-Object System.Drawing.Size(570, 65)
    $lstInc.Font = New-Object System.Drawing.Font("Consolas", 8)
    $tabBuild.Controls.Add($lstInc)

    $btnAddFile.Add_Click({
        $ofd = New-Object System.Windows.Forms.OpenFileDialog
        $ofd.Title = "Select File to Include"
        if ($ofd.ShowDialog() -eq "OK") { $lstInc.Items.Add($ofd.FileName) }
    })
    $btnAddDir.Add_Click({
        $fbd = New-Object System.Windows.Forms.FolderBrowserDialog
        $fbd.Description = "Select Directory to Include"
        if ($fbd.ShowDialog() -eq "OK") { $lstInc.Items.Add($fbd.SelectedPath) }
    })
    $btnClear.Add_Click({ $lstInc.Items.Clear() })

    # Output format
    $y += 75
    $lblFmt = New-Object System.Windows.Forms.Label
    $lblFmt.Text = "Output Format:"
    $lblFmt.Location = New-Object System.Drawing.Point(15, $y)
    $lblFmt.AutoSize = $true
    $tabBuild.Controls.Add($lblFmt)

    $rbImg = New-Object System.Windows.Forms.RadioButton
    $rbImg.Text = "FAT32 (.img)"
    $rbImg.Location = New-Object System.Drawing.Point(120, ($y - 2))
    $rbImg.AutoSize = $true
    $rbImg.Checked = $true
    $tabBuild.Controls.Add($rbImg)

    $rbIso = New-Object System.Windows.Forms.RadioButton
    $rbIso.Text = "ISO (.iso)"
    $rbIso.Location = New-Object System.Drawing.Point(250, ($y - 2))
    $rbIso.AutoSize = $true
    $tabBuild.Controls.Add($rbIso)

    # Volume label
    $y += 30
    $lblLabel = New-Object System.Windows.Forms.Label
    $lblLabel.Text = "Volume Label:"
    $lblLabel.Location = New-Object System.Drawing.Point(15, $y)
    $lblLabel.AutoSize = $true
    $tabBuild.Controls.Add($lblLabel)

    $txtLabel = New-Object System.Windows.Forms.TextBox
    $txtLabel.Text = "UEFITOOLS"
    $txtLabel.Location = New-Object System.Drawing.Point(120, ($y - 2))
    $txtLabel.Size = New-Object System.Drawing.Size(150, 23)
    $tabBuild.Controls.Add($txtLabel)

    # Filesystem
    $lblFs = New-Object System.Windows.Forms.Label
    $lblFs.Text = "Filesystem:"
    $lblFs.Location = New-Object System.Drawing.Point(290, $y)
    $lblFs.AutoSize = $true
    $tabBuild.Controls.Add($lblFs)

    $cmbFs = New-Object System.Windows.Forms.ComboBox
    $cmbFs.DropDownStyle = "DropDownList"
    $cmbFs.Items.AddRange(@("FAT32", "NTFS", "exFAT"))
    $cmbFs.SelectedIndex = 0
    $cmbFs.Location = New-Object System.Drawing.Point(370, ($y - 2))
    $cmbFs.Size = New-Object System.Drawing.Size(80, 23)
    $tabBuild.Controls.Add($cmbFs)

    # Image size
    $y += 30
    $lblSize = New-Object System.Windows.Forms.Label
    $lblSize.Text = "Extra Space (MB):"
    $lblSize.Location = New-Object System.Drawing.Point(15, $y)
    $lblSize.AutoSize = $true
    $tabBuild.Controls.Add($lblSize)

    $txtSize = New-Object System.Windows.Forms.TextBox
    $txtSize.Text = "32"
    $txtSize.Location = New-Object System.Drawing.Point(120, ($y - 2))
    $txtSize.Size = New-Object System.Drawing.Size(60, 23)
    $tabBuild.Controls.Add($txtSize)

    $rbIso.Add_CheckedChanged({ $txtSize.Enabled = -not $rbIso.Checked })

    # Build options + target toggle (one row)
    $chkVerbose = New-Object System.Windows.Forms.CheckBox
    $chkVerbose.Text = "Verbose"
    $chkVerbose.Location = New-Object System.Drawing.Point(195, ($y - 2))
    $chkVerbose.AutoSize = $true
    $tabBuild.Controls.Add($chkVerbose)

    $chkVerify = New-Object System.Windows.Forms.CheckBox
    $chkVerify.Text = "Verify"
    $chkVerify.Location = New-Object System.Drawing.Point(268, ($y - 2))
    $chkVerify.AutoSize = $true
    $tabBuild.Controls.Add($chkVerify)

    $chkGpt = New-Object System.Windows.Forms.CheckBox
    $chkGpt.Text = "GPT"
    $chkGpt.Location = New-Object System.Drawing.Point(328, ($y - 2))
    $chkGpt.AutoSize = $true
    $tabBuild.Controls.Add($chkGpt)

    $chkForce = New-Object System.Windows.Forms.CheckBox
    $chkForce.Text = "Force"
    $chkForce.Location = New-Object System.Drawing.Point(378, ($y - 2))
    $chkForce.AutoSize = $true
    $tabBuild.Controls.Add($chkForce)

    $chkUsb = New-Object System.Windows.Forms.CheckBox
    $chkUsb.Text = "Write to USB"
    $chkUsb.Location = New-Object System.Drawing.Point(440, ($y - 2))
    $chkUsb.AutoSize = $true
    $tabBuild.Controls.Add($chkUsb)

    # Output target
    $y += 30
    $lblOut = New-Object System.Windows.Forms.Label
    $lblOut.Text = "Output Target:"
    $lblOut.Location = New-Object System.Drawing.Point(15, $y)
    $lblOut.AutoSize = $true
    $tabBuild.Controls.Add($lblOut)

    $y += 22
    # File mode widgets
    $txtOut = New-Object System.Windows.Forms.TextBox
    $txtOut.Location = New-Object System.Drawing.Point(15, $y)
    $txtOut.Size = New-Object System.Drawing.Size(470, 23)
    $tabBuild.Controls.Add($txtOut)

    $btnOut = New-Object System.Windows.Forms.Button
    $btnOut.Text = "Browse..."
    $btnOut.Location = New-Object System.Drawing.Point(495, ($y - 1))
    $btnOut.Size = New-Object System.Drawing.Size(90, 25)
    $btnOut.Add_Click({
        $sfd = New-Object System.Windows.Forms.SaveFileDialog
        $sfd.Title = "Save Image As"
        if ($rbImg.Checked) {
            $sfd.Filter = "FAT32 Image (*.img)|*.img|All Files (*.*)|*.*"
            $sfd.DefaultExt = "img"
        } else {
            $sfd.Filter = "ISO Image (*.iso)|*.iso|All Files (*.*)|*.*"
            $sfd.DefaultExt = "iso"
        }
        if ($sfd.ShowDialog() -eq "OK") { $txtOut.Text = $sfd.FileName }
    })
    $tabBuild.Controls.Add($btnOut)

    # USB mode widgets (hidden by default)
    $cmbDrive = New-Object System.Windows.Forms.ComboBox
    $cmbDrive.Location = New-Object System.Drawing.Point(15, $y)
    $cmbDrive.Size = New-Object System.Drawing.Size(470, 23)
    $cmbDrive.DropDownStyle = "DropDownList"
    $cmbDrive.Font = New-Object System.Drawing.Font("Consolas", 9)
    $cmbDrive.Visible = $false
    $tabBuild.Controls.Add($cmbDrive)

    $btnRefresh = New-Object System.Windows.Forms.Button
    $btnRefresh.Text = "Refresh"
    $btnRefresh.Location = New-Object System.Drawing.Point(495, ($y - 1))
    $btnRefresh.Size = New-Object System.Drawing.Size(90, 25)
    $btnRefresh.Visible = $false
    $btnRefresh.Add_Click({
        $cmbDrive.Items.Clear()
        $script:usbDrives = Get-UsbDrives
        if ($script:usbDrives.Count -eq 0) {
            $cmbDrive.Items.Add("(no USB drives found)")
        } else {
            foreach ($d in $script:usbDrives) {
                $cmbDrive.Items.Add("$($d.Path)  $($d.Size)  $($d.Model)")
            }
        }
        if ($cmbDrive.Items.Count -gt 0) { $cmbDrive.SelectedIndex = 0 }
    })
    $tabBuild.Controls.Add($btnRefresh)

    $script:usbDrives = @()

    # Target toggle: show file widgets vs USB-drive widgets. The action-button
    # label is updated centrally by $setActionLabel (wired after $btnCreate).
    $chkUsb.Add_CheckedChanged({
        $txtOut.Visible = -not $chkUsb.Checked
        $btnOut.Visible = -not $chkUsb.Checked
        $cmbDrive.Visible = $chkUsb.Checked
        $btnRefresh.Visible = $chkUsb.Checked
        if ($chkUsb.Checked) { $btnRefresh.PerformClick() }
    })

    # Action button (single)
    $y += 35
    $btnCreate = New-Object System.Windows.Forms.Button
    $btnCreate.Text = "Create Image"
    $btnCreate.Location = New-Object System.Drawing.Point(220, $y)
    $btnCreate.Size = New-Object System.Drawing.Size(160, 30)
    $tabBuild.Controls.Add($btnCreate)

    # Action-button label reflects the source x target combination, matching
    # the Python GUI: Create Image / Write to USB / Clone to Image / Clone to USB.
    $setActionLabel = {
        $btnCreate.Text = if ($rbSrcUsb.Checked) {
            if ($chkUsb.Checked) { "Clone to USB" } else { "Clone to Image" }
        } else {
            if ($chkUsb.Checked) { "Write to USB" } else { "Create Image" }
        }
    }
    # Source-mode toggle: swap file<->USB-drive source widgets, disable the
    # build-only controls when cloning a whole device, and relabel the button.
    $rbSrcUsb.Add_CheckedChanged({
        $usbSrc = $rbSrcUsb.Checked
        $txtSrc.Visible = -not $usbSrc
        $btnSrc.Visible = -not $usbSrc
        $cmbSrcDrive.Visible = $usbSrc
        $btnSrcRefresh.Visible = $usbSrc
        foreach ($c in @($lblInc, $btnAddFile, $btnAddDir, $btnClear, $lstInc,
                         $lblFmt, $rbImg, $rbIso, $lblLabel, $txtLabel,
                         $lblFs, $cmbFs, $lblSize, $txtSize, $chkGpt)) {
            $c.Enabled = -not $usbSrc
        }
        if ($usbSrc) { $btnSrcRefresh.PerformClick() }
        & $setActionLabel
    })
    $chkUsb.Add_CheckedChanged($setActionLabel)
    & $setActionLabel

    # Log tab content (fills the Log tab page)
    $lblLog = New-Object System.Windows.Forms.Label
    $lblLog.Text = "Log:"
    $lblLog.Location = New-Object System.Drawing.Point(12, 10)
    $lblLog.AutoSize = $true
    $tabLog.Controls.Add($lblLog)

    $txtLog = New-Object System.Windows.Forms.TextBox
    $txtLog.Location = New-Object System.Drawing.Point(12, 32)
    $txtLog.Size = New-Object System.Drawing.Size(568, 362)
    $txtLog.Multiline = $true
    $txtLog.ReadOnly = $true
    $txtLog.ScrollBars = "Vertical"
    $txtLog.Font = New-Object System.Drawing.Font("Consolas", 8)
    $txtLog.Text = "Ready.`r`n"
    $tabLog.Controls.Add($txtLog)

    # No external dependencies — all native Windows

    # Event handler -- dispatches build / write / clone from source x target.
    $btnCreate.Add_Click({
        $usbSrc = $rbSrcUsb.Checked
        $toUsb  = $chkUsb.Checked
        $force  = $chkForce.Checked

        # Resolve + validate the target
        $targetDrive = $null; $out = ""
        if ($toUsb) {
            if ($script:usbDrives.Count -eq 0 -or $cmbDrive.SelectedIndex -lt 0) {
                [System.Windows.Forms.MessageBox]::Show("No target USB drive selected.`nClick Refresh if no drives appear.", "Error", "OK", "Error"); return
            }
            $targetDrive = $script:usbDrives[$cmbDrive.SelectedIndex]
        } else {
            $out = $txtOut.Text.Trim()
            if (-not $out) { [System.Windows.Forms.MessageBox]::Show("Output file is required.", "Error", "OK", "Error"); return }
        }

        # Resolve + validate the source
        $srcDrive = $null; $src = ""
        if ($usbSrc) {
            if ($script:srcDrives.Count -eq 0 -or $cmbSrcDrive.SelectedIndex -lt 0) {
                [System.Windows.Forms.MessageBox]::Show("No source USB drive selected.`nClick Refresh if no drives appear.", "Error", "OK", "Error"); return
            }
            $srcDrive = $script:srcDrives[$cmbSrcDrive.SelectedIndex]
            if ($targetDrive -and $srcDrive.Number -eq $targetDrive.Number) {
                [System.Windows.Forms.MessageBox]::Show("Source and target are the same drive.", "Error", "OK", "Error"); return
            }
        } else {
            $src = $txtSrc.Text.Trim()
            if (-not $src) { [System.Windows.Forms.MessageBox]::Show("Source is required.", "Error", "OK", "Error"); return }
            if (-not (Test-Path $src)) { [System.Windows.Forms.MessageBox]::Show("Source not found: $src", "Error", "OK", "Error"); return }
        }

        # A file source that is an existing disk image -> raw write/clone.
        $srcIsImage = (-not $usbSrc) -and (Test-Path $src -PathType Leaf) -and ($src -match '\.(img|iso)$')

        $includes = @()
        foreach ($item in $lstInc.Items) { $includes += $item.ToString() }
        $label = if ($txtLabel.Text.Trim()) { $txtLabel.Text.Trim() } else { "UEFITOOLS" }
        $sizeMB = if ($txtSize.Text -match '^\d+$') { [int]$txtSize.Text } else { 32 }

        $btnCreate.Enabled = $false
        $progress.Visible = $true
        $progress.MarqueeAnimationSpeed = 30
        $lblStatus.Text = "Working..."
        $txtLog.AppendText("`r`n--- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ---`r`n")
        $txtLog.AppendText("Action: $($btnCreate.Text)`r`n")
        $form.Refresh()

        if ($usbSrc -or $srcIsImage) {
            # --- Raw clone: device/image -> device/file --------------------
            $cloneArgs = @{ LogBox = $txtLog }
            if ($force) { $cloneArgs['SkipConfirm'] = $true }
            if ($chkVerify.Checked) { $cloneArgs['Verify'] = $true }
            if ($usbSrc) {
                $cloneArgs['SourceDiskNum'] = $srcDrive.Number
                $cloneArgs['SourceSizeBytes'] = $srcDrive.SizeBytes
            } else {
                $cloneArgs['SourcePath'] = $src
            }
            if ($toUsb) {
                $cloneArgs['DestDiskNum'] = $targetDrive.Number
                $cloneArgs['DestSizeBytes'] = $targetDrive.SizeBytes
                $cloneArgs['DestModel'] = $targetDrive.Model
            } else {
                $cloneArgs['DestPath'] = $out
            }
            Copy-DiskImage @cloneArgs
        } elseif ($toUsb) {
            # --- Build from files + write to USB ---------------------------
            $optSwitches = @{}
            if ($chkVerbose.Checked) { $optSwitches['Verbose'] = $true }
            if ($chkVerify.Checked) { $optSwitches['Verify'] = $true }
            if ($chkGpt.Checked) { $optSwitches['UseGpt'] = $true }
            if ($force) { $optSwitches['SkipConfirm'] = $true }
            $fs = $cmbFs.SelectedItem
            Write-UsbDrive -TargetDrive $targetDrive -SourceDir $src `
                -Includes $includes -Label $label -FileSystem $fs `
                -LogBox $txtLog @optSwitches
        } elseif ($rbIso.Checked) {
            # --- Build an ISO (with optional UDF bridge / hybrid) -----------
            New-IsoImage -SourceDir $src -Includes $includes -OutputFile $out `
                -Label $label -Udf:$chkUdf.Checked -Hybrid:$chkHybrid.Checked `
                -Verbose:$chkVerbose.Checked -LogBox $txtLog
        } else {
            # --- Build an image file: multi-partition (Options scheme) or the
            #     simple single-FAT32 path; gzip-compress if the target is .gz.
            $gz = ($out -match '\.gz$')
            $buildOut = if ($gz) { $out -replace '\.gz$', '' } else { $out }

            $scheme = if ($rbSchemeGpt.Checked) { 'GPT' } elseif ($rbSchemeMbr.Checked) { 'MBR' } else { 'None' }
            $ok = $true
            if ($scheme -ne 'None' -and $grid.Rows.Count -gt 0) {
                $parts = @()
                foreach ($row in $grid.Rows) {
                    $fsv = "$($row.Cells['Fs'].Value)"; if (-not $fsv) { $fsv = 'fat32' }
                    $szv = "$($row.Cells['Size'].Value)".Trim()
                    $clv = "$($row.Cells['Cluster'].Value)".Trim()
                    $srcv = "$($row.Cells['Source'].Value)".Trim(); if (-not $srcv) { $srcv = $src }
                    $parts += @{
                        Fs      = $fsv
                        SizeMB  = if ($szv -match '^\d+$') { [int]$szv } else { 0 }
                        Label   = "$($row.Cells['Label'].Value)"
                        Cluster = if ($clv -match '^\d+$') { [int]$clv } else { 0 }
                        Source  = $srcv
                    }
                }
                $ok = New-DiskImage -OutputFile $buildOut -PartStyle $scheme -Partitions $parts `
                    -Verbose:$chkVerbose.Checked -LogBox $txtLog
            } else {
                $verboseSwitch = if ($chkVerbose.Checked) { @{Verbose=$true} } else { @{} }
                $ok = New-UefiImage -SourceDir $src -Includes $includes -OutputFile $buildOut `
                    -Label $label -SizeMB $sizeMB -LogBox $txtLog @verboseSwitch
            }
            if ($gz -and $ok) {
                Compress-FileGzip -InputFile $buildOut -OutputFile $out -LogBox $txtLog | Out-Null
                Remove-Item $buildOut -ErrorAction SilentlyContinue
            }
        }

        $progress.MarqueeAnimationSpeed = 0
        $progress.Visible = $false
        $lblStatus.Text = "Done"
        $btnCreate.Enabled = $true
    })

    # --- Keyboard navigation (F-keys), mirroring the Python GUI ---------------
    $form.Add_KeyDown({
        param($s, $e)
        switch ($e.KeyCode) {
            ([System.Windows.Forms.Keys]::F1)  { $tabs.SelectedTab = $tabBuild;   $e.Handled = $true }
            ([System.Windows.Forms.Keys]::F2)  { $tabs.SelectedTab = $tabOptions; $e.Handled = $true }
            ([System.Windows.Forms.Keys]::F3)  { $tabs.SelectedTab = $tabTools;   $e.Handled = $true }
            ([System.Windows.Forms.Keys]::F4)  { $tabs.SelectedTab = $tabLog;     $e.Handled = $true }
            ([System.Windows.Forms.Keys]::F5)  { $tabs.SelectedTab = $tabHelp;    $e.Handled = $true }
            ([System.Windows.Forms.Keys]::F6)  { if ($btnRefresh.Visible) { $btnRefresh.PerformClick() }; $e.Handled = $true }
            ([System.Windows.Forms.Keys]::F12) { if ($btnCreate.Enabled) { $btnCreate.PerformClick() }; $e.Handled = $true }
        }
    })

    # --- Apply the theme to all controls (single DRY pass) --------------------
    # Secondary (neutral) buttons: flat white, hairline border, soft blue hover.
    foreach ($b in @($btnSrc, $btnSrcRefresh, $btnAddFile, $btnAddDir, $btnClear, $btnOut, $btnRefresh,
                     $btnAddPart, $btnRemovePart, $btnToolRefresh, $btnFmt, $btnWipe, $btnCheck,
                     $btnListBrowse, $btnList)) {
        $b.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
        $b.FlatAppearance.BorderColor = $clrBorder
        $b.FlatAppearance.BorderSize = 1
        $b.FlatAppearance.MouseOverBackColor = [System.Drawing.Color]::FromArgb(232, 240, 250)
        $b.BackColor = [System.Drawing.Color]::White
        $b.ForeColor = $clrText
        $b.Cursor = [System.Windows.Forms.Cursors]::Hand
    }

    # Primary action button: solid accent fill, white text, darker hover.
    $btnCreate.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $btnCreate.FlatAppearance.BorderSize = 0
    $btnCreate.FlatAppearance.MouseOverBackColor = $clrAccentDk
    $btnCreate.BackColor = $clrAccent
    $btnCreate.ForeColor = [System.Drawing.Color]::White
    $btnCreate.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 10, [System.Drawing.FontStyle]::Bold)
    $btnCreate.Cursor = [System.Windows.Forms.Cursors]::Hand

    # Section heading labels: accent-tinted and semibold for visual hierarchy.
    foreach ($lbl in @($lblSrc, $lblInc, $lblFmt, $lblOut, $lblLog)) {
        $lbl.ForeColor = $clrAccentDk
        $lbl.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9, [System.Drawing.FontStyle]::Bold)
    }

    # Body field labels: consistent ink color.
    foreach ($lbl in @($lblLabel, $lblFs, $lblSize)) { $lbl.ForeColor = $clrText }

    # Terminal-style log pane: dark background, green mono text.
    $txtLog.BackColor = [System.Drawing.Color]::FromArgb(24, 26, 32)
    $txtLog.ForeColor = [System.Drawing.Color]::FromArgb(126, 224, 145)
    $txtLog.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle

    # Inputs: crisp single-line borders.
    foreach ($tb in @($txtSrc, $txtOut, $txtLabel, $txtSize)) {
        $tb.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
    }

    [void]$form.ShowDialog()
}

# --- Entry point ---
if ($Action) {
    # CLI mode -- called from mkimage.py or command line
    switch ($Action) {
        'WriteUsb' {
            if ($DiskNumber -lt 0) {
                Write-Error "DiskNumber is required for WriteUsb action"
                exit 1
            }
            $disk = Get-Disk -Number $DiskNumber
            $sizeGB = [math]::Round($disk.Size / 1GB, 1)
            $drive = [PSCustomObject]@{
                Number    = $DiskNumber
                Name      = "Disk $DiskNumber"
                Size      = "${sizeGB}GB"
                SizeBytes = $disk.Size
                Model     = $disk.FriendlyName
                Path      = "\\.\PhysicalDrive$DiskNumber"
            }
            Write-UsbDrive -TargetDrive $drive -SourceDir $SourceDir `
                -Includes $Includes -Label $Label `
                -UseGpt:$UseGpt -Verbose:$Verbose -Verify:$Verify `
                -SkipConfirm:$SkipConfirm -CliProgressFile $ProgressFile
        }
        'CreateImg' {
            if (-not $OutputFile) { Write-Error "OutputFile required for CreateImg"; exit 1 }
            if (-not $SourceDir) { Write-Error "SourceDir required for CreateImg"; exit 1 }
            # gzip-compress when the target ends in .gz (build to a temp first).
            $gz = ($OutputFile -match '\.gz$')
            $buildOut = if ($gz) { $OutputFile -replace '\.gz$', '' } else { $OutputFile }
            $result = New-UefiImage -SourceDir $SourceDir -Includes $Includes `
                -OutputFile $buildOut -Label $Label -SizeMB $SizeMB `
                -Verbose:$Verbose -ProgressFile $ProgressFile
            if (-not $result) { exit 1 }
            if ($gz) {
                $cok = Compress-FileGzip -InputFile $buildOut -OutputFile $OutputFile -ProgressFile $ProgressFile
                Remove-Item $buildOut -ErrorAction SilentlyContinue
                if (-not $cok) { exit 1 }
            }
        }
        'CreateIso' {
            if (-not $OutputFile) { Write-Error "OutputFile required for CreateIso"; exit 1 }
            if (-not $SourceDir) { Write-Error "SourceDir required for CreateIso"; exit 1 }
            $result = New-IsoImage -SourceDir $SourceDir -Includes $Includes `
                -OutputFile $OutputFile -Label $Label -BootImage $BootImage `
                -Udf:$Udf -Hybrid:$Hybrid -Verbose:$Verbose
            if (-not $result) { exit 1 }
        }
        'CreateDisk' {
            if (-not $OutputFile) { Write-Error "OutputFile required for CreateDisk"; exit 1 }
            if (-not $PartitionsJson) { Write-Error "PartitionsJson required for CreateDisk"; exit 1 }
            # ConvertFrom-Json emits a top-level array as ONE object, so don't
            # wrap with @() (that nests it); coerce a lone object to an array.
            $parts = $PartitionsJson | ConvertFrom-Json
            if ($parts -isnot [array]) { $parts = @($parts) }
            $result = New-DiskImage -OutputFile $OutputFile -PartStyle $PartStyle `
                -Partitions $parts -Verbose:$Verbose -ProgressFile $ProgressFile
            if (-not $result) { exit 1 }
        }
        'Clone' {
            # Raw clone. Resolve source/dest from $SourceDiskNumber/$DiskNumber
            # (devices) and $SourceDir/$OutputFile (image files). Reuses
            # $DiskNumber as the destination device.
            $srcSize = 0.0; $dstSize = 0.0; $dstModel = ''
            if ($SourceDiskNumber -ge 0) {
                $sd = Get-Disk -Number $SourceDiskNumber -ErrorAction SilentlyContinue
                if (-not $sd) { Write-Error "Source disk $SourceDiskNumber not found"; exit 1 }
                $srcSize = [double]$sd.Size
            }
            if ($DiskNumber -ge 0) {
                $dd = Get-Disk -Number $DiskNumber -ErrorAction SilentlyContinue
                if (-not $dd) { Write-Error "Destination disk $DiskNumber not found"; exit 1 }
                $dstSize = [double]$dd.Size; $dstModel = $dd.FriendlyName
            }
            Copy-DiskImage -SourcePath $SourceDir -SourceDiskNum $SourceDiskNumber -SourceSizeBytes $srcSize `
                -DestPath $OutputFile -DestDiskNum $DiskNumber -DestSizeBytes $dstSize -DestModel $dstModel `
                -Verify:$Verify -SkipConfirm:$SkipConfirm -CliProgressFile $ProgressFile
        }
        'Wipe' {
            if ($DiskNumber -lt 0) { Write-Error "DiskNumber required for Wipe"; exit 1 }
            $d = Get-Disk -Number $DiskNumber -ErrorAction SilentlyContinue
            if (-not $d) { Write-Error "Disk $DiskNumber not found"; exit 1 }
            Invoke-WipeDrive -DiskNumber $DiskNumber -DiskSizeBytes ([double]$d.Size) `
                -Model $d.FriendlyName -SkipConfirm:$SkipConfirm -CliProgressFile $ProgressFile
        }
        'Check' {
            if ($DiskNumber -lt 0) { Write-Error "DiskNumber required for Check"; exit 1 }
            $d = Get-Disk -Number $DiskNumber -ErrorAction SilentlyContinue
            if (-not $d) { Write-Error "Disk $DiskNumber not found"; exit 1 }
            Test-DriveBadBlocks -DiskNumber $DiskNumber -DiskSizeBytes ([double]$d.Size) `
                -Model $d.FriendlyName -SkipConfirm:$SkipConfirm -CliProgressFile $ProgressFile
        }
        'Format' {
            if ($DiskNumber -lt 0) { Write-Error "DiskNumber required for Format"; exit 1 }
            $d = Get-Disk -Number $DiskNumber -ErrorAction SilentlyContinue
            if (-not $d) { Write-Error "Disk $DiskNumber not found"; exit 1 }
            # Format = write an empty source to the drive (clean + partition + format, no files).
            $empty = Join-Path $env:TEMP ("mkimage-empty-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
            New-Item -ItemType Directory -Force -Path $empty | Out-Null
            $gb = [math]::Round($d.Size / 1GB, 1)
            $drive = [PSCustomObject]@{
                Number = $DiskNumber; Name = "Disk $DiskNumber"; Size = "${gb}GB"
                SizeBytes = $d.Size; Model = $d.FriendlyName; Path = "\\.\PhysicalDrive$DiskNumber"
            }
            Write-UsbDrive -TargetDrive $drive -SourceDir $empty -Label $Label -FileSystem $FileSystem `
                -UseGpt:$UseGpt -Verbose:$Verbose -SkipConfirm:$SkipConfirm -CliProgressFile $ProgressFile
            Remove-Item $empty -Recurse -Force -ErrorAction SilentlyContinue
        }
        'ListImage' {
            if (-not $SourceDir) { Write-Error "SourceDir (image path) required for ListImage"; exit 1 }
            Get-ImageInfo -ImagePath $SourceDir -CliProgressFile $ProgressFile
        }
        default {
            Write-Error "Unknown action: $Action"
            exit 1
        }
    }
} else {
    # GUI mode -- launch WinForms interface
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Show-MainForm
}
