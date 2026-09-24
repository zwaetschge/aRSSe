#!/usr/bin/env python3
"""Write synthetic RSS feeds with current timestamps for the integration test."""

import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

BUDGET = ('Der Bundestag hat den Bundeshaushalt für das kommende Jahr beschlossen. '
          'Der Finanzminister verteidigt die Schuldenbremse, die Opposition kritisiert '
          'Kürzungen bei Bildung und Infrastruktur im Haushalt. Der Bundesrat muss dem '
          'Haushalt im Dezember noch zustimmen.')
CHIP = ('Ein Chiphersteller hat einen neuen Prozessor mit deutlich höherer Rechenleistung '
        'vorgestellt. Er verspricht effizientere Grafikeinheiten und längere Akkulaufzeit '
        'für Notebooks.')

FEEDS = {
    'alpha': [
        ('Bundestag beschließt Haushalt', BUDGET),
        ('Neuer Prozessor vorgestellt', CHIP),
        ('Wetter: Sonne am Wochenende', 'Meteorologen erwarten sommerliche Temperaturen.'),
    ],
    'beta': [
        # Agency copy: same text as alpha's budget article -> duplicate
        # (needs deduplication.min_body_tokens words besides the title)
        ('Bundestag beschließt Haushalt', BUDGET),
        ('Chiphersteller zeigt neuen Prozessor',
         'Der neue Prozessor soll Notebooks schneller machen. Erste Benchmarks zur '
         'Rechenleistung und Akkulaufzeit folgen in wenigen Wochen.'),
    ],
    'gamma': [
        ('Haushalt: Bundestag stimmt zu',
         'Nach langer Debatte stimmt der Bundestag dem Haushalt zu. Die Schuldenbremse '
         'bleibt, die Opposition kritisiert die Kürzungen bei der Bildung.'),
        ('Fußball: Pokalspiel endet unentschieden', 'Nach 120 Minuten stand es 1:1.'),
    ],
}


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    for name, items in FEEDS.items():
        entries = []
        for n, (title, text) in enumerate(items):
            published = format_datetime(now - timedelta(minutes=10 + n))
            entries.append(f"""<item>
  <title>{escape(title)}</title>
  <link>https://example.org/{name}/{n}</link>
  <guid>https://example.org/{name}/{n}</guid>
  <pubDate>{published}</pubDate>
  <description>{escape(text)}</description>
</item>""")
        (out / f'{name}.xml').write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>{name}</title><link>https://example.org/{name}</link><description>{name}</description>
{chr(10).join(entries)}
</channel></rss>
""", encoding='utf-8')


if __name__ == '__main__':
    main(sys.argv[1])
