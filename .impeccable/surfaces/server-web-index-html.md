---
version: 1
slug: "server-web-index-html"
primary_target: "server/web/index.html"
related_targets: ["server/web/app.js","server/web/styles.css"]
---

# Surface brief: the unified application (server/web)

Scope: every page of the single-page app, signed in and signed out. Mode: Operate (the login's brand panel is the one Persuade moment).

Audience and job: one technical owner-operator at a desk in daylight, during calling hours, who sets up campaigns, watches them run and acts on outcomes. Light is the primary theme; dark stays as an option.

Constraints: no route, API contract, write or permission check changes. Fonts, scripts and styles are served from the app's own origin (strict CSP). `tests/test_app.py` and `tests/test_client_theme.py` assert on strings in `app.js`, `styles.css` and `client-theme.css`. Status tones keep one meaning everywhere.

Chosen direction: Alphabet Storm, chosen by the user over the rolled direction. Memorable moment: the dashboard's state word breaking into a plume of letters made from the names the agent is calling.

## Direction contract

THESIS: Type is the material. The app's state is set as monumental black letters on paper, and what is in flight breaks off as letters. It refuses the gradient SaaS dashboard: no purple, no glow, no gradient fills, no icon tiles, no soft-shadowed cards.

OWN-WORLD: Paper ground (#f4f4f3) and white panels; storm ink (#0a0a0a) for text, rules and every primary control; rain gray (#8e949b) for what is spent; mist (#e6e8eb) for fills; silver for anything mid-flight. One family, Archivo: extra-condensed black capitals for display and figures, regular width for reading. Hairline rules instead of shadows, 4px corners, solid ink buttons with tracked capitals, outlined secondary buttons, uppercase tracked labels, status as a dot plus a word.

STORY: The operator reads the dialler's state in one word, the figures in one ruled band, then the campaigns, then what needs following up.

FIRST VIEWPORT: Light sidebar with the wordmark and tracked-capital navigation, the active item marked by a dot. Main: the dialler's state in condensed black capitals about 120px tall, the running-campaign count beneath it in rain gray, a plume of small letters streaming right from the end of the first line, then the sentence and the two actions (solid ink New campaign, outlined Import contacts). Beneath: one ruled band of six figures in condensed black numerals.

FORM: Alphabet Storm, a dealt challenger adopted by the user (not on the grounded list). Signature interaction: the letter plume is drawn from real names and drifts only while the dialler is running; everything being read holds still. Seed key 380dd9eb.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

Unresolved: none.
