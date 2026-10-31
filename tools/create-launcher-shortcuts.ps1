param(
    [string]$Catalog,
    [string]$DataDirectory,
    [string]$DesktopDirectory = [Environment]::GetFolderPath('Desktop'),
    [switch]$SkipStartMenu
)
$ErrorActionPreference = 'Stop'
$launcherRepo = Split-Path -Parent $PSScriptRoot
$launcherPython = Join-Path $launcherRepo '.venv\Scripts\pythonw.exe'
$launcherIcon = Join-Path $launcherRepo 'mc_manager\launcher\assets\launcher.ico'
foreach ($required in @($launcherPython, $launcherIcon)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Missing launcher file: $required" }
}

# Match branding.py so an opened/pinned shortcut belongs to the launcher group.
# https://learn.microsoft.com/windows/win32/shell/appids
if (-not ('Monkeycraft.ShortcutIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Monkeycraft {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid Format; public uint Id; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, IntPtr value);
        [PreserveSig] int SetValue(ref PropertyKey key, IntPtr value);
        [PreserveSig] int Commit();
    }
    public static class ShortcutIdentity {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr bindContext, uint flags,
            ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
        public static void Set(string path, string appId) {
            Guid iid = typeof(IPropertyStore).GUID;
            IPropertyStore store;
            Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store));
            IntPtr text = Marshal.StringToCoTaskMemUni(appId);
            IntPtr value = Marshal.AllocCoTaskMem(24);
            try {
                for (int n = 0; n < 24; n++) Marshal.WriteByte(value, n, 0);
                Marshal.WriteInt16(value, 31); // VT_LPWSTR
                Marshal.WriteIntPtr(value, 8, text);
                PropertyKey key = new PropertyKey { Format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Id = 5 };
                Marshal.ThrowExceptionForHR(store.SetValue(ref key, value));
                Marshal.ThrowExceptionForHR(store.Commit());
            } finally {
                Marshal.FreeCoTaskMem(value);
                Marshal.FreeCoTaskMem(text);
                Marshal.ReleaseComObject(store);
            }
        }
    }
}
'@
}

function Quote-LauncherArgument([string]$Value) {
    if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) { throw 'Invalid shortcut argument' }
    # Double trailing backslashes before the closing quote (Windows argv rules).
    return '"' + [regex]::Replace($Value, '(\\+)$', '$1$1') + '"'
}
$launcherArguments = '-m mc_manager.launcher.desktop'
if ($Catalog) { $launcherArguments += ' --catalog ' + (Quote-LauncherArgument $Catalog) }
if ($DataDirectory) { $launcherArguments += ' --data-directory ' + (Quote-LauncherArgument $DataDirectory) }
$launcherDestinations = @($DesktopDirectory)
if (-not $SkipStartMenu) { $launcherDestinations += [Environment]::GetFolderPath('Programs') }
$launcherShell = New-Object -ComObject WScript.Shell
try {
    foreach ($directory in $launcherDestinations) {
        $shortcutPath = Join-Path $directory 'Monkeycraft Launcher.lnk'
        $shortcut = $launcherShell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $launcherPython
        $shortcut.Arguments = $launcherArguments
        $shortcut.WorkingDirectory = $launcherRepo
        $shortcut.IconLocation = "$launcherIcon,0"
        $shortcut.Description = 'Monkeycraft Launcher - Minecraft Java and modpacks'
        $shortcut.WindowStyle = 1
        $shortcut.Save()
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut)
        [Monkeycraft.ShortcutIdentity]::Set($shortcutPath, 'Monkeycraft.Launcher')
        Write-Output "Created $shortcutPath"
    }
} finally {
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($launcherShell)
}
