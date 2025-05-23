"""
Filename: traffic.py
Author: Matyáš Strelec
Email: xstrel03@vut.cz
Date: 2025-05-20
Project: Routing Server
"""

import time
import os
import shapely
from collections import defaultdict

import geopandas as gpd
import networkx as nx
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from shapely import Point
from shapely.ops import unary_union

EdgeKeyTuple = tuple[Point, Point, int]
JamOverlapResult = list[tuple[EdgeKeyTuple, gpd.GeoDataFrame]]

db_config = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT")),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "dbname": os.getenv("DB_NAME"),
}


def get_edge_jam_overlaps(
    graph: nx.MultiDiGraph,
    traffic: gpd.GeoDataFrame,
) -> dict:
    """
    Filters graph edges overlapped by traffic jams and returns edge info
    paired with the overlapping jam data.
    Assumes input graph edges have valid LineString geometries.

    Identifies road graph edges that:
    1. Spatially intersect with buffered traffic jam geometries.
    2. Meet a minimum fractional overlap significance criterion.

    Args:
        graph: MultiDiGraph with road points and edges (LineStrings).
        traffic: GeoDataFrame of traffic jams (LineStrings), indexed appropriately.

    Returns:
        Dict of tuples: { date: ((u, v, key), jam_rows_df), ... }, where jam_rows_df
        is a GeoDataFrame containing data for jams overlapping the edge.
    """
    # Parameters for overlap filtering, found to work well on OSM data
    tolerance = 10.0
    min_overlap_fraction = 0.75

    start_time = time.time()
    edge_jam_overlaps = {}

    # Load edge_jam_overlaps from database
    connection = psycopg2.connect(
        host=db_config["host"],
        port=db_config["port"],
        user=db_config["user"],
        password=db_config["password"],
        dbname=db_config["dbname"],
    )
    try:
        cursor = connection.cursor()
    except psycopg2.Error as e:
        raise Exception(f"Error connecting to database: {e}")

    # Get edge_jam_overlaps from DB
    query = "SELECT DISTINCT unnest(jam_uuids) FROM edge_jam_overlaps"
    cursor.execute(query)

    db_jam_uuids_nested = cursor.fetchall()
    # Get all jam_uuids from edge_jam_overlaps
    if db_jam_uuids_nested:
        db_jam_uuids = set.union(*(set(x) for x in db_jam_uuids_nested))
    else:
        db_jam_uuids = set()

    # Get edge_jam_overlaps from DB
    query = "SELECT u, v, k, jam_uuids FROM edge_jam_overlaps"
    cursor.execute(query)
    rows = cursor.fetchall()
    db_edge_jam_overlaps = {
        (shapely.from_wkt(row[0]), shapely.from_wkt(row[1]), row[2]): row[3]
        for row in rows
    }
    edge_jam_overlaps = {}

    # Get all jam_uuids from traffic
    traffic_jam_uuids = set(traffic.index)

    # Replace the jam_uuids with the actual jam rows GDFs
    for (u, v, key), jam_uuids in db_edge_jam_overlaps.items():
        valid_uuids = [uuid for uuid in jam_uuids if uuid in traffic_jam_uuids]
        jam_rows = traffic.loc[valid_uuids]
        dates = jam_rows["date"].unique()
        if jam_rows.empty:
            continue
        for d in dates:
            if d not in edge_jam_overlaps:
                edge_jam_overlaps[d] = {}
            if (u, v, key) not in edge_jam_overlaps[d]:
                edge_jam_overlaps[d][(u, v, key)] = jam_rows
            else:
                edge_jam_overlaps[d][(u, v, key)] = pd.concat(
                    [edge_jam_overlaps[d][(u, v, key)], jam_rows]
                )

    # Filter the traffic GeoDataFrame to exclude jams that are already in the edge_jam_overlaps dict
    traffic = traffic[~traffic.index.isin(db_jam_uuids)]

    try:
        traffic_spatial_index = traffic.sindex
        print(f"Spatial index from {len(traffic)} traffic segments")
    except Exception as e:
        print(f"Error getting spatial index: {e}")
        return {}
    edge_jam_overlaps_to_db = []
    # Go over all edges in the graph
    print(f"Go over {graph.number_of_edges()} road edges")
    total = graph.number_of_edges()
    processed_count = 0
    skipped_invalid_geom = 0
    overlap_found_count = 0

    for u, v, key, edge in graph.edges(data=True, keys=True):
        processed_count += 1
        if processed_count % 5000 == 0:
            print(
                f"\tProcessed {processed_count} edges out of {total} = {processed_count / total:.2%}"
            )

        linestring = edge["geometry"]

        # Discard edges that are too short
        if linestring.length < 1e-6:
            skipped_invalid_geom += 1
            continue

        # Find potential matches using spatial index
        linestring_buffer = linestring.buffer(tolerance).bounds
        possible_matches_indices_pos = list(
            traffic_spatial_index.intersection(linestring_buffer)
        )
        if not possible_matches_indices_pos:
            continue

        # Get matching candidate rows
        possible_matches_indices = traffic.iloc[possible_matches_indices_pos].index
        candidate_rows = traffic.loc[possible_matches_indices]

        # Check intersections and overlaps
        try:
            # Buffer candidate geometries and check validity
            candidate_buffered_geoms = candidate_rows.geometry.buffer(tolerance)
            valid_buffers = candidate_buffered_geoms[
                candidate_buffered_geoms.is_valid & ~candidate_buffered_geoms.is_empty
            ]
            if valid_buffers.empty:
                continue

            # Check if any of the valid buffers intersect with the linestring
            intersecting_mask = valid_buffers.intersects(linestring)
            if not intersecting_mask.any():
                continue

            # Get the indices of intersecting buffers
            intersecting_indices = candidate_rows.index[intersecting_mask]
            intersecting_buffers_list = valid_buffers[intersecting_mask].tolist()

            # Get the overlap area
            combined_intersecting_area = unary_union(intersecting_buffers_list)
            if (
                not combined_intersecting_area.is_valid
                or combined_intersecting_area.is_empty
            ):
                continue
            intersection_geometry = linestring.intersection(combined_intersecting_area)
            if intersection_geometry.is_empty:
                continue

            # Calculate linear overlap length
            overlap_length = 0.0
            geom_type = intersection_geometry.geom_type
            if geom_type == "LineString" or geom_type == "MultiLineString":
                overlap_length = intersection_geometry.length
            elif geom_type == "GeometryCollection":
                for geom in intersection_geometry.geoms:
                    if geom.geom_type in ["LineString", "MultiLineString"]:
                        overlap_length += geom.length

            # Skip negligible overlaps
            if overlap_length < 1e-9:
                continue

            # Check if the overlap is significant enough
            fraction = overlap_length / linestring.length
            if fraction >= min_overlap_fraction:
                overlap_found_count += 1
                jam_indices = list(intersecting_indices)
                # find the jam rows from gdf
                jam_rows = candidate_rows.loc[jam_indices]
                for i, jam_row in jam_rows.iterrows():
                    date = jam_row["date"]
                    if date not in edge_jam_overlaps:
                        edge_jam_overlaps[date] = {}
                    if (u, v, key) not in edge_jam_overlaps[date]:
                        edge_jam_overlaps[date][(u, v, key)] = jam_rows
                    else:
                        edge_jam_overlaps[date][(u, v, key)] = pd.concat(
                            [edge_jam_overlaps[date][(u, v, key)], jam_rows]
                        )
                    if jam_row["finished"] and i not in db_jam_uuids:
                        edge_jam_overlaps_to_db.append((u.wkt, v.wkt, key, [i]))

        except Exception as e:
            print(f"Error processing edge: {e}")
            continue

    end_time = time.time()

    print(f"Finished overlap filtering on {processed_count} edges")
    if skipped_invalid_geom > 0:
        print(f"Skipped {skipped_invalid_geom} edges (bad geometry)")
    print(f"Found {overlap_found_count} edges with significant overlap")
    print(f"Total filtering time {end_time - start_time:.2f} seconds")

    # Find finished jams that are finished but don't have any overlaps, store them as inactive
    inactive_unmatched_jams = traffic[~traffic.index.isin(db_jam_uuids)]
    inactive_unmatched_jams = inactive_unmatched_jams[
        inactive_unmatched_jams["finished"]
    ]
    inactive_unmatched_jams = inactive_unmatched_jams[
        ~inactive_unmatched_jams.index.isin(db_jam_uuids)
    ]
    inactive_unmatched_jams = list(inactive_unmatched_jams.index.unique())
    # Remove duplicates
    inactive_unmatched_jams = list(set(inactive_unmatched_jams))
    if inactive_unmatched_jams:
        edge_jam_overlaps_to_db.append((None, None, None, inactive_unmatched_jams))

    merged = defaultdict(set)
    for u, v, k, jam_uuids in edge_jam_overlaps_to_db:
        key = (u, v, k)
        merged[key].update(jam_uuids)
    deduplicated = [
        (u, v, k, list(jam_uuids)) for (u, v, k), jam_uuids in merged.items()
    ]
    query = """
    INSERT INTO edge_jam_overlaps (u, v, k, jam_uuids)
    VALUES %s
    ON CONFLICT (u_wkb, v_wkb, k) DO UPDATE
    SET jam_uuids = (
        SELECT ARRAY(
            SELECT DISTINCT e
            FROM unnest(edge_jam_overlaps.jam_uuids || EXCLUDED.jam_uuids) AS e
        )
    )
    """
    if deduplicated:
        execute_values(cursor, query, deduplicated)
    connection.commit()
    cursor.close()
    connection.close()

    # Don't use in the graph
    if (None, None, None) in edge_jam_overlaps:
        del edge_jam_overlaps[(None, None, None)]

    db_time = time.time()
    print(
        f"Inserted {len(edge_jam_overlaps_to_db)} new edge jam overlaps to DB in {db_time - end_time:.2f} seconds"
    )

    return edge_jam_overlaps


