---
name: Ai-Voice-Agent
description: AI sales calling — ink on paper, type as the material
colors:
  storm-ink: "#0a0a0a"
  ink-hover: "#2b2c2e"
  paper: "#f4f4f3"
  panel-white: "#ffffff"
  reading-gray: "#54575c"
  faint-gray: "#686c71"
  hairline: "#d5d6d8"
  rain: "#8e949b"
  mist: "#e6e8eb"
  silver: "#c0c5cc"
  success: "#15803d"
  warning: "#b45309"
  danger: "#c0271f"
  info: "#2f5fa8"
  dark-ground: "#0a0a0a"
  dark-panel: "#121212"
  dark-paper-text: "#f2f2f0"
typography:
  display:
    fontFamily: "Archivo, 'Segoe UI', system-ui, sans-serif"
    fontSize: "84px"
    fontWeight: 900
    lineHeight: 0.86
    letterSpacing: "0"
    fontVariation: "'wdth' 62"
  headline:
    fontFamily: "Archivo, 'Segoe UI', system-ui, sans-serif"
    fontSize: "40px"
    fontWeight: 900
    lineHeight: 0.92
    letterSpacing: "0"
    fontVariation: "'wdth' 62"
  figure:
    fontFamily: "Archivo, 'Segoe UI', system-ui, sans-serif"
    fontSize: "52px"
    fontWeight: 900
    lineHeight: 0.92
    fontVariation: "'wdth' 62"
  title:
    fontFamily: "Archivo, 'Segoe UI', system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 900
    lineHeight: 1.05
    fontVariation: "'wdth' 62"
  body:
    fontFamily: "Archivo, 'Segoe UI', system-ui, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Archivo, 'Segoe UI', system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "0.09em"
rounded:
  sm: "3px"
  md: "4px"
  xl: "6px"
  pill: "999px"
spacing:
  cell: "18px"
  gap: "16px"
  section: "36px"
  page: "32px"
components:
  button-primary:
    backgroundColor: "{colors.storm-ink}"
    textColor: "{colors.panel-white}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "9px 16px"
  button-primary-hover:
    backgroundColor: "{colors.ink-hover}"
    textColor: "{colors.panel-white}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.storm-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "9px 16px"
  button-secondary-hover:
    backgroundColor: "{colors.storm-ink}"
    textColor: "{colors.paper}"
  input:
    backgroundColor: "{colors.panel-white}"
    textColor: "{colors.storm-ink}"
    rounded: "{rounded.md}"
    padding: "9px 12px"
    height: "40px"
  card:
    backgroundColor: "{colors.panel-white}"
    textColor: "{colors.storm-ink}"
    rounded: "{rounded.md}"
    padding: "18px"
  badge:
    backgroundColor: "transparent"
    textColor: "{colors.storm-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "2px 9px 2px 8px"
  badge-live:
    backgroundColor: "{colors.silver}"
    textColor: "{colors.storm-ink}"
    rounded: "{rounded.pill}"
---

# Design System: Ai-Voice-Agent

## Overview

**Creative North Star: "Alphabet Storm"**

Type is the material. The application's state is set as monumental black letters on paper, and what is in flight breaks off the end of a word as a plume of small letters. There is one family, one ink, and a ground of cool paper. Panels are white and divided by hairlines; nothing casts a shadow and nothing is a gradient.

The interface is an operator's control room used at a desk in daylight, so light is the primary theme. A dark theme inverts the same system: ink becomes the ground and paper becomes the text. Colour is reserved for status, and even there it is a dot beside a word, never a fill.

**Key Characteristics:**
- Extra-condensed black capitals for display and figures; regular width for reading.
- Storm ink on paper, white panels, hairline rules, 4px corners.
- Solid ink primary buttons and outlined secondary buttons, both in tracked capitals.
- Status is a printed mark: a toned dot plus a word in an outlined pill.
- Silver is a flat fill for anything in flight.
- The letter plume is the only thing that moves, and only while the dialler is running.

