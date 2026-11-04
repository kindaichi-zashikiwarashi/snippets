# mac_keyboard.ps1

Mac JIS キーボードを Windows で使うためのキーリマップツール。
サードパーティ製ソフト不要。PowerShell のみ。フックベースで **ON/OFF をライブ切り替え可能**。

## キーマッピング

| 物理キー | 変換後 |
|---------|--------|
| 左Ctrl | CapsLock |
| 左Alt (Option) | 左Ctrl |
| 英数 | 左Alt |
| F13 | Insert |
| F16 | PrintScreen |
| 英数 + 1 | IME オン/オフ |

> **Note:** Mac の Fn キーは OS から見えないため割り当て不可。代わりに F13 を Insert にしている。

## セットアップ

### 初回

1. `mac_keyboard.ps1` を右クリック → **「PowerShell で実行」**
2. 管理者権限の昇格プロンプトが出たら「はい」
3. **再起動**（ログアウトでは不可）

再起動して完了。

> **Note:** セットアップ時に、既存のレジストリ値 `Scancode Map` を削除する。別のキー配置をこの値で設定している場合は、先に控えておくこと。

## 日々の操作

| 操作 | スクリプト |
|------|----------|
| 一時無効化（ノートPC本体キーボード使用時など） | `mac_keyboard_off.bat` ダブルクリック |
| 再有効化 | `mac_keyboard_on.bat` ダブルクリック |
| Alt スタック緊急解除 | `unlock_alt.bat` ダブルクリック |

> **Note:** リマップは全てフックで処理されるので、フックを停止すれば即座にWindows標準動作に戻る。
> **Note:** `mac_keyboard_on.bat` 実行から実際にリマップが効くまで **2〜5秒** のラグがある（wscript → powershell → C#コンパイル → フック登録のチェーンが完了するまで）。

## 仕組み

全てのキーリマップを低レベルキーボードフック（`SetWindowsHookEx WH_KEYBOARD_LL`）で実装。
フック本体は `%APPDATA%\MacKeyboard\hook.ps1`、ログオン時にタスクスケジューラ `MacKeyboardHook` から起動。

| 種別 | 実装 |
|------|------|
| LCtrl→CapsLock, LAlt→LCtrl, F13→Insert | フック内で物理キーを抑止 → keybd_event で代替キー合成 |
| Eisu→Alt | キー押下中フラグ管理 + 他キー押下時に Alt 合成（IMEトグルとの両立のため） |
| Eisu+1→IME | VK_KANJI（半角/全角キー）を合成 |

**安全装置（Alt スタック対策）:**
- フック起動時に Alt-up を発射（前回クラッシュ時の残骸クリア）
- ProcessExit ハンドラで Alt-up
- 手動復旧用 `unlock_alt.bat`

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| 英数キーが Alt にならない | フックが動いているか確認、なければ `mac_keyboard_on.bat` |
| Alt+1 で IME が切り替わらない | タスクスケジューラで `MacKeyboardHook` が動いているか確認 |
| Alt が押しっぱなしの状態になった | `unlock_alt.bat` をダブルクリック（フックの2秒タイムアウトでも自動解除されるはず） |
| 設定を元に戻したい | タスク `MacKeyboardHook` を削除、`%APPDATA%\MacKeyboard\` を削除 |
| F1〜F12 が音量・明るさキーになる（このツール無関係） | `Fn + Esc` で Fn Lock トグル。Surface なら `Fn + Caps Lock`。効かなければ BIOS の "Action Keys Mode" / "Function Key Behavior" を変更 |

## ファイル

| ファイル | 説明 |
|---------|------|
| `mac_keyboard.ps1` | セットアップ本体 |
| `mac_keyboard_off.bat` / `mac_keyboard_on.bat` | 一時無効化 / 再有効化 |
| `unlock_alt.bat` | 緊急用: Alt がスタックした時にダブルクリックで解除 |
| `README.md` | これ |
