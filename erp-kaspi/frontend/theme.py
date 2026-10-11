"""Design tokens — the single visual system for all UI modules (R1 split)."""

from __future__ import annotations

# Дизайн-токены (единая визуальная система всех модулей)
# --------------------------------------------------------------------------- #

ACCENT = "#F14635"  # Kaspi red — акцент бренда
INK = "#111827"  # основной текст
MUTED = "#6B7280"  # вторичный текст
SURFACE = "#FFFFFF"  # карточки
LINE = "#E5E7EB"  # границы
GOOD = "#15803D"  # доход
BAD = "#B91C1C"  # расход

THEME_CSS = f"""
<style>
    :root {{
        --acc: {ACCENT}; --ink: {INK}; --muted: {MUTED};
        --surface: {SURFACE}; --line: {LINE}; --good: {GOOD}; --bad: {BAD};
    }}
    #MainMenu, footer {{visibility: hidden;}}
    .block-container {{padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1500px;}}
    h1 {{font-size: 1.45rem !important; font-weight: 700 !important;
        color: var(--ink); margin-bottom: 0.1rem !important;}}
    .erp-sub {{color: var(--muted); font-size: 0.86rem; margin-bottom: 0.9rem;}}
    .erp-card {{background: var(--surface); border: 1px solid var(--line);
        border-radius: 12px; padding: 0.85rem 1.05rem;}}
    .erp-kpi-label {{color: var(--muted); font-size: 0.74rem; font-weight: 600;
        text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.15rem;}}
    .erp-kpi-value {{color: var(--ink); font-size: 1.28rem; font-weight: 700;
        font-variant-numeric: tabular-nums;}}
    .erp-kpi-good {{color: var(--good);}}
    .erp-kpi-bad {{color: var(--bad);}}
    .erp-chip {{display: inline-block; padding: 0.1rem 0.55rem; border-radius: 999px;
        font-size: 0.74rem; font-weight: 600; border: 1px solid var(--line);
        color: var(--muted); background: #F9FAFB;}}
    .erp-chip-ok {{border-color: #BBF7D0; background: #F0FDF4; color: var(--good);}}
    .erp-chip-warn {{border-color: #FED7AA; background: #FFF7ED; color: #C2410C;}}
    .erp-chip-err {{border-color: #FECACA; background: #FEF2F2; color: var(--bad);}}
    [data-testid="stSidebar"] {{border-right: 1px solid var(--line);}}
    [data-testid="stSidebar"] hr {{margin-top: 1.2rem;}}
    .stDataFrame {{border: 1px solid var(--line); border-radius: 10px;}}
    div[data-testid="stExpander"] {{border: 1px solid var(--line); border-radius: 10px;}}
</style>
"""
