---
name: XAUUSD AI Trader — Trading Observatory
description: A calm, audit-first mission-control system for verified read-only trading telemetry.
colors:
  canvas: "#0b1117"
  sidebar: "#0d131a"
  panel: "#151a22"
  panel-raised: "#181f28"
  line: "#262c36"
  line-strong: "#303945"
  text: "#e7ecf3"
  muted: "#8d99a8"
  dim: "#96a3b3"
  signal-green: "#3bc784"
  caution-amber: "#e8b04a"
  fault-red: "#ef6262"
  information-blue: "#64a7f2"
typography:
  display:
    fontFamily: "Bahnschrift, Aptos Display, Segoe UI Variable, sans-serif"
    fontSize: "clamp(23px, 2vw, 29px)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.025em"
  title:
    fontFamily: "Bahnschrift, Aptos Display, Segoe UI Variable, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "0.01em"
  body:
    fontFamily: "Segoe UI Variable, Segoe UI, ui-sans-serif, system-ui, sans-serif"
    fontSize: "10px"
    fontWeight: 400
  label:
    fontFamily: "Segoe UI Variable, Segoe UI, ui-sans-serif, system-ui, sans-serif"
    fontSize: "8px"
    fontWeight: 700
    letterSpacing: "0.07em"
  mono:
    fontFamily: "Cascadia Code, SFMono-Regular, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 400
rounded:
  micro: "3px"
  compact: "5px"
  control: "6px"
  interactive: "7px"
  inset: "8px"
  summary: "9px"
  panel: "10px"
  pill: "999px"
spacing:
  "2xs": "4px"
  xs: "8px"
  sm: "10px"
  md: "12px"
  lg: "14px"
  xl: "18px"
  "2xl": "24px"
components:
  panel-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    padding: "14px"
  status-connected:
    backgroundColor: "#102019"
    textColor: "{colors.signal-green}"
    rounded: "{rounded.pill}"
    padding: "0 8px"
    height: "24px"
  status-warning:
    backgroundColor: "#1d190f"
    textColor: "{colors.caution-amber}"
    rounded: "{rounded.pill}"
    padding: "0 8px"
    height: "24px"
  field:
    backgroundColor: "#10161d"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "0 9px"
    height: "34px"
  button-text:
    backgroundColor: "#111d28"
    textColor: "{colors.information-blue}"
    rounded: "{rounded.compact}"
    padding: "0 9px"
    height: "28px"
  nav-active:
    backgroundColor: "#172431"
    textColor: "#e9f3ff"
    rounded: "{rounded.interactive}"
    padding: "0 11px"
    height: "38px"
  range-control:
    backgroundColor: "#10161d"
    textColor: "{colors.dim}"
    rounded: "{rounded.control}"
    padding: "2px"
    height: "29px"
  audit-timeline-item:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    padding: "0"
    height: "57px"
  event-severity-info:
    backgroundColor: "transparent"
    textColor: "{colors.information-blue}"
    rounded: "{rounded.micro}"
    padding: "2px 5px"
  empty-state:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.dim}"
    padding: "20px"
    height: "120px"
---

# Design System: XAUUSD AI Trader — Trading Observatory

## Overview

**Creative North Star: "The Read-Only Mission Control"**

The Trading Observatory is a compact operational flight deck where verified system truth is the visual center. Its charcoal-navy field, graphite seams, precise alignment, and sparse semantic signals make dense account, risk, health, and event data feel controlled rather than dramatic.

The system combines the approved audit-first composition with an Equity & Drawdown monitoring field and a realtime Event Stream. It is calm, factual, and explicitly non-transactional: hierarchy comes from chronology, typography, and tonal layering, never from casino color, promotional glow, or execution affordances.

**Key Characteristics:**

- Dense, desktop-first information architecture that preserves audit chronology.
- Flat graphite surfaces separated by one-pixel seams.
- Restrained blue information accents and explicit green, amber, and red status language.
- Monospaced telemetry for numbers, timestamps, tickets, identifiers, and state values.
- Honest empty, unavailable, unknown, planned, and disabled states.
- Responsive reordering that keeps system truth ahead of secondary monitoring.

## Colors

The palette is a cool charcoal infrastructure field with a single blue information voice and tightly rationed semantic signals.

### Primary

- **Information Blue:** Used for active navigation, focus indication, informational context, chart balance data, and timeline markers.

### Secondary

- **Signal Green:** Reserved for verified healthy or connected states, positive values, live transport, and equity data.

### Tertiary

- **Caution Amber:** Communicates read-only mode, planned or unknown states, warnings, and risk-budget emphasis.
- **Fault Red:** Communicates errors, critical states, negative values, and drawdown data.