## Colors

A near-monochrome palette: ink, paper and three grays, with four status tones used as marks.

### Primary
- **Storm Ink** (`storm-ink`): text, rules under page heads and table heads, every primary control, the agent's transcript bubbles, focus outlines.
- **Ink Hover** (`ink-hover`): the hover state of solid ink controls.

### Neutral
- **Paper** (`paper`): the page ground, the sidebar and the top bar.
- **Panel White** (`panel-white`): cards, tables, inputs, modals, alerts.
- **Reading Gray** (`reading-gray`): secondary text, labels above figures, table headers.
- **Faint Gray** (`faint-gray`): placeholders and group titles; the lightest gray allowed for text.
- **Hairline** (`hairline`): panel borders and row dividers.
- **Rain** (`rain`): what is spent or secondary: the second line of a display headline, neutral dots, the select chevron. Large text and marks only.
- **Mist** (`mist`): fills: progress tracks, skeletons, row hover, code chips.
- **Silver** (`silver`): anything in flight: the live badge, the running dialler pill, the in-progress segment of a progress bar.

### Status
- **Success** (`success`), **Warning** (`warning`), **Danger** (`danger`), **Info** (`info`): each keeps one meaning everywhere. They colour a dot, an icon or a hairline. Danger also colours a failed-calls figure above zero and destructive buttons.

### Named Rules
**The Dot and a Word Rule.** A status is always a toned dot plus its word. The tone never fills a panel and never colours a routine figure.

**The Flat Silver Rule.** In flight is a flat silver fill. It never shimmers and is never a gradient.

**The Ink Figures Rule.** Figures are ink. Red is kept for a figure that needs action.

## Typography

**Display Font:** Archivo at width 62 and weight 900 (self-hosted variable file, with Segoe UI and the system sans as fallback)
**Body Font:** Archivo at normal width
**Label Font:** Archivo at normal width, weight 600, tracked capitals

**Character:** One grotesk carries everything. Rank comes from width, weight and size, not from a second face or from decoration.

### Hierarchy
- **Display** (900, width 62, line-height 0.86, capitals, scaled to its container): the dashboard's state word, capped at 84px, and the sign-in headline, capped at 118px. The second line is set in rain.
- **Headline** (900, width 62, 40px; 56px on a campaign page; 34px on a phone): page titles.
- **Figure** (900, width 62, 52px; 32px compact; 36px for rates; tabular numerals, not uppercased): every count and rate.
- **Title** (900, width 62, 20–32px, capitals): the dashboard greeting with the operator's name (32px, ink, on its own line under the state word), campaign names on cards, modal titles, empty-state headings, the next-action line.
- **Body** (400, 13.5px, line-height 1.5): reading text; page descriptions are capped at 72ch.
- **Label** (600–800, 10–11.5px, 0.09em tracking, capitals): navigation, buttons, badges, tabs, table headers, card titles, field labels.

### Named Rules
**The One Grotesk Rule.** No second family. The embedded voice client is the only exception and keeps its own face.

## Layout

A 232px sidebar on paper, divided from the content by a hairline, with a 56px top bar. Content sits in a column up to 1400px wide with 32px page padding. Page heads end in an ink rule. Sections are introduced by a small label on a hairline, 36px above and 14px below.

Figures sit in one ruled band: cells divided by vertical hairlines with an ink rule beneath, not a row of cards. Rates use the same ruled cells. Cards in a grid keep a 16px gap.

Under 1100px the two-column page grids become one column and the sign-in brand panel is dropped. Under 960px the sidebar becomes a drawer. Under 600px figure bands and rates become two columns, form actions stack, modals rise from the bottom edge and small buttons grow to a 36px touch height.

## Elevation & Depth

Flat. The only gradient in the build is the loading skeleton's sweep. Depth is carried by hairlines and by the contrast between paper and white panels. A running campaign card is outlined in ink rather than lifted.

