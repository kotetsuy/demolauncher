# Demo Launcher — demo start/stop manager

A flet desktop GUI that starts and stops the AI demos on the NucBox EVO X2
(Ryzen AI MAX+ 395 / Radeon 8060S), one button per demo, mutually exclusive.

Built for running demos at events, it guarantees three things:

1. **Only one demo runs at a time** — the demos overlap heavily on ports
   (`:8000` is contended by four demos and `:8080` by six), so every start
   stops all demos first
2. **"Started" means it actually started** — instead of firing the script and
   forgetting it, the launcher polls each demo's health-check URL and only then
   updates the status line
3. **The machine can be powered off from the GUI** — so the venue can be packed
   up without touching a terminal

For internals see [`TECHNICAL.md`](./TECHNICAL.md); for the bug investigation
this design came out of, see [`BUG.md`](./BUG.md).
日本語版は [`READMEJ.md`](./READMEJ.md) / [`TECHNICALJ.md`](./TECHNICALJ.md)。

---

## Managed demos

| Button | Directory | Ports | Readiness check | Timeout |
|---|---|---|---|---|
| AIassistant | `~/AIassistant` | 8000, 8001, 8080 | `http://localhost:8000/status` | 600s |
| LLaVA-NPU | `~/LLaVA-NPU` | 8080, 8081, 8082 | `http://localhost:8080/` | 120s |
| RealtimeDepth | `~/RealtimeDepth` | 8000 | `http://localhost:8000/` | 180s |
| EarthTourGuide | `~/EarthTourGuide` | 8000–8003, 8080 | `http://localhost:8000/status` | 600s |
| AIjukebox | `~/AIjukebox` | 1234, 8080, 8100, 8765, 50021 | `http://localhost:8765/` | 600s |
| AIradio | `~/AIradio` | 1234, 8080, 8100, 8765, 50021 | `http://localhost:8765/` | 900s |
| AIreversi | `~/AIreversi` | 8000, 8081 | `http://localhost:8000/` | 360s |
| 3dslam3 | `~/3dslam3` | 8080 | Successful script exit (ROCm ready) | 210s |

Each demo directory must contain `start_all.sh` and `stop_all.sh`. The launcher
only invokes those two shell scripts; it knows nothing about what the demos do.

> **About LLaVA**: the current repository is `~/LLaVA-NPU` (kotetsuy/LLaVA-NPU).
> It runs YOLO11m on the XDNA2 NPU through a sidecar process that listens on
> `config.yaml`'s `npu.port` (8082 by default), so it uses one more port than
> the old `~/LLaVA`.

> **About AIjukebox / AIradio**: both stream through the same stack — VOICEVOX
> ENGINE in Docker (`:50021`), llama-server (`:8080`), Icecast (`:8100`),
> Liquidsoap's telnet control port (`:1234`) and the display server (`:8765`).
> `:8765` is the readiness check because it is the last thing `start_all.sh`
> brings up. Their `stop_all.sh` stops the Docker container too, so `:50021`
> really is released between demos.

> **About 3dslam3**: `start_all.sh` verifies `/api/health` and ROCm/DA3
> warmup. The launcher waits for successful script completion, then opens
> `http://127.0.0.1:8080/` in the default browser. This configuration uses
> the default port 8080; do not override `PORT` in the launch environment.

> **About AIreversi**: `:8000` is the game server and `:8081` is Player B's
> llama-server. Readiness is judged on `:8000/` alone — llama-server's
> `/health` answers `503` while the model is still loading, and the launcher
> treats any HTTP response as alive, so it cannot be used as the signal. The
> game screen therefore appears before Player B is ready to move.

---

## Requirements

| Item | Expected |
|---|---|
| Machine | NucBox EVO X2 (AMD Ryzen AI MAX+ 395, gfx1151, 48GB unified) |
| OS | Ubuntu 26.04 (resolute) |
| Python | 3.14, in a project venv at `.venv` (not the system interpreter) |
| GUI | flet 0.86.x (`flet` + `flet-desktop`) |
| ROCm | 7.14 (`/opt/rocm`) — used by the demos, not by the launcher |
| Desktop | GNOME (Wayland). `DISPLAY` or `WAYLAND_DISPLAY` must be set |

The launcher itself depends on neither ROCm nor the GPU. Only the demos it
starts use ROCm, so upgrading ROCm requires no change to this tool.

---

## Setup

### 1. Create the venv and install flet

Ubuntu 26.04's system Python 3.14 is marked `EXTERNALLY-MANAGED` (PEP 668) and
`ensurepip` is disabled, so the launcher runs from its own venv:

```bash
uv python install 3.14
cd ~/demolauncher
uv venv --managed-python --python 3.14 .venv
uv pip install --python .venv/bin/python flet flet-desktop
```

