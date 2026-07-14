# Routing Server

A traffic-aware routing engine for Brno, built as part of the bachelor's thesis
[*Routing Algorithm for Traffic Planning in Brno*](https://www.vut.cz/en/students/final-thesis/detail/164964?zp_id=164964)
(BUT FIT, 2024/2025). It combines [OpenStreetMap](https://www.openstreetmap.org/) road
network data with historical Waze traffic-jam records to compute routes whose cost
reflects how congested the roads actually were during a given time window, rather than
just their free-flow speed.

Given a source and destination coordinate plus a date range, the server returns the
fastest path between them, its estimated travel time both with and without historical
traffic, and a street-by-street breakdown colored by congestion severity — ready to be
rendered on a map.

## How it works

- **Map data** (`osm.py`, `graph.py`) — road geometry for an area (e.g. "Brno") is
  pulled from the Overpass API and converted into a `networkx` directed graph, with
  edges weighted by per-road-type speed limits (motorway, residential, service, etc.).
- **Traffic data** (`traffic.py`) — historical Waze jam records are loaded from
  PostgreSQL, spatially matched against graph edges, and used to compute a
  traffic-adjusted traversal time and severity (0–2) for each road segment, based on how
  often and how badly it was jammed within the requested date range.
- **Routing** (`routing.py`) — shortest paths are found with A*, using an ALT heuristic
  (A*, Landmarks, Triangle inequality) precomputed from a set of landmark nodes, which
  is much faster than plain straight-line A* on a real road network.
- **API** (`main.py`) — a FastAPI service that builds the graph and traffic overlays on
  startup, keeps a cache of traffic-adjusted graphs per date range (refreshed daily and
  pre-warmed for the last 7 days), and exposes an endpoint to compute routes on demand.

The architecture is geography-agnostic — swapping the `AREA` constant is enough to
route in a different city, as long as matching Waze traffic data is available.

## API

### `POST /find_route_by_coord`

Finds the fastest route between two coordinates for a given date range.

**Request body:**
```json
{
  "src_coord": [16.6068, 49.1951],
  "dst_coord": [16.5765, 49.2108],
  "from_time": "2025-05-01",
  "to_time": "2025-05-07",
  "use_traffic": true
}
```

**Response:**
```json
{
  "streets_coord": [
    {"street_name": "Údolní", "path": [[lat, lon], [lat, lon]], "severity": "0", "color": "green"},
    {"street_name": "Kounicova", "path": [[lat, lon], [lat, lon]], "severity": "1", "color": "orange"},
    {"street_name": "Veveří", "path": [[lat, lon], [lat, lon]], "severity": "2", "color": "red"}
  ],
  "route": [[lat, lon], ...],
  "src_street": "Údolní",
  "dst_street": "Veveří",
  "length": 1234.5,
  "time_with_traffic": 210.3,
  "time_without_traffic": 180.0
}
```

## Running the server

### Prerequisites

- Database credentials in `.env` file
  - `DB_HOST`
  - `DB_PORT`
  - `DB_USER`
  - `DB_PASSWORD`
  - `DB_NAME`
- Python 3.12+ for local setup
- Docker and Docker Compose for Docker setup

### Local setup
```
pip install -r requirements.txt
chmod +x run.sh
./run.sh
```

### Docker setup
```
docker compose up -d
```
