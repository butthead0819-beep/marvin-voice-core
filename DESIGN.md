# Design System — Marvin

## Product Context
- **What this is:** Discord Voice AI Agent landing page
- **Who it's for:** Twitch streamers with active Discord communities (100–5000 members)
- **Space/industry:** Creator tools / Discord bots / Voice AI
- **Project type:** Marketing site / landing page

## Memorable Thing
> "I want to try this right now."

Every design decision serves this. The first interaction is sound, not a signup form.

## Aesthetic Direction
- **Direction:** Cinematic Minimal
- **Decoration level:** Minimal (waveform is the only decorative element)
- **Mood:** Recording studio at 2am. The feeling of being live. Warm, alive, present — not gamey, not corporate SaaS.

## Typography
- **Display/Hero:** Fraunces (serif with warmth and personality — like a voice has character)
- **Body/UI:** Instrument Sans (clean, readable, modern without being generic)
- **Loading:** Google Fonts CDN
  ```html
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,400;0,9..144,600;1,9..144,300;1,9..144,400&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  ```
- **Scale:**
  - Hero: clamp(42px, 6vw, 80px), weight 300, letter-spacing -0.03em
  - Section title: clamp(32px, 4vw, 48px), weight 300, letter-spacing -0.02em
  - Body: 16–18px, weight 400, line-height 1.6–1.7
  - Label/caption: 12–13px, weight 500–600, letter-spacing 0.08–0.12em
  - Nav/UI: 14–15px, weight 500

## Color
- **Approach:** Restrained — amber is rare and meaningful, never decorative

| Token | Hex | Usage |
|-------|-----|-------|
| `--bg` | `#0A0908` | Page background (warm near-black, not cold) |
| `--surface` | `#141210` | Card / section backgrounds |
| `--surface2` | `#1C1917` | Borders, dividers |
| `--text` | `#F5F0EB` | Primary text (warm white) |
| `--muted` | `#7A7068` | Secondary text, labels |
| `--muted2` | `#4A4540` | Borders on interactive elements |
| `--amber` | `#F5A623` | Accent — "going live" indicator color. Used for: CTAs, highlights, waveform, live dot, section labels |
| `--amber-dim` | `#9B6614` | Step numbers, decorative amber (low emphasis) |
| `--amber-glow` | `rgba(245,166,35,0.15)` | Radial glow behind hero waveform |

**Dark mode:** This IS the dark mode. No light mode variant.

**Why amber, not purple:** Every competitor (Discord, Twitch, MEE6, Streamcord) uses purple/blue. Amber owns a unique position and directly references "going live" indicator lights that streamers already associate with being on-air.

## Spacing
- **Base unit:** 8px
- **Density:** Comfortable
- **Scale:** 4 / 8 / 16 / 24 / 32 / 40 / 48 / 64 / 96 / 120
- **Section padding:** 96px vertical / 48px horizontal (desktop), 64px / 24px (mobile)
- **Max content width:** 1100px

## Layout
- **Approach:** Grid-disciplined with centered hero
- **Hero:** Full-bleed, vertically centered, max-width 820px for headline
- **Content sections:** max-width 1100px, centered
- **Cards:** 3-column grid desktop, 1-column mobile
- **Border radius:** 6px (buttons), 8px (CTAs), 12px (cards), 16px (feature grid)

## Components

### Waveform (hero visual)
- 21 bars, varying heights (8px–64px)
- Width: 3px per bar, gap: 4px
- Color: `--amber`, opacity 0.4 idle / 0.8 hover / 1.0 playing
- Animation: `scaleY` pulse, staggered delays 0–0.4s, duration 1.4s ease-in-out
- Click to play demo audio — the primary interactive moment

### Buttons
```css
/* Primary */
background: var(--amber); color: var(--bg);
padding: 16px 32px; border-radius: 8px;
font: 600 16px Instrument Sans;

/* Secondary */
background: transparent; color: var(--muted);
border: 1px solid var(--muted2); padding: 15px 28px;
border-radius: 8px;

/* Tier (pricing) */
border: 1px solid var(--muted2); padding: 13px;
border-radius: 7px; width: 100%;
```

### Live Badge
```css
background: rgba(245,166,35,0.1);
border: 1px solid rgba(245,166,35,0.2);
border-radius: 100px; padding: 6px 14px;
font: 600 12px/1 Instrument Sans; letter-spacing: 0.08em;
color: var(--amber); text-transform: uppercase;
```
With animated dot: 7px circle, `--amber`, pulse animation.

### Section Label
```css
font: 600 11px Instrument Sans;
letter-spacing: 0.12em; text-transform: uppercase;
color: var(--amber); margin-bottom: 16px;
```

## Motion
- **Approach:** Minimal-functional — only the waveform animates, everything else is static
- **Waveform:** `scaleY` 1→0.3→1, 1.4s ease-in-out, staggered per bar
- **Live dot:** opacity + scale pulse, 2s ease-in-out
- **Hover transitions:** 0.2s ease (color, opacity, border-color)
- **CTA hover:** translateY(-1px), 0.1s
- **No scroll animations, no entrance animations** — content is present immediately

## Page Structure

```
NAV (fixed, fade-to-transparent)
HERO
  ├── Live badge
  ├── H1 headline (Fraunces italic amber on key word)
  ├── Subheadline
  ├── Waveform (click to play)
  └── CTAs: "Hear Marvin" (primary) + "Add to Discord — it's free" (secondary)
SOCIAL PROOF STRIP (4 stats)
HOW IT WORKS (3 steps)
FEATURES (3-column grid)
PRICING (Free / $15 Streamer / $30 Community)
FINAL CTA
FOOTER
```

## Copy Rules
- Headlines: Fraunces, weight 300, italic on the emotional word (e.g. *you*, *missing*)
- No exclamation marks in hero copy
- CTA copy leads with the experience, not the action: "Hear Marvin" not "Sign Up"
- Pricing CTA: "Add to Discord" not "Get Started" or "Subscribe"

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-05-13 | Amber #F5A623 accent | All competitors use purple. Amber references "going live" indicator lights. |
| 2026-05-13 | Fraunces for hero type | Warmth and personality — like a voice has character. Differentiates from SaaS grotesks. |
| 2026-05-13 | Waveform as primary visual | Voice is the product. No Discord screenshots, no bot avatars. |
| 2026-05-13 | Audio demo button in hero | Memorable thing is "I want to try this." First interaction is sound. |
| 2026-05-13 | No light mode | Target audience (streamers, Discord users) lives in dark mode. |
| 2026-05-13 | Created via /design-consultation | Based on office-hours session — Twitch streamer market, MEE6 alternative positioning |
