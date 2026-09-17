import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio

# VS Code's notebook viewer has no built-in renderer for plotly's default
# widget mimetype (application/vnd.plotly.v1+json) — that includes the
# "vscode" renderer, which (confusingly) emits that same mimetype and still
# needs the "Jupyter Notebook Renderers" Marketplace extension. This one
# instead embeds a self-contained interactive figure as plain HTML/JS
# (loading plotly.js from a CDN), which VS Code renders natively.
pio.renderers.default = "notebook_connected"

map_df = original_latlon.copy()
map_df["frame"] = df.index

# Plotly's built-in map_style="satellite"/"satellite-streets" is, as
# shipped, a MapLibre demo style for Catalonia, Spain — its only source with
# real global reach (ESRI World Imagery) is capped at zoom 16 in that style,
# and the rest only covers Catalonia. Point at ESRI World Imagery directly
# instead for a basemap that actually works anywhere.
satellite_style = {
    "version": 8,
    "sources": {
        "esri-world-imagery": {
            "type": "raster",
            "tiles": [
                "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
            ],
            "tileSize": 256,
            "maxzoom": 19,
            "attribution": "Tiles © Esri",
        }
    },
    "layers": [
        {"id": "esri-world-imagery", "type": "raster", "source": "esri-world-imagery"}
    ],
}

fig = px.scatter_map(
    map_df, lat="lat", lon="lon",
    animation_frame="frame",
    zoom=18,
    center=dict(lat=map_df["lat"].mean(), lon=map_df["lon"].mean()),
    map_style=satellite_style,
)
fig.update_traces(marker=dict(size=14, color="red"))

# Static full track underneath, for context — added after animation setup so
# it isn't touched by per-frame updates and stays visible throughout.
fig.add_trace(go.Scattermap(
    lat=map_df["lat"], lon=map_df["lon"],
    mode="lines", line=dict(width=2, color="rgba(255,255,255,0.6)"),
    showlegend=False,
))

# One fix per ~1s means hundreds of frames — speed up playback accordingly.
fig.layout.updatemenus[0].buttons[0].args[1]["frame"]["duration"] = 40
fig.layout.updatemenus[0].buttons[0].args[1]["transition"]["duration"] = 0

fig.update_layout(title="GPS track playback", margin=dict(l=0, r=0, t=40, b=0))
fig.show()
