---
version: 1
slug: "app-py"
primary_target: "app.py"
related_targets: []
---

# SURU shared-command dashboard (app.py)

Mode: Operate. Audience: hackathon judges watching a presenter drive a shared screen. Story: the Seed-42 multi-incident city, where resources deplete across Sheffield.

## Direction contract

THESIS: The city's response reads like the road network it runs on: one map wall where incidents, stations, routes and the agents' mediation highway trace back to a place in Sheffield. It refuses the glowing near-black command-centre with KPI tiles and card lists.

OWN-WORLD: Dark asphalt ground (#0a1a22). Motorway-blue panels (#1f5aa6) for stations and the KPI gantry, primary-route green (#00703c) for incident panels, yellow (#f2c300) route-number patches for ticks and active routes, and white lettering inside a white panel border. Overpass. Flat fills, no glows, no gradients. Professional and clean over themed.

STORY: Judges see where incidents are, how far along each one is, who decided what along the highway (triage, agencies, A_cr, allocator), and what each dispatch cost in vehicles.

FIRST VIEWPORT: Top bar with SURU, the tick in a yellow patch, and Advance/Reset in the sidebar. Below it, a full-width blue gantry of 6 KPIs. Then a 420px rail of green incident panels with progress bars beside a full-height pydeck map. Stations are blue squares showing stock, incidents are severity-coloured markers, active routes are yellow. Each incident panel opens the mediation highway dialog.

FORM: UK road signage (Primary Route), position 1 on the ordered list (IMPECCABLE'S PICK), seed:ce66717e25be9cc1. Signature move: the mediation highway, a plain flat plotly lane diagram of real board entries.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Open decisions
- A1/A2/A3 = FIRE/POLICE/AMBULANCE (assumed, unconfirmed).
- A_cr is not in code yet (#3). The highway shows it as "not yet active" until board entries carry sender A_CR / round / reads.
- Personnel stock has no schema field. Show vehicles only.
