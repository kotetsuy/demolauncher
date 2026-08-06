# Demo Launcher — デモ起動管理ツール

NucBox EVO X2 (Ryzen AI MAX+ 395 / Radeon 8060S) 上の各種 AI デモを、
ボタン 1 つで排他的に起動・停止するための flet 製デスクトップ GUI。

展示・デモ会場での運用を想定しており、次の 3 点を保証することが目的:

1. **同時に 1 つのデモしか動かない** — デモ間でポートが重複しているため
   （`:8000` と `:8080` は 3〜4 デモが取り合う）、起動前に必ず全デモを停止する
2. **「起動しました」が実態と一致する** — スクリプトを投げっぱなしにせず、
   各デモのヘルスチェック URL が応答するまで待ってからステータスを更新する
3. **GUI から電源が落とせる** — 会場で端末を触らずに片付けられる

内部設計の詳細は [`TECHNICALJ.md`](./TECHNICALJ.md)、
過去のバグ調査記録は [`BUG.md`](./BUG.md) を参照。
English versions: [`README.md`](./README.md) / [`TECHNICAL.md`](./TECHNICAL.md)

---

## 管理対象のデモ

| ボタン | ディレクトリ | 使用ポート | 起動完了の判定 | 待ち時間上限 |
|---|---|---|---|---|
| AIassistant | `~/AIassistant` | 8000, 8001, 8080 | `http://localhost:8000/status` | 600s |
| LLaVA-NPU | `~/LLaVA-NPU` | 8080, 8081, 8082 | `http://localhost:8080/` | 120s |
| RealtimeDepth | `~/RealtimeDepth` | 8000 | `http://localhost:8000/` | 180s |
| EarthTourGuide | `~/EarthTourGuide` | 8000〜8003, 8080 | `http://localhost:8000/status` | 600s |
| AI2048 | `~/AI2048` | 8000, 8009, 8080, 9222 | `http://localhost:8000/status` | 600s |

各デモのディレクトリに `start_all.sh` と `stop_all.sh` があることが前提。
ランチャーはこの 2 本のシェルスクリプトを叩くだけで、デモの中身には関与しない。

> **LLaVA について**: 現行は `~/LLaVA-NPU` (kotetsuy/LLaVA-NPU)。
> YOLO11m を XDNA2 NPU 上で回すサイドカーが `config.yaml` の `npu.port`（既定 8082）で
> 待ち受けるため、旧 `~/LLaVA` より使用ポートが 1 つ多い。

---

## 必要なもの

| 項目 | 想定値 |
|---|---|
| マシン | NucBox EVO X2 (AMD Ryzen AI MAX+ 395, gfx1151, 48GB unified) |
| OS | Ubuntu 26.04 (resolute) |
| Python | 3.14。ただしシステム標準ではなくプロジェクト内の `.venv` を使う |
| GUI | flet 0.86.x (`flet` + `flet-desktop`) |
| ROCm | 7.14 (`/opt/rocm`) — ランチャー自体は非依存、各デモが使用 |
| デスクトップ | GNOME (Wayland)。`DISPLAY` または `WAYLAND_DISPLAY` が必要 |

ランチャー本体は ROCm にも GPU にも依存しない。ROCm を使うのは起動先のデモ側だけなので、
ROCm のバージョンを上げてもこのツールの改修は不要。

---

## セットアップ

### 1. venv の作成と flet のインストール

Ubuntu 26.04 のシステム Python 3.14 は PEP 668 の `EXTERNALLY-MANAGED` 扱いで、
`ensurepip` も無効化されている。ランチャーは専用の venv から起動する:

```bash
uv python install 3.14
cd ~/demolauncher
uv venv --managed-python --python 3.14 .venv
uv pip install --python .venv/bin/python flet flet-desktop
```

確認:

```bash
.venv/bin/python -c "import flet, flet_desktop; print(flet.__version__)"   # -> 0.86.5
```

> `flet-desktop` は明示的に入れること。デスクトップウィンドウの描画に必要で、
> 省略すると素の `flet` が初回起動時にダウンロードを試み、ドックから起動した
> 場合に無言で失敗する。

`.venv` は gitignore 済みなので、clone し直した環境ではこの手順をやり直す必要がある。

ユーザー領域ではなく venv を使う理由は、次の OS アップグレードに耐えるのは
ランチャー専用のインタプリタに固定する方式だから。24.04 → 26.04 のアップグレードで
このツールは一度壊れている。flet を `/usr/local/lib/python3.12/dist-packages` に
pip で入れていたため、`python3` が 3.14 になった時点で 3.12 のツリーごと
見えなくなった。このマシンの他プロジェクト（`~/AI2048`、`~/LLaVA-NPU`）も
同じ理由で venv を使っている。

