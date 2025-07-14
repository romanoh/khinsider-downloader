import os
import re
import sys
import argparse
import requests
from urllib.parse import urljoin, urlsplit, unquote
from bs4 import BeautifulSoup

BASE_URL = 'https://downloads.khinsider.com/'

class KhinsiderError(Exception):
    pass

class NonexistentSoundtrackError(KhinsiderError):
    pass

class NonexistentFormatsError(KhinsiderError):
    def __init__(self, available_formats, requested_formats):
        self.available_formats = available_formats
        self.requested_formats = requested_formats

class Soundtrack:
    def __init__(self, soundtrack_id):
        self.id = soundtrack_id
        self.url = urljoin(BASE_URL, f'game-soundtracks/album/{self.id}')
        self._content_soup = None

    def _get_soup(self):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (compatible; KHInsiderDownloader/1.0)'}
            r = requests.get(self.url, headers=headers, timeout=10)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, 'html.parser')
            content = soup.find(id='pageContent')
            if content and content.find('p') and content.find('p').text.strip() == 'No such album':
                raise NonexistentSoundtrackError(f'Soundtrack "{self.id}" does not exist.')
            return content
        except requests.RequestException as e:
            raise KhinsiderError(f'Network error: {e}')

    @property
    def content_soup(self):
        if self._content_soup is None:
            self._content_soup = self._get_soup()
        return self._content_soup

    @property
    def name(self):
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
        formats = [f for f in headings if f not in ('', 'track', 'song name', 'download', 'size')]
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
        path = os.path.abspath(path or self.name)
        if not os.path.exists(path):
            os.makedirs(path)

        if format_order:
            format_order = [fmt.lower().lstrip('.') for fmt in format_order]
            if not set(self.available_formats).intersection(format_order):
                raise NonexistentFormatsError(self.available_formats, format_order)

        success = True
        total_files = len(self.songs)
        for idx, song in enumerate(self.songs, 1):
            file_to_download = song.get_preferred_file(format_order)
            if file_to_download is None:
                if verbose:
                    print(f'Skipping song {idx}/{total_files} "{song.name}": no matching format found.')
                success = False
                continue
            if verbose:
                print(f'Downloading song {idx}/{total_files}: {file_to_download.filename}')
            try:
                file_to_download.download(path)
            except Exception as e:
                if verbose:
                    print(f'Failed to download {file_to_download.filename}: {e}')
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
            headers = {'User-Agent': 'Mozilla/5.0 (compatible; KHInsiderDownloader/1.0)'}
            r = requests.get(self.url, headers=headers, timeout=10)
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
            anchors = self.soup.find_all('a', href=re.compile(r'^https?://[^/]+/(?:soundtracks|ost)/.+$'))
            self._files = [File(urljoin(self.url, a['href'])) for a in anchors]
        return self._files

    def get_preferred_file(self, format_order=None):
        if not self.files:
            return None
        if format_order is None:
            return self.files[0]
        for ext in format_order:
            for f in self.files:
                if f.filename.lower().endswith('.' + ext):
                    return f
        return self.files[0]

class File:
    def __init__(self, url):
        self.url = url
        self.filename = unquote(url.rsplit('/', 1)[-1])

    def download(self, directory):
        path = os.path.join(directory, self._sanitize_filename(self.filename))
        if os.path.exists(path):
            return
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; KHInsiderDownloader/1.0)'}
        with requests.get(self.url, headers=headers, stream=True, timeout=10) as r:
            r.raise_for_status()
            content_type = r.headers.get('Content-Type', '')
            if not any(ct in content_type for ct in ('audio', 'application/octet-stream')):
                raise KhinsiderError(f'URL did not return an audio file: {self.url}')
            with open(path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

    @staticmethod
    def _sanitize_filename(filename):
        invalid_chars = r'<>:"/\\|?*'
        return ''.join(c if c not in invalid_chars else '-' for c in filename).rstrip(' .')

def search(term):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; KHInsiderDownloader/1.0)'}
    try:
        r = requests.get(urljoin(BASE_URL, 'search'), params={'search': term}, headers=headers, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')
        path = urlsplit(r.url).path
        if path.startswith('/game-soundtracks/album/'):
            soundtrack_id = path.rsplit('/', 1)[-1]
            return [Soundtrack(soundtrack_id)]

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
                        s._content_soup = None
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

    args = parser.parse_args()

    if not args.soundtrack:
        parser.print_help()
        sys.exit(1)

    format_order = None
    if args.format:
        format_order = [fmt.strip().lower() for fmt in args.format.split(',')]

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
            success = soundtrack.download(output_dir, format_order=format_order, verbose=True)
            if not success:
                print('Some files failed to download.', file=sys.stderr)
                sys.exit(1)
    except KhinsiderError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
