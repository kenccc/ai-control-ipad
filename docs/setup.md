# Setup

```bash
git clone <this repo> ~/ai-control
cd ~/ai-control
./setup.sh
```

`setup.sh` checks your tooling, builds the web app, generates an access token into the
macOS Keychain, writes `~/.ai-control/config.yaml`, creates the database, and installs
the LaunchAgent. It prints the access token once — it lives in your Keychain
afterwards.

## Configure your repositories

Agents are restricted to what you list here. An empty list allows nothing.

```yaml
# ~/.ai-control/config.yaml
host: 127.0.0.1
port: 8787

repositories:
  inventory:
    path: /Users/you/dev/inventory
    forgejo: special/inventory
  labels:
    path: /Users/you/dev/labels
    forgejo: special/labels

forgejo:
  url: https://git.example.com
```

Then store the Forgejo token (Keychain, never the config file):

```bash
./scripts/set-secret.sh forgejo-token
```

## Reaching it from the iPad

Install Tailscale on the Mac and the iPad and sign both into the same tailnet. On the
Mac, the standalone app works without Homebrew:

```bash
curl -LO https://pkgs.tailscale.com/stable/Tailscale-<version>-macos.zip
unzip Tailscale-*-macos.zip && mv Tailscale.app /Applications/
open -a Tailscale        # sign in
./scripts/install-tailscale-cli.sh
```

The CLI is a small `exec` wrapper, not a symlink, because the app binary resolves its
own bundle identifier from its path — a symlink aborts with *"The current
bundleIdentifier is unknown to the registry"*, and running the bundle binary directly
launches the GUI and never returns. The script writes the wrapper to `/usr/local/bin`
when that is writable and `~/.local/bin` otherwise, so it needs no password.

Then bind the tailnet interface:

```yaml
# ~/.ai-control/config.yaml
host: tailscale      # resolves to this machine's 100.x address at startup
port: 8787
```

`host: tailscale` means AI Control listens **only** on the tailnet — reachable from
your iPad, invisible to whatever café or hotel network the Mac is also on, and not even
on loopback. If Tailscale has not come up yet the server waits for it (the daemon often
starts after the LaunchAgent at login) and, if it never appears, refuses to start rather
than quietly falling back to `0.0.0.0`.

Find your address and open it on the iPad:

```bash
tailscale status
launchctl kickstart -k gui/$UID/com.aicontrol.agent
```

Open `http://<your-mac>.ts.net:8787`, sign in, then **Share → Add to Home Screen**. It
runs fullscreen with safe-area insets and reconnects on wake.

Traffic inside the tailnet is WireGuard-encrypted, so plain HTTP over it is private on
the wire. The session cookie is only marked `Secure` when you terminate TLS yourself;
authentication, CSRF and origin checks apply either way, and origins outside the tailnet
are rejected.

## Optional: live write control for Codex Desktop

Out of the box AI Control can read every Codex Desktop session and continue any that
has no turn in flight. To also steer, interrupt and approve a session the desktop app
is *actively running*:

```bash
./scripts/enable-codex-daemon.sh
```

This needs OpenAI's standalone Codex install and starts the shared app-server daemon.
Afterwards set `codexSharedDaemon: true` and check `/diagnostics`: *"Continue a session
with a turn in flight"* should read ✓. If it does not, AI Control keeps reporting the
capability as unavailable rather than offering a control that would fail.

## Service management

```bash
launchctl kickstart -k gui/$UID/com.aicontrol.agent   # restart
launchctl bootout gui/$UID/com.aicontrol.agent        # stop
tail -f ~/.ai-control/logs/aicontrol.log              # logs (JSON)
```

## Mac sleep, lid closed, locked

Agents run on the Mac. Nothing executes while macOS is fully asleep — no software can
change that, and AI Control does not pretend otherwise.

| Mac state | What works |
|---|---|
| Awake, unlocked | Everything. |
| **Locked** (screen locked, awake) | Everything. LaunchAgents keep running; agents keep working; the iPad stays connected. |
| **Display asleep**, Mac awake | Everything. |
| **Lid closed, on power, with an external display or `caffeinate`** | Everything. Otherwise the Mac suspends — see below. |
| **Lid closed on battery / asleep** | Nothing runs. The iPad shows `○ Offline`, keeps cached session metadata readable, and disables every control that needs the Mac. It reconnects automatically on wake. |

To keep it reachable with the lid closed:

```bash
caffeinate -s &                                   # for one session
sudo pmset -c sleep 0 disablesleep 1              # permanent, on AC power
```

Tailscale can wake a sleeping Mac only if the Mac supports and has enabled
Wake-on-LAN (`sudo pmset -c womp 1`) and something on the same physical LAN sends the
magic packet — a Tailscale subnet router on that LAN, for instance. Over the internet
alone it cannot. AI Control does not claim to wake your Mac; it reconnects cleanly when
the Mac wakes.
