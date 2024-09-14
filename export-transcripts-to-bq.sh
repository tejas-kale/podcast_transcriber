#!/bin/bash

# Activate the virtual environment
source ./venv/bin/activate

# Run the Python script
python ./export_transcripts_to_bq.py

# Deactivate the virtual environment
deactivate