"""
The navigation bar shared by every page this server serves.

The dashboard and the calendar are generated HTML files that also open on their own from disk,
so the bar is added when they are *served*, not baked into them: a standalone copy stays
exactly as it was, and the served one can link to the app. The app (`index.html`) carries the
same links in its own header because it highlights the open screen from the URL hash; a test
holds the two lists together so a link added in one place cannot be missing from the other.
"""

import re

# (label, href, key). `key` is what a page passes as `active`.
ITEMS = (
    ("Plan", "/app#plan", "plan"),
    ("Coach", "/app#coach", "coach"),
    ("Signals", "/app#signals", "signals"),
    ("Setup", "/app#setup", "setup"),
    ("Settings", "/app#settings", "settings"),
    ("Ladder", "/app#ladder", "ladder"),
    ("Dashboard", "/dashboard", "dashboard"),
    ("Calendar", "/calendar", "calendar"),
)

_CSS = """
<style id="fc-nav-style">
  #fc-nav { display:flex; flex-wrap:wrap; align-items:center; gap:4px 20px; padding:8px 16px;
            background:#1f2937; border-bottom:1px solid #374151; flex-shrink:0;
            font:14px/1.4 Inter, system-ui, sans-serif; position:relative; z-index:100; }
  #fc-nav b { font-size:15px; margin-right:6px; color:#f9fafb; }
  #fc-nav a { color:#9ca3af; text-decoration:none; padding:5px 12px; border-radius:6px; }
  #fc-nav a:hover { color:#f9fafb; }
  #fc-nav a[aria-current="page"] { color:#f9fafb; background:rgba(255,255,255,.08); }
  #fc-nav.bleed { margin:-1.5rem -1.5rem 1.5rem; }
  #fc-nav .fc-badge { display:inline-block; min-width:18px; margin-left:6px; padding:0 5px; border-radius:9px;
                      background:#fbbf24; color:#111827; font-size:11px; font-weight:700; text-align:center; }
</style>
"""

# A count on the Coach link when a weekly check-in or a block review is due. The rules live in
# src/coach/due.py and come back from the server; this only shows the number.
_BADGE_SCRIPT = """<script>
fetch('/api/chat/due').then(r => r.ok ? r.json() : null).then(d => {
  if (!d || !d.attention) return;
  const a = document.querySelector('#fc-nav a[href="/app#coach"]');
  if (a) { const b = document.createElement('span'); b.className = 'fc-badge'; b.textContent = d.attention; a.appendChild(b); }
}).catch(() => {});
</script>"""


def nav_html(active: str, bleed: bool = False) -> str:
    links = "".join(
        f'<a href="{href}"' + (' aria-current="page"' if key == active else "") + f">{label}</a>"
        for label, href, key in ITEMS)
    return (f'{_CSS}<nav id="fc-nav"{" class=bleed" if bleed else ""} aria-label="Main">'
            f"<b>Fitness Coach</b>{links}</nav>{_BADGE_SCRIPT}")


def with_nav(html: str, active: str, bleed: bool = False) -> str:
    """Insert the bar as the first thing inside <body>. A page with no <body> is returned as is."""
    match = re.search(r"<body[^>]*>", html, flags=re.IGNORECASE)
    if match is None:
        return html
    return html[:match.end()] + nav_html(active, bleed) + html[match.end():]
