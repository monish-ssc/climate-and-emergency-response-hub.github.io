# Manual test checklist

- [ ] `ClimateGuard_Frontend_Lab.html` remains the canonical frontend.
- [ ] The root page opens `ClimateGuard_Frontend_Lab.html`.
- [ ] Each region/hazard selection shows live, stale, unavailable, or demonstration status.
- [ ] Missing regional risk files show unavailable status; sample scenario values stay labelled.
- [ ] Empty event layers show a clear no-active-events message.
- [ ] The Health facilities control loads 7,525 records, clusters them at national zoom, and expands clusters when selected.
- [ ] Individual facility popups show name, type, location and the unverified status/capacity/hazard warning.
- [ ] Each hazard tab updates the title, score, summary, and recommendation.
- [ ] Each region updates the location and measurements.
- [ ] The briefing dialog opens, contains three actions, and closes.
- [ ] Keyboard users can reach every control.
- [ ] The layout remains readable at 375 px width.
- [ ] Pipeline freshness and simulation/safety disclaimers are always visible.
- [ ] The demo works with the network disabled.
- [ ] A manual **Refresh live hazard data** workflow run produces a validated `live-hazard-data-<run-id>` artifact.

## Operations workspace integration

- [ ] Open `/` and confirm it redirects to `ClimateGuard_Frontend_Lab.html`.
- [ ] Open **Layers & sources**, switch a live layer and inspect its timestamp and source.
- [ ] **Check for updates** refreshes map layers, risk, community screening and briefings.
- [ ] Background refresh runs while visible and pauses while hidden.
- [ ] A failed watchlist or briefing refresh retains the last successfully loaded content.
- [ ] Region and community selections update population context; Fit Canada restores the national total.
- [ ] Topo → Simple keeps regional dots visible and clickable.
- [ ] Resource assumptions, facility inventory, both wildfire replays, EN/FR advisories, scenarios and plan review work from the root entry point.
- [ ] Light/dark modes preserve selections and remain readable.
