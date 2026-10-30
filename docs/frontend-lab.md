# Operations workspace preview

This experiment is on `mock-frontend`. Open `ClimateGuard_Frontend_Lab.html`
through a local HTTP server:

```bash
python3 -m http.server 8765 --bind 127.0.0.1
```

Visit <http://localhost:8765/ClimateGuard_Frontend_Lab.html>.

The original `ClimateGuard_Mockup.html` and the `index.html` entry point are
unchanged. The preview copies the original dashboard, retains its data adapters
and interactions, and adds `assets/frontend-lab.css` and
`assets/frontend-lab.js`. Its initial loading animation is shortened.

## Review the design

- The workspace uses **Ink & teal**. Choose **Light** or **Dark** in the header.
  The initial appearance follows the system setting; an explicit choice is
  remembered in this browser. Severity colours keep the same meaning. Simple
  maps, population colours, map controls, labels and popups adapt to the mode.
- Use **Regional watch** to search configured regions, filter hazards, and select
  a saved assessment. Results are ordered by score; **All hazards** shows each
  region's highest available hazard assessment.
- Inspect **Assessment**, **Resources**, and **Evidence** in the right panel.
- Open **Layers & sources** for map controls, source status, and timestamps.
- Switch between Topo, Satellite, and Simple. Regional dots use a separate SVG
  pane above the basemap and population boundaries. Empty space in that pane
  passes clicks through to the underlying map layers.
- Use **Community context** for population search and **Sensitivity explorer**
  for the existing score adjustment control.
- Use the navigation rail for briefings, illustrative scenarios, and plan review.
- Choose Clinton or West Kelowna with the wildfire hazard selected to open a
  historical replay. Replay and sensitivity modes are labelled explicitly.
- Press `/` to focus the regional search. Tab groups support arrow keys;
  dialogs close with Escape.

## Data semantics

Coverage and elevated-assessment counts come from the existing regional risk
index. Elevated assessments count distinct configured regions with at least one
saved high or critical assessment. These are not counts of official warnings.
Snapshot currency uses the assessments' expiry timestamps. Missing assessments
remain unavailable, and expired assessments are marked stale.

The population card starts with the annual Canada estimate. Selecting a region
or community through the map, regional list, dropdown, or community search changes
its population, place label, and estimate year. **Fit Canada** and **Canada totals**
restore the national total. Zero remains zero; missing values show unavailable.
These are population estimates, not a measure of people exposed to a hazard.
Resource planning retains its existing assumptions and facility-inventory
limitations. Canada-wide live-layer outputs are not committed to the repository;
when they are unavailable locally, the interface says so.

No backend, pipeline, published entry point, or original dashboard changes are
part of this preview.
