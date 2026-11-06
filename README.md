# Echo Dot 2 USB Speaker and Microphone Bridge

Echo Dot 第2世代をUSB経由でWindows PCのスピーカー・マイクとして使うための、PC側ブリッジです。Echo本体の中央マイクとスピーカーを利用し、マイク信号にはWebRTC AEC3による音響エコー低減を適用します。

このプロジェクトは**USB接続**です。Bluetoothマイク化、Wi-Fi音声転送、Echo単体でのChatGPT/Alexa代替、OpenAI API接続は行いません。音声チャットアプリはWindows PC上で動かします。

## できること

- USB ADB経由の双方向音声転送
- Windowsアプリから利用する仮想スピーカー・マイク経路（Voicemeeter Banana）
- Echoのマイク入力と同時取得したスピーカー参照信号を使うWebRTC AEC3処理
- 接続、停止、マイクミュート、再生音量の操作画面
- USB接続待ちと上限付き自動再接続

ブリッジは音声データをファイルに保存せず、クラウドAPIへ送信しません。音声は選択したADB経路でPCへ運び、WindowsのVoicemeeter仮想音声端点を通って利用アプリに渡されます。ネットワークADBを指定した場合は、そのADB接続を通ります。USBが検証済みの接続方式です。Windows上の音声アプリが行う通信や録音は、そのアプリ側の動作です。

## 構成

```text
Windowsアプリの再生
  → Voicemeeter Input / B2
  → USB ADB音声ブリッジ
  → Echo Dotスピーカー

Echo Dot中央マイク + スピーカー参照信号
  → USB ADB音声ブリッジ
  → WebRTC AEC3
  → Voicemeeter AUX Input / B1
  → Windowsアプリのマイク
```

標準USB Audio Classデバイスにはなりません。PC側の仮想デバイスを介して音声アプリへ接続します。

## 必要な環境

- Echo Dot 第2世代（Fire OS 6系、`biscuit_puffin`）
- 既にroot ADBが利用でき、`tinycap` / `tinyplay` が本体にある環境
- Windows PC、USBデータケーブル、ADB Platform Tools（`adb` を `PATH` に設定）
- 64-bit Python 3.10
- Voicemeeter Bananaとその仮想音声デバイス

このリポジトリには、端末のroot化・ブートローダー解除・ファームウェア書き込み手順やファームウェア、端末側バイナリを含めていません。端末の改造状態によっては利用できません。

## セットアップ

PowerShellでリポジトリのフォルダーを開き、Python 3.10の仮想環境を作成します。

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

ADBで対象のEchoが認識されることを確認し、対象端末のシリアルをコマンド引数で明示して起動します。

```powershell
adb devices
.\.venv\Scripts\python.exe tools\echo_audio.py --serial "YOUR_ADB_SERIAL" --auto
```

`--serial` の代わりに環境変数 `ECHO_DOT_SERIAL` を設定できます。ADBが `PATH` にない場合は、`ADB_PATH` に `adb.exe` の場所を設定してください。Voicemeeterを標準外の場所へ入れた場合は `VOICEMEETER_DIR` を設定できます。

必要に応じて、VoicemeeterのStrip 3/4ルーティングを次のコマンドで設定します。実行前に現在のVoicemeeter設定を確認してください。既存値の控えをリポジトリ直下の `voicemeeter-before-echo.json` に保存します。

```powershell
.\.venv\Scripts\python.exe tools\vm_audio.py
```

音声アプリの入出力には `Voicemeeter Input` と `Voicemeeter Out B1` を選びます。自動起動を設定する場合は、Windowsのスタートアップへ自分でショートカットを追加してください。

## 検証

オフラインの回帰テストとPython構文確認は次で実行できます。

```powershell
.\tools\verify-audio.ps1
```

物理Echoでの動作は、Echo Dot第2世代とWindowsの組み合わせで確認しています。1回の小音量合成音声試験では、AEC処理後のマイク信号全体が処理前より約7.3 dB低下しました。この値は背景音を含む全信号の比較であり、エコーだけの除去率や完全なエコー除去を示すものではありません。実会話、部屋の違い、最大音量、長時間運転、Windows再サインイン後の自動起動は保証していません。

## 注意点

- 接続中はEcho側の通常mixerを止めて音声経路を切り替えます。停止時に通常状態へ戻します。
- USBが検証済みの接続方式です。ブリッジ自体はネットワーク接続を開始しませんが、ADBの接続先がUSBかどうかは検査しません。USBで接続した端末を指定してください。
- 起動時にデバイス型番とroot ADBを確認します。対象機器が一致しない場合は停止します。
- 利用には、既にroot ADBが使える対象端末が必要です。改造前のEchoへそのままインストールするアプリではありません。
- AECの効果はマイク、スピーカー、部屋、音量、発話の重なり方で変わります。
- この方式はBluetooth音声や無線マイクには対応しません。

## 公開範囲

このリポジトリには、ブリッジのソース、依存パッケージ一覧、オフラインテスト、一般化した使用説明のみを置いています。端末シリアル、PCのユーザーパス、実機ログ、測定データJSON、録音、秘密鍵、APIキー、ファームウェア、ドライバーやインストーラーのバイナリは含めません。
