"""
Single-page dashboard — all charts embedded, expand-to-modal, chat placeholder.
Generates a standalone HTML file with Plotly loaded once from CDN.
"""

import json
from datetime import date
from pathlib import Path
from src.visualizations import pmc, load, recovery
from src.db.schema import get_connection

CHARTS_DIR = Path(__file__).parent.parent.parent / "data" / "charts"

# Date-range filter presets. "days" is used to seed the slider position; YTD/All are computed
# relative to today at render time in JS since "days ago" isn't fixed for either of them.
RANGE_PRESETS = [
    ("ytd", "YTD",  None),
    ("1mo", "1mo",  30),
    ("3mo", "3mo",  90),
    ("6mo", "6mo",  180),
    ("1yr", "1yr",  365),
    ("all", "All",  None),
]
DEFAULT_PRESET = "3mo"


def _earliest_data_date() -> str:
    """Earliest date across wellness/activities — bounds the filter's "All" / slider max."""
    conn = get_connection()
    row = conn.execute("""
        SELECT MIN(d) FROM (
            SELECT MIN(date) as d FROM wellness
            UNION ALL
            SELECT MIN(date) as d FROM activities
        )
    """).fetchone()
    conn.close()
    return row[0] or date.today().isoformat()


def _embed(fig, div_id: str) -> str:
    """Embed a Plotly figure as a responsive div (no bundled JS)."""
    # Remove fixed height so the div fills its CSS container; keep autosize for resize.
    fig.update_layout(autosize=True, height=None, margin=dict(l=40, r=16, t=48, b=64))
    return fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        div_id=div_id,
        config={"responsive": True, "displayModeBar": True,
                "modeBarButtonsToRemove": ["select2d", "lasso2d"],
                "displaylogo": False},
    )


