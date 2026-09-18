set shell := ["pwsh", "-NoProfile", "-Command"]

binary := "sockseek/sockseek.exe"

# test connectivity/auth with the Subsonic server (ping)
test *args:
    uv run subify.py test {{ args }}

# check which tracks from a Spotify CSV are missing on the server
check *args:
    uv run subify.py check {{ args }}

# generate MISSING and FORMAT-MISMATCH lists from a Spotify CSV
wish *args:
    uv run subify.py wish {{ args }}

# generate sockseek input CSV file
sockseek *args:
    uv run subify.py sockseek {{ args }}

# create/update a Subsonic playlist from a Spotify CSV export
sync *args:
    uv run subify.py sync {{ args }}

# sockseek-preview input="sockseek_tracks.csv":
#     & {{ binary }} {{ input }} --print tracks

# sockseek-download input="sockseek_tracks.csv":
#     & {{ binary }} {{ input }}
