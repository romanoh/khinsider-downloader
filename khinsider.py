import os
import re
import sys
import argparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin, urlsplit, unquote
from bs4 import BeautifulSoup
from tqdm import tqdm

BASE_URL = 'https://downloads.khinsider.com/'
USER_AGENT = 'Mozilla/5.0 (compatible; KHInsiderDownloader/1.1)'
REQUEST_TIMEOUT = 30  # seconds


def _build_session():
    """Build a requests.Session with sane defaults, keep-alive and retries."""
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        status=5,
        backoff_factor=1.0,  # 1s, 2s, 4s, 8s, 16s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(['GET', 'HEAD']),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


# Module-level shared session (keep-alive across all requests).
SESSION = _build_session()


class KhinsiderError(Exception):
    pass


class NonexistentSoundtrackError(KhinsiderError):
    pass


class NonexistentFormatsError(KhinsiderError):
    def __init__(self, available_formats, requested_formats):
        self.available_formats = available_formats
        self.requested_formats = requested_formats
        super().__init__(
            f'None of the requested formats {requested_formats} are available. '
            f'Available formats: {available_formats}'
        )


# Filesystem limit is 255 bytes per name on ext4/NTFS; stay well below it to
# leave room for an extension and the ".part" suffix.
MAX_FILENAME_BYTES = 200

INVALID_FILENAME_CHARS = frozenset('<>:"/\\|?*')

# Windows refuses to create files with these names, extension or not.
WINDOWS_RESERVED_NAMES = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL']
    + [f'COM{i}' for i in range(1, 10)]
    + [f'LPT{i}' for i in range(1, 10)]
)


