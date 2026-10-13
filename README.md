# adb transfer

A small local web UI with three tabs:

- **Files** — `adb push` / `adb pull`. Two file panes, your computer on the
  left and the phone on the right: pick files, hit Push or Pull.
- **Movies** — genres and running time for every movie in a folder, browsable
  by genre and length.
- **Displays** — switch your laptop screen and external monitors on and off,
  and arrange them, using `xrandr`.

## Run

    ~/Projects/adb-transfer/adb-transfer

It opens http://127.0.0.1:8767/ in your browser (add `#movies` or `#displays`
to land on that tab). Ctrl+C to stop.
Set `ADB_TRANSFER_PORT` to use a different port.

## What it does

- Browse both sides, sort by name/size/date, double-click folders to open
- Multi-select with checkboxes (shift-click for a range, header box for all)
- Live progress: percent, transfer speed, ETA; Stop cancels mid-transfer
- Transfers run one at a time so they don't fight over the USB bus
- Whole folders transfer recursively
- Free space shown for both sides
- `+` creates a new folder on either side (works on the phone too)
- `✎` renames the selected item, edited inline in the row (or press F2)
- `🔒` shows and edits permissions — see the caveat below
- `✂ Cut` / `📋 Paste` moves files and folders within one side
- `🗑 Delete` removes files and folders, with a confirmation showing what goes
- **⏏ Safe to unplug** — flushes pending writes before you pull the cable
- Remembers the last folder you were in on each side
- Flags anything that is already on the other side: **Already on phone** (same
  size), **On phone, different size**, or **Folder also on phone**. Pushing or
  pulling such items asks first: Cancel, Skip existing, or Overwrite

## Movies

