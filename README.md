# BeatTheBoard  https://beattheboard.thankfulpond-632cee48.eastus2.azurecontainerapps.io

I work in NYC and live in NJ, so I take NJ Transit out of Penn Station, and I wanted to get on the train before the crowd of people form. 

## What it does

For each NJ Transit departure at Penn it shows one of three things:

- the official track, once the board has posted it
- a predicted track, with a confidence percentage
- nothing, if it does not know

Each row also says when the train departs and when it arrives, how many
minutes before departure we called the track and how many minutes before NJ
Transit posted it, and a warning triangle if there is a delay or an NJ
Transit alert for that train or its line (tap it for the text). Type your
stop in the box at the top to see only the trains that stop there, with the
time they get there; the stop is remembered on your device.

On held-out data it predicts about three quarters of departures, is right
about 98% of the time when it does predict, and gets there a median of 13
minutes before the official board. The board's own lead is about 10 minutes, so in
practice you know roughly 20 to 25 minutes before departure. It says nothing
rather than guess.

The idea came from a friend who built a similar tool.

## How it works

There is no machine learning in this. NJ Transit runs a public developer API
(developer.njtransit.com, free account, RailData product) that powers their
own departure screens. Two fields in that API turn out to reveal the track
assignment before the track itself is published.

The first is in getVehicleData, which lists every active train with a field
called ICS_TRACK_CKT: the signalling track circuit the train is currently on.
At Penn Station the circuits belong to Amtrak's A and JO interlockings, and
their names encode the platform track. AA-A180TK is track 4. JO-AJO16TK is
track 6. Once a train is routed into the station, its circuit tells you where
it is going, well before the board says so.

The second is in getTrainSchedule, the departure board itself. Each row has a
latitude and longitude. It looks like a live position, but it is a fixed
coordinate per track, the spot where the train sits at the platform, and it
appears before the TRACK field is filled in. It only exists for tracks 1 to 3
and 10 to 14.

The two signals cover different tracks, which is why using both is worth much
more than either alone.

Both are decoded with lookup tables in codebook.json, built by logging the API
for a couple of weeks and joining each circuit and coordinate to the track
that was eventually posted. A circuit or coordinate goes into the table only
if it maps to a single track at least 95% of the time. Anything ambiguous is
left out, which is why the app abstains instead of guessing.

Things I tried that did not work, in case you are thinking of them:

- Per-train history, "this train is usually on track 9." Trains at Penn do
  not use the same track from day to day. Guessing the most common track for
  a train number was right about 16% of the time.
- Ruling out occupied tracks. Most tracks are empty at any given moment, so
  elimination almost never narrows it to one.
- The GTFS feeds. They have no track or platform data for NJ Transit.
- Predicting a departing train's track from the track its inbound equipment
  arrived on. NJ Transit does not publish arrival tracks anywhere.

## The code

Everything is standard library Python. There is nothing to install.

- engine.py: the web app. A background poller fetches the board and vehicle
  feed from NJ Transit every 5 seconds, decodes the two signals, and serves
  the page from that snapshot. It also keeps the report card on the page: a
  prediction only counts if it was showing at least 30 seconds before NJ
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

## Disclaimer

Not affiliated with NJ Transit or Amtrak. A prediction is a prediction. Check
the station display before you board.