def update_graph_with_traffic(
    graph: nx.MultiDiGraph,
    edge_jam_overlaps: JamOverlapResult,
    date_range: int,
) -> nx.MultiDiGraph:
    """
    Updates graph edge weights based on traffic events and their severity.
    Severity levels:
    0: No traffic issues (fine)
    1: Moderate traffic
    2: Congested traffic

    Args:
        graph: The NetworkX MultiDiGraph to modify.
        edge_jam_overlaps: List from get_edge_jam_overlaps: [((u,v,key), jam_rows_gdf)].
        date_range: The number of unique dates spanned by the traffic data period.

    Returns:
        The graph with updated traffic attributes.
    """
    print(f"Applying {len(edge_jam_overlaps)} traffic segments to road graph")

    for (u, v, key), jam_rows in edge_jam_overlaps.items():
        try:
            # Access the specific edge's data using u, v, and the key
            edge_data = graph.edges[u, v, key]

            edge_length = edge_data.get("length")  # Meters
            base_time = edge_data.get("traversal_time")  # Seconds
            seconds_in_range = date_range * 86400  # Total time window in seconds

            # Count traffic events and calculate severity
            num_events = len(jam_rows)
            if num_events < date_range * 3:
                severity = 0
            elif num_events < date_range * 7:
                severity = 1
            elif num_events >= date_range * 7:
                severity = 2

            # Total delay time across all jam events (in seconds)
            total_weighted_delay = 0.0
            total_jam_duration = 0.0

            for _, jam in jam_rows.iterrows():
                jam_length = jam["length"]
                jam_delay = jam["delay"]
                jam_duration = jam["duration"]

                if jam_length <= 0 or jam_duration <= 0:
                    continue

                if jam_delay == -1:
                    # Standstill: assume 1 km/h (0.278 m/s)
                    standstill_speed = 0.278
                    standstill_time = edge_length / standstill_speed
                    segment_scaled_delay = standstill_time - base_time
                elif jam_delay > 0:
                    segment_scaled_delay = (jam_delay / jam_length) * edge_length

                else:
                    continue

                total_weighted_delay += segment_scaled_delay * jam_duration
                total_jam_duration += jam_duration

            if total_jam_duration > 0:
                jam_fraction = min(total_jam_duration / seconds_in_range, 1.0)
                avg_delay_when_jammed = total_weighted_delay / total_jam_duration
                adjusted_time = (1 - jam_fraction) * base_time + jam_fraction * (
                    base_time + avg_delay_when_jammed
                )
            else:
                adjusted_time = base_time

            # Update edge data
            edge_data["is_penalized_by_traffic"] = num_events > 0
            edge_data["traffic_severity"] = severity
            edge_data["num_traffic_events"] = num_events
            edge_data["traversal_time_with_traffic"] = adjusted_time

        except Exception as e:
            print(f"Error updating edge ({u}, {v}, k={key}): {e}")

    print("Finished updating graph with traffic data")
    return graph


