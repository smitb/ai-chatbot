#!/bin/bash
cd ./.devcontainer

# create local infra
pip install -r ./requirements.txt
python ./infra.py

cd ..

# create virtual env and install deps
# python3.11 -m venv .venv
# source ./.venv/bin/activate
pip3 install -r requirements.txt

