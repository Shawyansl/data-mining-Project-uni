# Car and Real-Estate Market Analysis Pipeline

This project collects public **car listings from Bama** and **real-estate listings from Sheypoor**, builds a separate CSV file for each run, compares prices against similar listings, appends the results to PostgreSQL, and displays them in Grafana.

```text
Run → fetch Bama and Sheypoor listings → separate CSVs → price analysis → append snapshot to PostgreSQL → Grafana dashboards
```

Prices are the asking prices in the listings and may differ from actual transaction prices. The analyses are explainable statistical baselines, not trained machine-learning models.

## Running the Project from Scratch

### Prerequisites

- Docker and Docker Compose must be installed and running.
- The first run needs internet access to build the images and fetch listings.
- Run all the commands below from the project's root folder.

### 1. Configure passwords

```bash
cp .env.example .env
```

Open the `.env` file and change the PostgreSQL and Grafana passwords if needed. This file is local and must not be committed. If you already have a `.env`, do not overwrite it.

### 2. Build the scraper and analysis image

```bash
docker compose --profile tools build pipeline
```

Docker images are pulled from the ArvanCloud registry, and Python libraries are installed from the Liara PyPI mirror. This step is only needed the first time or after changing code/dependencies.

### 3. Run both scrapers and save to the database

```bash
docker compose --profile tools run --rm pipeline --pages 1
```

This command also starts the database and Grafana if needed, then fetches cars and real estate, analyzes them, and appends the successful snapshots to PostgreSQL. The `--pages 1` option means at most one page per car search and per real-estate category.

At the end of the run, you should see a success message for both markets and a run ID (`run-id`). The outputs of that run are located here:

- Car CSV: `data/cars/run_<run-id>.csv`
- Real-estate CSV: `data/real_estate/run_<run-id>.csv`
- Reports and charts: `reports/<run-id>/cars/` and `reports/<run-id>/real_estate/`

If the run fails, the terminal output shows which market failed. Until analysis and snapshot insertion succeed for a market, its fresh data will not appear in the dashboard.

### 4. Open the dashboard

Default Grafana address: [http://localhost:3001](http://localhost:3001)

Log in with `GRAFANA_USER` and `GRAFANA_PASSWORD` from `.env`. Under **Dashboards**, open the **Used-car market** and **Real-estate market** folders. The car dashboard refreshes every 30 seconds and the real-estate dashboard every 5 minutes; click Refresh to see results immediately.

## Re-running and Accumulating Data for Evaluation

Run the same command again each time:

```bash
docker compose --profile tools run --rm pipeline --pages 1
```

Each run creates a new CSV file with a unique name and appends a new snapshot to the history. Previous tables are not deleted. To fetch more pages, increase the number, for example:

```bash
docker compose --profile tools run --rm pipeline --pages 3
```

Each additional page may increase the run time. The scrapers pause between requests so they send requests to the sites at a reasonable rate.

`--pages` is only a cap on requests; if Bama returns the first page again instead of the next page, the scraper stops there and states the reason in the summary. The list of target cars is in `cars/queries.txt`; to cover more models, add other valid searches to that file.

To count snapshots and unique listings:

```bash
docker compose exec db psql -U cars_app -d car_market -c "SELECT 'cars' AS market, COUNT(*) AS snapshots, COUNT(DISTINCT ad_id) AS unique_ads FROM car_listing_snapshots UNION ALL SELECT 'real_estate', COUNT(*), COUNT(DISTINCT ad_id) FROM real_estate_listing_snapshots;"
```

If `snapshots` is greater than `unique_ads`, some listings were seen in multiple runs and their history has been kept.

## How to Read the Dashboards

### Cars

The car dashboard shows the number of unique listings, valid-price coverage, median price, and underpriced listings. Comparison is done against listings with the same variant, a production year within at most one year, the same zero-km/used status, and similar mileage. At least two peers are required for an estimate, and an underpriced candidate must be at least 15% below the estimate.

### Real Estate

The real-estate dashboard separates sales and rentals, and shows the median sale price per square meter or the monthly rent and deposit by city and neighborhood. Prices are compared with listings of the same type in the same city and neighborhood, with area differing by at most 25% and a similar number of rooms (when available).

An estimate requires at least two priced peers. An anomalous candidate requires at least four peers and a sufficient statistical difference (modified z-score). Daily rentals are not included in the monthly rent estimate.

**A zero estimate or zero anomalous candidates does not necessarily mean something is broken.** If the number of listings in a neighborhood/variant, or the number of valid prices, is not enough to satisfy the comparison conditions, the result stays zero. Re-scraping the same listings usually does not increase the number of unique listings; it may only increase the number of snapshots.

## Maintenance, Stopping, and Deleting Data

Stop the services without deleting the database:

```bash
docker compose down
```

Running the main command again brings the services back up, and the PostgreSQL history is preserved. **Run this command only when you intentionally want to erase the database history and the Grafana state:**

```bash
docker compose down -v
```

The CSVs in `data/` and the reports in `reports/` remain in the project folder; deleting the database volume does not remove those files.

## Troubleshooting Common Errors

- **Port 3001 is in use:** In `.env`, set `GRAFANA_PORT` to, for example, `3002`, and open Grafana at `http://localhost:3002`.
- **Building the image after a code change gives old results:** Rebuild the pipeline, then run it: `docker compose --profile tools build pipeline`.
- **CSV was not created or the row count is zero:** Check the CSV file paths in the run output and the scraper messages; network access to Bama and Sheypoor is required to fetch live data.
- **I increased `--pages` but the number of cars did not increase:** The page count is a cap. Check the scraper summary for the message "pagination parameter was not accepted"; in that case Bama did not serve the next page, and searches for other models must be added to `cars/queries.txt`.
- **The dashboard did not change after editing a JSON file:** Restart Grafana with `docker compose restart grafana`.
- **There is no data in Grafana:** Make sure the analysis and save step for that market finished successfully, then check the PostgreSQL counter above and click Refresh.
- **The estimate or anomalous candidates are zero:** Check the peer conditions in the previous section; this is normal for sparse data or scattered neighborhoods.

## Folders

```text
cars/                  Bama scraper, searches, and car analysis
real_estate/           Sheypoor scraper and real-estate analysis
pipeline/              Orchestrated run of both scrapers and Docker dependencies
grafana/               Datasource config, provisioning, and dashboards
data/cars/             CSV for each car run (local)
data/real_estate/      CSV for each real-estate run (local)
reports/<run-id>/      Reports and charts for each run (local)
```
