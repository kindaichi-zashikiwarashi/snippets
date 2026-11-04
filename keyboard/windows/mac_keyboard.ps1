# mac_keyboard.ps1 - Mac JIS keyboard -> Windows layout
# Right-click -> "Run with PowerShell"
#
# Hook-only architecture (no Scancode Map):
#   LCtrl  -> CapsLock     (in hook)
#   LAlt   -> LCtrl        (in hook)
#   Eisu   -> LAlt         (in hook)
#   F13    -> Insert       (in hook)
#   Eisu+1 -> IME toggle   (in hook)
# This makes the entire remapping live-toggleable via on/off bat files.

$HOOK_SCRIPT = @"
`$logPath  = Join-Path `$env:APPDATA 'MacKeyboard\hook.log'
`$flagPath = Join-Path `$env:APPDATA 'MacKeyboard\disabled.flag'
"START `$(Get-Date -Format 'HH:mm:ss')" | Out-File `$logPath -Encoding UTF8

if (Test-Path `$flagPath) {
    "DISABLED flag found, exiting" | Out-File `$logPath -Append -Encoding UTF8
    exit 0
}

try {
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;

public class MacKeyHook {
    const int  WH_KEYBOARD_LL  = 13;
    const int  WH_MOUSE_LL     = 14;
    const int  WM_KEYDOWN      = 0x0100;
    const int  WM_SYSKEYDOWN   = 0x0104;
    const int  WM_KEYUP        = 0x0101;
    const int  WM_SYSKEYUP     = 0x0105;
    const int  WM_LBUTTONDOWN  = 0x0201;
    const int  WM_RBUTTONDOWN  = 0x0204;
    const int  WM_MBUTTONDOWN  = 0x0207;
    const int  WM_XBUTTONDOWN  = 0x020B;

    const uint VK_LCONTROL = 0xA2;
    const uint VK_LMENU    = 0xA4;
    const uint VK_CAPITAL  = 0x14;
    const uint VK_INSERT   = 0x2D;
    const uint VK_KANJI    = 0x19;
    const uint VK_F13      = 0x7C;
    const uint VK_F16      = 0x7F;
    const uint VK_SNAPSHOT = 0x2C;
    const uint VK_1        = 0x31;
    const uint VK_EISU     = 0x1A;

    const byte SC_LCONTROL = 0x1D;
    const byte SC_LALT     = 0x38;
    const byte SC_CAPITAL  = 0x3A;
    const byte SC_INSERT   = 0x52;
    const byte SC_KANJI    = 0x29;

    const uint KEYEVENTF_KEYUP       = 0x0002;
    const uint KEYEVENTF_EXTENDEDKEY = 0x0001;
    const uint LLKHF_INJECTED        = 0x10;

    [StructLayout(LayoutKind.Sequential)]
    struct KBDLLHOOKSTRUCT { public uint vk, sc, fl, t; public IntPtr e; }
    [StructLayout(LayoutKind.Sequential)]
    struct MSG { public IntPtr hw; public uint msg; public UIntPtr wp; public IntPtr lp; public uint t; public int x, y; }

    delegate IntPtr LLProc(int n, IntPtr w, IntPtr l);

    [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int id, LLProc cb, IntPtr h, uint t);
    [DllImport("user32.dll")] static extern bool   UnhookWindowsHookEx(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr h, int n, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern int    GetMessage(out MSG m, IntPtr h, uint mn, uint mx);
    [DllImport("user32.dll")] static extern bool   TranslateMessage(ref MSG m);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref MSG m);
    [DllImport("user32.dll")] static extern void   keybd_event(byte vk, byte scan, uint flags, IntPtr extra);

    static LLProc hookProc   = Callback;
    static LLProc mouseProc  = MouseCallback;
    static IntPtr hookId     = IntPtr.Zero;
    static IntPtr mouseId    = IntPtr.Zero;
    static bool   eisuHeld   = false;
    static bool   altSent    = false;
    static DateTime altSentAt = DateTime.MinValue;
    static string logPath;

    static void Log(string s) {
        try { File.AppendAllText(logPath, DateTime.Now.ToString("HH:mm:ss.fff") + " " + s + "\r\n"); } catch {}
    }

    public static void Install(string logFile) {
        logPath = logFile;
        Log("Installing hook");
        keybd_event((byte)VK_LMENU, SC_LALT, KEYEVENTF_KEYUP, IntPtr.Zero);
        keybd_event(0x12,           SC_LALT, KEYEVENTF_KEYUP, IntPtr.Zero);
        AppDomain.CurrentDomain.ProcessExit += (s, e) => ReleaseAlt();
        hookId  = SetWindowsHookEx(WH_KEYBOARD_LL, hookProc,  IntPtr.Zero, 0);
        mouseId = SetWindowsHookEx(WH_MOUSE_LL,    mouseProc, IntPtr.Zero, 0);
        Log("SetWindowsHookEx returned " + hookId.ToString());
        MSG msg;
        while (GetMessage(out msg, IntPtr.Zero, 0, 0) > 0) {
            TranslateMessage(ref msg);
            DispatchMessage(ref msg);
        }
        UnhookWindowsHookEx(hookId);
        UnhookWindowsHookEx(mouseId);
        ReleaseAlt();
    }

    static IntPtr MouseCallback(int nCode, IntPtr wParam, IntPtr lParam) {
        if (nCode >= 0 && eisuHeld && !altSent) {
            int m = (int)wParam;
            if (m == WM_LBUTTONDOWN || m == WM_RBUTTONDOWN || m == WM_MBUTTONDOWN || m == WM_XBUTTONDOWN) {
                SendLAlt(true);
                altSent = true;
            }
        }
        return CallNextHookEx(mouseId, nCode, wParam, lParam);
    }

    static void ReleaseAlt() {
        if (altSent) {
            keybd_event((byte)VK_LMENU, SC_LALT, KEYEVENTF_KEYUP, IntPtr.Zero);
            altSent = false;
        }
    }

    static void SendLAlt(bool down) {
        keybd_event((byte)VK_LMENU, SC_LALT, down ? 0u : KEYEVENTF_KEYUP, IntPtr.Zero);
        altSentAt = down ? DateTime.Now : DateTime.MinValue;
    }

    static IntPtr Callback(int nCode, IntPtr wParam, IntPtr lParam) {
        if (nCode >= 0) {
            var  k  = (KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(KBDLLHOOKSTRUCT));
            int  m  = (int)wParam;
            bool dn = m == WM_KEYDOWN || m == WM_SYSKEYDOWN;
            bool up = m == WM_KEYUP   || m == WM_SYSKEYUP;

            if ((k.fl & LLKHF_INJECTED) != 0) return CallNextHookEx(hookId, nCode, wParam, lParam);

            if (k.vk == VK_LCONTROL) {
                keybd_event((byte)VK_CAPITAL, SC_CAPITAL, dn ? 0u : KEYEVENTF_KEYUP, IntPtr.Zero);
                return (IntPtr)1;
            }
            if (k.vk == VK_LMENU) {
                keybd_event((byte)VK_LCONTROL, SC_LCONTROL, dn ? 0u : KEYEVENTF_KEYUP, IntPtr.Zero);
                return (IntPtr)1;
            }
            if (k.vk == VK_F13) {
                uint fl = KEYEVENTF_EXTENDEDKEY | (dn ? 0u : KEYEVENTF_KEYUP);
                keybd_event((byte)VK_INSERT, SC_INSERT, fl, IntPtr.Zero);
                return (IntPtr)1;
            }
            if (k.vk == VK_F16) {
                uint fl = KEYEVENTF_EXTENDEDKEY | (dn ? 0u : KEYEVENTF_KEYUP);
                keybd_event((byte)VK_SNAPSHOT, 0x37, fl, IntPtr.Zero);
                return (IntPtr)1;
            }
            if (k.vk == VK_EISU) {
                if (dn) { eisuHeld = true; altSent = false; }
                if (up) {
                    if (altSent) SendLAlt(false);
                    eisuHeld = false;
                    altSent  = false;
                }
                return (IntPtr)1;
            }

            if (eisuHeld) {
                if (dn && k.vk == VK_1) {
                    ToggleIME();
                    return (IntPtr)1;
                }
                if (dn && !altSent) {
                    SendLAlt(true);
                    altSent = true;
                }
            }
        }
        return CallNextHookEx(hookId, nCode, wParam, lParam);
    }

    static void ToggleIME() {
        keybd_event((byte)VK_KANJI, SC_KANJI, 0, IntPtr.Zero);
        keybd_event((byte)VK_KANJI, SC_KANJI, KEYEVENTF_KEYUP, IntPtr.Zero);
    }
}
'@
[MacKeyHook]::Install(`$logPath)
} catch {
    "ERROR: `$_" | Out-File `$logPath -Append -Encoding UTF8
}
"@

# Elevate to admin
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")) {
    Start-Process powershell.exe "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

Write-Host ""
Write-Host "=== Mac keyboard setup (hook-only) ===" -ForegroundColor Cyan
Write-Host ""

# 1. Clear existing Scancode Map (migrated to hook)
$keyPath = "HKLM:\SYSTEM\CurrentControlSet\Control\Keyboard Layout"
if ((Get-ItemProperty -Path $keyPath -Name "Scancode Map" -ErrorAction SilentlyContinue)."Scancode Map") {
    Remove-ItemProperty -Path $keyPath -Name "Scancode Map" -Force
    Write-Host "[OK] Cleared Scancode Map (reboot required to fully unload)" -ForegroundColor Green
} else {
    Write-Host "[OK] Scancode Map already absent" -ForegroundColor Green
}

# 2. Kill any running hook
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like "*MacKeyboard\hook.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 3. Deploy hook + VBS launcher (VBS = fully hidden window)
$hookDir     = "$env:APPDATA\MacKeyboard"
$hookScript  = "$hookDir\hook.ps1"
$vbsLauncher = "$hookDir\hook_launcher.vbs"
New-Item -ItemType Directory -Force $hookDir | Out-Null
$HOOK_SCRIPT | Out-File -FilePath $hookScript -Encoding UTF8
@'
Set s = CreateObject("WScript.Shell")
p = s.ExpandEnvironmentStrings("%APPDATA%") & "\MacKeyboard\hook.ps1"
s.Run "powershell.exe -ExecutionPolicy Bypass -File """ & p & """", 0, True
'@ | Out-File -FilePath $vbsLauncher -Encoding ASCII
Write-Host "[OK] Hook deployed to $hookScript" -ForegroundColor Green

# 4. Register startup task (via VBS launcher = no visible window)
$action   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbsLauncher`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Critical: disable battery power restrictions (or task dies when unplugging adapter)
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit 0 `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries     = $false
Register-ScheduledTask -TaskName "MacKeyboardHook" `
    -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "[OK] Startup task registered (hidden, battery-resilient, auto-restart)" -ForegroundColor Green

Write-Host ""
Write-Host ">>> REBOOT NOW to complete migration <<<" -ForegroundColor Yellow
Write-Host "    (Scancode Map is cleared from registry but still loaded in kernel until reboot)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "After reboot:" -ForegroundColor Cyan
Write-Host "  - Hook auto-starts and handles all remapping"
Write-Host "  - mac_keyboard_off.bat  → temporarily disable (use laptop keyboard normally)"
Write-Host "  - mac_keyboard_on.bat   → re-enable"
Write-Host ""
Read-Host "Press Enter to close"