`/usr/bin/python3.14` ではなく `uv` 管理のインタプリタを使う理由は、システムの
Python にリンクした venv では対策が中途半端だから。次のアップグレードで
`/usr/bin/python3.x` が入れ替わると venv のシンボリックリンクが切れ、同じ壊れ方を
繰り返す。`~/.local/share/uv/python` 配下の管理インタプリタはその影響を受けない。

### 2. デスクトップエントリを venv に向ける

`~/.local/share/applications/demo-launcher.desktop` は `python3` ではなく
venv のインタプリタを呼ぶこと:

```ini
Exec=/home/test/demolauncher/.venv/bin/python /home/test/demolauncher/demo_launcher.py
```

パスは 2 つとも絶対パスで書くこと（デスクトップエントリは `~` を展開しない）。
貼り付ける前に `pwd` で確認すること。

編集後はデスクトップデータベースを更新する:

```bash
update-desktop-database ~/.local/share/applications
```

### 3. 電源オフの sudoers 設定（初回のみ）

GUI にはパスワード入力手段が無いため、`shutdown` を NOPASSWD にしておく:

```bash
sudo bash setup_sudoers.sh
```

`/etc/sudoers.d/demo-launcher-shutdown` を作成し、`visudo -c` で構文検証する。
未実行の場合、「PC 電源オフ」を押すとステータス欄にその旨のエラーが出る
（黙って無反応にはならない）。

---

## 使い方

```bash
cd ~/demolauncher
.venv/bin/python demo_launcher.py
```

GNOME のアプリ一覧から「Demo Launcher」を選んでもよい。デスクトップエントリ経由で
同じコマンドが実行される。

- **各デモのボタン** — 全デモを停止 → ポート解放を待つ → 対象を起動 → ready まで待機。
  同じボタンを 2 回押しても問題ない（自分自身も停止対象に含まれる）
- **全て停止** — 5 デモすべてに `stop_all.sh` を流す。1 つ失敗しても残りは止め、
  失敗したデモ名をまとめて表示する
- **PC 電源オフ** — 確認ダイアログ → 全デモ停止 → `sudo -n shutdown -h now`

処理中はプログレスバーが出て全ボタンが無効化される（連打による競合の防止）。

多重起動はできない。既にランチャーが動いている状態で起動すると
`Demo Launcher は既に起動しています。多重起動はできません。` と表示して終了コード 0 で抜ける。

---

## トラブルシューティング

### `ModuleNotFoundError: No module named 'flet'`

`.venv/bin/python demo_launcher.py` ではなく `python3 demo_launcher.py` を実行している。
flet は `.venv` の中にしか入っておらず、システムの Python 3.14 には入れない方針。
venv 自体が無い場合（clone 直後など）は上記「1. venv の作成と flet のインストール」を実行する。

デスクトップエントリから起動して同じエラーが出る場合は、`Exec` 行が `python3` のまま。
「2. デスクトップエントリを venv に向ける」を参照。

### 「警告: DISPLAY が無いため Chrome が開けない可能性があります」

SSH 経由や autostart など GUI セッション外から起動している。各デモの `start_all.sh` は
最後に Chrome を開くため、この状態ではデモが起動しても画面が出ない。

### 起動が「タイムアウト（サービスが応答しませんでした）」で終わる

ヘルスチェック URL が待ち時間上限内に応答しなかった。まずデモ側を単体で確認する:

```bash
cd ~/<デモ名> && bash start_all.sh
```

llama-server のモデルロードは初回や大きい context 設定だと数分かかる。
恒常的に足りない場合は `demo_launcher.py` の `ready_timeout` を延ばす。

### 「一部停止に失敗」と出る

`stop_all.sh` が非ゼロ終了したか 120 秒でタイムアウトした。残存プロセスを確認する:

```bash
tmux ls
ss -ltnp | grep -E ':(8000|8001|8002|8003|8009|8080|8081|8082|9222)'
```

---

## ファイル構成

| ファイル | 役割 |
|---|---|
| `demo_launcher.py` | 本体（GUI + デモ制御）。これ 1 本で完結 |
| `setup_sudoers.sh` | 電源オフ用 sudoers 設定（初回のみ sudo 実行） |
| `icon.svg` | アイコン素材（現状コードからは未参照） |
| `BUG.md` | 2026-06-12 のバグ調査記録と修正状況 |
| `TECHNICALJ.md` | 内部設計 |