def build_dashboard() -> str:
    today = date.today().strftime("%B %d, %Y")
    today_iso = date.today().isoformat()
    earliest_iso = _earliest_data_date()

    # Full history baked in for all three — the date-range filter is client-side (Plotly
    # relayout, zooming the visible x-axis range), so it can only reveal data already present.
    charts = {
        "pmc":      _embed(pmc.build(weeks=None),     "chart_pmc"),
        "load":     _embed(load.build(weeks=None),    "chart_load"),
        "recovery": _embed(recovery.build(days=None), "chart_recovery"),
    }

    preset_buttons = "\n".join(
        f'<button class="preset-btn" data-preset="{key}" onclick="selectPreset(\'{key}\')">{label}</button>'
        for key, label, _ in RANGE_PRESETS
    )
    presets_json = json.dumps({key: days for key, _, days in RANGE_PRESETS})
    max_days = max((date.today() - date.fromisoformat(earliest_iso)).days, 7)
    default_days = dict((key, days) for key, _, days in RANGE_PRESETS)[DEFAULT_PRESET]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fitness Coach — Ian Auger</title>
  <link rel="icon" href="data:image/svg+xml,&lt;svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22&gt;&lt;text y=%22.9em%22 font-size=%2290%22&gt;🚴&lt;/text&gt;&lt;/svg&gt;">
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg:       #111827;
      --surface:  #1f2937;
      --border:   #374151;
      --text:     #f9fafb;
      --subtext:  #9ca3af;
      --accent:   #60a5fa;
      --radius:   8px;
    }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: Inter, system-ui, sans-serif;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}

    /* ── Header ── */
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 1.5rem;
      height: 52px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }}
    header h1 {{
      font-size: 1rem;
      font-weight: 600;
      color: var(--text);
      letter-spacing: 0.01em;
    }}
    header span {{
      font-size: 0.8rem;
      color: var(--subtext);
    }}
    header .calendar-link {{
      font-size: 0.8rem;
      color: var(--accent);
      text-decoration: none;
      padding: 4px 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
    }}
    header .calendar-link:hover {{
      background: rgba(255,255,255,0.06);
    }}
    header .header-right {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}

    /* ── Main layout ── */
    .layout {{
      display: grid;
      grid-template-columns: 1fr 360px;
      flex: 1;
      overflow: hidden;
    }}

    /* ── Charts panel ── */
    .charts-panel {{
      overflow-y: auto;
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }}

    .chart-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      position: relative;
      flex-shrink: 0;
    }}

    .chart-row {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
      flex-shrink: 0;
    }}

    /* ── Date-range filter bar ── */
    .range-bar {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 0.6rem 1rem;
      display: flex;
      align-items: center;
      gap: 1rem;
      flex-shrink: 0;
    }}
    .range-presets {{
      display: flex;
      gap: 0.35rem;
      flex-shrink: 0;
    }}
    .preset-btn {{
      background: transparent;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--subtext);
      font-size: 0.75rem;
      padding: 4px 10px;
      cursor: pointer;
      transition: background 0.15s, color 0.15s, border-color 0.15s;
    }}
    .preset-btn:hover {{ background: rgba(255,255,255,0.06); color: var(--text); }}
    .preset-btn.active {{
      background: rgba(96,165,250,0.15);
      border-color: var(--accent);
      color: var(--accent);
    }}
    .range-slider-row {{
      flex: 1;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      min-width: 0;
    }}
    .range-slider-row input[type="range"] {{
      flex: 1;
      accent-color: var(--accent);
    }}
    .range-label {{
      font-size: 0.75rem;
      color: var(--subtext);
      white-space: nowrap;
      min-width: 82px;
    }}

    .chart-inner {{
      width: 100%;
      height: 100%;
      display: block;
    }}

    /* chart heights — set on the card so the inner div inherits 100% */
    .chart-card.tall   {{ height: 860px; }}
    .chart-card.medium {{ height: 400px; }}
    .chart-card.short  {{ height: 340px; }}

    /* ensure Plotly's generated divs fill their container */
    .chart-inner > div {{ width: 100% !important; height: 100% !important; }}
    .js-plotly-plot, .plotly-graph-div {{ width: 100% !important; height: 100% !important; }}

    /* ── Expand button ── */
    .expand-btn {{
      position: absolute;
      top: 10px;
      right: 10px;
      background: rgba(255,255,255,0.08);
      border: 1px solid var(--border);
      border-radius: 4px;
      color: var(--subtext);
      font-size: 11px;
      padding: 3px 8px;
      cursor: pointer;
      z-index: 10;
      transition: background 0.15s;
    }}
    .expand-btn:hover {{ background: rgba(255,255,255,0.15); color: var(--text); }}

    /* ── Modal ── */
    .modal-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.82);
      z-index: 1000;
      align-items: center;
      justify-content: center;
    }}
    .modal-overlay.open {{ display: flex; }}
    .modal-box {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      width: 94vw;
      height: 88vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    .modal-header {{
      display: flex;
      justify-content: flex-end;
      padding: 8px 12px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }}
    .modal-close {{
      background: none;
      border: none;
      color: var(--subtext);
      font-size: 1.2rem;
      cursor: pointer;
      padding: 4px 8px;
      border-radius: 4px;
    }}
    .modal-close:hover {{ background: var(--border); color: var(--text); }}
    .modal-content {{
      flex: 1;
      overflow: hidden;
    }}
    #modal-chart-container {{
      width: 100%;
      height: 100%;
    }}

    /* ── Chat panel ── */
    .chat-panel {{
      background: var(--surface);
      border-left: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    .chat-header {{
      padding: 1rem;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }}
    .chat-header h2 {{
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--text);
    }}
    .chat-header p {{
      font-size: 0.75rem;
      color: var(--subtext);
      margin-top: 2px;
    }}
    .chat-messages {{
      flex: 1;
      overflow-y: auto;
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }}
    .chat-placeholder {{
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 0.5rem;
      color: var(--subtext);
      text-align: center;
      padding: 2rem;
    }}
    .chat-placeholder .icon {{
      font-size: 2rem;
      opacity: 0.4;
    }}
    .chat-placeholder p {{
      font-size: 0.8rem;
      line-height: 1.5;
    }}
    .chat-input-area {{
      padding: 0.75rem;
      border-top: 1px solid var(--border);
      flex-shrink: 0;
    }}
    .chat-input-row {{
      display: flex;
      gap: 0.5rem;
    }}
    .chat-input {{
      flex: 1;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--subtext);
      font-size: 0.85rem;
      padding: 0.5rem 0.75rem;
      outline: none;
      cursor: not-allowed;
    }}
    .chat-send {{
      background: var(--accent);
      border: none;
      border-radius: 6px;
      color: #111;
      font-size: 0.85rem;
      font-weight: 600;
      padding: 0.5rem 1rem;
      cursor: not-allowed;
      opacity: 0.4;
    }}
    .coming-soon-badge {{
      display: inline-block;
      background: rgba(96,165,250,0.15);
      border: 1px solid rgba(96,165,250,0.3);
      color: var(--accent);
      font-size: 0.7rem;
      padding: 2px 8px;
      border-radius: 99px;
      margin-left: 0.5rem;
    }}
  </style>
