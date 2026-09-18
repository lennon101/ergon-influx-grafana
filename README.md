# Ergon → InfluxDB → Grafana

Imports Ergon NEM12 detailed meter exports into an **InfluxDB 2.x you already
run**, and visualises them in a **Grafana you already run**.

This repo does not start InfluxDB or Grafana. It contains one thing: a one-shot
importer, plus a dashboard to import into your Grafana.

## What you need

- InfluxDB 2.x reachable over the network (tested against 2.1.1)
- Grafana, any recent version
- Docker on the machine that will run the importer

## Data model

Measurement `grid_energy`.

| Kind | Keys |
| --- | --- |
| Tags | `nmi`, `register` |
| Fields | `kwh`, `watts_average`, `quality` |

`kwh` is the energy consumed during the interval. `watts_average` is the
equivalent average power over that interval. Timestamps are interval **start**,
in `Australia/Brisbane`.

### Why `register` is a tag

An Ergon export contains one `200` record per register. The sample file in
`data/` has two, `E1` and `E2`, covering identical dates:

    register E1: 87 days  20260623..20260917  total 1724.4 kWh
    register E2: 87 days  20260623..20260917  total  708.6 kWh

Same NMI, same timestamps. Without `register` in the tag set, both land in the
same series and the second silently overwrites the first, storing about 709 kWh
instead of 2433 kWh. Tagging by register keeps them separate and correct.

What `E1` and `E2` mean depends on your tariff. A split roughly like this one is
typically general consumption versus a controlled-load circuit, but confirm
against your own Ergon tariff rather than assuming.

### Why `quality` is a field, not a tag

NEM12 quality flags change between exports: a day exported as `V` (variable) can
come back as `A` (actual) once validated. As a tag that would create a second
series and double-count the interval. As a field, a re-import updates the point
in place.

This is what makes re-importing safe. The same NMI, register and timestamp write
to the same series, so importing overlapping exports overwrites rather than
accumulates. You can download a fresh export containing historical data and
import it without double-counting.

Where a day carries `400` records, the per-interval flags from those records are
used instead of the day-level flag.

## 1. Create the bucket and token in InfluxDB

In the InfluxDB UI:

1. **Load Data → Buckets → Create Bucket**, named `ergon-data`.
2. **Load Data → API Tokens → Generate API Token**, with write access to that
   bucket. Copy the token; InfluxDB shows it only once.

## 2. Configure

    cp .env.example .env

Edit `.env`:

    INFLUX_URL=http://192.168.1.22:8086
    INFLUX_ORG=home
    INFLUX_BUCKET=ergon-data
    INFLUX_TOKEN=<the token you just created>

## 3. Import

Put one or more Ergon detailed CSV exports in `./data/`, then:

    docker compose run --rm importer

Check the parse before writing anything:

    DRY_RUN=1 docker compose run --rm importer

Either way it prints a per-register summary, which is the quickest way to confirm
the file was read correctly:

    Reading 3033136017_..._ERGON_DETAILED.csv
      NMI 3033136017 register E1: 25,056 intervals over 87 days, 1,724.4 kWh
      NMI 3033136017 register E2: 25,056 intervals over 87 days, 708.6 kWh
      total 2,433.0 kWh across all registers

There is **no image to build**. The importer runs on the stock `python:3.12-slim`
image with `./importer` bind-mounted, and writes InfluxDB line protocol over HTTP
using only the Python standard library.

### Running it from Dockhand

Because nothing needs building, this stack deploys as an ordinary compose stack.

1. Clone or copy this directory onto the Docker host.
2. In Dockhand, create a stack from `docker-compose.yml`.
3. Set the environment variables from `.env` in the stack's environment.

Two things to expect:

- The importer is a **one-shot job**. It runs, prints its summary and exits, so
  Dockhand will show it as `Exited (0)`. That is success, not a crash. Re-run it
  by restarting the container.
- The compose file uses **relative** bind mounts (`./importer`, `./data`). If
  Dockhand resolves those against a different path than you expect, replace them
  with absolute host paths.

## 4. Grafana

### Datasource

**Connections → Add new connection → InfluxDB**:

| Field | Value |
| --- | --- |
| Query language | `Flux` |
| URL | `http://192.168.1.22:8086` |
| Organization | `home` |
| Token | your InfluxDB token |
| Default bucket | `ergon-data` |

Leave Basic auth off. Save and test.

If instead you can mount files into Grafana, there are provisioning files under
`grafana/provisioning/`. They are optional; the UI route above is equivalent.

### Dashboard

**Dashboards → New → Import**, then upload or paste
`grafana/dashboards/ergon-energy.json`.

The dashboard has two variables, so it does not care what your datasource UID or
bucket name are:

- **Datasource** — pick the InfluxDB datasource you just created
- **Bucket** — defaults to `ergon-data`, editable at the top of the dashboard

Panels:

| Panel | Shows |
| --- | --- |
| Latest day | Newest complete day in the selected time range |
| Previous day | The day before it |
| Average day | Mean daily consumption across the range |
| Peak power | Highest combined power across all registers |
| Power by register | `E1` and `E2` as separate series |
| Consumption by time of day | Hourly heatmap, registers combined |
| Daily consumption | Per-day totals, registers combined |

The first three follow the dashboard's time range rather than the wall clock, and
they report the newest day **present in the data**. With a batch import from a
downloaded CSV that is usually not literally today, which is why they are not
labelled "Today" and "Yesterday".

### A note on day boundaries

`aggregateWindow(every: 1d)` buckets on **UTC** midnight, which in Brisbane makes
every "day" run 10:00 to 10:00 and silently splits each local day across two
buckets. Every daily panel therefore uses `offset: -10h`, which is exact because
Queensland does not observe daylight saving.

The obvious timezone-aware alternatives do not work on InfluxDB 2.1.1: `date.sub`
does not exist, `today() - 1d` fails to parse, and `date.truncate` rejects a
`location` argument. If you upgrade InfluxDB, `timezone.location` becomes the
cleaner option.

## Recommended next step

Add tariff data as a separate measurement rather than baking today's tariff into
the raw meter data. That lets you change tariff assumptions without re-importing
historical meter readings, and `register` is already there to join against.
