"""Foto-Suche MVP — Suche über digiKam-Sammlung mit semantischer Vektorsuche."""
import os

# HF_HUB_ENABLE_HF_TRANSFER ist auf diesem Rechner global gesetzt, aber
# hf_transfer ist nicht installiert — der Download bricht dann mit
# ModuleNotFoundError ab.
os.environ.pop("HF_HUB_ENABLE_HF_TRANSFER", None)

# hf-mirror.com leitet lediglich auf huggingface.co weiter und war zeitweise
# unerreichbar; direkter Download ist zuverlässiger.
os.environ["HF_ENDPOINT"] = "https://huggingface.co"

# Alle Modelle (bge-m3) liegen vollständig im lokalen HF-Cache — der Online-Check
# auf Updates ist unnötig, erzeugt nur Warnungen und verzögert den Modellstart.
# Kann bei Bedarf extern mit HF_HUB_OFFLINE=0 deaktiviert werden.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

__version__ = "0.1.0"
