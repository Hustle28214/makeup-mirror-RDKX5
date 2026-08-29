#!/bin/bash
# Run face-tracking. Usage:
#   ./run.sh                 # normal
#   ./run.sh --show          # with preview window (needs DISPLAY)
#   ./run.sh --no-servo      # detect only, don't drive servos
cd "$(dirname "$0")"
exec python3 face_track.py "$@"