def _truncate_bytes(text, max_bytes):
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes without splitting a character."""
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode('utf-8', 'ignore')


def _sanitize_filename(filename):
    """Turn an arbitrary (possibly remote) string into one safe path component."""
    cleaned = ''.join(c if c not in INVALID_FILENAME_CHARS else '-' for c in filename)
    cleaned = ''.join(c for c in cleaned if c.isprintable())
    cleaned = cleaned.strip(' .')
    if not cleaned:
        return 'unnamed'

    stem, ext = os.path.splitext(cleaned)
    # Keep a recognisable extension when trimming an over-long name.
    ext_bytes = len(ext.encode('utf-8'))
    if ext_bytes > 16:
        stem, ext = cleaned, ''
        ext_bytes = 0
    stem = _truncate_bytes(stem, MAX_FILENAME_BYTES - ext_bytes).rstrip(' .') or 'unnamed'
    cleaned = stem + ext

    if cleaned.split('.')[0].strip().upper() in WINDOWS_RESERVED_NAMES:
        cleaned = '_' + cleaned
    return cleaned


class Soundtrack:
    def __init__(self, soundtrack_id):
        self.id = soundtrack_id
        self.url = urljoin(BASE_URL, f'game-soundtracks/album/{self.id}')
        self._content_soup = None
        # Optional cached name from search results, avoids an extra HTTP request.
        self._lazy_name = None

    def _get_soup(self):
        try:
            r = SESSION.get(self.url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            raise KhinsiderError(f'Network error: {e}') from e

        soup = BeautifulSoup(r.content, 'html.parser')
        content = soup.find(id='pageContent')
        if content is None:
            # Blocked/challenged request, maintenance page or a layout change.
            # Raising here keeps the failure readable instead of an AttributeError.
            raise KhinsiderError(
                f'Unexpected page for soundtrack "{self.id}": no album content found '
                '(request blocked, site under maintenance, or page layout changed).'
            )
        if content.find(string=re.compile('No such album')):
            raise NonexistentSoundtrackError(f'Soundtrack "{self.id}" does not exist.')
        return content

    @property
    def content_soup(self):
        if self._content_soup is None:
            self._content_soup = self._get_soup()
        return self._content_soup

    @property
    def name(self):
        # Prefer name cached from search results (no HTTP needed).
        if self._lazy_name:
            return self._lazy_name
        h2 = self.content_soup.find('h2')
        if h2:
            return h2.text.strip()
        return self.id

    @property
    def available_formats(self):
        table = self.content_soup.find('table', id='songlist')
        if not table:
            return []
        header = table.find('tr')
        if not header:
            return []
        headings = [th.get_text(strip=True).lower() for th in header.find_all(['th', 'td'])]
        formats = [f for f in headings if f not in ('', 'track', 'song name', 'download', 'size', 'file size')]
        return formats if formats else ['mp3']

    @property
    def songs(self):
        table = self.content_soup.find('table', id='songlist')
        if not table:
            return []
        songs = []
        for tr in table.find_all('tr'):
            if tr.find('th'):
                continue
            a = tr.find('a')
            if a and 'href' in a.attrs:
                song_url = urljoin(self.url, a['href'])
                songs.append(Song(song_url))
        return songs

    def download(self, path='', format_order=None, verbose=False):
        # Only the album name derived from the site is sanitized; an explicit,
        # user-provided path is used as given.
        path = os.path.abspath(path or _sanitize_filename(self.name))
        os.makedirs(path, exist_ok=True)

        if format_order:
            format_order = [fmt.lower().lstrip('.') for fmt in format_order]
            if not set(self.available_formats).intersection(format_order):
                raise NonexistentFormatsError(self.available_formats, format_order)

        success = True
        songs = self.songs
        total_files = len(songs)
        for idx, song in enumerate(songs, 1):
            # Everything that can touch the network belongs inside the try: a
            # broken song page must fail that song, not the whole album.
            try:
                file_to_download = song.get_preferred_file(format_order)
                if file_to_download is None:
                    requested = '/'.join(format_order or []) or 'any'
                    offered = ', '.join(song.available_formats) or 'none'
                    print(
                        f'Skipping song {idx}/{total_files} "{song.name}": '
                        f'no {requested} file (track offers: {offered}).',
                        file=sys.stderr,
                    )
                    success = False
                    continue
                if verbose:
                    print(f'[{idx}/{total_files}] {file_to_download.filename}')
                file_to_download.download(path, show_progress=verbose)
            except (requests.RequestException, OSError, KhinsiderError) as e:
                print(
                    f'Failed to download song {idx}/{total_files} ({song.url}): {e}',
                    file=sys.stderr,
                )
                success = False
        return success


class Song:
    def __init__(self, url):
        self.url = url
        self._soup = None
        self._name = None
        self._files = None

    def _get_soup(self):
        try:
            r = SESSION.get(self.url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            if r.url.endswith('/404'):
                raise KhinsiderError('Song page not found (404)')
            return BeautifulSoup(r.content, 'html.parser')
        except requests.RequestException as e:
            raise KhinsiderError(f'Network error: {e}')

    @property
    def soup(self):
        if self._soup is None:
            self._soup = self._get_soup()
        return self._soup

    @property
    def name(self):
        if self._name is None:
            p_tags = self.soup.find_all('p')
            if len(p_tags) >= 3:
                b_tags = p_tags[2].find_all('b')
                if len(b_tags) >= 2:
                    self._name = b_tags[1].get_text(strip=True)
                else:
                    self._name = 'Unknown'
            else:
                self._name = 'Unknown'
        return self._name

    @property
    def files(self):
        if self._files is None:
            anchors = self.soup.find_all('a', href=re.compile(r'^https://[^/]+/(?:soundtracks|ost)/.+$'))
            self._files = [File(urljoin(self.url, a['href'])) for a in anchors]
        return self._files

    @property
    def available_formats(self):
        """Extensions offered by this track's page, in page order."""
        formats = []
        for f in self.files:
            ext = os.path.splitext(f.filename)[1].lower().lstrip('.')
            if ext and ext not in formats:
                formats.append(ext)
        return formats

    def get_preferred_file(self, format_order=None):
        if not self.files:
            return None
        if format_order is None:
            # No preference expressed: take whatever the page lists first.
            return self.files[0]
        for ext in format_order:
            for f in self.files:
                if f.filename.lower().endswith('.' + ext):
                    return f
        # A format preference was given and none of it matched. Returning None
        # (instead of the first file) fails this track with a clear message
        # rather than silently downloading a format the user did not ask for.
        return None


