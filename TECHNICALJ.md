# TECHNICALJ.md — Demo Launcher 内部設計

対象: `demo_launcher.py`（単一ファイル、391 行）
利用者向けの説明は [`READMEJ.md`](./READMEJ.md)、
この設計に至った経緯は [`BUG.md`](./BUG.md) を参照。
English version: [`TECHNICAL.md`](./TECHNICAL.md)

---

## 1. 全体構造

外部モジュールを持たない 1 ファイル構成。上から 4 層に分かれている。

| 層 | 行 | 内容 |
|---|---|---|
| 単一インスタンス制御 | 16–31 | abstract UNIX socket による多重起動防止 |
| 設定 | 38–72 | `DEMOS` 辞書。デモの追加・変更はここだけ |
| 低レベルヘルパ | 80–128 | HTTP 生存確認、ポート解放待ち、プロセスグループ kill |
| デモ制御 | 134–209 | `stop_demo` / `start_demo` / `wait_ready` / `stop_all_demos` |
| UI | 212–383 | flet の GUI とイベントハンドラ |

デモの中身（llama-server, tmux, Chrome, ROCm など）には一切関与しない。
ランチャーが知っているのは **`start_all.sh` / `stop_all.sh` を叩くこと**、
**ポート番号**、**ヘルスチェック URL** の 3 つだけで、これが唯一の契約になっている。
そのため ROCm のバージョンアップやデモ側の実装変更はランチャーに波及しない。

---

## 2. `DEMOS` スキーマ

```python
"AI2048": {
    "dir":           HOME / "AI2048",              # start_all.sh / stop_all.sh の置き場
    "ports":         [8000, 8009, 8080, 9222],     # 停止後に解放を待つポート
    "ready_url":     "http://localhost:8000/status",  # 起動完了の判定に叩く URL
    "ready_timeout": 600,                          # ready 待ちの上限（秒）
},
```

- **`ports`** は「このデモが bind するポートの全集合」。1 つでも漏れると
  `_wait_ports_free()` が早期に True を返し、次のデモが bind 衝突する
  （LLaVA-NPU の NPU サイドカー `:8082` がまさにこれで漏れていた）
- **`ready_url`** は「そのデモの中で最も遅く立ち上がるもの」を指すのが望ましい。
  AIassistant / EarthTourGuide / AI2048 が `:8000/status` を使っているのは、
  この endpoint が llama のロード完了後に初めて応答するため
- **`ready_timeout`** は llama-server のモデルロード時間で決まる。
  LLaVA-NPU だけ 120s と短いのは、`ready_url` が Web サーバ (`:8080/`) で、
  llama のロード完了を待たないため

### ポート衝突マトリクス

| ポート | AIassistant | LLaVA-NPU | RealtimeDepth | EarthTourGuide | AI2048 |
|---|---|---|---|---|---|
| 8000 | ● | | ● | ● | ● |
| 8001 | ● | | | ● | |
| 8002–8003 | | | | ● | |
| 8009 | | | | | ● |
| 8080 | ● | ● | | ● | ● |
| 8081–8082 | | ● | | | |
| 9222 | | | | | ● |

`:8000` を 4 デモ、`:8080` を 4 デモが取り合う。**排他起動が設計上の必須要件**で、
「複数デモの同時起動」は選択肢として存在しない。

---

## 3. 起動シーケンス（`start_demo` → `wait_ready`）

```
ボタン押下
  ├─ set_status(busy=True)            全ボタンを disabled 化
  └─ run_bg(task)                     page.run_thread 経由で非同期実行
       │
       ├─ 全デモに stop_demo()        ← 対象デモ自身も含む（重要）
       │    ├─ _kill_start_process()  前回の start_all.sh を killpg
       │    └─ bash stop_all.sh       check=True / timeout=120
       │
       ├─ _wait_ports_free(name)      対象デモのポートが空くまで最大 30s
       │
       ├─ Popen(bash start_all.sh, start_new_session=True)
       │
       └─ wait_ready()                ready_url を 2s 間隔でポーリング
            ├─ 成功 → 「起動しました」
            ├─ スクリプトが非 0 で終了 → その exit code を表示
            └─ ready_timeout 超過 → 「タイムアウト」
```

### なぜ対象デモ自身も止めるのか

`start_all.sh` の多くは「既に起動していれば exit 1」で防御している
（tmux セッションの存在チェック、PID ファイルの生存チェック）。
対象を止めずに起動すると、同じボタンの 2 回目が必ず失敗する。
先に全部止めてから起動すれば、ボタンの意味が常に
「**このデモだけが動いている状態にする**」という冪等な操作になる。

### `start_new_session=True` の役割

`start_all.sh` は内部で tmux / llama-server / Chrome を起動し、
`wait_http` で最大 600 秒ブロックする。この待機中に別のボタンが押されたとき、
`stop_all.sh` は個々のプロセスは殺すが **走行中の `start_all.sh` 自体は殺さない**。
生き残ったスクリプトが後から tmux を作り直したり Chrome を開いたりして、
新しいデモと競合する。

