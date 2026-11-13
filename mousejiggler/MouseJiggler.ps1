# MouseJiggler.ps1
# 10秒ごとに、マウスを左右に約5〜6cm（小刻みに）動かすツール
# Slack等の在席判定（システムアイドル時間）をリセットするため、
# 座標の書き換え(SetCursorPos)ではなく、実際の入力イベント(mouse_event)を注入します。
#
# 使い方:
#   PowerShell で次を実行
#     powershell -ExecutionPolicy Bypass -File .\MouseJiggler.ps1
#   止めるとき: Ctrl + C
#
# パラメータ (任意で変更可):
#   -IntervalSeconds : 動作の間隔（秒）。既定 10
#   -DistanceCm      : 左右に動かす距離（cm）。既定 5.5
#   -StepPixels      : 1ステップあたりの移動量（px）。小さいほど小刻み。既定 4
#   -StepDelayMs     : 1ステップ間の待ち時間（ms）。既定 8

param(
    [double]$IntervalSeconds = 10,
    [double]$DistanceCm      = 5.5,
    [int]$StepPixels         = 4,
    [int]$StepDelayMs        = 8
)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Mouse {
    // 相対移動の実入力を注入する。これによりシステムアイドルタイマー
    // (GetLastInputInfo) がリセットされ、Slack等が在席と認識する。
    [DllImport("user32.dll")]
    public static extern void mouse_event(uint dwFlags, int dx, int dy, uint dwData, IntPtr dwExtraInfo);
    // 画面の水平方向DPIを取得（cm→px換算用）
    [DllImport("gdi32.dll")]
    public static extern int GetDeviceCaps(IntPtr hdc, int nIndex);
    [DllImport("user32.dll")]
    public static extern IntPtr GetDC(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
}
"@ -ErrorAction SilentlyContinue

$MOUSEEVENTF_MOVE = 0x0001  # 相対移動

# cm を ピクセル に換算（画面のDPIを使用、取得できなければ96dpiとみなす）
function Get-ScreenDpiX {
    try {
        $hdc = [Mouse]::GetDC([IntPtr]::Zero)
        # 88 = LOGPIXELSX
        $dpi = [Mouse]::GetDeviceCaps($hdc, 88)
        [Mouse]::ReleaseDC([IntPtr]::Zero, $hdc) | Out-Null
        if ($dpi -le 0) { return 96 }
        return $dpi
    } catch {
        return 96
    }
}

$dpiX = Get-ScreenDpiX
# 1インチ = 2.54cm。 距離(px) = cm / 2.54 * dpi
$distancePx = [int][math]::Round($DistanceCm / 2.54 * $dpiX)
if ($distancePx -lt 1) { $distancePx = 1 }

Write-Host "マウスジグラーを開始しました。" -ForegroundColor Green
Write-Host ("  間隔        : {0} 秒" -f $IntervalSeconds)
Write-Host ("  移動距離    : {0} cm (約 {1} px, DPI={2})" -f $DistanceCm, $distancePx, $dpiX)
Write-Host ("  1ステップ   : {0} px / {1} ms" -f $StepPixels, $StepDelayMs)
Write-Host "  方式        : mouse_event(相対移動)でアイドルタイマーをリセット"
Write-Host "止めるには Ctrl + C を押してください。" -ForegroundColor Yellow
Write-Host ""

# 指定方向に小刻みに相対移動するヘルパー（実入力を注入）
function Move-Smooth([int]$deltaX) {
    $sign = [math]::Sign($deltaX)
    $remaining = [math]::Abs($deltaX)
    $moved = 0
    while ($moved -lt $remaining) {
        $stepNow = [math]::Min($StepPixels, $remaining - $moved)
        $moved += $stepNow
        [Mouse]::mouse_event($MOUSEEVENTF_MOVE, ($sign * $stepNow), 0, 0, [IntPtr]::Zero)
        Start-Sleep -Milliseconds $StepDelayMs
    }
}

try {
    while ($true) {
        # 右へ小刻みに移動 → 元の位置へ小刻みに戻る（実入力なのでアイドル解除）
        Move-Smooth  $distancePx
        Move-Smooth (-$distancePx)
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    Write-Host ""
    Write-Host "マウスジグラーを停止しました。" -ForegroundColor Cyan
}
