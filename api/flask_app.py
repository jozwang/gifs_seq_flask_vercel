import os
from flask import Flask, render_template, request
import pandas as pd
import requests
import folium
from folium.features import DivIcon
from google.transit import gtfs_realtime_pb2
from datetime import datetime, timedelta
import pytz

# --- Constants ---
VEHICLE_POSITIONS_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Bus"
TRIP_UPDATES_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates/Bus"
BRISBANE_TZ = pytz.timezone('Australia/Brisbane')
REFRESH_INTERVAL_SECONDS = 60

# --- Flask App Setup ---
# The template folder needs to be specified relative to the root for Vercel
app = Flask(__name__, template_folder='../templates')

# --- Data Fetching & Processing Logic ---

def fetch_gtfs_rt(url: str) -> bytes | None:
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content
    except requests.RequestException as e:
        print(f"Error fetching GTFS-RT data: {e}")
        return None

def parse_vehicle_positions(content: bytes) -> pd.DataFrame:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    vehicles = [
        {
            "trip_id": v.trip.trip_id, "route_id": v.trip.route_id, "vehicle_id": v.vehicle.label,
            "lat": v.position.latitude, "lon": v.position.longitude, "stop_sequence": v.current_stop_sequence,
            "timestamp": datetime.fromtimestamp(v.timestamp, BRISBANE_TZ).strftime('%Y-%m-%d %H:%M:%S %Z') if v.HasField("timestamp") else "N/A"
        } for entity in feed.entity if entity.HasField("vehicle") for v in [entity.vehicle]
    ]
    return pd.DataFrame(vehicles)

def parse_trip_updates(content: bytes) -> pd.DataFrame:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    updates = []
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            tu = entity.trip_update
            if tu.stop_time_update:
                delay = tu.stop_time_update[0].arrival.delay
                status = "Delayed" if delay > 300 else ("Early" if delay < -60 else "On Time")
                updates.append({"trip_id": tu.trip.trip_id, "delay": delay, "status": status})
    return pd.DataFrame(updates)

def get_live_bus_data() -> tuple[pd.DataFrame, datetime]:
    now = datetime.now(BRISBANE_TZ)
    vehicle_content = fetch_gtfs_rt(VEHICLE_POSITIONS_URL)
    trip_content = fetch_gtfs_rt(TRIP_UPDATES_URL)

    if not vehicle_content or not trip_content:
        return pd.DataFrame(), now

    vehicles_df = parse_vehicle_positions(vehicle_content)
    updates_df = parse_trip_updates(trip_content)

    if vehicles_df.empty:
        return pd.DataFrame(), now

    live_data = vehicles_df.merge(updates_df, on="trip_id", how="left")
    live_data["delay"].fillna(0, inplace=True)
    live_data["status"].fillna("On Time", inplace=True)
    live_data["route_name"] = live_data["route_id"].str.split('-').str[0]

    def categorize_region(lat):
        if -27.75 <= lat <= -27.0: return "Brisbane"
        elif -28.2 <= lat <= -27.78: return "Gold Coast"
        elif -26.9 <= lat <= -26.3: return "Sunshine Coast"
        else: return "Other"
    live_data["region"] = live_data["lat"].apply(categorize_region)
    
    return live_data, now

# --- Flask Route ---
@app.route('/')
def index():
    master_df, last_refreshed_time = get_live_bus_data()

    if master_df.empty:
        return "<h1>Could not retrieve live bus data.</h1><p>The external API may be down. Please try again later.</p>", 503

    # Get user's current selections from URL
    selected_region = request.args.get('region', 'Gold Coast')
    selected_route = request.args.get('route', '700')
    selected_status = request.args.getlist('status')
    selected_vehicle = request.args.get('vehicle', 'All')

    # --- CASCADING LOGIC TO GENERATE FILTER OPTIONS ---
    
    # Region options are based on the whole dataset
    region_options = ["All"] + sorted(master_df["region"].unique().tolist())

    # Filter data by selected region to get route options
    df_for_routes = master_df[master_df["region"] == selected_region] if selected_region != "All" else master_df
    route_options = ["All"] + sorted(df_for_routes["route_name"].unique().tolist())

    # Filter data further by selected route to get status options
    df_for_status = df_for_routes[df_for_routes["route_name"] == selected_route] if selected_route != "All" else df_for_routes
    status_options = sorted(df_for_status["status"].unique().tolist())

    # If status is not provided in the URL, default to all available statuses for the current selection
    if not selected_status:
        selected_status = status_options

    # Filter data further by status to get vehicle options
    df_for_vehicles = df_for_status[df_for_status["status"].isin(selected_status)]
    vehicle_options = ["All"] + sorted(df_for_vehicles["vehicle_id"].unique().tolist())

    # --- APPLY FINAL FILTERS FOR THE MAP ---
    # The final filtered data is the one we used for the last dropdown
    filtered_df = df_for_vehicles
    if selected_vehicle != "All":
        filtered_df = filtered_df[filtered_df["vehicle_id"] == selected_vehicle]

    # --- Create Folium Map (Animation logic removed) ---
    if not filtered_df.empty:
        map_center = [filtered_df['lat'].mean(), filtered_df['lon'].mean()]
        m = folium.Map(location=map_center, zoom_start=12, tiles="cartodbpositron")
        for _, row in filtered_df.iterrows():
            color = "red" if row['status'] == 'Delayed' else ("blue" if row['status'] == 'Early' else "green")
            popup_html = f"<b>Route:</b> {row['route_name']} ({row['route_id']})<br><b>Vehicle ID:</b> {row['vehicle_id']}<br><b>Status:</b> {row['status']}"
            folium.Marker([row['lat'], row['lon']], popup=folium.Popup(popup_html, max_width=300), icon=folium.Icon(color=color, icon="bus", prefix="fa")).add_to(m)
            
            label_text = f"vehicle: {row['vehicle_id']} on stop_seq: {row['stop_sequence']}"
            label_icon_html = f'<div style="font-size: 10pt; font-weight: bold; color: {color}; background-color: #f5f5f5; padding: 4px 8px; border: 1px solid {color}; border-radius: 5px; box-shadow: 3px 3px 5px rgba(0,0,0,0.3); white-space: nowrap;">{label_text}</div>'
            folium.Marker(location=[row['lat'], row['lon']], icon=DivIcon(icon_size=(200, 36), icon_anchor=(85, 15), html=label_icon_html)).add_to(m)
        
        map_html = m._repr_html_()
    else:
        map_html = "<p style='text-align:center; padding-top: 50px;'>No buses match the current filter criteria.</p>"

    context = {
        "tracked_buses_count": len(filtered_df),
        "last_refreshed": last_refreshed_time.strftime('%I:%M:%S %p %Z'),
        "next_refresh": (last_refreshed_time + timedelta(seconds=REFRESH_INTERVAL_SECONDS)).strftime('%I:%M:%S %p %Z'),
        "current_date": datetime.now(BRISBANE_TZ).strftime('%A, %d %B %Y'),
        "map_html": map_html,
        "region_options": region_options,
        "route_options": route_options,
        "status_options": status_options,
        "vehicle_options": vehicle_options,
        "selected_filters": { "region": selected_region, "route": selected_route, "status": selected_status, "vehicle": selected_vehicle },
        "refresh_interval": REFRESH_INTERVAL_SECONDS * 1000,
        "brisbane_tz_str": BRISBANE_TZ.zone
    }
    
    return render_template('index.html', **context)
