"""Foto-Suche MVP — Suche über digiKam-Sammlung mit semantischer Vektorsuche."""
import os

# HF_HUB_ENABLE_HF_TRANSFER ist auf diesem Rechner global gesetzt, aber
# hf_transfer ist nicht installiert — der Download bricht dann mit
# ModuleNotFoundError ab.
os.environ.pop("HF_HUB_ENABLE_HF_TRANSFER", None)

# hf-mirror.com leitet lediglich auf huggingface.co weiter und war zeitweise
# unerreichbar; direkter Download ist zuverlässiger.
os.environ["HF_ENDPOINT"] = "https://huggingface.co"

__version__ = "0.1.0"
