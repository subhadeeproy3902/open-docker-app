# Installing Berth on Windows

Berth is free and open source. You do not need an account, a licence key or a code-signing certificate to install it.
Windows only shows a one-time SmartScreen notice (explained below) because the `.exe` is not signed by a paid certificate.

| | |
|---|---|
| Supported | Windows 10 (21H2+) and Windows 11, 64-bit |
| Download | `Berth-windows-x64.zip` from the [latest release](https://github.com/subhadeeproy3902/open-docker-app/releases/latest) |
| Size | about 80 MB unzipped (it carries its own GTK 4 runtime); no installer, nothing written to the registry |

There are two ways to run Berth on Windows. **Option A** is the native app; **Option B** runs the Linux build inside
WSL 2, which is the smoothest today if you already use WSL.

---

## Option A — native Windows app

### 1. Give Berth an engine to talk to

**Docker Desktop**: nothing to set up — Berth talks to it through its named pipe (`\\.\pipe\docker_engine`), the
same way the `docker` command does. Just have Docker Desktop running. Podman Desktop's pipe is found too.

**Docker Engine inside WSL 2** (no Docker Desktop needed). Berth reaches it over TCP. In an Ubuntu WSL terminal:

```sh
sudo apt install docker.io                    # the Docker Engine
sudo mkdir -p /etc/systemd/system/docker.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/bin/dockerd -H fd:// -H tcp://127.0.0.1:2375\n' \
  | sudo tee /etc/systemd/system/docker.service.d/tcp.conf
sudo systemctl daemon-reload && sudo systemctl restart docker
```

WSL forwards `localhost` to Windows; in Berth pick **Settings → Docker Engine → Other engine** `tcp://localhost:2375`. (Needs systemd in WSL: it is on by
default in current Ubuntu images; otherwise add `[boot]` `systemd=true` to `/etc/wsl.conf` and run `wsl --shutdown`.)

> Security note (WSL setup only): port 2375 has no password. It listens on `127.0.0.1` only, so only programs on your own PC
> can reach it. Never expose 2375 on a network interface.

### 2. Install Berth

1. Download **`Berth-windows-x64.zip`** from the [latest release](https://github.com/subhadeeproy3902/open-docker-app/releases/latest).
2. Right-click the zip → **Properties** → tick **Unblock** (if shown) → **OK**. This skips most SmartScreen prompts.
3. Right-click → **Extract All…**, for example into `C:\Users\<you>\Apps\Berth`.
4. Double-click **`Berth.exe`**.
5. If a blue **"Windows protected your PC"** window appears: click **More info** → **Run anyway**. Windows remembers
   the choice.
6. Optional: right-click `Berth.exe` → **Show more options → Send to → Desktop (create shortcut)**, or pin it to Start.

To update, delete the folder and extract the new zip in its place; your settings are kept.

---

## Option B — the Linux build inside WSL 2 (WSLg)

Windows 11 and recent Windows 10 builds show Linux GUI apps from WSL as normal windows. In your WSL distro:

```sh
curl -fsSL https://raw.githubusercontent.com/subhadeeproy3902/open-docker-app/main/scripts/install.sh | sh
berth
```

It talks to the Docker Engine in the same WSL distro through its socket, so no TCP setup is needed. Berth then appears
in the Start menu under your distro's name.

---

## Uninstall

- **Option A:** delete the Berth folder and any shortcut. Settings are in `%LOCALAPPDATA%\berth` (delete it for a clean slate).
- **Option B:** `curl -fsSL https://raw.githubusercontent.com/subhadeeproy3902/open-docker-app/main/scripts/install.sh | sh -s -- --uninstall`

Berth never installs services, so nothing keeps running after you close it.

## Why does SmartScreen warn, and do I need a certificate?

No. SmartScreen warns about any new `.exe` that is not signed with a paid code-signing certificate and has not yet been
downloaded by many people. It does not mean anything was found in the file. A certificate is only a convenience that
makes the warning go away; it is not needed to install, run or share open-source software. The full source and the exact
build steps are public ([build.yml](../.github/workflows/build.yml)), and every release lists SHA-256 checksums:

```powershell
Get-FileHash $env:USERPROFILE\Downloads\Berth-windows-x64.zip -Algorithm SHA256
```

Compare the result with `SHA256SUMS.txt` on the release page. (Open-source projects can get free signing through the
[SignPath Foundation](https://signpath.org/); Berth may apply once it has a track record.)

## Troubleshooting

| Problem | Fix |
|---|---|
| "Docker Engine is not running" | Start Docker Desktop (Berth connects through its pipe as soon as it runs). For an engine in WSL: in PowerShell, `curl.exe http://localhost:2375/_ping` must print `OK`. |
| Engine elsewhere | **Settings → Docker Engine → Other engine**, e.g. `ssh://you@server` (your SSH keys) or `tcp://192.168.1.20:2375`. |
| Blurry or tiny text | **Settings → Accessibility → Text size**, or Windows *Display → Scale*. |
| Windows Defender quarantines a file | A false positive that sometimes hits PyInstaller apps; restore it from *Protection history* and [tell us](https://github.com/subhadeeproy3902/open-docker-app/issues/new/choose). |
| Something else | [Open an issue](https://github.com/subhadeeproy3902/open-docker-app/issues/new/choose) with the output of `Berth.exe --version`. |

Native Windows named-pipe support (`npipe:////./pipe/docker_engine`, so no TCP setting is needed) is on the roadmap.
