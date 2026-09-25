#!/usr/bin/env python3
"""
Download the NLTK stopwords at build time and verify their checksum.

nltk.download() fetches whatever nltk_data publishes at the moment. This
script pins one commit of nltk_data and checks the SHA-256 of the archive,
so every build clusters with the same stopword lists and a tampered or
changed download fails the build instead of silently changing the results.

Usage: python fetch_nltk_data.py [--dest DIR]
    DIR defaults to $NLTK_DATA, else ~/nltk_data (where NLTK looks).
"""

import argparse
import hashlib
import io
import os
import sys
import urllib.request
import zipfile

# nltk_data commit 550b662 (gh-pages); the German list is the one the
# clustering threshold was calibrated with
STOPWORDS_URL = ('https://raw.githubusercontent.com/nltk/nltk_data/'
                 '550b6625bcef1f2abff2ff770a5a0d272c9c6b2a/packages/corpora/stopwords.zip')
STOPWORDS_SHA256 = '48c0e52d8b52546e827f53761fb30300c0ab94f70660d28bd65ba0a86270946b'
TIMEOUT = 60


class ChecksumError(ValueError):
    """The download does not match the pinned checksum."""


def fetch(url: str, sha256: str, dest: str) -> str:
    """
    Download a corpus archive, verify it and unpack it to dest/corpora.

    Returns:
        The directory the archive was unpacked to.

    Raises:
        ChecksumError: If the SHA-256 of the download differs.
    """
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        raise ChecksumError(f"{url}: SHA-256 {digest} does not match the pinned {sha256}")
    corpora = os.path.join(dest, 'corpora')
    os.makedirs(corpora, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(corpora)
    return corpora


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--dest', default=os.getenv('NLTK_DATA')
                        or os.path.join(os.path.expanduser('~'), 'nltk_data'),
                        help='NLTK data directory (default: $NLTK_DATA or ~/nltk_data)')
    args = parser.parse_args(argv)
    try:
        corpora = fetch(STOPWORDS_URL, STOPWORDS_SHA256, args.dest)
    except (OSError, ChecksumError, zipfile.BadZipFile) as e:
        print(f"Cannot install the NLTK stopwords: {e}", file=sys.stderr)
        return 1
    print(f"NLTK stopwords installed in {corpora}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
