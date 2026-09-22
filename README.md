# BeatTheBoard  https://beattheboard.net

I work in NYC and live in NJ, so I take NJ Transit out of Penn Station, and I wanted to get on the train before the crowd of people form above the track stairs 

## What it does

For each NJ Transit departure at Penn it shows one of three things:

- official track, once the board has posted it
- predicted track, with a confidence percentage
- nothing, if there is no data or low confidence

Other information shown:

- departure time and time to it
- arrival time and time to it, if destination is set
- minutes before departure BeatTheBoard called the track, as BTB + xx mins
- minutes before NJ Transit called the track, as NJT + xx mins
- delays as a warning triangle with details in a popup when clicked

** destination is cached on your local device, use the same browser and avoid incognito mode

XXXXXXXXXXXXXX - live report card here?

## Report card

Refreshed nightly from the live site by a GitHub Action
(.github/workflows/readme-stats.yml). Same numbers as the bottom of the page.

<!-- stats:start -->
6 days, 09-17-2026 to 09-22-2026

| | |
|---|---|
| Coverage | 75.2% (499 of 664 trains called 60+ s before the board) |
| Accuracy | 99.2% (495 of 499 predictions correct) |
| BeatTheBoard, before departure | median 27.3 min, mean 30.5 min |
| NJ Transit board, before departure | median 10.2 min, mean 9.9 min |
<!-- stats:end -->

## How it works

NJ Transit Developer API: developer.njtransit.com
RailData is a free API service provided by NJT.
The track comes from 2 pieces sources. 

1. getVehicleData - which contains ICS_TRACK_CKT, or the current track of every active train. 
Circuits belong to Amtrak's A and JO interlockings in PSNY, and
their names encode the platform track. For ex. AA-A180TK is track 4, JO-AJO16TK is
track 6. Once a train enters PSNY its circuit tells you where it is going before the board does.

2. getTrainSchedule - the departure board. Each row has a
latitude and longitude which is a fixed coordinate per track, where the train sits at the platform, and it
appears before the TRACK field is filled in. It only exists for tracks 1 to 3 and 10 to 14.

The two signals cover different tracks, so combining them increases coverage. 

Both are decoded with lookup tables in codebook.json, built by logging the API
for a couple of weeks and joining each circuit and coordinate to the track
that was eventually posted. A circuit or coordinate goes into the table only
if it maps to a single track at least 95% of the time. Anything ambiguous is
left out, leading to the app predicting nothing rather than guessing. 

Things I tried that did not work:

- Per-train history, "this train is usually on track 9." Trains at PSNY do
  not use the same track from day to day. Guessing the most common track for
  a train number was right about 16% of the time.
- Ruling out occupied tracks. Most tracks are empty at any given moment.
- The GTFS feeds. They have no track or platform data for NJ Transit.
- Predicting a departing train's track from the track its inbound equipment
  arrived on. NJ Transit does not publish arrival tracks anywhere.

## The code

Everything is standard library Python. There is nothing to install.

- engine.py: the web app. A background poller fetches the board and vehicle
  feed from NJ Transit every 5 seconds, decodes the two signals, and serves
  the page from that snapshot. It also keeps the report card on the page: a
  prediction only counts if it was showing at least a minute before NJ
  Transit posted the track. Also builds codebook.json from collected data
  with --rebuild.
- njt_logger.py: shared API client (token handling, the multipart POST format
  the API requires) plus the original data logger that records the board and
  vehicle feed to a local database. Only needed to grow the codebook.
- check.py: scores the deployed app against the real board. Records the first
  prediction shown for each train and compares it to the track that is
  eventually posted. --report prints coverage, accuracy, lead time, and how
  much of the board is being predicted.
- benchmark.py: the same scoring, side by side with another track prediction
  site, for a baseline.
- analyze.py, turn_match.py, gtfs_ingest.py, predict.py: analysis scripts from
  working out what does and does not predict the track. Kept for reference.
- test_connection.py, debug_auth.py: API connectivity checks.
- run_logger.py: keeps the logger running across crashes for multi-day runs.
- set_azure_secrets.py: pushes the API credentials from .env into the Azure
  app as secrets, so the password never goes through a shell.
- codebook.json: the two lookup tables. The only derived data in the repo.
- Dockerfile and .github/workflows/deploy.yml: the app builds and deploys to
  Azure Container Apps on every push to main.

## Running it
** You must register your NJT account for the developer portal @ https://developer.njtransit.com/registration/register

Create a file called .env with your NJ Transit developer credentials:

    NJT_USERNAME=your_portal_username
    NJT_PASSWORD=your_portal_password

Then:

    python engine.py

and open http://localhost:8080. The .env file, the token cache, and all
databases are gitignored and never leave your machine.


Or.

Visit beattheboard.net
## Disclaimer

Not affiliated with NJ Transit or Amtrak. A prediction is a prediction. Check
the station display before you board.
