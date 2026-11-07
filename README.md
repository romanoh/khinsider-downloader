# khinsider-downloader

A small Python CLI to download full video game soundtracks from
[downloads.khinsider.com](https://downloads.khinsider.com/).

Supports searching by name, downloading whole albums, and choosing a preferred
audio format (e.g. FLAC over MP3 when available).

## Features

- Download a full album by its khinsider album ID (slug from the URL)
- Search albums by name from the command line
- Pick preferred format(s) per album (e.g. `flac,mp3,ogg`); a track without
  any of them is reported, never silently downloaded in another format
- Resumable downloads: an interrupted transfer continues from where it stopped
  (HTTP `Range`), with an atomic `.part` → final rename only on success
- Automatic retry with exponential backoff on transient network errors
- Progress bar per file (via `tqdm`)
- Shared HTTP connection (keep-alive) for faster multi-track downloads

## Requirements

- Python 3.10+
- See [`requirements.txt`](requirements.txt)

## Installation

```bash
git clone https://github.com/romanoh/khinsider-downloader.git
cd khinsider-downloader
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

The album ID is the last segment of the album URL on khinsider.
For example, for `https://downloads.khinsider.com/game-soundtracks/album/chrono-trigger-original-sound-version`
the album ID is `chrono-trigger-original-sound-version`.

### Download an album

```bash
python khinsider.py chrono-trigger-original-sound-version
```

By default the album is saved in a folder named after the album in the current
directory.

### Choose an output directory

```bash
python khinsider.py chrono-trigger-original-sound-version "D:\Music\OSTs\Chrono Trigger"
```

### Prefer FLAC, fall back to MP3

```bash
python khinsider.py chrono-trigger-original-sound-version -f flac,mp3
```

Formats are tried in the order given, and the download never silently falls back
to a format you did not ask for.

- If the album offers none of the requested formats, nothing is downloaded and
the command exits with an error listing the formats the album does offer.
- If only some tracks lack the requested formats, those tracks are skipped with
a message naming what each one offers, the remaining tracks are downloaded, and
the command exits with code 1.

Without `-f`, the first file listed on each track page is downloaded.

### Search instead of downloading

```bash
python khinsider.py "chrono trigger" -s
```

Prints a list of `<album-id>: <album-name>` pairs you can then feed back into
the download command.

### Full help

```bash
python khinsider.py --help
```

## Exit codes

| Code | Meaning                                                      |
|------|--------------------------------------------------------------|
| 0    | All requested tracks downloaded successfully                 |
| 1    | One or more tracks failed, or an unrecoverable error occurred|

## Disclaimer

This tool is provided for **personal, educational, and archival purposes only**.

The legality of downloading video game soundtracks from khinsider varies by
jurisdiction and depends on the copyright status of each individual work.
Most soundtracks hosted on the site are copyrighted material owned by their
respective publishers and composers.

You are solely responsible for ensuring that your use of this tool complies
with the [khinsider Terms of Service](https://downloads.khinsider.com/) and
with the copyright laws of your country. The authors of this tool do not
endorse or encourage copyright infringement and accept no liability for misuse.

Please consider supporting the artists and publishers behind the music you
enjoy by buying official releases when available.

## Contributing

Bug reports and pull requests are welcome. If you plan a larger change, please
open an issue first to discuss it.

## License

[MIT](LICENSE) © 2026 romanoh