### Neutral

- **Observatory Canvas:** The page background and deepest application field.
- **Navigation Charcoal:** Separates the persistent rail and footer from the workspace.
- **Graphite Panel:** The default card, table, timeline, and chart surface.
- **Raised Graphite:** A restrained alternate surface for locally emphasized regions.
- **Graphite Seam:** The standard one-pixel panel, row, and section divider.
- **Strong Graphite Seam:** Used where controls, chart grids, and timeline connectors need clearer definition.
- **Telemetry White:** Primary headings, values, and verified high-emphasis content.
- **Muted Steel:** Supporting copy and secondary labels.
- **Dim Steel:** Timestamps, kickers, helper text, and lower-emphasis telemetry.

### Named Rules

**The Signal Is Evidence Rule.** Green, amber, and red must always accompany text, a label, or a recognizable state shape; color never carries status alone.

**The Blue Is Information Rule.** Information Blue identifies navigation, focus, context, and neutral system activity; it never implies permission to trade.

**The No Casino Color Rule.** Saturated color stays scarce and semantic. Do not add profit spectacle, promotional gradients, neon glow, or decorative market heat.

## Typography

**Display Font:** Bahnschrift (with Aptos Display, Segoe UI Variable, and sans-serif fallbacks)
**Body Font:** Segoe UI Variable (with Segoe UI, ui-sans-serif, system-ui, and sans-serif fallbacks)
**Label/Mono Font:** Cascadia Code (with SFMono-Regular, Consolas, and monospace fallbacks)

**Character:** Condensed display headings establish an engineered, instrument-panel cadence, while Segoe keeps dense operational copy familiar on Windows. Cascadia Code makes numeric comparison and audit scanning stable through tabular figures.

### Hierarchy

- **Display** (700, fluid 23–29px, 1 line-height): Page titles only; compact but unmistakable.
- **Title** (700, 13px, 1.1 line-height): Panel headers and primary module labels.
- **Body** (400, 10–11px): Dense descriptions, metric labels, table values, and operational explanations.
- **Label** (600–700, 7–9px, 0.04–0.12em letter-spacing, usually uppercase): Kicker text, service labels, table headers, filters, and status telemetry.
- **Mono** (400, 8–25px depending on role): Numeric values, UTC timestamps, tickets, correlation IDs, prices, percentages, and counts.

### Named Rules

**The Telemetry Alignment Rule.** Use the monospace stack and tabular numerals whenever operators compare values across time, rows, or cards.

**The Label Compression Rule.** Uppercase, tracked labels stay short and secondary; they orient the operator without competing with data.

## Layout

The application uses a fixed 224px left rail and a 64px sticky status header around a centered workspace capped at 1920px. Page padding is 24px, primary panel gaps are 12px, and the Overview lead uses three columns: a compact account/market stack, a dominant Audit Timeline, and a health/risk stack. Equity & Drawdown and Event Stream follow at full width, then positions and trades share a two-column table row.

Spacing follows a dense 8px rhythm with deliberate 10–14px compact adjustments inside controls, headers, and telemetry rows. Panels use 14px body padding; panel headers use 11px by 14px padding; table rows are 36px tall; primary navigation rows are 38px tall.

At 1360px the rail narrows to 188px, page padding becomes 18px, and secondary grids reduce column count. At 1050px the rail becomes an off-canvas drawer, the Audit Timeline moves first across both columns, and paired tables stack. At 760px the content becomes single-column, page padding becomes 14px by 10px, the compact service strip yields to an explicit READ ONLY badge, and interactive targets expand toward 34px minimum heights.

**The Audit-First Order Rule.** Responsive layouts preserve the operator’s sequence: transport and timeline first, verified state next, then performance, events, and historical tables.

## Elevation & Depth

The system is flat by default. Depth comes from tonal separation between canvas, navigation, panels, headers, and controls plus one-pixel graphite seams; resting cards do not use shadows. The only structural shadow is the off-canvas mobile navigation drawer, where separation from the obscured workspace is necessary. Focus uses a two-stage blue ring against the canvas rather than decorative elevation.

### Shadow Vocabulary

- **Mobile Drawer** (`box-shadow: 18px 0 45px #0009`): Separates the open navigation drawer from the scrim-covered workspace below 1050px.
- **Focus Ring** (`box-shadow: 0 0 0 2px var(--canvas), 0 0 0 4px var(--blue)`): Makes keyboard focus visible on buttons, links, fields, and selects.

### Named Rules

