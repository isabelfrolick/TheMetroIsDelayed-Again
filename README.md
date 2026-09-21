# The métro is delayed. Again.

One day, while I was delayed on the Montréal métro, I thought to myself - "How does this keep happening? Like seriously, how often does this happen?". This page is the result. 



**To see the live site: [https://isabelfrolick.github.io/TheMetroIsDelayed-Again/](https://isabelfrolick.github.io/TheMetroIsDelayed-Again/)**

<!-- Add a screenshot: save it as docs/screenshot.png in this repo -->
![Screenshot of the Montréal métro delay tracker](docs/screenshot.png)

If you would like to contribute to this page, either by adding additional statistics or another city, please feel free.


## Contributing

Ideas for new statistics or other cities are welcome. Open an issue to suggest one, or fork the repo and send a pull request. 

Note: adding another city needs an open dataset that records incidents or delays with start and end times per line, to fit with the formatting of this project.


## Run it locally

```bash
pip install -r requirements.txt
python pipeline/fetch.py          # download the latest CSV
python pipeline/process.py        # merge, clean and compute site/data/*.json
python pipeline/validate.py       # sanity checks
python -m http.server             # then open http://localhost:8000/site/
```


## What it shows

- **Chance of a delay at a given time.** Pick a day, an hour and a line to see how often a delay of 5+ minutes was under way then, with a day × hour heatmap of the last 24 months.
- **Most delayed lines and stations** for any month or full year since 2019, with network-wide averages.
- **An interactive map** of the four lines. Hover a line for its average delay, worst delay, share of service hours with a delay and most common cause.

## How it works

```
STM open data (CSV) ──> GitHub Action (weekly) ──> Python pipeline ──> JSON ──> static site on GitHub Pages
```

1. **Fetch.** Every Monday a scheduled GitHub Action downloads the STM's incident CSV. If the file hasn't changed since the last run, the job stops there.
2. **Merge.** New incidents are added to a stored history (`data/raw/incidents_history.csv.gz`), matched on incident number so revised records replace older versions. The STM file only guarantees the last 3 months, so the repo keeps the full history itself.
3. **Process.** A pandas pipeline (`pipeline/process.py`) cleans the data and computes:
   - per-line average and worst delay, and the share of service hours with a delay under way
   - monthly and yearly rankings of lines (by delay minutes) and stations (by delays caused)
   - the probability of a delay for every line × weekday × hour
4. **Validate.** `pipeline/validate.py` checks the results before anything is published: expected columns, all four lines present, history never shrinking, parse failures under 5%, probabilities between 0 and 1. If a check fails, the job stops and the live site keeps its last good data.
5. **Publish.** The updated data is committed to the repo and the `site/` folder is deployed to GitHub Pages. The page is plain HTML, CSS and JavaScript with [Leaflet](https://leafletjs.com/) for the map. It has no build step and no server.

Line shapes and station locations come from the STM's GTFS schedule feed, converted once to GeoJSON (`python pipeline/fetch.py --geo`).

### How delays are measured

- Only **train incidents** count toward delays; the STM notes that station incidents don't affect métro service.
- A **delay** is a train incident that interrupted service for **5 minutes or more**, the same threshold the STM uses in its own reliability indicators. Its length runs from the start to the end of the delay to passengers, as recorded by the STM.
- An incident that affected several lines counts once for each line, and once in network totals.
- The **chance of a delay** is the share of past days on which a delay was under way at some point during that hour, over a rolling 24-month window.
- **Station rankings** count delays recorded at a station during service hours (5:00 to 1:00). Incidents between stations count toward their line but not toward any station.
- The STM publishes incidents a few months after they happen, so the latest data is usually about 3 months behind today.


## Repository layout

```
.github/workflows/update.yml   weekly fetch → process → validate → commit → deploy
pipeline/                      fetch.py, process.py, validate.py
data/raw/                      stored incident history
data/geo/                      métro lines and stations (GeoJSON, from STM GTFS)
site/                          the static page (index.html, app.js, style.css) and its data
```

## Data source and credit

Incident data: **Société de transport de Montréal (STM)**, [*Incidents du réseau du métro*](https://donnees.montreal.ca/dataset/incidents-du-reseau-du-metro), published on the City of Montréal's open data portal. Used under the portal's [terms of use](https://donnees.montreal.ca/pages/licence-d-utilisation). Questions about the dataset itself go to the STM at ccia@stm.info.

Line geometry: STM GTFS feed. Map tiles: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, © [CARTO](https://carto.com/).

This is an independent project and is not affiliated with or endorsed by the STM.

## Contact

Questions, concerns, issues, or suggestions for new features: [isabel.frolick@mail.mcgill.ca](mailto:isabel.frolick@mail.mcgill.ca)