def load_jam_data_from_db(n_results: int = None) -> pd.DataFrame:
    """
    Load traffic jam data from a PostgreSQL database and return it as a DataFrame.

    Args:
        db_config (dict): Database connection parameters including host, port, user, password, and database name.
        n_results (int, optional): Number of results to limit the query. If None, all results are fetched.

    Returns:
        pd.DataFrame: DataFrame containing traffic jam data.

    """
    # Connect to PostgreSQL database
    connection = psycopg2.connect(
        host=db_config["host"],
        port=db_config["port"],
        user=db_config["user"],
        password=db_config["password"],
        dbname=db_config["dbname"],
    )
    # Get whole table
    try:
        cursor = connection.cursor()
    except psycopg2.Error as e:
        raise Exception(f"Error connecting to database: {e}")
    if n_results is None:
        query = """
        SELECT
            uuid AS id,
            street,
            published_at,
            last_updated,
            active,
            delay,
            speed,
            jam_length AS length,
            ST_AsText(jam_line::geometry) AS geometry
        FROM
            jams
        ORDER BY
            published_at
        DESC
  """
    else:
        query = f"""
        SELECT
            uuid AS id,
            street,
            published_at,
            last_updated,
            active,
            delay,
            speed,
            jam_length AS length,
            ST_AsText(jam_line::geometry) AS geometry
        FROM
            jams
        ORDER BY
            published_at
        DESC
        LIMIT {n_results}
        """

    cursor.execute(query)
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    cursor.close()
    connection.close()

    return pd.DataFrame(rows, columns=columns)


