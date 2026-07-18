<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white">
  <img alt="GUI" src="https://img.shields.io/badge/GUI-Tkinter-0f766e">
  <img alt="Downloader" src="https://img.shields.io/badge/Downloader-yt--dlp-2EA043">
  <img alt="FFmpeg" src="https://img.shields.io/badge/FFmpeg-required-007808?logo=ffmpeg">
  <img alt="Media Servers" src="https://img.shields.io/badge/Emby%20%7C%20Plex%20%7C%20Jellyfin-ready-ffc230">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-blue">
</p>

Series Trailer Downloader V2.0
==============================

![Moonlit wolf banner](assets/wolf-banner.png)

A Python GUI and CLI that finds one good trailer for every TV series in a
library, downloads it, validates it, normalizes it to H.264/AAC MP4, and stores
it in the show-level `Trailers` folder expected by Emby, Plex, and Jellyfin.

```text
C:\TV Shows
└── The Expanse (2015) [tmdbid-63639]
    ├── Season 01
    └── Trailers
        └── trailer.mp4
```

The downloader is series-level by design. It does not place files beside
episodes and does not replace season or episode extras.

What carried over from Movie Trailer Downloader V2.0
-----------------------------------------------------

- Multi-source discovery with TMDb, KinoCheck, YouTube Data API, Internet
  Archive, Dailymotion, and yt-dlp search fallbacks.
- Conservative title/year matching, official-source scoring, duration limits,
  quality caps, and rejection of reviews, reactions, fan edits, clips, foreign
  dubs, 3D variants, full episodes, and obvious episode promos.
- Available-format quality probing before a winner is selected.
- YouTube cookie, JavaScript-runtime, EJS, player-client, and format-recovery
  fallbacks.
- FFmpeg media validation, H.264/AAC conversion, loudness normalization, CPU or
  GPU encoding, and original-file fallback when conversion is unavailable.
- Throttled download progress with percentage, transferred size, speed, ETA,
  and FFmpeg elapsed-time updates in both the GUI log and CLI.
- Restartable results history, polite delays, progress logging, clean Ctrl+C /
  GUI cancellation, and temporary-file cleanup.
- Safe total re-download: current trailers are renamed to `.old`, restored if
  every new candidate fails or the run is cancelled, and deleted only after a
  valid replacement is saved.

Series-specific matching
------------------------

- TMDb uses TV search and TV video endpoints, including `first_air_date_year`.
- KinoCheck uses its `/shows` endpoint.
- Folder names with provider hints are understood, for example
  `Show (2024) [tmdbid-12345]`, `Show (2024) [tvdbid-67890]`, and
  `Show (2024) {tmdb-12345}`.
- Network and streaming channels such as Netflix, HBO/Max, Prime Video, Apple
  TV, Disney+, Hulu, Paramount+, BBC, and others receive official-source weight.
- Season 1 trailers are preferred; later-season trailers are retained only as
  weaker fallbacks. Episode-specific promos are rejected.
- Any playable video already in the show-level `Trailers` folder counts as an
  existing trailer, so the default run does not trample curated media.

Subtitle handling
-----------------

Subtitle and automatic-caption downloads are explicitly disabled. The FFmpeg
output maps only the first video stream and optional first audio stream and also
uses `-sn`, so embedded subtitle tracks are omitted from the finished MP4.
Candidates advertised as subbed, captioned, or carrying hardcoded subtitles
are rejected. Text that is visibly burned into the picture cannot be removed
reliably, but the title filtering reduces the chance of selecting it.

Requirements
------------

- Windows 10/11
- Python 3.14
- yt-dlp with its default extras and `curl-cffi`
- FFmpeg
- Deno (recommended) or Node.js 22+ for modern YouTube challenges

Install or repair everything automatically:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Or install the Python dependency manually:

```powershell
python -m pip install -U -r .\requirements.txt
```

Quick start
-----------

Open the GUI:

```powershell
python .\series_trailer_downloader.py
```

### Windows EXE

The release build runs the same Tkinter GUI without opening a console window.
Settings, cookies, results, and logs are stored beside the EXE so they persist
normally when using PyInstaller's one-file mode.

Build a tested release folder and ZIP:

```powershell
python -m pip install -U -r .\requirements-build.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Outputs:

```text
dist\Series-Trailer-Downloader-V2.0\Series Trailer Downloader.exe
dist\Series-Trailer-Downloader-V2.0-Windows-x64.zip
```

FFmpeg and Deno/Node remain external dependencies. Keep `install.ps1` beside
the EXE so the GUI's **Install / Repair Dependencies** button can use it.

Preview a library without changing anything:

```powershell
python .\series_trailer_downloader.py --root "C:\TV Shows" --dry-run --limit 3
```

Download trailers:

```powershell
python .\series_trailer_downloader.py --root "C:\TV Shows" --max-height 1080
```

UNC paths work too:

```powershell
python .\series_trailer_downloader.py --root "\\server\Media\TV Shows" --dry-run
```

If shows are nested below category or alphabet folders, enable **Recursive
scan** in the GUI or add `--recursive`. Recursive mode identifies show folders
from season directories and episode-style `S01E01` filenames while ignoring
season, specials, trailers, and extras folders.

API keys
--------

No shared keys or secrets are included. Bring your own credentials:

- [TMDb API](https://developer.themoviedb.org/docs/getting-started) for the most
  reliable series/year identity and official video records.
- [KinoCheck API](https://api.kinocheck.com/) for verified show footage.
- [YouTube Data API v3](https://developers.google.com/youtube/v3/getting-started)
  for cleaner API-backed YouTube discovery.

Keys can be entered in the GUI, passed with `--tmdb-token`,
`--kinocheck-token`, and `--youtube-api-key`, or supplied with the documented
environment variables shown by `--help`.

Saved settings
--------------

Copy the safe example and edit it, or simply use the GUI and click **Save
Settings**:

```powershell
Copy-Item .\series_trailer_downloader.settings.example.json .\series_trailer_downloader.settings.json
python .\series_trailer_downloader.py --settings-file .\series_trailer_downloader.settings.json --dry-run --limit 3
```

The real settings file is ignored by Git because it can contain API keys and
machine-specific paths. Command-line options override saved values.

Useful commands
---------------

```powershell
# Check tools without running a scan
python .\series_trailer_downloader.py --check-deps

# Watch progress from another PowerShell window
Get-Content .\series_trailer_downloader.log -Wait -Tail 80

# Replace existing trailers, with automatic backup/restore on failure
python .\series_trailer_downloader.py --root "C:\TV Shows" --redownload-existing

# Avoid all Google-hosted discovery
python .\series_trailer_downloader.py --root "C:\TV Shows" --source-order "tmdb,kinocheck,internet-archive"

# Generate a reusable cookies.txt file
python .\series_trailer_downloader.py --extract-cookies-from-browser edge --cookies-file youtube-cookies.txt
```

Run `python .\series_trailer_downloader.py --help` for every option.

After downloading
-----------------

Run a library scan or refresh metadata in your media server. Emby documents a
`trailers` subfolder beneath a series folder; Plex and Jellyfin likewise support
show-level trailer/extras folders. Some Plex clients have more complete TV-extra
support than others.

Testing
-------

```powershell
python -m unittest discover -s .\tests -v
python -m py_compile .\series_trailer_downloader.py
```

The tests cover folder parsing, provider IDs, portable trailer placement,
existing-trailer protection, rollback, TMDb TV endpoints, series scoring, and
source defaults.

License
-------

MIT. See [LICENSE](LICENSE).