Verify:

```bash
.venv/bin/python -c "import flet, flet_desktop; print(flet.__version__)"   # -> 0.86.5
```

> Install `flet-desktop` explicitly. Rendering the desktop window needs it, and
> the base `flet` package would otherwise download it on first launch — which
> fails silently when the launcher is started from the dock.

`.venv` is gitignored, so a fresh clone has to run this step again.

Why a venv rather than the user site directory: pinning the launcher to an
interpreter it owns is what survives the next release upgrade. The 24.04 → 26.04
upgrade already broke this tool once — flet had been pip-installed into
`/usr/local/lib/python3.12/dist-packages`, and when `python3` became 3.14 the
whole 3.12 tree stopped being visible. Every other project on this machine
(`~/AIjukebox`, `~/LLaVA-NPU`) uses a venv for the same reason.

Why a `uv`-managed interpreter rather than `/usr/bin/python3.14`: a venv built
against the system interpreter is only half a fix. The next release upgrade
replaces `/usr/bin/python3.x` and leaves the venv with a dangling symlink,
reproducing the same breakage. The managed interpreter under
`~/.local/share/uv/python` is not touched by distro upgrades.

### 2. Point the desktop entry at the venv

`~/.local/share/applications/demo-launcher.desktop` must invoke the venv
interpreter, not `python3`:

```ini
Exec=/home/test/demolauncher/.venv/bin/python /home/test/demolauncher/demo_launcher.py
```

Both paths must be absolute — the desktop entry does not expand `~`. Check with
`pwd` before pasting.

After editing, refresh the desktop database:

```bash
update-desktop-database ~/.local/share/applications
```

### 3. Configure sudoers for power-off (once)

The GUI has no way to prompt for a password, so make `shutdown` NOPASSWD:

```bash
sudo bash setup_sudoers.sh
```

This writes `/etc/sudoers.d/demo-launcher-shutdown` and validates it with
`visudo -c`. If you skip it, pressing "PC 電源オフ" reports the failure in the
status line rather than silently doing nothing.

---

## Usage

```bash
cd ~/demolauncher
.venv/bin/python demo_launcher.py
```

Or launch "Demo Launcher" from the GNOME application grid, which runs the same
command via the desktop entry.

- **Demo buttons** — stop every demo, wait for the ports to be released, start
  the target, then wait until it is ready. Pressing the same button twice is
  safe, because the target is included in the stop set
- **全て停止 (Stop all)** — runs `stop_all.sh` for all eight demos. If one fails
  the rest are still stopped, and the failures are reported together
- **PC 電源オフ (Power off)** — confirmation dialog, stop all demos, then
  `sudo -n shutdown -h now`

While an operation is in flight a progress bar appears and every button is
disabled, which prevents concurrent start/stop races.

Only one instance can run. Starting a second one prints
`Demo Launcher は既に起動しています。多重起動はできません。` and exits with
status 0.

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'flet'`

You ran `python3 demo_launcher.py` instead of `.venv/bin/python
demo_launcher.py`. flet is only installed inside `.venv`; the system Python 3.14
does not have it and is not meant to. If the venv itself is missing (fresh
clone), see "1. Create the venv and install flet" above.

The same error from the desktop entry means its `Exec` line still says
`python3` — see "2. Point the desktop entry at the venv".

### "警告: DISPLAY が無いため Chrome が開けない可能性があります"

You started the launcher outside a GUI session (over SSH, from autostart, …).
Every `start_all.sh` opens Chrome as its last step, so nothing will appear on
screen even if the demo comes up.

### A start ends in "タイムアウト（サービスが応答しませんでした）"

The health-check URL did not respond within the timeout. Check the demo on its
own first:

```bash
cd ~/<demo> && bash start_all.sh
```

llama-server model loading can take minutes on a cold cache or with a large
context. If it is consistently too tight, raise `ready_timeout` in
`demo_launcher.py`.

### "一部停止に失敗" (some demos failed to stop)

`stop_all.sh` exited non-zero or hit the 120 s timeout. Look for survivors:

```bash
tmux ls
docker ps                    # AIjukebox / AIradio leave VOICEVOX behind
ss -ltnp | grep -E ':(1234|800[0-3]|808[0-2]|8100|8765|50021)'
```

---

## Files

| File | Role |
|---|---|
| `demo_launcher.py` | The whole thing: GUI plus demo control |
| `setup_sudoers.sh` | sudoers setup for power-off (run once with sudo) |
| `icon.svg` | Icon asset (not referenced from the code yet) |
| `BUG.md` | Bug investigation from 2026-06-12 and its resolution |
| `TECHNICAL.md` | Internals |
