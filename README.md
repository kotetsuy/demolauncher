# Demo Launcher — demo start/stop manager

A flet desktop GUI that starts and stops the AI demos on the NucBox EVO X2
(Ryzen AI MAX+ 395 / Radeon 8060S), one button per demo, mutually exclusive.

Built for running demos at events, it guarantees three things:

1. **Only one demo runs at a time** — the demos overlap heavily on ports
   (`:8000` and `:8080` are each contended by four demos), so every start
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
| AI2048 | `~/AI2048` | 8000, 8009, 8080, 9222 | `http://localhost:8000/status` | 600s |

Each demo directory must contain `start_all.sh` and `stop_all.sh`. The launcher
only invokes those two shell scripts; it knows nothing about what the demos do.

> **About LLaVA**: the current repository is `~/LLaVA-NPU` (kotetsuy/LLaVA-NPU).
> It runs YOLO11m on the XDNA2 NPU through a sidecar process that listens on
> `config.yaml`'s `npu.port` (8082 by default), so it uses one more port than
> the old `~/LLaVA`.

---

## Requirements

| Item | Expected |
|---|---|
| Machine | NucBox EVO X2 (AMD Ryzen AI MAX+ 395, gfx1151, 48GB unified) |
| OS | Ubuntu 26.04 (resolute) |
| Python | 3.14 (system interpreter) |
| GUI | flet 0.86.x (`flet[all]`) |
| ROCm | 7.14 (`/opt/rocm`) — used by the demos, not by the launcher |
| Desktop | GNOME (Wayland). `DISPLAY` or `WAYLAND_DISPLAY` must be set |

The launcher itself depends on neither ROCm nor the GPU. Only the demos it
starts use ROCm, so upgrading ROCm requires no change to this tool.

---

## Setup

### 1. Install flet

Ubuntu 26.04's system Python 3.14 is marked `EXTERNALLY-MANAGED` (PEP 668) and
`ensurepip` is disabled, so install into the user site directory with `uv`:

```bash
uv pip install --python /usr/bin/python3 \
  --target ~/.local/lib/python3.14/site-packages "flet[all]"
```

The `--target` path must match what
`python3 -c "import site; print(site.getusersitepackages())"` prints. Then
`python3` picks it up with no further configuration.

Verify:

```bash
python3 -c "import flet, flet_desktop; print(flet.__version__)"   # -> 0.86.2
```

> Install `flet[all]`, not plain `flet`. Rendering the desktop window needs
> `flet_desktop`, which the base package does not include.

### 2. Configure sudoers for power-off (once)

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
python3 demo_launcher.py
```

- **Demo buttons** — stop every demo, wait for the ports to be released, start
  the target, then wait until it is ready. Pressing the same button twice is
  safe, because the target is included in the stop set
- **全て停止 (Stop all)** — runs `stop_all.sh` for all five demos. If one fails
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

flet is not installed in Python 3.14's user site directory — see
"1. Install flet" above. A flet living in the old
`~/.local/lib/python3.12/site-packages` is invisible to Python 3.14.

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
ss -ltnp | grep -E ':(8000|8001|8002|8003|8009|8080|8081|8082|9222)'
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
