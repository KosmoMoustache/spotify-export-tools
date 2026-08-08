set shell := ["pwsh", "-NoProfile", "-Command"]

binary := "sockseek/sockseek.exe"
input  := "sldl_tracks.csv"
input_all := "sldl_all-var.csv"

# Regenerate the sldl feed from results.txt (missing tracks only).
generate:
    python results_to_sldl.py

# Regenerate the sldl feed including format-mismatch tracks.
generate-all:
    python results_to_sldl.py --include-format-mismatch --out {{ input_all }}

# Dry run: list what would be downloaded without touching the network.
preview: generate
    & {{ binary }} {{ input }} --print tracks

# Download the missing tracks.
run: generate
    & {{ binary }} {{ input }}

# Download missing + format-mismatch tracks.
run-all: generate-all
    & {{ binary }} {{ input_all }}

# Show current config help.
config:
    & {{ binary }} --help config