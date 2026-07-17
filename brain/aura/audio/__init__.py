"""Audio front-end: VAD gating, wake-word detection, per-user capture, STT.

All PCM lives in RAM only — audio never touches disk (CLAUDE.md constraint 5).
"""
