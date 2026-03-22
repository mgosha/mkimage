<#
.SYNOPSIS
    mkimage - Bootable Media Creator (native Windows, no WSL required)

.DESCRIPTION
    Creates FAT32 (.img) or ISO (.iso) images containing UEFI applications.
    Can also write directly to USB flash drives (including unformatted ones).

    All operations use native Windows APIs:
    - FAT32 .img: VHD create/mount/format/robocopy, then strip VHD footer
    - ISO: oscdimg.exe (Windows ADK) or IMAPI2 COM fallback
    - USB: Clear-Disk/Initialize-Disk/New-Partition/Format-Volume/robocopy

    Safety: Never writes to the C: drive. Rejects drives larger than 256GB.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File mkimage.ps1
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$MAX_USB_SIZE_GB = 256

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
        [System.Windows.Forms.TextBox]$LogBox
    )

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
        $LogBox.AppendText("[ERROR] No files found in source directory.`r`n")
        return $false
    }

    $totalBytes = ($allFiles | Measure-Object -Property Length -Sum).Sum
    $totalMB = [math]::Ceiling($totalBytes / 1MB)
    $fileCount = $allFiles.Count

    # Auto-size: content + extra space (default 32MB)
    # FAT32 minimum is ~36MB usable; VHD overhead needs ~4MB extra, so 40MB floor
    $SizeMB = [math]::Max(40, $totalMB + $SizeMB)
    $LogBox.AppendText("Image size: ${SizeMB}MB (${totalMB}MB content + $($SizeMB - $totalMB)MB free)`r`n")
    $LogBox.AppendText("$fileCount files ($([math]::Round($totalBytes/1024))KB) to include`r`n")
    $LogBox.Parent.Refresh()

    if ($isImg) {
        return New-Fat32Image -SourceDir $SourceDir -Includes $Includes `
            -OutputFile $OutputFile -Label $Label -SizeMB $SizeMB `
            -Verbose:$Verbose -LogBox $LogBox
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
        [System.Windows.Forms.TextBox]$LogBox
    )

    $labelTrim = $Label.Substring(0, [Math]::Min($Label.Length, 11))
    $vhdPath = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.vhd'

    try {
        $LogBox.AppendText("Creating VHD ($SizeMB MB)...`r`n")
        $LogBox.Parent.Refresh()

        # Create fixed-size VHD
        New-VHD -Path $vhdPath -SizeBytes ($SizeMB * 1MB) -Fixed | Out-Null

        # Mount it
        $LogBox.AppendText("Mounting VHD...`r`n")
        Mount-VHD -Path $vhdPath
        $disk = Get-VHD -Path $vhdPath | Get-Disk

        # Initialize, partition, format
        $LogBox.AppendText("Initializing and formatting FAT32...`r`n")
        Initialize-Disk -Number $disk.Number -PartitionStyle MBR -ErrorAction SilentlyContinue
        $part = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter -IsActive
        $driveLetter = $part.DriveLetter
        Start-Sleep -Seconds 2

        # Format with format.com /X (force dismount)
        $fmtOut = cmd.exe /c "echo Y | format ${driveLetter}: /FS:FAT32 /Q /V:$labelTrim /X 2>&1"
        $fmtText = ($fmtOut | Out-String).Trim()
        if ($fmtText -notmatch "Format complete") {
            throw "format.com failed: $fmtText"
        }
        $LogBox.AppendText("  Format complete`r`n")

        $destRoot = "${driveLetter}:\"
        $LogBox.AppendText("Copying files to ${driveLetter}:...`r`n")
        $LogBox.Parent.Refresh()

        # Copy source directory
        if (Test-Path $SourceDir -PathType Container) {
            $srcNorm = $SourceDir.TrimEnd('\', '/')
            if ($Verbose) {
                $rcOut = robocopy $srcNorm $destRoot /S /E /NP /NJH /NJS 2>&1
                foreach ($line in $rcOut) {
                    $t = ("$line").Trim()
                    if ($t) { $LogBox.AppendText("  $t`r`n") }
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
                if ($Verbose) { $LogBox.AppendText("  $fileName`r`n") }
                robocopy $srcDir $destRoot $fileName /NJH /NJS /NP 2>&1 | Out-Null
            } elseif (Test-Path $inc -PathType Container) {
                $incNorm = $inc.TrimEnd('\', '/')
                if ($Verbose) {
                    $rcOut = robocopy $incNorm $destRoot /S /E /NP /NJH /NJS 2>&1
                    foreach ($line in $rcOut) {
                        $t = ("$line").Trim()
                        if ($t) { $LogBox.AppendText("  $t`r`n") }
                    }
                } else {
                    robocopy $incNorm $destRoot /S /E /NP /NFL /NDL /NJH /NJS 2>&1 | Out-Null
                }
            }
        }

        $copiedCount = (Get-ChildItem $destRoot -Recurse -File).Count
        $LogBox.AppendText("Copied $copiedCount files to image`r`n")

        # Dismount VHD
        $LogBox.AppendText("Dismounting VHD...`r`n")
        Dismount-VHD -Path $vhdPath

        # Strip the 512-byte VHD footer to produce a raw FAT32 image
        $LogBox.AppendText("Converting to raw image...`r`n")
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
        $LogBox.AppendText("[OK] Created $OutputFile ($([math]::Round($size/1KB))KB, FAT32)`r`n")
        return $true

    } catch {
        $LogBox.AppendText("[ERROR] $_`r`n")
        # Cleanup: dismount if still mounted
        Dismount-VHD -Path $vhdPath -ErrorAction SilentlyContinue
        return $false
    } finally {
        Remove-Item $vhdPath -ErrorAction SilentlyContinue
    }
}

# Create an ISO image using oscdimg.exe (Windows ADK) or a staging directory.
# Falls back to a simple copy-to-directory if no ISO tool is found.
function New-IsoImage {
    param(
        [string]$SourceDir,
        [string[]]$Includes,
        [string]$OutputFile,
        [string]$Label,
        [switch]$Verbose,
        [System.Windows.Forms.TextBox]$LogBox
    )

    $isoLabel = $Label.Substring(0, [Math]::Min($Label.Length, 32))

    # Stage all files into a temp directory
    $staging = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "mkimage-iso-$([guid]::NewGuid().ToString('N').Substring(0,8))")
    New-Item -ItemType Directory -Path $staging -Force | Out-Null

    try {
        $LogBox.AppendText("Staging files for ISO...`r`n")
        $LogBox.Parent.Refresh()

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
        $LogBox.AppendText("Staged $stagedCount files`r`n")

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
            $LogBox.AppendText("Creating ISO with oscdimg.exe...`r`n")
            $LogBox.Parent.Refresh()
            $args = "-l$isoLabel", "-o", "-m", $staging, $OutputFile
            $proc = Start-Process -FilePath $oscdimg -ArgumentList $args `
                -NoNewWindow -Wait -PassThru
            if ($proc.ExitCode -ne 0) {
                $LogBox.AppendText("[ERROR] oscdimg.exe failed (exit $($proc.ExitCode))`r`n")
                return $false
            }
        } else {
            # No ISO tool found — create ISO using .NET (basic ISO 9660)
            $LogBox.AppendText("oscdimg.exe not found (install Windows ADK for ISO support)`r`n")
            $LogBox.AppendText("Creating ISO with built-in writer...`r`n")
            $LogBox.Parent.Refresh()

            # Use IMAPI2 COM object (available on Windows 7+)
            $fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
            $fsi.FileSystemsToCreate = 4  # FsiFileSystemISO9660 | FsiFileSystemJoliet
            $fsi.VolumeName = $isoLabel

            # Add all staged files
            $fsi.Root.AddTree($staging, $false)

            $resultStream = $fsi.CreateResultImage()
            $isoStream = $resultStream.ImageStream

            # Write stream to file
            $outFs = [System.IO.File]::Create($OutputFile)
            $buffer = New-Object byte[] (64 * 1024)
            do {
                $bytesRead = [uint32]0
                $isoStream.Read($buffer, $buffer.Length, [ref]$bytesRead)
                if ($bytesRead -gt 0) { $outFs.Write($buffer, 0, $bytesRead) }
            } while ($bytesRead -gt 0)
            $outFs.Close()
            [System.Runtime.InteropServices.Marshal]::ReleaseComObject($isoStream) | Out-Null
            [System.Runtime.InteropServices.Marshal]::ReleaseComObject($fsi) | Out-Null
        }

        $size = (Get-Item $OutputFile).Length
        $LogBox.AppendText("[OK] Created $OutputFile ($([math]::Round($size/1KB))KB, ISO)`r`n")
        return $true

    } catch {
        $LogBox.AppendText("[ERROR] $_`r`n")
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
        [switch]$UseGpt,
        [switch]$Verbose,
        [switch]$Verify,
        [System.Windows.Forms.TextBox]$LogBox
    )

    # Safety: reject C: drive
    $partitions = Get-Partition -DiskNumber $TargetDrive.Number -ErrorAction SilentlyContinue
    $hasCDrive = $partitions | Where-Object { $_.DriveLetter -eq 'C' }
    if ($hasCDrive) {
        [System.Windows.Forms.MessageBox]::Show(
            "This disk contains the C: drive. Refusing to write.",
            "Safety Check Failed", "OK", "Error")
        return
    }

    # Safety: reject > 256GB
    if ($TargetDrive.SizeBytes -gt ($MAX_USB_SIZE_GB * 1GB)) {
        [System.Windows.Forms.MessageBox]::Show(
            "This disk is larger than ${MAX_USB_SIZE_GB}GB. Refusing to write.",
            "Safety Check Failed", "OK", "Error")
        return
    }

    # Confirmation
    $confirm = [System.Windows.Forms.MessageBox]::Show(
        "WARNING: ALL DATA on $($TargetDrive.Path) ($($TargetDrive.Size) $($TargetDrive.Model)) WILL BE DESTROYED.`n`nAre you sure?",
        "Confirm Write", "YesNo", "Warning")
    if ($confirm -ne "Yes") {
        $LogBox.AppendText("Aborted.`r`n")
        return
    }

    $diskNum = $TargetDrive.Number
    $labelTrim = $Label.Substring(0, [Math]::Min($Label.Length, 11))

    # Use diskpart to clean, partition, and format the USB drive.
    # Then copy files directly — no raw disk write needed.
    $progressFile = [System.IO.Path]::GetTempFileName()
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

    # Step 1a: clean the disk
    $dpClean = @"
select disk __DISKNUM__
clean
"@
    "  diskpart: clean..." | Out-File -Append __PROGRESS__
    ($dpClean | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1

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
format fs=fat32 quick label=__LABEL__
"@
    } else {
        $dpSetup = @"
select disk __DISKNUM__
create partition primary
active
format fs=fat32 quick label=__LABEL__
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
    $writeScript = $writeScript.Replace('__SOURCES__', $fileSourcesStr)
    $writeScript = $writeScript.Replace('__VERBOSE__', "$(if ($Verbose) { '$true' } else { '$false' })")
    $writeScript = $writeScript.Replace('__VERIFY__', "$(if ($Verify) { '$true' } else { '$false' })")
    $writeScript = $writeScript.Replace('__USEGPT__', "$(if ($UseGpt) { '$true' } else { '$false' })")
    [System.IO.File]::WriteAllText($tmpScript, $writeScript)

    # Log the operation summary
    $LogBox.AppendText("Target: Disk $diskNum ($($TargetDrive.Size) $($TargetDrive.Model))`r`n")
    $LogBox.AppendText("Source: $SourceDir`r`n")
    if ($Includes.Count -gt 0) {
        $LogBox.AppendText("Includes: $($Includes -join ', ')`r`n")
    }
    $LogBox.AppendText("Label: $Label`r`n")
    $LogBox.AppendText("Formatting disk $diskNum as FAT32 and copying files...`r`n")
    $LogBox.Parent.Refresh()

    try {
        $LogBox.AppendText("Requesting Administrator access...`r`n")
        $LogBox.Parent.Refresh()

        $proc = Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $tmpScript `
            -Verb RunAs -PassThru -WindowStyle Hidden

        if ($null -eq $proc) {
            $LogBox.AppendText("Aborted - Administrator access denied.`r`n")
            return
        }

        # Poll progress file for new lines while subprocess runs
        $linesRead = 0
        while (-not $proc.HasExited) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 300
            if (Test-Path $progressFile) {
                $allLines = @(Get-Content $progressFile -ErrorAction SilentlyContinue)
                if ($allLines.Count -gt $linesRead) {
                    for ($i = $linesRead; $i -lt $allLines.Count; $i++) {
                        $line = $allLines[$i]
                        if ($line) { $LogBox.AppendText("  $line`r`n") }
                    }
                    $linesRead = $allLines.Count
                    $LogBox.Parent.Refresh()
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
                    if ($line) { $LogBox.AppendText("  $line`r`n") }
                }
            }
            # Last line is the status
            $finalMsg = if ($allLines.Count -gt 0) { $allLines[-1].Trim() } else { "" }
        }
        if ($finalMsg -match "^OK:(\d+)") {
            $count = $Matches[1]
            # Ensure the drive letter is visible in the current user session.
            # The elevated subprocess may have assigned a letter that the
            # non-elevated Explorer session doesn't see.
            # Try to find and open the drive letter
            $driveLine = $allLines | Where-Object { $_ -match "Assigned drive letter: (.):$" } | Select-Object -Last 1
            $usbLetter = $null
            if ($driveLine -match "Assigned drive letter: (.):") { $usbLetter = $Matches[1] }

            if ($usbLetter -and (Test-Path "${usbLetter}:\")) {
                $LogBox.AppendText("  Opening ${usbLetter}: in Explorer...`r`n")
                Start-Process "explorer.exe" "${usbLetter}:\" -ErrorAction SilentlyContinue
            } else {
                $LogBox.AppendText("  NOTE: Eject and re-insert the USB drive to see it in Explorer.`r`n")
            }
            $LogBox.AppendText("[OK] Wrote $count files to USB drive ($($TargetDrive.Size)). You can safely remove it.`r`n")
        } elseif ($finalMsg -match "^ERROR:") {
            $LogBox.AppendText("[ERROR] $finalMsg`r`n")
        } elseif ($finalMsg) {
            $LogBox.AppendText("  $finalMsg`r`n")
        } else {
            $LogBox.AppendText("[ERROR] Write subprocess produced no output. Check if Administrator access was granted.`r`n")
        }
    } catch {
        if ($_.Exception.Message -match "canceled by the user") {
            $LogBox.AppendText("Aborted - Administrator access denied.`r`n")
        } else {
            $LogBox.AppendText("[ERROR] $_`r`n")
        }
    } finally {
        Remove-Item $tmpScript -ErrorAction SilentlyContinue
        Remove-Item $progressFile -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Main WinForms GUI
# ---------------------------------------------------------------------------

function Show-MainForm {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "mkimage - Bootable Media Creator"
    $form.Size = New-Object System.Drawing.Size(620, 580)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedSingle"
    $form.MaximizeBox = $false
    $form.Font = New-Object System.Drawing.Font("Segoe UI", 9)

    $y = 15

    # Source directory
    $lblSrc = New-Object System.Windows.Forms.Label
    $lblSrc.Text = "Source Directory:"
    $lblSrc.Location = New-Object System.Drawing.Point(15, $y)
    $lblSrc.AutoSize = $true
    $form.Controls.Add($lblSrc)

    $y += 22
    $txtSrc = New-Object System.Windows.Forms.TextBox
    $txtSrc.Location = New-Object System.Drawing.Point(15, $y)
    $txtSrc.Size = New-Object System.Drawing.Size(470, 23)
    $form.Controls.Add($txtSrc)

    $btnSrc = New-Object System.Windows.Forms.Button
    $btnSrc.Text = "Browse..."
    $btnSrc.Location = New-Object System.Drawing.Point(495, ($y - 1))
    $btnSrc.Size = New-Object System.Drawing.Size(90, 25)
    $btnSrc.Add_Click({
        $fbd = New-Object System.Windows.Forms.FolderBrowserDialog
        $fbd.Description = "Select Source Directory"
        if ($fbd.ShowDialog() -eq "OK") { $txtSrc.Text = $fbd.SelectedPath }
    })
    $form.Controls.Add($btnSrc)

    # Extra includes
    $y += 35
    $lblInc = New-Object System.Windows.Forms.Label
    $lblInc.Text = "Extra Includes:"
    $lblInc.Location = New-Object System.Drawing.Point(15, $y)
    $lblInc.AutoSize = $true
    $form.Controls.Add($lblInc)

    $btnAddFile = New-Object System.Windows.Forms.Button
    $btnAddFile.Text = "Add File"
    $btnAddFile.Location = New-Object System.Drawing.Point(120, ($y - 3))
    $btnAddFile.Size = New-Object System.Drawing.Size(70, 23)
    $form.Controls.Add($btnAddFile)

    $btnAddDir = New-Object System.Windows.Forms.Button
    $btnAddDir.Text = "Add Dir"
    $btnAddDir.Location = New-Object System.Drawing.Point(195, ($y - 3))
    $btnAddDir.Size = New-Object System.Drawing.Size(70, 23)
    $form.Controls.Add($btnAddDir)

    $btnClear = New-Object System.Windows.Forms.Button
    $btnClear.Text = "Clear"
    $btnClear.Location = New-Object System.Drawing.Point(270, ($y - 3))
    $btnClear.Size = New-Object System.Drawing.Size(60, 23)
    $form.Controls.Add($btnClear)

    $y += 25
    $lstInc = New-Object System.Windows.Forms.ListBox
    $lstInc.Location = New-Object System.Drawing.Point(15, $y)
    $lstInc.Size = New-Object System.Drawing.Size(570, 65)
    $lstInc.Font = New-Object System.Drawing.Font("Consolas", 8)
    $form.Controls.Add($lstInc)

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
    $form.Controls.Add($lblFmt)

    $rbImg = New-Object System.Windows.Forms.RadioButton
    $rbImg.Text = "FAT32 (.img)"
    $rbImg.Location = New-Object System.Drawing.Point(120, ($y - 2))
    $rbImg.AutoSize = $true
    $rbImg.Checked = $true
    $form.Controls.Add($rbImg)

    $rbIso = New-Object System.Windows.Forms.RadioButton
    $rbIso.Text = "ISO (.iso)"
    $rbIso.Location = New-Object System.Drawing.Point(250, ($y - 2))
    $rbIso.AutoSize = $true
    $form.Controls.Add($rbIso)

    # Volume label
    $y += 30
    $lblLabel = New-Object System.Windows.Forms.Label
    $lblLabel.Text = "Volume Label:"
    $lblLabel.Location = New-Object System.Drawing.Point(15, $y)
    $lblLabel.AutoSize = $true
    $form.Controls.Add($lblLabel)

    $txtLabel = New-Object System.Windows.Forms.TextBox
    $txtLabel.Text = "UEFITOOLS"
    $txtLabel.Location = New-Object System.Drawing.Point(120, ($y - 2))
    $txtLabel.Size = New-Object System.Drawing.Size(150, 23)
    $form.Controls.Add($txtLabel)

    # Image size
    $y += 30
    $lblSize = New-Object System.Windows.Forms.Label
    $lblSize.Text = "Extra Space (MB):"
    $lblSize.Location = New-Object System.Drawing.Point(15, $y)
    $lblSize.AutoSize = $true
    $form.Controls.Add($lblSize)

    $txtSize = New-Object System.Windows.Forms.TextBox
    $txtSize.Text = "32"
    $txtSize.Location = New-Object System.Drawing.Point(120, ($y - 2))
    $txtSize.Size = New-Object System.Drawing.Size(60, 23)
    $form.Controls.Add($txtSize)

    $rbIso.Add_CheckedChanged({ $txtSize.Enabled = -not $rbIso.Checked })

    # Write to USB toggle
    $chkVerbose = New-Object System.Windows.Forms.CheckBox
    $chkVerbose.Text = "Verbose"
    $chkVerbose.Location = New-Object System.Drawing.Point(200, ($y - 2))
    $chkVerbose.AutoSize = $true
    $form.Controls.Add($chkVerbose)

    $chkVerify = New-Object System.Windows.Forms.CheckBox
    $chkVerify.Text = "Verify"
    $chkVerify.Location = New-Object System.Drawing.Point(280, ($y - 2))
    $chkVerify.AutoSize = $true
    $form.Controls.Add($chkVerify)

    $chkGpt = New-Object System.Windows.Forms.CheckBox
    $chkGpt.Text = "GPT"
    $chkGpt.Location = New-Object System.Drawing.Point(345, ($y - 2))
    $chkGpt.AutoSize = $true
    $form.Controls.Add($chkGpt)

    $chkUsb = New-Object System.Windows.Forms.CheckBox
    $chkUsb.Text = "Write to USB"
    $chkUsb.Location = New-Object System.Drawing.Point(405, ($y - 2))
    $chkUsb.AutoSize = $true
    $form.Controls.Add($chkUsb)

    # Output target
    $y += 30
    $lblOut = New-Object System.Windows.Forms.Label
    $lblOut.Text = "Output Target:"
    $lblOut.Location = New-Object System.Drawing.Point(15, $y)
    $lblOut.AutoSize = $true
    $form.Controls.Add($lblOut)

    $y += 22
    # File mode widgets
    $txtOut = New-Object System.Windows.Forms.TextBox
    $txtOut.Location = New-Object System.Drawing.Point(15, $y)
    $txtOut.Size = New-Object System.Drawing.Size(470, 23)
    $form.Controls.Add($txtOut)

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
    $form.Controls.Add($btnOut)

    # USB mode widgets (hidden by default)
    $cmbDrive = New-Object System.Windows.Forms.ComboBox
    $cmbDrive.Location = New-Object System.Drawing.Point(15, $y)
    $cmbDrive.Size = New-Object System.Drawing.Size(470, 23)
    $cmbDrive.DropDownStyle = "DropDownList"
    $cmbDrive.Font = New-Object System.Drawing.Font("Consolas", 9)
    $cmbDrive.Visible = $false
    $form.Controls.Add($cmbDrive)

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
    $form.Controls.Add($btnRefresh)

    $script:usbDrives = @()

    # Toggle handler
    $chkUsb.Add_CheckedChanged({
        if ($chkUsb.Checked) {
            $txtOut.Visible = $false
            $btnOut.Visible = $false
            $cmbDrive.Visible = $true
            $btnRefresh.Visible = $true
            $btnCreate.Text = "Write to Target"
            # Auto-refresh drives
            $btnRefresh.PerformClick()
        } else {
            $txtOut.Visible = $true
            $btnOut.Visible = $true
            $cmbDrive.Visible = $false
            $btnRefresh.Visible = $false
            $btnCreate.Text = "Create Image"
        }
    })

    # Action button (single)
    $y += 35
    $btnCreate = New-Object System.Windows.Forms.Button
    $btnCreate.Text = "Create Image"
    $btnCreate.Location = New-Object System.Drawing.Point(220, $y)
    $btnCreate.Size = New-Object System.Drawing.Size(160, 30)
    $form.Controls.Add($btnCreate)

    # Log
    $y += 40
    $lblLog = New-Object System.Windows.Forms.Label
    $lblLog.Text = "Log:"
    $lblLog.Location = New-Object System.Drawing.Point(15, $y)
    $lblLog.AutoSize = $true
    $form.Controls.Add($lblLog)

    $y += 20
    $txtLog = New-Object System.Windows.Forms.TextBox
    $txtLog.Location = New-Object System.Drawing.Point(15, $y)
    $txtLog.Size = New-Object System.Drawing.Size(570, 120)
    $txtLog.Multiline = $true
    $txtLog.ReadOnly = $true
    $txtLog.ScrollBars = "Vertical"
    $txtLog.Font = New-Object System.Drawing.Font("Consolas", 8)
    $txtLog.Text = "Ready.`r`n"
    $form.Controls.Add($txtLog)

    # No external dependencies — all native Windows

    # Event handler
    $btnCreate.Add_Click({
        $src = $txtSrc.Text.Trim()
        if (-not $src) {
            [System.Windows.Forms.MessageBox]::Show("Source directory is required.", "Error", "OK", "Error")
            return
        }

        $toUsb = $chkUsb.Checked

        if ($toUsb) {
            # Validate USB drive selection
            if ($script:usbDrives.Count -eq 0 -or $cmbDrive.SelectedIndex -lt 0) {
                [System.Windows.Forms.MessageBox]::Show(
                    "No USB drive selected.`nClick Refresh if no drives appear.",
                    "Error", "OK", "Error")
                return
            }
            $targetDrive = $script:usbDrives[$cmbDrive.SelectedIndex]
        } else {
            $out = $txtOut.Text.Trim()
            if (-not $out) {
                [System.Windows.Forms.MessageBox]::Show("Output file is required.", "Error", "OK", "Error")
                return
            }
        }

        $includes = @()
        foreach ($item in $lstInc.Items) { $includes += $item.ToString() }

        $label = if ($txtLabel.Text.Trim()) { $txtLabel.Text.Trim() } else { "UEFITOOLS" }
        $sizeMB = if ($txtSize.Text -match '^\d+$') { [int]$txtSize.Text } else { 32 }

        $btnCreate.Enabled = $false
        $txtLog.AppendText("`r`n--- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ---`r`n")
        $txtLog.AppendText("Source: $src`r`n")
        if ($includes.Count -gt 0) { $txtLog.AppendText("Includes: $($includes -join ', ')`r`n") }
        $txtLog.AppendText("Format: $(if ($rbImg.Checked) { 'FAT32 (.img)' } else { 'ISO (.iso)' })`r`n")
        $txtLog.AppendText("Label: $label`r`n")
        if ($toUsb) {
            $txtLog.AppendText("Target: USB - $($targetDrive.Path) ($($targetDrive.Size) $($targetDrive.Model))`r`n")
        } else {
            $txtLog.AppendText("Target: File - $out`r`n")
        }
        $txtLog.AppendText("Verbose: $($chkVerbose.Checked), Verify: $($chkVerify.Checked), GPT: $($chkGpt.Checked)`r`n")
        $txtLog.AppendText("`r`nCreating image...`r`n")
        $form.Refresh()

        if ($toUsb) {
            # Format USB and copy files directly — no temp image needed
            $optSwitches = @{}
            if ($chkVerbose.Checked) { $optSwitches['Verbose'] = $true }
            if ($chkVerify.Checked) { $optSwitches['Verify'] = $true }
            if ($chkGpt.Checked) { $optSwitches['UseGpt'] = $true }
            Write-UsbDrive -TargetDrive $targetDrive -SourceDir $src `
                -Includes $includes -Label $label -LogBox $txtLog @optSwitches
        } else {
            $verboseSwitch = if ($chkVerbose.Checked) { @{Verbose=$true} } else { @{} }
            New-UefiImage -SourceDir $src -Includes $includes -OutputFile $out `
                -Label $label -SizeMB $sizeMB -LogBox $txtLog @verboseSwitch
        }

        $btnCreate.Enabled = $true
    })

    [void]$form.ShowDialog()
}

# --- Entry point ---
Show-MainForm