</head>
<body>

<header>
  <h1>Fitness Coach &mdash; Ian Auger</h1>
  <div class="header-right">
    <a class="calendar-link" href="calendar.html">&#128197; Calendar</a>
    <span>{today}</span>
  </div>
</header>

<div class="layout">

  <!-- Charts panel -->
  <div class="charts-panel">

    <!-- Date-range filter — drives PMC, Load, and Recovery in sync -->
    <div class="range-bar">
      <div class="range-presets" id="preset-buttons">
        {preset_buttons}
      </div>
      <div class="range-slider-row">
        <input type="range" id="range-slider" min="7" max="{max_days}" step="1" value="{default_days}">
        <span class="range-label" id="range-label"></span>
      </div>
    </div>

    <!-- PMC full width -->
    <div class="chart-card tall">
      <button class="expand-btn" onclick="expandChart('chart_pmc')">&#x26F6; Expand</button>
      <div class="chart-inner">{charts["pmc"]}</div>
    </div>

    <!-- Row 2: Load + Recovery -->
    <div class="chart-row">
      <div class="chart-card medium">
        <button class="expand-btn" onclick="expandChart('chart_load')">&#x26F6; Expand</button>
        <div class="chart-inner">{charts["load"]}</div>
      </div>
      <div class="chart-card medium">
        <button class="expand-btn" onclick="expandChart('chart_recovery')">&#x26F6; Expand</button>
        <div class="chart-inner">{charts["recovery"]}</div>
      </div>
    </div>

  </div>

  <!-- Chat panel -->
  <div class="chat-panel">
    <div class="chat-header">
      <h2>Coach <span class="coming-soon-badge">coming soon</span></h2>
      <p>Weekly check-ins &amp; conversational coaching</p>
    </div>
    <div class="chat-placeholder">
      <div class="icon">&#x1F4AC;</div>
      <p>The coaching chat interface will live here.<br>
         Run <code style="font-size:0.75rem; color:#60a5fa">scripts/ask.py</code> in the terminal for now.</p>
    </div>
    <div class="chat-input-area">
      <div class="chat-input-row">
        <input class="chat-input" type="text" placeholder="Chat interface coming soon..." disabled>
        <button class="chat-send" disabled>Send</button>
      </div>
    </div>
  </div>

</div>

<!-- Expand modal -->
<div class="modal-overlay" id="modal" onclick="closeModal(event)">
  <div class="modal-box">
    <div class="modal-header">
      <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="modal-content">
      <div id="modal-chart-container"></div>
    </div>
  </div>
</div>

