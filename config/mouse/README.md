# SteerMouse settings

Japanese version: [README_JA.md](README_JA.md)

`steermouse/Device.smsetting` is the live settings store for all registered
devices. The `.smsetting_dev` files in this directory are separate per-device
exports for manual import; they are not updated automatically.

On macOS, setup links
`~/Library/Application Support/SteerMouse & CursorSense` to this checkout's
`config/mouse/steermouse/` directory. Linking the directory keeps saves that
replace `Device.smsetting` inside the repository. Changes made in SteerMouse
then appear in Git and require an explicit commit and push.
This repository is public. Before publishing a settings update, inspect custom
button actions in SteerMouse for private text, commands, or URLs; Git shows the
store as a binary change rather than a readable settings diff.

## Setup

```sh
bash scripts/setup_steermouse.sh --dry-run
bash scripts/setup_steermouse.sh
# Equivalent once the mise tasks are deployed:
mise run steermouse-setup
```

This also runs during `zsh main.sh --profile full` and
`zsh main.sh --only config --profile full` when `features.macos=true`.
CLI profiles and Linux do not apply it. `--only config --dry-run` only previews it.

Before applying, quit SteerMouse and CursorSense, including their background
managers. The script does not stop them. It refuses to replace a foreign link,
different live settings, or a folder containing additional files. Reconcile and
preserve those files first; to keep the current device settings, copy the live
`Device.smsetting` into `config/mouse/steermouse/` while the apps are stopped,
review the Git change, and rerun setup.

An existing matching directory is moved intact into a unique
`~/Library/Application Support/SteerMouse-backup.*/settings` directory before
linking. Keep that backup until you have checked the result. Open SteerMouse
and use **Reconfigure** in the **Device** tab, as described in the
[official FAQ](https://plentycom.jp/inc/faq.php?style=e&type=sm).

Use a checkout you will retain: moving or deleting it breaks the live link.
The script intentionally refuses to switch an existing link to a different
worktree. The settings folder is shared with CursorSense; licensing preferences
under `~/Library/Preferences` are not managed here.

## Undo

Quit the apps and their managers. Verify that the path above is still the
managed symlink, then remove **only the symlink** and copy the repository's
`steermouse/` directory back to that path as a real directory. This keeps your
latest changes. To restore the pre-setup state instead, move the saved backup's
`settings` directory back. Reopen SteerMouse and use **Reconfigure**.