そこで独立したプロセスグループで起動し、`_running_starts` に `Popen` を保持しておき、
停止時に `os.killpg(pgid, SIGTERM)` → 3 秒待って `SIGKILL` で確実に刈り取る
（`_kill_start_process`, 108–128 行）。

---

## 4. 生存判定のルール

### `_http_ok()` — 4xx/5xx も「生きている」

```python
except urllib.error.HTTPError:
    return True
```

判定したいのは「アプリが正しく動くか」ではなく「**サーバプロセスが listen していて
HTTP を喋れるか**」。404 や 500 を返せるならプロセスは起動済みなので ready とみなす。
接続拒否・タイムアウトのみが失敗。

### `_port_free()` — connect ベース

`connect_ex()` が非 0 なら誰も listen していない。bind して確かめる方式ではないため、
`SO_REUSEADDR` 待ちの TIME_WAIT ソケットを誤検知しない。

### `_wait_ports_free()` のタイムアウトは「失敗」ではない

30 秒待って解放されなくても例外にせず、最後の判定結果を返してそのまま起動へ進む。
ここで止めてしまうと、無関係な常駐プロセスが同じポートを掴んでいるだけで
ランチャーが完全に使えなくなるため。bind 失敗は後段の `wait_ready` が
タイムアウトとして検出する。

---

## 5. 単一インスタンス制御

```python
_SINGLE_INSTANCE_ADDR = "\0demo_launcher_single_instance"
```

先頭の `\0` は Linux の **abstract socket namespace**。ファイルシステム上に実体を作らず、
プロセス終了時にカーネルが自動で解放する。

ロックファイル方式と比べて:

- クラッシュや `SIGKILL` でも残骸が残らない（stale lock の掃除が不要）
- `/tmp` の permission や tmpfs のクリアポリシーに依存しない

`_instance_lock_socket` にグローバル参照を残しているのは、GC でソケットが閉じられて
ロックが解けるのを防ぐため。この参照を消してはいけない。

判定は `import` 時（386 行）に行い、既に動いていればメッセージを出して
`SystemExit(0)` で抜ける。エラー扱いにしないのは、autostart から二重に呼ばれても
ログにエラーを残さないため。

---

## 6. UI のスレッドモデル

flet の UI 更新はイベントループ側で行う必要がある。素の `threading.Thread` から
`page.update()` を呼ぶと、更新が反映されない／例外になることがある（タイミング依存）。
さらに `except` 節も同じ `set_status()` を呼ぶため二重に失敗し、
daemon スレッドが黙って死んで「起動中...」のまま固まる。

```python
def run_bg(task):
    runner = getattr(page, "run_thread", None)
    if callable(runner):
        runner(task)
    else:
        threading.Thread(target=task, daemon=True).start()
```

`getattr` で存在確認しているのは、`page.run_thread` を持たない古い flet でも
起動だけはできるようにするフォールバック。flet 0.86 には存在するので通常は前者を通る。

`set_status(busy=True)` は `action_buttons` 全部を `disabled` にする。
start と stop のスレッドが並走すると、停止途中のポートに向かって起動が走るため。

---

## 7. 電源オフ

```python
subprocess.run(["sudo", "-n", "shutdown", "-h", "now"], capture_output=True, timeout=15)
```

`-n`（非対話）が要点。sudoers 未設定だと sudo はパスワードを要求するが、
GUI には入力手段が無いのでプロンプトのまま無応答になる。`-n` なら即座に非 0 で失敗し、
stderr をステータス欄に出せる。

シャットダウン前に `stop_all_demos()` をベストエフォートで実行する
（llama-server にモデルを綺麗にアンロードさせる意図）。

---

## 8. デモを追加するには

1. `DEMOS` にエントリを 1 つ足す（`dir` / `ports` / `ready_url` / `ready_timeout`）
2. `main()` の `page.add(...)` に `btn(...)` を 1 行足す

`ports` の洗い出しはデモを実際に起動して確認するのが確実:

```bash
cd ~/<デモ名> && bash start_all.sh
ss -ltnp | grep <該当プロセス>
```

設定ファイル側でポートが可変になっているもの（LLaVA-NPU の `config.yaml` の
`npu.port` など）は、既定値だけでなく実際の設定値を確認すること。

---

## 9. 既知の制約

- **デモの稼働状態を表示しない** — ステータス欄は最後に実行した操作の結果を出すだけで、
  デモの現在の生死をポーリングしていない。ランチャーを再起動すると
  「待機中」に戻り、既に動いているデモがあっても分からない
- **`stop_all.sh` の失敗は起動時には握り潰される** — `start_demo` の停止ループは
  ベストエフォート（これから起動し直すため）。停止失敗を知りたいときは
  「全て停止」ボタンを使う
- **`icon.svg` は未使用** — flet にはアセットディレクトリが渡っているが、
  コードからは参照していない