<script>
  // Force every chart to resize to its actual container on load — Plotly's autosize can
  // compute against a not-yet-settled flex/grid layout on first paint, leaving charts
  // rendered (and then clipped by .chart-card's overflow:hidden) at the wrong height.
  function resizeAllCharts() {{
    document.querySelectorAll('.js-plotly-plot').forEach(el => {{
      Plotly.Plots.resize(el);
    }});
  }}
  window.addEventListener('resize', resizeAllCharts);
  window.addEventListener('load', resizeAllCharts);
  // Belt-and-suspenders: run again shortly after load in case fonts/layout shift after
  // the load event fires (webfonts loading late can still change container heights).
  window.addEventListener('load', () => setTimeout(resizeAllCharts, 250));

  // ── Date-range filter — one control drives PMC, Load, and Recovery in sync ──
  const PRESET_DAYS = {presets_json};   // {{key: days_back}}, null = computed specially (YTD/All)
  const EARLIEST_ISO = '{earliest_iso}';
  const TODAY_ISO = '{today_iso}';
  const DEFAULT_PRESET = '{DEFAULT_PRESET}';

  // Per-chart x-axis keys to set on relayout — PMC is a 4-row shared_xaxes subplot, Load is
  // single-axis, Recovery is a 2-row shared_xaxes subplot. Setting every row's axis explicitly
  // rather than relying on shared_xaxes to propagate a scripted relayout across rows.
  const CHART_AXES = {{
    chart_pmc:      ['xaxis', 'xaxis2', 'xaxis3', 'xaxis4'],
    chart_load:     ['xaxis'],
    chart_recovery: ['xaxis', 'xaxis2'],
  }};

  function daysAgoIso(days) {{
    const d = new Date(TODAY_ISO);
    d.setDate(d.getDate() - days);
    return d.toISOString().slice(0, 10);
  }}

  function startDateForPreset(key) {{
    if (key === 'ytd') {{
      return TODAY_ISO.slice(0, 4) + '-01-01';
    }}
    if (key === 'all') {{
      return EARLIEST_ISO;
    }}
    return daysAgoIso(PRESET_DAYS[key]);
  }}

  function applyRange(startIso) {{
    for (const [divId, axes] of Object.entries(CHART_AXES)) {{
      const el = document.getElementById(divId);
      if (!el || !el.data) continue; // chart may be empty (no data) — nothing to relayout
      const relayoutUpdate = {{}};
      axes.forEach(ax => {{ relayoutUpdate[ax + '.range'] = [startIso, TODAY_ISO]; }});
      Plotly.relayout(el, relayoutUpdate);
    }}
  }}

  function updateRangeLabel(startIso) {{
    document.getElementById('range-label').textContent = startIso + ' → ' + TODAY_ISO;
  }}

  function daysBetween(startIso) {{
    const start = new Date(startIso);
    const today = new Date(TODAY_ISO);
    return Math.round((today - start) / 86400000);
  }}

  function setActivePreset(key) {{
    document.querySelectorAll('.preset-btn').forEach(btn => {{
      btn.classList.toggle('active', btn.dataset.preset === key);
    }});
  }}

  function selectPreset(key) {{
    const startIso = startDateForPreset(key);
    applyRange(startIso);
    updateRangeLabel(startIso);
    setActivePreset(key);
    const slider = document.getElementById('range-slider');
    slider.value = Math.min(Math.max(daysBetween(startIso), slider.min), slider.max);
  }}

  function onSliderInput() {{
    const slider = document.getElementById('range-slider');
    const startIso = daysAgoIso(Number(slider.value));
    applyRange(startIso);
    updateRangeLabel(startIso);
    // Only highlight a preset button if the slider landed exactly on its value —
    // otherwise this is a free drag and no preset should look selected.
    const days = Number(slider.value);
    const matched = Object.entries(PRESET_DAYS).find(([key, d]) => d === days);
    setActivePreset(matched ? matched[0] : null);
  }}

  document.getElementById('range-slider').addEventListener('input', onSliderInput);
  window.addEventListener('load', () => selectPreset(DEFAULT_PRESET));

  function expandChart(divId) {{
    const source = document.getElementById(divId);
    if (!source) return;
    const container = document.getElementById('modal-chart-container');
    const overlay = document.getElementById('modal');

    // Show the overlay BEFORE drawing — Plotly reads the container's real clientWidth/
    // Height at newPlot() time, and a display:none container reports 0, which makes
    // Plotly silently fall back to its hardcoded 700x450 default instead of autosizing.
    overlay.classList.add('open');

    // clone the plotly data into the modal container. source.layout.width/height get
    // baked in by Plotly's own autosize once the small card renders — must be stripped
    // or the modal chart renders at the old small-card size instead of filling 94vw/88vh.
    const data = source.data;
    const layout = Object.assign({{}}, source.layout, {{
      height: window.innerHeight * 0.82,
      autosize: true,
      margin: {{ l: 48, r: 24, t: 48, b: 72 }},
    }});
    delete layout.width;
    Plotly.newPlot(container, data, layout, {{responsive: true, displaylogo: false}});
  }}

  function closeModal(event) {{
    if (event && event.target !== document.getElementById('modal')) return;
    document.getElementById('modal').classList.remove('open');
    Plotly.purge(document.getElementById('modal-chart-container'));
  }}

  // Esc key closes modal
  document.addEventListener('keydown', e => {{
    if (e.key === 'Escape') closeModal();
  }});
</script>
</body>
</html>"""


def write_dashboard(open_browser: bool = False) -> Path:
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out = CHARTS_DIR / "dashboard.html"
    out.write_text(build_dashboard(), encoding="utf-8")
    if open_browser:
        import webbrowser
        webbrowser.open(out.as_uri())
    return out