def preprocess_jams(jams: pd.DataFrame) -> gpd.GeoDataFrame:
    """
    Preprocess traffic data from a DataFrame, perform preprocessing, convert CRS and return a GeoDataFrame.

    Args:
        jams (pd.DataFrame): DataFrame containing traffic data with 'geometry' column in WKT format.

    Returns:
        gpd.GeoDataFrame: A GeoDataFrame containing the processed traffic data.
    """
    geometry = gpd.GeoSeries.from_wkt(jams["geometry"])
    traffic_data = gpd.GeoDataFrame(jams, geometry=geometry, crs="EPSG:4326")
    if traffic_data.empty:
        return traffic_data

    # Check if the data is in EPSG:4326
    if traffic_data.crs is None:
        raise ValueError("No CRS found in the file")
    # Convert coordinates to CRS EPSG:32633
    traffic_data.to_crs("EPSG:32633", inplace=True)

    # Convert pubMillis to datetime
    traffic_data["started"] = pd.to_datetime(traffic_data["published_at"], unit="ms")
    traffic_data["lastupdated"] = pd.to_datetime(
        traffic_data["last_updated"], unit="ms"
    )
    traffic_data["date"] = traffic_data["started"].dt.date

    # If finished, calculate duration
    traffic_data["finished"] = ~traffic_data["active"].astype(bool)
    traffic_data.loc[traffic_data["finished"], "duration"] = (
        traffic_data["lastupdated"] - traffic_data["started"]
    ).dt.total_seconds()
    # If not, default to 1 hour
    traffic_data.loc[~traffic_data["finished"], "duration"] = 3600
    # If longer than 4 hours, set to 4 hours
    traffic_data.loc[traffic_data["duration"] > 14400, "duration"] = 14400

    columns_to_keep = [
        "id",
        "country",
        "city",
        "street",
        "date",
        "started",
        "lastupdated",
        "finished",
        "duration",
        "delay",
        "speed",
        "geometry",
        "length",
    ]

    for col in traffic_data.columns:
        if col not in columns_to_keep:
            traffic_data.drop(columns=col, inplace=True)

    traffic_data.drop_duplicates(subset=["id"], inplace=True)

    if traffic_data["id"].is_unique:
        traffic_data.set_index("id", inplace=True, verify_integrity=True)
    else:
        raise ValueError("Duplicate IDs found in traffic data")

    return traffic_data