Pick **Phone** or **This computer** and a folder (default `/sdcard/Movies`),
then press **Update missing genres**. Every video file in it (subfolders too)
that has no genre yet is looked up on [Wikidata](https://www.wikidata.org),
which is free and needs no account or API key. Movies that were already found
are not looked up again, so after adding new movies the button only does the
new ones.

- The title and year come from the filename: `Nobody.2.2025.1080p.WEB-DL…mkv`
  is searched as "Nobody 2" (2025). The year has to match (±1), so a remake
  is not mistaken for the original.
- **By genre** lists each genre with the movies in it. A movie with several
  genres appears under each. **List** is a single table you can sort by
  title, year or length. The length buttons (under 1h 45m, 1h 45m–2h 15m,
  over 2h 15m) and the genre buttons filter both views.
- **Not matched** lists files it could not identify. **Fix** lets you type
  the right title and year.
- Results are kept in `~/.config/adb-transfer/movies.json`. Lookups are about
  one request a second (Wikidata's rate limit), so a first run over 30 movies
  takes a couple of minutes.

This is the only part of the app that uses the internet: it sends the movie
titles to Wikidata.

## Renaming

Select one item and press `✎` or F2. The row turns into a text field with the
basename preselected, so the extension is easy to keep. Enter commits,
Escape cancels.

It will not overwrite an existing file: on the phone `mv -n` exits 0 even when
it declines to clobber, so the result is verified afterwards rather than
trusted. Renaming something that is mid-transfer is refused.

## Permissions

The Mode column shows the octal mode (setuid/setgid/sticky appear as a 4th
digit). `🔒` opens a read/write/execute grid for owner/group/others, wired to
an octal field that updates both ways.

**The important caveat:** `/sdcard` and `/storage/emulated` are a FUSE mount.
Android synthesises those permissions from the folder and the owning app, so
`chmod` there exits 0 and changes nothing. Verified on this device: `chmod 777`
on a file in `/sdcard` left it at `rw-rw----`. Rather than report a success
that did not happen, the dialog says so up front and the server refuses.

Permissions do work on real filesystems such as `/data/local/tmp` (verified:
600 → 751), and on the computer side. After any change the mode is re-read and
compared, so a filesystem that quietly ignores the request is reported as a
failure, not a success.

## Moving files

Select items, press **Cut**, navigate to the destination folder, press
**Paste**. The clipboard is side-locked: phone files move within the phone,
computer files within the computer. (To go between the two, use Push/Pull.)

Within `/sdcard` a move is just a relink — 600 MB moved in 0.05s on this
device. Moving between different mounts has to copy the bytes (measured
~680 MB/s), so if the destination is on another filesystem you get a warning
first rather than an unexplained wait.

Refused, with the reason shown per item: a name that already exists at the
destination, a folder into its own subfolder, the folder it is already in, and
anything mid-transfer. Nothing is overwritten.

## Deleting

Select items and press **Delete**. The confirmation lists exactly what will go,
including how many files are inside each folder, and says plainly that there is
no recycle bin on either side.

Refused: `/`, `/sdcard`, `/storage/emulated/0`, `/data/local/tmp`, `/system`,
your home folder, and anything mid-transfer. Deletions are verified afterwards,
so a failure is reported as a failure.

## Safe to unplug

`adb` has no "eject" for a USB device, but pulling the cable while data is
still buffered can leave a truncated file on the phone. The ⏏ button:

1. Refuses if this app has transfers queued or running.
2. Refuses if an `adb push`/`pull` started *elsewhere* (a terminal) is live,
   listing the offending commands — with a "Disconnect anyway" override.
3. Runs `sync` on the device so cached writes reach flash. (Measured: ~0.09s
   idle vs ~0.82s straight after an 800 MB push — it really is flushing.)
4. Refreshes the media library. Usually redundant on Android 11+, where
   `adb push` registers files with MediaStore through FUSE, but it is cheap
   and covers files replaced in place.
5. Shows a green "safe to unplug" banner and locks the transfer buttons.

For a network device (`adb connect host:port`) it also runs a real
`adb disconnect`. The banner clears itself once the cable is actually out.

Note it does **not** run `adb kill-server` — that would abort transfers
started in other terminals.

## Displays

One card per screen that is plugged in, each with an **on/off switch**. The
switch takes effect immediately:

- **Off** is refused for the last screen that is on, so you can never end up
  with a black desktop. If you switch off the primary screen, "primary" (panel,
  new windows) moves to the biggest screen still on.
- **On** restores the resolution, position and rotation that screen had the
  last time it was switched off here (stored in
  `~/.config/adb-transfer/display-last.json`, and only reused if the same
  monitor is still on that port). A screen seen for the first time comes on
  at its own preferred mode, to the right of the others.
- A cable pulled out while its screen was on still takes up desktop space.
  That card shows **Unplugged** so you can switch it off.

After every change the layout is read back from the X server. If it doesn't
match what was asked for, you see what differs rather than a silent success.

**Brightness:** the built-in panel gets a real **Backlight** slider, set
through systemd-logind, so no root or udev rules are needed. External
monitors get a software **Brightness** slider (`xrandr --brightness`). That
dims the picture but not the monitor's own backlight, and it stops at 20% so a
screen can't be dimmed to invisible.

**Quick layouts:** *<screen> only*, *Extend* (built-in panel on the left) and
*Mirror* (the largest resolution every screen supports, or scaling if they
share none). *+ Save current layout…* stores named layouts in
`~/.config/adb-transfer/display-layouts.json`. They are saved by resolution
rather than xrandr's mode ids, which change between sessions. If a saved layout
needs a monitor that isn't plugged in, you are told which one.

**Arrangement:** drag screens to match your desk. When you let go, a screen
snaps flush against its neighbour, since a gap between screens traps the mouse.
Click a screen to change its resolution, refresh rate, orientation, desktop
size (scaling) or which one is primary.

These changes can leave a screen blank (for example, a refresh rate the TV
can't actually show), so they aren't permanent straight away. After **Apply**
you get **Keep changes / Revert** with a 15-second countdown. If nobody presses
Keep, the server switches back on its own. That still works if the browser is
on the screen that went black, or the tab was closed.

Caveats:

- X11 only. Under Wayland `xrandr` can't change anything.
- Changes made here are not written to Cinnamon's own display settings, so a
  replug or re-login may bring back the desktop's saved layout. Use a saved
  layout here to switch back in one click.
- `xrandr --dryrun` still changes the primary display (an xrandr quirk). Don't
  rely on it for testing.

## Requirements

`adb` on PATH, Python 3, USB debugging enabled on the phone. For Displays:
an X11 session with `xrandr`; `busctl` (part of systemd) for the laptop
backlight.
Nothing to install — Python standard library only.

## Notes

Binds to 127.0.0.1 only and requires a per-run token on every API call,
so no other machine (or stray webpage) can drive your device.