### Shadow Vocabulary
- **Overlay** (`0 18px 50px -18px rgba(10, 10, 10, .45)`): modals, toasts and the open mobile drawer only.

### Named Rules
**The Hairline Rule.** Every division is a one-pixel rule. A surface that is not an overlay has no shadow.

## Shapes

Small corners: 4px on buttons, inputs, cards and alerts, 3px on small chips, 6px on modals. Pills are reserved for badges, chips and the dialler pill. Progress bars and outcome bars are square-ended. Controls carry a one-pixel ink border; panels carry a one-pixel hairline.

## Components

### Buttons
- **Shape:** 4px corners, one-pixel ink border, tracked capitals at 11px.
- **Primary:** solid storm ink with white text, 9px 16px padding; hover lightens to ink-hover.
- **Secondary:** transparent with ink text and border; hover inverts to solid ink.
- **Ghost:** no border; hover fills with mist.
- **Danger:** danger border and text; hover fills with danger. A solid danger variant confirms destructive actions.
- **Focus:** a 2px ink outline offset by 2px.

### Badges
- **Style:** an outlined pill in ink with a 6px toned dot and the status word in tracked capitals.
- **Live:** a flat silver fill with ink text; its dot pulses a ring.

### Cards / Containers
- **Corner Style:** 4px.
- **Background:** white on paper.
- **Shadow Strategy:** none.
- **Border:** one-pixel hairline; ink when the campaign is running.
- **Internal Padding:** 18px; headers carry the title as a tracked-capital label above a hairline.

### Inputs / Fields
- **Style:** white, one-pixel ink border, 4px corners, 40px tall, with a tracked-capital label above.
- **Focus:** a second ink line plus a 4px soft ink ring.
- **Error / Disabled:** a danger border with the message beneath; disabled fields sit on paper with a hairline border.

### Navigation
- Tracked capitals at 11.5px with a 17px line icon. Hover fills the row; the active item is heavier and marked by one ink dot that slides to it. Tabs are tracked capitals with a 2px ink underline on the active tab.

### Tables
- White, with a tracked-capital header above an ink rule and hairline row dividers. The first column holds a bold name with a gray second line. Row hover fills with mist.

### Alerts
- White panels with a hairline. A warning has an ink hairline and a toned icon; an error has a danger hairline.

### Transcript
- The agent speaks in solid ink bubbles; the contact is set in outlined white bubbles. Speaker names are tracked capitals.

### Outcome bars
- The total as a figure, then one row per outcome: toned dot, name, a square ink bar on a mist track, count and share.

### The letter plume (signature)
- A canvas drawn over the display headline. Paper-coloured letters nibble the outer edge of the last glyph of the first line; ink letters leave from that edge, rise into the headroom above the headline and thin out to the right. The letters are real names (contacts in the recent calls, running campaigns) on the dashboard. It drifts only while the dialler is running and is still otherwise, on the sign-in page, and for reduced motion. When the word fills the row there is no plume.

## Do's and Don'ts

### Do:
- **Do** set every count and rate in the figure style with tabular numerals.
- **Do** divide with one-pixel hairlines and end page heads and figure bands with an ink rule.
- **Do** show status as a toned dot plus a word.
- **Do** keep the last glyph of a display word legible under the plume.
- **Do** serve fonts, scripts and styles from the application's own origin; the content security policy blocks anything else.
- **Do** keep `--color-primary` in `client-theme.css` equal to the first `--primary` in `styles.css`, and its dark `--color-background` equal to the dark `--surface`; a test checks both.

### Don't:
- **Don't** use gradients, glows, glass or drop shadows on surfaces.
- **Don't** colour a routine figure with a status tone.
- **Don't** add a second typeface or an icon tile beside a figure.
- **Don't** animate anything being read; only live dots pulse and only the plume drifts, while dialling.
- **Don't** fill a panel with a status colour.
