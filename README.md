# ShelfSaver

ShelfSaver watches the video folders you choose and shrinks the files that are worth
shrinking by converting them to x265 on your NVIDIA graphics card. It checks every new file
(length, tracks, plays from start to finish, meaningfully smaller) before it replaces the
original, and it never removes anything you did not agree to.

It runs on one Windows 11 PC with an NVIDIA graphics card, and you use it from a web page.

**This app is dumb on purpose.** There are no advanced settings and no secret menu of encoder
knobs. I just wanted to free up some disk space without earning a degree in video compression.
If you came for the fancy version, this isn't it, and I'm thrilled about that.

## Screenshots

These show a fresh install with nothing converted yet, and the folder names are made up.

**Home**

![The home page, with space saved, libraries and history](screenshots/home.png)

**Library**

![A library page, with its stats and history](screenshots/library.png)

**Queue**

![The queue page, with nothing waiting](screenshots/queue.png)

**Settings**

![The settings page, with the scratch disk and holding folder](screenshots/settings.png)

## What it does

- Treats each folder you add as a library, and picks up new files as they arrive.
- Decides for each video whether converting it is worthwhile, and skips the rest.
- Has one setting for how hard to compress: Low, Medium or High.
- Shows each library's space saved, plus every file's old and new size and format.
- Builds new files on a scratch disk you choose, and swaps them in once they pass the checks.
- Can keep replaced originals in a holding folder until you clear them.
- Can remove non-English soundtracks, except on dubbed movies. This is off unless you turn it on.
- Offers logs and status over a read-only MCP connection.

## Install

You need Windows 11, an NVIDIA graphics card and an internet connection.

1. Download or copy this folder to the PC.
2. Open PowerShell as administrator (right-click PowerShell, choose "Run as administrator").
3. Run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
   ```

The script installs Python and FFmpeg if they are missing, puts the program in
`C:\Program Files\ShelfSaver`, sets it to start with the PC, opens its port on
Domain, Private and Public networks, and prints the web address (port 8642) at the end.

Your settings, history and login live in `C:\ProgramData\ShelfSaver`. Only administrators
can open that folder.

To update, get the newer copy of this folder and run the same command again. The new version
is built first, then swapped in; if anything goes wrong the old version is put back. Your data
is left alone. Run setup from a folder outside `C:\Program Files\ShelfSaver`.

## Uninstall

In PowerShell as administrator:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Uninstall
```

This stops ShelfSaver and removes the startup task, the firewall rule and the program folder.
Your settings and history are kept. To delete those too, add `-RemoveData`. Your video
libraries, holding folders and scratch disks are never touched.

## Stopping ShelfSaver

ShelfSaver restarts itself if it is stopped (and checks every few minutes), so ending the task in
Task Manager will not keep it stopped. To stop it until you want it back, in PowerShell as
administrator:

```powershell
Disable-ScheduledTask -TaskName ShelfSaver
Stop-ScheduledTask -TaskName ShelfSaver
```

To turn it back on: `Enable-ScheduledTask -TaskName ShelfSaver` then `Start-ScheduledTask -TaskName ShelfSaver`.

## Compare Low, Medium and High

This tool makes short clips from one of your files at each setting. The installer puts it at
`C:\Program Files\ShelfSaver\scripts\make_compare_clips.py`. Run it from PowerShell as administrator:

```powershell
& "C:\Program Files\ShelfSaver\venv\Scripts\python.exe" "C:\Program Files\ShelfSaver\scripts\make_compare_clips.py" --source "D:\Media\Movies\Example.mkv" --start 600 --seconds 30 --out "D:\Clips"
```

(From a development copy of this folder, `scripts\make_compare_clips.py` works the same way.)

This writes `Example Original.mkv`, `Example Low.mkv`, `Example Medium.mkv` and
`Example High.mkv` into the output folder and prints how big each one is. The output folder
must not be inside the folder of the source video. The source is never changed.

## Development

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

Tests marked `gpu` need an NVIDIA graphics card.

## Licence

Apache License 2.0. See `LICENSE`.
