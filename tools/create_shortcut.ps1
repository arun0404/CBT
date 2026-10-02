<#
.SYNOPSIS
    Creates a Desktop (and optionally Start Menu) shortcut for CBT.

.DESCRIPTION
    Points the shortcut at launch.vbs by default, which is what gives the
    silent, console-free launch. A .lnk cannot carry an icon of its own,
    so the icon is taken from the supplied .ico file; without -IconPath
    the shortcut inherits the generic wscript icon, which looks
    unfinished on a user's desktop.

    Run from the package root:
        powershell -ExecutionPolicy Bypass -File tools\create_shortcut.ps1

.PARAMETER Target
    'vbs' (default, silent) | 'exe' (frozen build) | 'bat' (visible console)

.PARAMETER IconPath
    Optional .ico file. Defaults to tools\CBT.ico when present.

.PARAMETER StartMenu
    Also create a Start Menu entry.
#>

[CmdletBinding()]
param(
    [ValidateSet('vbs', 'exe', 'bat')]
    [string]$Target = 'vbs',

    [string]$ShortcutName = 'CBT',

    [string]$IconPath,

    [switch]$StartMenu
)

$ErrorActionPreference = 'Stop'

# tools\ -> package root
$PackageRoot = Split-Path -Parent $PSScriptRoot

$TargetFile = switch ($Target) {
    'vbs' { Join-Path $PackageRoot 'launch.vbs' }
    'exe' { Join-Path $PackageRoot 'CBT.exe' }
    'bat' { Join-Path $PackageRoot 'run.bat' }
}

if (-not (Test-Path -LiteralPath $TargetFile)) {
    throw "Launcher target not found: $TargetFile"
}

if (-not $IconPath) {
    $defaultIcon = Join-Path $PSScriptRoot 'CBT.ico'
    if (Test-Path -LiteralPath $defaultIcon) { $IconPath = $defaultIcon }
}

function New-AppShortcut {
    param([string]$Directory)

    if (-not (Test-Path -LiteralPath $Directory)) {
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    }

    $linkPath = Join-Path $Directory "$ShortcutName.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($linkPath)

    $shortcut.TargetPath = $TargetFile

    # Critical: without this the launcher inherits whatever directory
    # Explorer happened to be in, which breaks any relative resolution.
    $shortcut.WorkingDirectory = $PackageRoot

    $shortcut.Description = 'Offline text-to-speech reader (runs locally, no internet required)'

    # 7 = minimized. Only meaningful for the .bat target; the VBS and the
    # windowed .exe are already invisible.
    $shortcut.WindowStyle = if ($Target -eq 'bat') { 7 } else { 1 }

    if ($IconPath -and (Test-Path -LiteralPath $IconPath)) {
        $shortcut.IconLocation = "$IconPath,0"
    }

    $shortcut.Save()

    # Release the COM object rather than waiting for GC.
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)

    Write-Host "Created: $linkPath"
}

New-AppShortcut -Directory ([Environment]::GetFolderPath('Desktop'))

if ($StartMenu) {
    $startMenuDir = Join-Path ([Environment]::GetFolderPath('Programs')) 'CBT'
    New-AppShortcut -Directory $startMenuDir
}

Write-Host ''
Write-Host "Target : $TargetFile"
Write-Host "Workdir: $PackageRoot"
if ($IconPath) { Write-Host "Icon   : $IconPath" } else { Write-Host 'Icon   : (default - supply tools\CBT.ico for a custom icon)' }