**The Flat Infrastructure Rule.** Resting panels, tables, and controls use tone and seams, never decorative drop shadows.

## Shapes

The form language is compact and gently technical. Primary containers use 10px corners, summary cards use 9px, inset identity blocks use 8px, interactive controls use 5–7px, and micro status tags use 3px. Pills are reserved for finite states and counts. One-pixel borders define nearly every surface and control; circles are limited to status dots and audit timeline markers.

**The Radius Has Meaning Rule.** Large radii belong to containing surfaces, medium radii to controls, and full pills only to state or count tokens.

## Components

Components feel restrained, data-dense, and unmistakably operational. Interaction states sharpen contrast or reveal focus; they do not bounce, glow, or imply trading action.

### Buttons

- **Shape:** Compact controls use 5–7px corners; icon buttons are 34px square and range buttons are 23px high on desktop.
- **Text Action:** Information Blue text on a dark blue-graphite field with a one-pixel border and 9px horizontal padding.
- **Range Control:** A grouped dark field with 2px inset padding; the active option gains a stronger graphite fill and primary text.
- **Hover / Focus:** Hover changes foreground or background within 150ms; focus receives the two-stage blue ring. On small screens, range buttons expand to 34px high.

### Chips

- **Style:** Status pills are 24px high with full rounding, an explicit text state, a five-pixel dot, and a tone-matched dark background and border.
- **State:** Connected and online use Signal Green; degraded, unknown, and planned use Caution Amber; errors use Fault Red; disabled uses a muted neutral treatment.

### Cards / Containers

- **Corner Style:** Gently curved panel corners (10px) and summary-card corners (9px).
- **Background:** Graphite Panel on Observatory Canvas, with raised or header tones only where hierarchy needs reinforcement.
- **Shadow Strategy:** Flat at rest; see Elevation & Depth.
- **Border:** A one-pixel Graphite Seam surrounds panels and divides headers, rows, and toolbars.
- **Internal Padding:** 14px bodies with compact 11px by 14px headers.

### Inputs / Fields

- **Style:** Dark inset field, one-pixel strong graphite border, 6px corners, 34px height, and 9px horizontal padding.
- **Focus:** Two-stage canvas-and-blue focus ring with no layout shift.
- **Error / Disabled:** Error meaning appears in adjacent explicit copy or status language; disabled states are muted rather than hidden.

### Navigation

The desktop rail uses 38px rows, 7px corners, 12px semibold labels, and thin line icons. Hover lifts contrast with a graphite fill. The active destination uses a darker blue field, near-white text, and a two-pixel blue edge marker. Below 1050px navigation becomes an off-canvas drawer with a scrim and explicit open/close controls.

### Audit Timeline

The dominant chronology uses a fixed UTC column, circular blue markers, a one-pixel connector, compact event titles, descriptive copy, and source/correlation metadata. Warning and error events change marker tone while retaining text labels.

### Event Stream

The realtime stream uses a sticky 31px header and 36px rows across UTC, severity, source, event, description, and correlation columns. Severity and source filters plus an explicit auto-scroll checkbox remain above the table; narrow screens preserve the full dataset through horizontal scrolling and a visible scroll hint.

### Empty and Data States

Loading, error, unavailable, planned, and empty results occupy the same calm centered container pattern. Every state names what is known, why data is absent, and whether the system is waiting, disconnected, unavailable, or intentionally empty.

### Motion

Navigation color and background transitions use 150ms ease; the off-canvas drawer uses 200ms ease. When reduced motion is requested, transition and animation durations collapse to 0.001ms and repeated animation is disabled.

## Do's and Don'ts

### Do:

- **Do** place verified chronology and service truth ahead of secondary analytics.
- **Do** pair every semantic color with explicit status text or a readable value.
- **Do** use monospace and tabular numerals for values that operators compare.
- **Do** preserve one-pixel seams, compact spacing, and honest empty states.
- **Do** keep READ ONLY and execution-disabled context visible at every viewport size.
- **Do** expose wide operational tables through scrolling instead of deleting fields on small screens.

### Don't:

- **Don't** add BUY, SELL, CLOSE, MODIFY, pending-order, or execution controls to this visual system.
- **Don't** fabricate charts, trades, decisions, account values, or service health to fill an empty view.
- **Don't** use gradients, glow, decorative shadows, or saturated color as atmosphere.
- **Don't** communicate connected, warning, error, planned, unknown, or disabled state through color alone.
- **Don't** promote labels, ornament, or analytics above the audit chronology.
- **Don't** replace explicit Unavailable, Unknown, Planned, Disabled, or em-dash states with optimistic defaults.
