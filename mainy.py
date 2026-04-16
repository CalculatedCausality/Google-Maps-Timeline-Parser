# Legacy entry point — the application has been restructured.
# Use main.py (CLI) instead:
#
#   python main.py Timeline.json --gpx --distance --heatmap
#
# This file is kept for backwards compatibility only.
from main import main

if __name__ == '__main__':
    main()