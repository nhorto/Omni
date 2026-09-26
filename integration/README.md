# User integration

Run `python3 install.py` to preview paths, then `python3 install.py --apply` to install user launchers, the app entry, the voice service, and the Omarchy plugin. Existing changed files are backed up under `~/.local/share/omi/integration-backups/`. The source checkout must remain in place.

Activate the service in your graphical session:

```bash
systemctl --user daemon-reload
systemctl --user enable --now omi-voice.service
omarchy plugin validate ~/.config/omarchy/plugins/local.omi
```

Add `local.omi` through Omarchy's bar/plugin settings. The plugin contains both the bar item and a movable recording indicator. Review `bindings.lua` before adding its shortcuts to your Hyprland Lua configuration. Resolve existing key assignments explicitly; the installer never overwrites them.

The service uses `~/.local/bin/omi-voice`; the app and plugin use `~/.local/bin/omi`. `omi.desktop` is a template whose launcher path is rendered by the installer. Templates contain no machine-specific checkout paths.

After changing Hyprland config, run `hyprctl reload` and `hyprctl configerrors`. After source updates, restart `omi-voice.service` and reopen the app. Voxtype, a working capture device, and its configured transcription model are required for real voice input.
