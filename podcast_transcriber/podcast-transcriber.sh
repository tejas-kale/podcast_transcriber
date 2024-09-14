#!/bin/bash

# Get the directory of the script
DIR="/Users/tejaskale/Code/podcast_transcriber"

# Activate the virtual environment if you're using one
source $DIR/venv/bin/activate

# Run the Django command
python $DIR/podcast_transcriber/manage.py app_control $@