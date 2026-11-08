# Installing Berth on macOS

Berth is free and open source. You do **not** need an Apple Developer account, and you do not pay anything,
to download, install or run it. The only difference from an App Store app is a one-time "Open Anyway" click
(explained below), because the app is not notarized by Apple.

| | |
|---|---|
| Supported | macOS 13 Ventura or newer, **Apple Silicon** (M1/M2/M3/M4…) |
| Intel Macs | Use [Run from source](#run-from-source-any-mac-including-intel) (two commands) |
| Download | `Berth-macos-arm64.dmg` from the [latest release](https://github.com/subhadeeproy3902/open-docker-app/releases/latest) |
| Size | about 60 MB (it carries its own GTK 4 runtime) |

## 1. Get a Docker engine

Berth is a desktop app for a Docker engine; it does not ship a virtual machine of its own. Pick one:

| Engine | Install | Notes |
|---|---|---|
| **Colima** (recommended, free, open source) | `brew install colima docker && colima start` | Lightest option. `colima start --cpu 2 --memory 2` to size it. |
| OrbStack | [orbstack.dev](https://orbstack.dev) | Fast; free for personal use. |
| Rancher Desktop | [rancherdesktop.io](https://rancherdesktop.io) | Choose the *dockerd (moby)* engine. |
| Docker Desktop | [docker.com](https://www.docker.com/products/docker-desktop/) | Works, but is what Berth replaces. |

Berth finds the socket of each of these by itself (`~/.colima/default/docker.sock`, `~/.orbstack/run/docker.sock`,
`~/.rd/docker.sock`, `~/.docker/run/docker.sock`, or whatever `DOCKER_HOST` / `docker context` points to).
No Homebrew yet? Install it from [brew.sh](https://brew.sh).

## 2. Install Berth

1. Download **`Berth-macos-arm64.dmg`** from the [latest release](https://github.com/subhadeeproy3902/open-docker-app/releases/latest).
2. Open it and drag **Berth** onto **Applications**.
3. Open Berth from Launchpad or Applications. The first time, macOS says it *cannot verify* the app. Click **Done**
   (not *Move to Bin*).
4. Open **System Settings → Privacy & Security**, scroll down to *"Berth" was blocked…*, click **Open Anyway**,
   confirm with your password or Touch ID.

That's it: from now on Berth opens like any other app.

**Prefer the terminal?** This does the same as steps 3–4:

```sh
xattr -dr com.apple.quarantine /Applications/Berth.app
open /Applications/Berth.app
```

### "Berth is damaged and can't be opened"

That message is Gatekeeper's wording for a downloaded app with no Apple notarization; the file is not broken.
Run the `xattr` command above. You can check what you downloaded against `SHA256SUMS.txt` on the release page:

```sh
shasum -a 256 ~/Downloads/Berth-macos-arm64.dmg
```

## Run from source (any Mac, including Intel)

```sh
brew install gtk4 libadwaita pygobject3 adwaita-icon-theme
git clone https://github.com/subhadeeproy3902/open-docker-app && cd open-docker-app
python3 -m berth
```

Update later with `git pull`.

## Uninstall

Drag `/Applications/Berth.app` to the Bin. Settings live in `~/.config/berth` (delete that folder too for a clean slate).
Berth never installs background services, so nothing else is left behind.

## Why is there a warning at all?

Apple shows it for every app that is not **notarized**. Notarization requires a paid Apple Developer membership
(US$99 per year). It is a convenience, not a requirement: the app is identical either way, and the full source and the
build that produced the `.dmg` are public ([build.yml](../.github/workflows/build.yml)). The release build is
*ad-hoc signed* (signed without an Apple identity), which is what Apple Silicon needs to run it. If the project gets
sponsorship for a developer account, notarized builds will follow and the warning disappears.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Docker Engine is not running" | Start your engine (`colima start`), then press **Retry** in Berth. |
| Berth finds the wrong engine | **Settings → Docker Engine → Connected engine** lists every engine found; or set a custom address there. |
| Text looks small or large | **Settings → Accessibility → Text size** (85–150 %). |
| Something else | [Open an issue](https://github.com/subhadeeproy3902/open-docker-app/issues/new/choose) with the output of `/Applications/Berth.app/Contents/MacOS/Berth --version`. |