class File:
    def __init__(self, url):
        self.url = url
        self.filename = unquote(url.rsplit('/', 1)[-1])

    def download(self, directory, show_progress=False):
        final_path = os.path.join(directory, _sanitize_filename(self.filename))
        if os.path.exists(final_path):
            return
        tmp_path = final_path + '.part'

        # An interrupted earlier run leaves a .part behind; continue from there.
        resume_from = 0
        if os.path.exists(tmp_path):
            try:
                resume_from = os.path.getsize(tmp_path)
            except OSError:
                resume_from = 0

        headers = {'Range': f'bytes={resume_from}-'} if resume_from else None

        with SESSION.get(self.url, stream=True, timeout=REQUEST_TIMEOUT, headers=headers) as r:
            if resume_from and r.status_code == 416:
                # Nothing left to fetch: the partial file is already complete.
                os.replace(tmp_path, final_path)
                return
            r.raise_for_status()

            if resume_from and r.status_code != 206:
                # Server ignored the Range request, so the body is the whole file.
                resume_from = 0

            content_type = r.headers.get('Content-Type', '')
            if not any(ct in content_type for ct in ('audio', 'application/octet-stream')):
                raise KhinsiderError(f'URL did not return an audio file: {self.url}')

            total = self._total_size(r, resume_from)
            progress = None
            if show_progress:
                progress = tqdm(
                    total=total,
                    initial=resume_from,
                    unit='B',
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=self.filename[:40],
                    leave=False,
                )

            bytes_written = resume_from
            try:
                with open(tmp_path, 'ab' if resume_from else 'wb') as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                            bytes_written += len(chunk)
                            if progress is not None:
                                progress.update(len(chunk))
            finally:
                if progress is not None:
                    progress.close()

            if total is not None and bytes_written != total:
                # Keep the partial file: the next run resumes from here.
                raise KhinsiderError(
                    f'Incomplete download: expected {total} bytes, got {bytes_written}'
                )

        # Atomic rename only on full success.
        os.replace(tmp_path, final_path)

    @staticmethod
    def _total_size(response, already_have):
        """Full size of the remote file, or None when the server hides it."""
        content_range = response.headers.get('Content-Range')
        if content_range:
            match = re.match(r'bytes\s+\d+-\d+/(\d+)', content_range.strip())
            if match:
                return int(match.group(1))
        length = response.headers.get('Content-Length')
        if length:
            try:
                return already_have + int(length)
            except ValueError:
                return None
        return None

    @staticmethod
    def _sanitize_filename(filename):
        return _sanitize_filename(filename)


def search(term):
    try:
        r = SESSION.get(urljoin(BASE_URL, 'search'), params={'search': term}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')
        path = urlsplit(r.url).path
        if path.startswith('/game-soundtracks/album/'):
            soundtrack_id = path.rsplit('/', 1)[-1]
            return [[Soundtrack(soundtrack_id)]]

        tables = soup.find_all('table', class_='albumList')
        if not tables:
            raise KhinsiderError('No search results found.')

        results = []
        for table in tables:
            soundtracks = []
            for tr in table.find_all('tr')[1:]:
                td = tr.find_all('td')
                if len(td) > 1:
                    a = td[1].find('a')
                    if a and 'href' in a.attrs:
                        sid = a['href'].split('/')[-1]
                        name = a.get_text(strip=True)
                        s = Soundtrack(sid)
                        s._lazy_name = name
                        soundtracks.append(s)
            results.append(soundtracks)
        return results
    except requests.RequestException as e:
        raise KhinsiderError(f'Network error during search: {e}')


def main():
    parser = argparse.ArgumentParser(description='Download full soundtracks from KHInsider.')
    parser.add_argument('soundtrack', nargs='?', help='Soundtrack ID or search term')
    parser.add_argument('output_dir', nargs='?', default=None, help='Directory to save the soundtrack')
    parser.add_argument('-f', '--format', help='Preferred format(s), comma-separated (e.g. flac,mp3)')
    parser.add_argument('-s', '--search', action='store_true', help='Search for soundtracks instead of downloading')
    parser.add_argument('-q', '--quiet', action='store_true', help='Suppress per-file progress output')

    args = parser.parse_args()

    if not args.soundtrack:
        parser.print_help()
        sys.exit(1)

    format_order = None
    if args.format:
        format_order = [fmt.strip().lower() for fmt in args.format.split(',')]

    verbose = not args.quiet

    try:
        if args.search:
            results = search(args.soundtrack)
            if results:
                print('Search results:')
                for group in results:
                    for s in group:
                        print(f'{s.id}: {s.name}')
            else:
                print('No results found.')
        else:
            soundtrack = Soundtrack(args.soundtrack)
            output_dir = args.output_dir or soundtrack.name
            success = soundtrack.download(output_dir, format_order=format_order, verbose=verbose)
            if not success:
                print('Some files failed to download.', file=sys.stderr)
                sys.exit(1)
    except KhinsiderError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nInterrupted by user.', file=sys.stderr)
        sys.exit(130)


if __name__ == '__main__':
    main()
